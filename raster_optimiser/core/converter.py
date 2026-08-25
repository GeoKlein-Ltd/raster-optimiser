"""Raster Optimiser conversion module.

Pure GDAL/Python, no QGIS imports. Takes an already-classified
DetectionResult (see core/detector.py) for a file that was NOT refused,
and does the actual work from
docs/GeoKlein_raster_optimisation_workflow.md Steps 3-4: Translate
(convert format) then Build Overviews (pyramids), with the profile's
resolved creation options and extra command-line parameters.

Detection stays pure content-type/profile classification and never
judges "is this file already good enough" - that's this module's job:
it always compares the current file's structure against the target
settings BEFORE writing anything, and does nothing at all only when the
file is already tiled, has overviews, AND is already on the target
compression - compression-aware, not just tiling/overviews (see
convert()'s already_optimised/at_target_compression check below). A file
that's tiled with overviews but still on a non-target codec proceeds
anyway, for the compression gain alone. See docs/plugin_design_notes.md
for the reasoning.

Run directly against a file:

    C:\\OSGeo4W\\bin\\python-qgis.bat core\\converter.py path\\to\\file.tif [--profile lossy|lossless]

Needs a GDAL Python environment (see core/detector.py's docstring).
"""

from __future__ import annotations

import argparse
import configparser
import dataclasses
import datetime
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from osgeo import gdal

try:
    from .detector import (
        detect, DetectionResult, RECOMMENDED_SETTINGS, resolve_profile_reason,
        describe_detection, content_label,
    )
except ImportError:  # running as a plain script, not as part of the core package
    from detector import (
        detect, DetectionResult, RECOMMENDED_SETTINGS, resolve_profile_reason,
        describe_detection, content_label,
    )

gdal.UseExceptions()

# ---------------------------------------------------------------------------
# Result structures
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    ran: bool = False
    block_size: Optional[tuple] = None
    overview_count: int = 0
    compression: Optional[str] = None
    expected_compression: Optional[str] = None
    expected_block_size: Optional[tuple] = None
    block_size_ok: bool = False
    overviews_ok: bool = False
    compression_ok: bool = False
    passed: bool = False
    issues: list = field(default_factory=list)


@dataclass
class AppliedSettings:
    """What was actually passed to Translate/BuildOverviews, in a form
    that doesn't require knowing GDAL's creation-option key names
    (COMPRESS, ZSTD_LEVEL vs JPEG_QUALITY, PREDICTOR) to read. Built
    from the same creation_options/overview_config dicts convert()
    already constructs for the GDAL calls themselves - not read back
    from VerificationResult, which stays an independent check of the
    output file so a mismatch between intended and actually-written
    settings stays detectable rather than getting silently papered over
    by this record agreeing with itself.
    """
    compression: str  # "ZSTD" | "JPEG"
    compression_detail: Optional[str] = None  # "level 9" | "quality 90 with YCbCr"
    predictor: Optional[str] = None  # "2" | "3" | None (JPEG has none)
    tiled: bool = True
    block_size: tuple = (512, 512)
    alpha_reattached: bool = False  # True only when the lossy profile dropped and remasked a real alpha band
    resampling: str = "AVERAGE"  # overview resampling method - a real decision, not recoverable from the file afterwards, unlike whether overviews exist at all


@dataclass
class DecisionSummary:
    """Every plain-English decision explanation a run has to offer,
    assembled once inside convert() so the end-of-run summary and the
    output file's embedded metadata read identical text rather than
    each formatting their own. profile_reason/nodata_message describe
    facts, not display policy - profile_consequential/nodata_consequential
    are also facts (what happened), and it's up to whichever caller
    reads this (the log summary vs. the file metadata) to decide which
    facts are worth including under its own rule.

    No profile_honoured field: resolve_profile_reason() doesn't return
    one (see its docstring) - consequential alone already decides
    everything a caller needs to about severity.
    """
    profile_reason: str
    profile_consequential: bool = False  # a genuine match can still be worth flagging - see resolve_profile_reason()'s docstring (e.g. Analysis requested on a JPEG source)
    nodata_message: Optional[str] = None  # same object as ConversionResult.nodata_message, not duplicated text
    nodata_consequential: bool = False  # derived from nodata_message_severity != "routine"
    applied: Optional[AppliedSettings] = None  # None until creation_options are resolved; stays None for already_optimised (nothing written)


@dataclass
class ConversionResult:
    source_path: str
    ok: bool = False
    # already_optimised | converted | converted_incomplete | converted_unverified
    # | blocked | refused_upstream | cancelled | error
    action: str = "unknown"
    message: str = ""
    output_path: Optional[str] = None
    profile_used: Optional[str] = None
    primary_reason: Optional[str] = None  # "tiling" | "overviews" | None
    translate_ok: Optional[bool] = None
    overviews_ok: Optional[bool] = None
    translate_seconds: Optional[float] = None
    overview_seconds: Optional[float] = None
    verification: Optional[VerificationResult] = None
    source_bytes: Optional[int] = None
    output_bytes: Optional[int] = None
    size_summary: Optional[str] = None
    size_note: Optional[str] = None  # set only when output_bytes > source_bytes
    nodata_mode_requested: str = "keep"  # echoes the request: "auto" | "reveal" | "keep"
    nodata_cleared: bool = False  # what actually happened
    nodata_message: Optional[str] = None  # human explanation of what happened and why
    nodata_message_severity: str = "routine"  # "routine" | "emphasis" | "warning" - see _resolve_nodata_handling
    warnings: list = field(default_factory=list)
    decisions: Optional[DecisionSummary] = None  # see DecisionSummary docstring - read by the end-of-run summary and output-file metadata


def _settings_key(detection: "DetectionResult", profile: str) -> str:
    if profile == "lossy":
        return "lossy"
    # Predictor by dtype, not by content type: checking dtype directly
    # (rather than the FLOAT32_CONTINUOUS sentinel, which only ever
    # applied to single-band elevation) is what makes this correct for
    # the CONTINUOUS bucket too - a multi-band Float32 file needs
    # PREDICTOR=3 exactly the same way single-band elevation does, and a
    # content-type check alone would miss that.
    return "lossless_float" if detection.dtype == "Float32" else "lossless_integer"


# NoData mode identity strings - self-describing everywhere, same reason
# the lossy/lossless profile identity is a string rather than a bridged
# index (see algorithms/optimise_raster.py's PURPOSE_ANALYSIS comment):
# a QGIS dropdown index meaning something different from a same-numbered
# constant in this file is exactly the kind of mapping a future edit can
# get backwards without it showing up anywhere except the output.
NODATA_MODE_AUTO = "auto"
NODATA_MODE_REVEAL = "reveal"
NODATA_MODE_KEEP = "keep"

# Below the threshold detector.py's NoData risk assessment can actually
# produce (NODATA_INTERIOR_CELL_RISK_FRACTION = 0.001, i.e. 0.1%), so
# this branch is defensive rather than reachable today - kept anyway so
# a future change to that threshold can't reintroduce a message reading
# "around 0.00%", which would look broken rather than reassuring.
_NODATA_PCT_DISPLAY_FLOOR = 0.005


def nodata_pct_phrase(pct: float) -> str:
    if pct < _NODATA_PCT_DISPLAY_FLOOR:
        return "a small but detectable amount"
    return "{:.2f}%".format(pct)


def _auto_cleared_message(nodata_risk: "NoDataRisk") -> str:
    pct = nodata_risk.interior_max_cell_fraction * 100
    return (
        "Cleared NoData: around {} of this image's interior was pure "
        "black and hidden behind a NoData value of 0, real content, "
        "usually shadow or water, not just the transparent collar. "
        "Those pixels are now visible in the output."
    ).format(nodata_pct_phrase(pct))


def _keep_meaningful_message(nodata_risk: "NoDataRisk") -> str:
    pct = nodata_risk.interior_max_cell_fraction * 100
    return (
        "NoData handling: kept, as requested. Around {} of this "
        "image's interior is pure black and hidden behind a NoData "
        "value of 0, usually shadow or water, not the transparent "
        "collar. Those pixels stay hidden in this output. Run again "
        "with Reveal hidden pixels or Automatic to bring them back."
    ).format(nodata_pct_phrase(pct))


_AUTO_KEPT_COLLAR_ONLY_MESSAGE = (
    "Kept NoData: this file's NoData value only marks the transparent "
    "collar, so nothing real was hidden. Left unchanged."
)

# Distinct from the collar-only message above: "collar_only" means
# detection looked and confirmed nothing real was hidden.
# "insufficient_sample" means detection couldn't tell either way -
# interior_checked came back 0 in _black_pixel_sample(), which happens
# for two different reasons, not just a small file: the image's
# dimensions can be too small for the sampling grid to have any
# interior cells at all, OR a large file can still have sparse, thin
# coverage (an oddly-shaped survey area) where every interior cell
# individually falls under NODATA_MIN_CELL_VALID_PIXELS valid pixels.
# Reporting this as "confirmed collar-only" would be a false
# reassurance neither cause earns - "didn't have enough interior area
# to sample reliably" is true of both without claiming to know which.
_AUTO_KEPT_INSUFFICIENT_SAMPLE_MESSAGE = (
    "Kept NoData: this file didn't have enough interior area to "
    "sample reliably, so it wasn't possible to tell whether real "
    "content is hidden behind NoData=0. Left unchanged. If dark areas "
    "look like they have holes in them, run again with 'Reveal hidden "
    "pixels'."
)


_NODATA_REVEAL_NO_EFFECT_ELEVATION_MESSAGE = (
    "Hidden pixels: left as they are. Reveal applies to 8-bit imagery, "
    "where every value from 0 to 255 is a legitimate colour and a "
    "NoData value of 0 can hide real pixels. This is elevation data, "
    "where NoData marks genuinely empty ground using a value no real "
    "height could take, such as -9999. Clearing it would turn the "
    "collar into real height values."
)

_NODATA_REVEAL_NO_EFFECT_GENERIC_MESSAGE = (
    "Hidden pixels: nothing to reveal. This file has no NoData value "
    "of 0, so nothing was being hidden behind one. The output is the "
    "same as it would have been with any other setting."
)


def _resolve_nodata_handling(detection: "DetectionResult", mode: str):
    """Decide whether to append -a_nodata none, and what to tell the
    caller about it. Returns (should_clear: bool, message: Optional[str],
    severity: str), severity one of "routine" (plain pushInfo),
    "emphasis" (bold pushFormattedMessage - a consequential NoData
    finding), or "warning" (feedback.pushWarning() - a Bucket A
    coercion, see algorithms/optimise_raster.py's module docstring).

    Profile-independent by design - this used to be baked separately
    into each ProfileOption's translate_extra_args in detector.py (lossy
    cleared when structurally safe, lossless never did, regardless of
    the detected risk); that inconsistency is gone, replaced by this one
    decision point every profile goes through identically.

    mode == NODATA_MODE_AUTO uses detection.nodata_risk.assessment:
    "meaningful" clears; "collar_only" and "insufficient_sample" both
    keep, but with different messages - conflating them would report
    "confirmed nothing was hidden" for a file detection actually
    couldn't read at all, which is a false reassurance neither the file
    nor the user earned. NODATA_MODE_KEEP on a "meaningful" file is never
    gated by the QGIS wrapper's checkParameterValues() - it's a fully
    legitimate choice, surfaced here with "emphasis" severity instead
    (see algorithms/optimise_raster.py's module docstring for why a hard
    gate on this was tried and reverted).
    """
    nodata_risk = detection.nodata_risk

    if nodata_risk is None or not nodata_risk.applies:
        # No NoData=0 condition on this file at all (elevation always
        # lands here, since detect_metadata_only never even creates a
        # NoDataRisk for FLOAT32_CONTINUOUS - and plenty of RGB files
        # have no NoData=0 either). Neither case blocks execution any
        # more (Bucket A of the purpose-question rework, Phase 2) - this
        # is the ONLY place either message is produced now, run on every
        # entry route since every caller goes through convert(). Only
        # worth a log line if the caller actually asked to reveal;
        # Automatic and Keep both stay quiet, since there's nothing to
        # report either way.
        if mode == NODATA_MODE_REVEAL:
            if detection.content_type == "FLOAT32_CONTINUOUS":
                return False, _NODATA_REVEAL_NO_EFFECT_ELEVATION_MESSAGE, "warning"
            return False, _NODATA_REVEAL_NO_EFFECT_GENERIC_MESSAGE, "routine"
        return False, None, "routine"

    if not nodata_risk.clear_possible:
        # nodata_only_transparency: Automatic stays conservative and
        # never clears here - that IS Automatic's purpose, unaffected
        # by this change. An explicit Reveal is a different thing: a
        # deliberate choice to accept the collar becoming solid black,
        # so it's honoured rather than refused. This was structurally
        # impossible before this change (clear_possible was read as an
        # unconditional block, regardless of mode) even though
        # raster_optimiser_ui_text.md already documented Reveal as
        # always clearing NoData and warning about exactly this border
        # effect - this file type is the one that sentence describes.
        # Profile availability is untouched by this: lossy still isn't
        # offered on these files (see docs/plugin_design_notes.md's
        # internal-mask-band deferred item for why, and for what's left
        # before it could be).
        if mode == NODATA_MODE_REVEAL:
            return True, (
                "Hidden pixels: cleared, as requested. This file marked "
                "its transparent collar with a NoData value of 0 and "
                "has no alpha band, so clearing it removes the "
                "collar's transparency as well as any interior holes. "
                "The border may now render as solid black. Every pixel "
                "value is unchanged. To keep the collar transparent, "
                "run again with Automatic or Keep as-is."
            ), "warning"
        return False, None, "routine"

    if mode == NODATA_MODE_REVEAL:
        return True, "NoData handling: cleared, as requested.", "routine"

    if mode == NODATA_MODE_KEEP:
        if nodata_risk.assessment == "meaningful":
            # Not a refusal - Keep as-is is a fully legitimate choice,
            # not gated by checkParameterValues() (see
            # algorithms/optimise_raster.py's module docstring for why
            # gating it there produced an unclosable modal loop twice).
            # Surfaced here instead, with the same pushFormattedMessage
            # emphasis as Automatic's "cleared" finding ("emphasis"
            # below) - the finding is exactly as consequential either
            # way, only the outcome (kept vs cleared) differs.
            return False, _keep_meaningful_message(nodata_risk), "emphasis"
        return False, "NoData handling: kept, as requested.", "routine"

    # mode == NODATA_MODE_AUTO
    if nodata_risk.assessment == "meaningful":
        return True, _auto_cleared_message(nodata_risk), "emphasis"
    if nodata_risk.assessment == "insufficient_sample":
        return False, _AUTO_KEPT_INSUFFICIENT_SAMPLE_MESSAGE, "routine"
    return False, _AUTO_KEPT_COLLAR_ONLY_MESSAGE, "routine"


def _default_overview_levels(xsize: int, ysize: int, min_dim: int = 256) -> list:
    """Replicate gdaladdo's "leave blank" default level series.

    That behaviour lives in the gdaladdo CLI utility, not the core GDAL
    API - BuildOverviews itself requires an explicit level list and
    raises on None. Halve repeatedly until the overview's larger
    dimension drops under min_dim, matching the doc's "down to thumbnail
    size" description - which means the factor that FIRST takes it under
    min_dim is included, not stopped short of it: appending only while
    the current candidate is still above the threshold (the previous
    form of this loop) stops one level early, verified directly against
    real gdaladdo's own no-levels default on the same test dimensions
    (45184x27264): that produces 8 levels ending at 177x107, one more
    than the 7 ending at 353x213 the previous version of this loop gave.
    """
    levels = []
    factor = 2
    while True:
        levels.append(factor)
        if max(xsize // factor, ysize // factor) <= min_dim:
            break
        factor *= 2
    return levels


class _ProgressTracker:
    """Wraps a caller-supplied GDAL progress callback so a caught
    exception from Translate/BuildOverviews can be told apart: a
    deliberate user cancellation (the wrapped callback returned falsy -
    GDAL's own cancellation convention) versus a genuine GDAL failure.
    Only the former should delete the partial output; the latter must
    keep it, per this module's existing honest-failure-reporting
    behaviour (converted_incomplete keeps a Translate-only file on disk
    rather than silently discarding it).
    """

    def __init__(self, user_cb, user_cb_data):
        self._user_cb = user_cb
        self._user_cb_data = user_cb_data
        self.cancelled = False

    def __call__(self, complete, message, _cb_data):
        ok = True
        if self._user_cb is not None:
            ok = self._user_cb(complete, message, self._user_cb_data)
        if not ok:
            self.cancelled = True
        return ok


def _safe_remove(path: Optional[str], warnings: list) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        os.remove(path)
    except OSError as exc:
        warnings.append(
            f"Cancelled, but could not remove the partial output at "
            f"{path}: {exc}. Delete it manually before re-running - it is "
            "NOT a valid result."
        )


def _same_file(a: str, b: str) -> bool:
    """True if a and b refer to the same file on disk.

    Two checks, because neither alone is enough on Windows: normcase +
    abspath catches case differences (C:\\Foo\\bar.tif vs
    c:\\foo\\BAR.tif are the same file, but plain string equality after
    abspath() alone treats them as different - a real gap the previous
    version of this guard had). os.path.samefile() catches a junction or
    symlink alias pointing at the same underlying file even when neither
    path-string trick would - the exact scenario this repo's own
    tools/qgis-*-dev.bat junction setup can produce. samefile() only
    works when both paths already exist, which is guaranteed here: if a
    and b really are the same file, that file obviously exists.
    """
    a_norm = os.path.normcase(os.path.abspath(a))
    b_norm = os.path.normcase(os.path.abspath(b))
    if a_norm == b_norm:
        return True
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def output_same_as_source_message(output_path: str) -> str:
    """Shared wording for the "output would overwrite the source" refusal -
    used both by convert()'s own guard (the last line of defence, cannot
    be bypassed by force/overwrite) and by the QGIS wrapper's
    checkParameterValues, which wants the identical message instantly via
    _same_file() rather than after a full conversion attempt. Kept as one
    function so the two call sites can't drift apart - same pattern as
    output_exists_message.
    """
    return (
        f"Output cannot be the same file as the input ({output_path}). "
        "This cannot be bypassed by Replace existing output file - choose "
        "a different output path."
    )


def output_exists_message(output_path: str) -> str:
    """Shared wording for the "output already exists, not overwriting"
    refusal - used both here (convert()'s own guard, the last line of
    defence) and by the QGIS wrapper's checkParameterValues, which needs
    the identical message but wants to show it instantly via a plain
    os.path.exists() check rather than after a full conversion attempt.
    Kept as one function so the two call sites can't drift apart.
    """
    return (
        f"{output_path} already exists. Not overwriting silently - "
        "delete it, choose a different output path, or pass force=True."
    )


def _build_applied_settings(creation_options: dict, overview_config: dict) -> AppliedSettings:
    """Turns the creation_options/overview_config dicts actually passed
    to Translate/BuildOverviews into an AppliedSettings record - see
    that dataclass's docstring for why this reads the dicts, not the
    output file. COMPRESS is always either "ZSTD" (lossless, both
    dtypes) or "JPEG" (lossy) per RECOMMENDED_SETTINGS, so the two are
    handled explicitly rather than generically - a third compression
    would need this updated anyway, same as RECOMMENDED_SETTINGS itself
    would. Everything here is known before Translate even runs, unlike
    whether BuildOverviews will actually succeed - deliberately: this
    record only ever states settings that were decided, never an
    outcome that might not happen.
    """
    compress = creation_options["COMPRESS"]
    if compress == "ZSTD":
        detail = f"level {creation_options['ZSTD_LEVEL']}"
    elif compress == "JPEG":
        quality = creation_options.get("JPEG_QUALITY")
        photometric = creation_options.get("PHOTOMETRIC")
        detail = f"quality {quality}" + (" with YCbCr" if photometric == "YCBCR" else "")
    else:
        detail = None
    return AppliedSettings(
        compression=compress,
        compression_detail=detail,
        predictor=creation_options.get("PREDICTOR"),
        tiled=creation_options.get("TILED") == "YES",
        block_size=(int(creation_options["BLOCKXSIZE"]), int(creation_options["BLOCKYSIZE"])),
        resampling=overview_config.get("RESAMPLING", "AVERAGE"),
    )


def _read_plugin_version() -> str:
    """Reads the plugin version from metadata.txt - the same file QGIS's
    Plugin Manager reads it from - rather than hardcoding it here, per
    Phase 5 of the purpose-question rework. metadata.txt lives at the
    plugin package root, one level up from this file's core/ directory;
    no QGIS import needed to read it, it's a plain INI file.
    """
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    metadata_path = os.path.join(package_root, "metadata.txt")
    parser = configparser.ConfigParser()
    parser.read(metadata_path, encoding="utf-8")
    return parser.get("general", "version", fallback="unknown")


def _compression_display_label(compression: str) -> str:
    """The user-facing name for a COMPRESS creation-option value.
    "Lossless" is prefixed for ZSTD (used for both the lossless-integer
    and lossless-float settings, so the word is needed to disambiguate)
    but not for JPEG, which is unambiguously lossy in this codebase -
    matches Phase 5's own worked examples. One function, used by both
    GEOKLEIN_5_APPLIED and TIFFTAG_IMAGEDESCRIPTION below, so the same
    compression can't end up described two different ways - this used
    to be written independently in each place.
    """
    return f"Lossless {compression}" if compression == "ZSTD" else compression


def _format_applied_settings(applied: "AppliedSettings") -> str:
    """GEOKLEIN_5_APPLIED's text, built from the structured record
    rather than re-deriving anything from GDAL creation-option key
    names a second time.
    """
    parts = [f"{_compression_display_label(applied.compression)} {applied.compression_detail}"]
    if applied.predictor:
        parts.append(f"predictor {applied.predictor}")
    if applied.alpha_reattached:
        parts.append("alpha reattached as mask")
    parts.append(f"tiled {applied.block_size[0]}x{applied.block_size[1]}")
    parts.append(f"pyramids resampled with {applied.resampling}")
    return ", ".join(parts)


def _format_reproduce_commands(full_args: list, overview_config: dict, overview_levels: list) -> str:
    """GEOKLEIN_7_REPRODUCE's text: the actual gdal_translate and gdaladdo
    invocations for this run, built from full_args (exactly what was
    passed to gdal.Translate - the same co_args this module built plus
    whatever translate_extra_args/-a_nodata applied) and overview_config/
    overview_levels (exactly what BuildOverviews used below) - not a
    hand-written second copy of the settings, so this can't drift from
    what the run actually did.

    "input.tif"/"output.tif" stand in for the real paths deliberately:
    the real source path embeds the local folder structure and the
    Windows username, and the real output path is frequently a temp
    directory that no longer exists by the time anyone reads this
    metadata back - neither is reproducible information, and whoever
    reproduces this will substitute their own paths anyway. The flags
    are the part nobody could reconstruct unaided.

    The two commands are numbered ("1. gdal_translate...", "2.
    gdaladdo...") rather than separated by a bare newline alone: QGIS's
    Layer Properties panel doesn't reliably render the \\n between them,
    which used to run both commands together into one unrunnable line
    with no visible boundary. The number survives that collapse - "...
    "output.tif" 2. gdaladdo ..." still reads as two commands even on
    one line - so the fix doesn't depend on whatever the panel decides
    to do with whitespace.
    """
    translate_cmd = "gdal_translate " + " ".join(full_args) + ' "input.tif" "output.tif"'

    addo_parts = ["gdaladdo"]
    for key, value in overview_config.items():
        if key != "RESAMPLING":
            addo_parts += ["--config", key, value]
    resampling = overview_config.get("RESAMPLING", "AVERAGE").lower()
    levels = " ".join(str(level) for level in overview_levels)
    addo_parts += ["-r", resampling, '"output.tif"', levels]
    addo_cmd = " ".join(addo_parts)

    return (
        f"1. {translate_cmd}\n2. {addo_cmd}\n"
        "Same operations in QGIS: Raster > Conversion > Translate, and "
        "Raster > Miscellaneous > Build Overviews. Full manual workflow: "
        "docs/GeoKlein_raster_optimisation_workflow.md."
    )


def _write_decision_metadata(
    out_ds: "gdal.Dataset", detection: "DetectionResult", requested_profile: Optional[str],
    profile_used: str, decisions: "DecisionSummary",
    full_args: list, overview_config: dict, overview_levels: list,
) -> None:
    """Writes the GEOKLEIN_* decision chain and TIFFTAG_IMAGEDESCRIPTION
    onto the output dataset - Phase 5 of the purpose-question rework.
    Called on out_ds straight after gdal.Translate() returns it, before
    BuildOverviews - metadata belongs in the TIFF header at the front of
    the file, settled before hundreds of megabytes of pyramid data are
    appended behind it, not rewritten afterwards.

    full_args/overview_config/overview_levels are passed in (rather than
    re-derived here) purely for GEOKLEIN_7_REPRODUCE - see
    _format_reproduce_commands()'s docstring for why reusing the exact
    values convert() already built matters.

    Strips any inherited GEOKLEIN_* keys first: Translate (like
    CreateCopy) inherits source metadata, so re-running this tool on a
    file it already produced would otherwise leave a stale record
    sitting next to the fresh one - overwrite, never append.
    """
    for key in out_ds.GetMetadata():
        if key.startswith("GEOKLEIN_"):
            out_ds.SetMetadataItem(key, None)

    version = _read_plugin_version()
    today = datetime.date.today()
    date_str = f"{today.day} {today.strftime('%B')} {today.year}"

    requested_name = "Viewing" if (requested_profile or profile_used) == "lossy" else "Analysis"
    # Names both options and what each does, chosen one first, so a
    # later reader (client, auditor) knows what the alternative would
    # have done without this doc open - a bare "Viewing" on its own
    # didn't say that. Two sentences, not one "X, chosen from ... or
    # X" clause: whichever name is chosen would otherwise appear twice
    # in the same breath (once naming the choice, once in the "or"
    # list), which read as a stutter rather than a record.
    requested_label = (
        f"{requested_name}. The options were Analysis (every pixel "
        "value preserved) and Viewing (smallest possible file)."
    )

    items = {
        "GEOKLEIN_1_TOOL": (
            f"GeoKlein Raster Optimiser {version}, a QGIS plugin, {date_str}. "
            "https://github.com/GeoKlein-Ltd/raster-optimiser (placeholder "
            "until the plugins.qgis.org listing exists)"
        ),
        "GEOKLEIN_2_DETECTED": describe_detection(detection),
        "GEOKLEIN_3_REQUESTED": requested_label,
        "GEOKLEIN_4_DECISION": decisions.profile_reason,
        "GEOKLEIN_5_APPLIED": _format_applied_settings(decisions.applied),
    }
    if decisions.nodata_message:
        items["GEOKLEIN_6_HIDDEN_PIXELS"] = decisions.nodata_message
    items["GEOKLEIN_7_REPRODUCE"] = _format_reproduce_commands(
        full_args, overview_config, overview_levels
    )

    for key, value in items.items():
        out_ds.SetMetadataItem(key, value)

    # Deliberately NOT the full GEOKLEIN_2_DETECTED/GEOKLEIN_4_DECISION
    # text - this tag exists for software that ignores GDAL's own
    # metadata domain (ArcGIS, ExifTool, Photoshop) and just needs a
    # short line, not the same paragraph repeated a second time in
    # Layer Properties. One sentence: tool and version, what the file
    # is, what compression was applied.
    out_ds.SetMetadataItem(
        "TIFFTAG_IMAGEDESCRIPTION",
        f"Optimised by GeoKlein Raster Optimiser {version}. "
        f"{content_label(detection)}, written as "
        f"{_compression_display_label(decisions.applied.compression)}.",
    )


def _format_bytes(n: int) -> str:
    # Binary units (1024-based), labelled accordingly (KiB/MiB/GiB, not
    # KB/MB/GB) - the math here was always base-1024, but the labels
    # used to read as the decimal (1000-based) SI units, which don't
    # match what's actually displayed. Confirmed in GUI testing: a
    # measured -79.4% change matches binary-unit arithmetic, not
    # decimal, so the labels were wrong, not the numbers.
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}TiB"  # unreachable in practice, keeps the loop total


# Shown after a successful conversion whenever the output ends up larger
# than the source. Only reachable with the Analysis profile - Viewing
# compression is dramatically smaller than nearly any source, so this
# fires almost exclusively there. A source that arrives already
# compressed (DEFLATE is Metashape/Terra's typical default) can be close
# enough to ZSTD's size that seven overview levels - which add roughly a
# third back on top of the base image, regardless of profile - push the
# total past the original. That is a real, expected outcome, not a
# failure: this plugin trades file size for pan/zoom speed on the base
# image, and pyramids are an unavoidable part of buying that speed.
_SIZE_INCREASE_EXPLANATION = (
    "Output is larger than source. Expected when the source was already "
    "compressed, since pyramids add back roughly a third. Not a failure: "
    "the gain here is speed, not size."
)

# Appended to _SIZE_INCREASE_EXPLANATION only when Viewing is genuinely
# available for this file (detection.profile_mode == "choice") - this
# size growth is most likely on a file already forced to Analysis
# (elevation, 16-bit/multispectral, or an RGB file with no alpha band),
# where suggesting Viewing would be advice the user cannot act on, and
# which forced_reason has often just finished explaining the tool will
# not do anyway.
_SIZE_INCREASE_VIEWING_SUGGESTION = (
    "A file written for viewing would be smaller, if size matters more "
    "than preserving every pixel value."
)


def _size_summary(source_bytes: int, output_bytes: int) -> str:
    # Percentage rounded to a whole number, not one decimal place: the
    # sizes above it are shown to 2 decimal places (GiB/MiB precision),
    # which isn't enough significant figures to reproduce a 1-decimal
    # percentage - a reader checking 1.66GiB to 1.76GiB by hand gets
    # +6.0%, not the +5.8% a naively-rounded pair could show. Rounding
    # the percentage instead keeps it consistent with the precision the
    # sizes actually carry, rather than implying false precision.
    pct = ((output_bytes - source_bytes) / source_bytes * 100) if source_bytes else 0.0
    sign = "+" if pct >= 0 else ""
    return (
        f"Source: {_format_bytes(source_bytes)}  Output: {_format_bytes(output_bytes)}  "
        f"Change: {sign}{pct:.0f}%"
    )


def _is_tiled(block_size: tuple, raster_size: tuple) -> bool:
    # A stripped/untiled band's block spans the full image width (and
    # usually just a few rows tall). Tiled data has both dimensions
    # smaller than the image itself.
    return block_size[0] < raster_size[0] and block_size[1] < raster_size[1]


def already_optimised_at_target(detection: "DetectionResult", resolved_profile: str) -> bool:
    """True only when there's genuinely nothing left to gain: tiled,
    with overviews, AND already compressed with the target codec for the
    profile that would be used. A file that's tiled with overviews but
    still on LZW/DEFLATE/uncompressed does NOT count as "already
    optimised" here.

    Public (no leading underscore) and imported by the QGIS wrapper's
    checkParameterValues() pre-flight block - this used to be a second,
    hand-mirrored copy of the rule living in algorithms/optimise_raster.py,
    with its own comment saying so. One function now, so the pre-flight
    block and convert()'s own decision below cannot drift apart.
    """
    if not (
        _is_tiled(detection.block_size, detection.raster_size)
        and detection.overview_count > 0
    ):
        return False
    target_compression = RECOMMENDED_SETTINGS[
        _settings_key(detection, resolved_profile)
    ]["creation_options"]["COMPRESS"]
    # Containment, not exact equality - GDAL reports "YCbCr JPEG" for
    # this tool's own lossy/Viewing output, not bare "JPEG" (the same
    # trap _is_jpeg_compression() above exists to document). An exact
    # match here meant a Viewing-profile output, re-run with Viewing,
    # was never recognised as already at target - it got reprocessed
    # instead, with a warning claiming the compression wasn't JPEG yet.
    # Matches _verify()'s compression_ok check below, the one comparison
    # in this file that already guarded against this.
    return target_compression.upper() in (detection.compression or "").upper()


def _verify(output_path: str, expected_compress: str, expected_block: tuple) -> VerificationResult:
    vr = VerificationResult(ran=True, expected_compression=expected_compress,
                             expected_block_size=expected_block)
    ds = gdal.Open(output_path, gdal.GA_ReadOnly)
    band1 = ds.GetRasterBand(1)
    vr.block_size = tuple(band1.GetBlockSize())
    vr.overview_count = band1.GetOverviewCount()
    md = ds.GetMetadata("IMAGE_STRUCTURE")
    vr.compression = md.get("COMPRESSION")
    ds = None

    vr.block_size_ok = vr.block_size == expected_block
    vr.overviews_ok = vr.overview_count > 0
    vr.compression_ok = expected_compress.upper() in (vr.compression or "").upper()

    if not vr.block_size_ok:
        vr.issues.append(f"block size {vr.block_size} != expected {expected_block}")
    if not vr.overviews_ok:
        vr.issues.append("no overviews found")
    if not vr.compression_ok:
        vr.issues.append(
            f"compression tag '{vr.compression}' does not contain expected '{expected_compress}'"
        )
    vr.passed = vr.block_size_ok and vr.overviews_ok and vr.compression_ok
    return vr


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def convert(
    path: str,
    detection: Optional["DetectionResult"] = None,
    chosen_profile: Optional[str] = None,
    output_path: Optional[str] = None,
    force: bool = False,
    nodata_mode: str = NODATA_MODE_AUTO,
    force_reprocess: bool = False,
    translate_progress_cb=None,
    translate_progress_cb_data=None,
    overview_progress_cb=None,
    overview_progress_cb_data=None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> ConversionResult:
    """Convert one file per the resolved detection/profile, or report why not.

    translate_progress_cb / overview_progress_cb, if given, are passed
    through to gdal.Translate and BuildOverviews respectively - GDAL's own
    callback(complete, message, cb_data) convention, one call per phase so
    a caller can scale/label each phase independently (e.g. a single
    combined progress bar: Translate 0-70%, overviews 70-100%). Returning
    a falsy value from either cancels that GDAL operation - its built-in
    cancellation mechanism - and this module then deletes whatever partial
    output exists before returning action="cancelled", so a cancelled run
    never leaves a file on disk that looks like a finished result. Nothing
    here imports QGIS; a caller with a progress dialog plugs in here
    without this module changing.

    nodata_mode (default NODATA_MODE_AUTO) - one of NODATA_MODE_AUTO /
    NODATA_MODE_REVEAL / NODATA_MODE_KEEP; see _resolve_nodata_handling
    for exactly what each does. Profile-independent: whether NoData=0
    gets cleared no longer depends on which profile is picked, only on
    this.

    force_reprocess (default False) - bypasses the "already tiled with
    overviews, do nothing" short-circuit below. Without this, a file
    that's already optimised is left untouched regardless of force or
    clear_nodata - that check exists specifically to protect against
    redoing work that wouldn't make the file any faster, so it needs its
    own explicit override rather than being folded into force (which is
    about the destination path, a different concern).

    log_cb, if given, is called as log_cb(phase, elapsed_seconds) once per
    completed phase - phase is "translate" or "overviews" (detection is
    timed by the caller, not here - this module never calls detect()).
    Kept separate from the GDAL progress callbacks since those fire many
    times per phase; this fires once, when there's an actual number to
    report, and structured rather than pre-formatted so a caller can
    drive its own UI (e.g. QGIS feedback.setProgressText()) off the phase
    name without parsing a string.
    """
    result = ConversionResult(source_path=path)

    if detection is None:
        detection = detect(path)

    if not detection.ok or detection.refused:
        result.action = "refused_upstream"
        result.message = (
            f"Detection refused this file ({detection.refusal_code}): "
            f"{detection.refusal_reason}"
        )
        return result

    # ---- resolve profile ----
    if detection.profile_mode == "forced":
        profile = detection.forced_profile
    else:
        if chosen_profile not in ("lossy", "lossless"):
            result.action = "error"
            result.message = (
                "This file needs an explicit profile choice (lossy or "
                "lossless) - detection did not force one."
            )
            return result
        profile = chosen_profile

    profile_opt = next((o for o in detection.profile_options if o.profile == profile), None)
    if profile_opt is None or not profile_opt.available:
        reason = profile_opt.reason_blocked if profile_opt else "not offered for this file"
        result.action = "blocked"
        result.message = f"The {profile} profile is not available: {reason}"
        return result

    result.profile_used = profile

    # ---- assemble the decision record (Phase 4/5 of the purpose-
    # question rework) - profile_reason/consequential set now since both
    # are already fully known; nodata_message/applied filled in further
    # down as each becomes known.
    # resolve_profile_reason() is the one place this text is produced,
    # in detector.py - see its docstring.
    profile_reason, profile_consequential = resolve_profile_reason(detection, chosen_profile)
    result.decisions = DecisionSummary(
        profile_reason=profile_reason, profile_consequential=profile_consequential,
    )

    # ---- compare current state to target, decide whether to do anything ----
    # Settings are resolved here (rather than just below, where they used
    # to be) because deciding whether there's anything left to gain needs
    # the target compression, not just tiling/overviews.
    settings_key = _settings_key(detection, profile)
    settings = RECOMMENDED_SETTINGS[settings_key]
    creation_options = settings["creation_options"]
    overview_config = settings["overview_config"]
    target_compression = creation_options["COMPRESS"]

    # Computed once here, from the source's own dimensions - Translate
    # never resizes in any profile this tool uses, no -outsize is ever
    # passed - and reused for both GEOKLEIN_7_REPRODUCE below and the
    # real BuildOverviews() call in Step 4, so the reproduce command
    # can never name a different level list than what was actually
    # built.
    overview_levels = _default_overview_levels(detection.raster_size[0], detection.raster_size[1])

    is_tiled = _is_tiled(detection.block_size, detection.raster_size)
    has_overviews = detection.overview_count > 0
    already_optimised = is_tiled and has_overviews
    # Delegates to already_optimised_at_target() rather than repeating
    # the compression-match logic inline - the same function
    # algorithms/optimise_raster.py's checkParameterValues() calls for
    # its pre-flight echo of this same decision, so the two can't drift
    # apart.
    at_target_compression = already_optimised_at_target(detection, profile)

    if already_optimised and at_target_compression:
        if not force_reprocess:
            result.ok = True
            result.action = "already_optimised"
            result.message = (
                "Already tiled with overviews, and already compressed with "
                f"{target_compression} - the speed problem this plugin "
                "exists to fix is already solved here, and there's nothing "
                "left to gain on file size either. Not touching it."
            )
            return result
        # force_reprocess overrides a genuinely nothing-to-gain file: a
        # deliberate choice (e.g. switching profile after the fact), not
        # a normal tiling/overviews/compression rebuild - primary_reason
        # wouldn't mean anything here, so this is a warning instead.
        result.warnings.append(
            "Already tiled, with overviews, and at the target compression, "
            "but reprocessing anyway - Force reprocess is ticked."
        )
    elif already_optimised:
        # Tiled with overviews, so pan/zoom speed is already fine, but the
        # current compression isn't the target one (LZW, DEFLATE, or
        # uncompressed rather than ZSTD/JPEG) - there's a real file-size
        # gain here, so this proceeds regardless of force_reprocess. Force
        # reprocess stays reserved for the genuinely-nothing-to-gain case
        # above, per the user's explicit "force reprocess stays for the
        # first case only" instruction.
        result.primary_reason = "compression"
        # Suppressed specifically for the JPEG-source-plus-Analysis case
        # (result.decisions.profile_consequential): this message promises
        # "expect a smaller file", which is false there - recompressing
        # already-JPEG-degraded pixels as lossless ZSTD typically grows
        # the file substantially (measured 4.4x on a real file) rather
        # than shrinking it. The profile_reason warning pushed elsewhere
        # already explains what's actually happening; this one would
        # only contradict it.
        if not result.decisions.profile_consequential:
            result.warnings.append(
                "Already tiled with overviews, so pan and zoom speed was "
                f"already fine. Reprocessing anyway because the current "
                f"compression ({detection.compression or 'none'}) is not "
                f"{target_compression} yet. Expect a smaller file, not a "
                "faster one."
            )
    else:
        result.primary_reason = "tiling" if not is_tiled else "overviews"

    # ---- resolve output path, with hard guards ----
    if output_path is None:
        stem, _ext = os.path.splitext(path)
        output_path = f"{stem}_optimised.tif"

    # Never write over the source. Not a check-and-warn: this cannot be
    # bypassed by force=True or any other flag. _same_file() catches case
    # differences and junction/symlink aliases, not just literal string
    # equality after abspath() - see its docstring for why plain abspath
    # comparison alone isn't enough on Windows.
    if _same_file(output_path, path):
        result.action = "error"
        result.message = output_same_as_source_message(output_path)
        return result

    if os.path.exists(output_path) and not force:
        result.action = "blocked"
        result.output_path = output_path
        result.message = output_exists_message(output_path)
        return result

    co_args = []
    for k, v in creation_options.items():
        co_args += ["-co", f"{k}={v}"]
    full_args = co_args + list(profile_opt.translate_extra_args or [])

    # Built from creation_options/overview_config themselves (the same
    # dicts turned into co_args above and used by BuildOverviews
    # below), not read back from the output file - see AppliedSettings'
    # docstring for why that separation matters. Nothing here states an
    # outcome that hasn't happened yet: resampling method is a decision
    # already made, not a claim about whether BuildOverviews (Step 4,
    # below) will succeed - whether overviews actually exist is left to
    # the file itself to show, not restated here.
    result.decisions.applied = _build_applied_settings(creation_options, overview_config)
    result.decisions.applied.alpha_reattached = bool(profile == "lossy" and detection.has_alpha)

    # ---- resolve NoData handling (profile-independent - see
    # _resolve_nodata_handling and convert()'s own docstring) ----
    should_clear_nodata, nodata_message, nodata_severity = _resolve_nodata_handling(detection, nodata_mode)
    result.nodata_mode_requested = nodata_mode
    result.nodata_cleared = should_clear_nodata
    result.nodata_message = nodata_message
    result.nodata_message_severity = nodata_severity
    result.decisions.nodata_message = nodata_message
    result.decisions.nodata_consequential = nodata_severity != "routine"
    if should_clear_nodata:
        full_args += ["-a_nodata", "none"]

    # ---- Step 3: Translate ----
    translate_tracker = _ProgressTracker(translate_progress_cb, translate_progress_cb_data)
    t0 = time.perf_counter()
    try:
        translate_opts = gdal.TranslateOptions(
            options=full_args, callback=translate_tracker, callback_data=None,
        )
        out_ds = gdal.Translate(output_path, path, options=translate_opts)
    except Exception as exc:  # noqa: BLE001 - surface any GDAL failure to the caller
        if translate_tracker.cancelled:
            _safe_remove(output_path, result.warnings)
            result.action = "cancelled"
            result.translate_ok = False
            result.message = "Cancelled during Translate - partial output removed."
            return result
        result.action = "error"
        result.translate_ok = False
        # Not removed: unlike a cancellation (the user's own choice) or
        # BuildOverviews failing after Translate already fully succeeded
        # (see below), a file left behind by a genuine Translate failure
        # might be incomplete, but deleting it outright on this module's
        # own judgement risks discarding something the user could still
        # inspect or recover from - silently losing data is worse than
        # leaving an orphan they've been told about.
        result.message = (
            f"Translate failed: {exc}. If a file exists at {output_path}, "
            "Translate did not finish writing it and it may be incomplete "
            "or invalid - check it before using it, and delete it before "
            "re-running this tool on the same output path."
        )
        return result

    if out_ds is None:
        out_ds = None
        if translate_tracker.cancelled:
            _safe_remove(output_path, result.warnings)
            result.action = "cancelled"
            result.translate_ok = False
            result.message = "Cancelled during Translate - partial output removed."
            return result
        result.action = "error"
        result.translate_ok = False
        result.message = "Translate failed (no output produced)."
        return result

    # Phase 5 of the purpose-question rework: metadata written here,
    # on the still-open Translate handle, before it's closed and
    # reopened for BuildOverviews below - see _write_decision_metadata()'s
    # docstring for why this has to happen before pyramids, not after.
    _write_decision_metadata(
        out_ds, detection, chosen_profile, profile, result.decisions,
        full_args, overview_config, overview_levels,
    )
    out_ds = None  # flush/close before reopening for BuildOverviews
    result.translate_ok = True
    result.output_path = output_path
    result.translate_seconds = time.perf_counter() - t0
    if log_cb:
        log_cb("translate", result.translate_seconds)

    # ---- Step 4: Build Overviews ----
    overview_tracker = _ProgressTracker(overview_progress_cb, overview_progress_cb_data)
    prior_config = {k: gdal.GetConfigOption(k) for k in overview_config if k != "RESAMPLING"}
    for k, v in overview_config.items():
        if k != "RESAMPLING":
            gdal.SetConfigOption(k, v)
    t1 = time.perf_counter()
    try:
        out_ds = None
        try:
            out_ds = gdal.Open(output_path, gdal.GA_Update)
            out_ds.BuildOverviews(
                overview_config.get("RESAMPLING", "AVERAGE"),
                overviewlist=overview_levels,
                callback=overview_tracker, callback_data=None,
            )
            out_ds = None
            result.overviews_ok = True
            result.overview_seconds = time.perf_counter() - t1
            if log_cb:
                log_cb("overviews", result.overview_seconds)
        except Exception as exc:  # noqa: BLE001
            out_ds = None  # release the GA_Update handle before any delete
            result.overviews_ok = False
            if overview_tracker.cancelled:
                _safe_remove(output_path, result.warnings)
                result.action = "cancelled"
                result.message = (
                    "Cancelled while building overviews - output removed "
                    "(the base image was complete but pyramids weren't, "
                    "and a file with no pyramids is exactly the slow-pan "
                    "problem this plugin exists to fix, so it isn't left "
                    "behind looking like a finished result)."
                )
                return result
            # Translate already succeeded and the file is on disk. Keep it -
            # re-running Translate on a large file is expensive, and
            # overviews can be retried on this exact output directly. Report
            # honestly rather than silently calling this success. The QGIS
            # wrapper still raises on this action (see _HARD_FAILURE_ACTIONS
            # in algorithms/optimise_raster.py - the run genuinely didn't
            # finish what it promised), but that exception carries this
            # message verbatim, so whoever sees it - GUI dialog, log,
            # qgis_process stderr, direct API/CLI use - is told a usable
            # file already exists and exactly how to finish it, not just
            # that something failed.
            result.action = "converted_incomplete"
            result.message = (
                f"Translate succeeded but building overviews failed: {exc}. "
                f"The output at {output_path} exists, tiled and compressed, "
                "but has no pyramids yet. Add them without re-converting: "
                "in QGIS, Raster > Miscellaneous > Build Overviews on this "
                "file, or run this tool again on it."
            )
            return result
    finally:
        for k, v in prior_config.items():
            gdal.SetConfigOption(k, v)

    # ---- Verify (doc Step 4 "Verify") ----
    expected_block = (
        int(creation_options["BLOCKXSIZE"]), int(creation_options["BLOCKYSIZE"]),
    )
    verification = _verify(output_path, creation_options["COMPRESS"], expected_block)
    result.verification = verification

    result.source_bytes = os.path.getsize(path)
    result.output_bytes = os.path.getsize(output_path)
    result.size_summary = _size_summary(result.source_bytes, result.output_bytes)
    if result.output_bytes > result.source_bytes:
        size_note = _SIZE_INCREASE_EXPLANATION
        if detection.profile_mode == "choice":
            size_note += " " + _SIZE_INCREASE_VIEWING_SUGGESTION
        result.size_note = size_note

    if verification.passed:
        result.ok = True
        result.action = "converted"
        result.message = f"Converted and verified: {output_path}"
    else:
        result.ok = False
        result.action = "converted_unverified"
        result.message = (
            "Converted, but verification found problems: "
            f"{'; '.join(verification.issues)}. Output at {output_path} may "
            "not be fully optimised - check manually."
        )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _to_jsonable(obj):
    if dataclasses.is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def _print_report(result: ConversionResult) -> None:
    print(f"Source: {result.source_path}")
    print(f"Action: {result.action}")
    print(f"  {result.message}")
    if result.profile_used:
        print(f"  Profile: {result.profile_used}")
    if result.primary_reason:
        print(f"  Primary reason for conversion: {result.primary_reason}")
    if result.output_path:
        print(f"  Output: {result.output_path}")
    if result.translate_seconds is not None:
        print(f"  Translate time: {result.translate_seconds:.1f}s")
    if result.overview_seconds is not None:
        print(f"  Build overviews time: {result.overview_seconds:.1f}s")
    if result.size_summary:
        print(f"  {result.size_summary}")
    if result.size_note:
        print(f"  {result.size_note}")
    if result.nodata_message:
        print(f"  {result.nodata_message}")
    if result.verification and result.verification.ran:
        v = result.verification
        print("\nVerification:")
        print(f"  Block size: {v.block_size} (expected {v.expected_block_size}) "
              f"{'OK' if v.block_size_ok else 'FAILED'}")
        print(f"  Overviews: {v.overview_count} {'OK' if v.overviews_ok else 'FAILED'}")
        print(f"  Compression: {v.compression} (expected to contain "
              f"'{v.expected_compression}') {'OK' if v.compression_ok else 'FAILED'}")
        print(f"  Overall: {'PASSED' if v.passed else 'FAILED'}")
    for w in result.warnings:
        print(f"\nWarning: {w}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Raster Optimiser conversion")
    parser.add_argument("path", help="Path to a raster file")
    parser.add_argument("--profile", choices=["lossy", "lossless"], default=None,
                         help="Required when detection offers a choice")
    parser.add_argument("--output", default=None, help="Output path (default: <stem>_optimised.tif)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing output file")
    parser.add_argument("--nodata-mode", choices=["auto", "reveal", "keep"], default="auto",
                         help="How to handle NoData=0 on RGB imagery (default: auto)")
    parser.add_argument("--force-reprocess", action="store_true",
                         help="Reprocess even if the source is already tiled with overviews")
    parser.add_argument("--quiet", action="store_true", help="No terminal progress bar")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a human report")
    args = parser.parse_args(argv)

    detection = detect(args.path)
    progress_cb = None if args.quiet else gdal.TermProgress_nocb
    log_cb = None if args.quiet else (lambda phase, secs: print(f"{phase} finished in {secs:.1f}s"))
    result = convert(
        args.path, detection=detection, chosen_profile=args.profile,
        output_path=args.output, force=args.force, nodata_mode=args.nodata_mode,
        force_reprocess=args.force_reprocess,
        translate_progress_cb=progress_cb, overview_progress_cb=progress_cb,
        log_cb=log_cb,
    )

    if args.json:
        print(json.dumps(_to_jsonable(result), indent=2))
    else:
        _print_report(result)

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
