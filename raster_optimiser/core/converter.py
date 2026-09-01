"""Raster Optimiser conversion module.

Pure GDAL/Python, no QGIS imports. Takes an already-classified
DetectionResult (see core/detector.py) for a file that was NOT refused,
and does the actual work from
docs/GeoKlein_raster_optimisation_workflow.md Steps 3-4, now merged into
one: v1 always writes a Cloud Optimized GeoTIFF (see
docs/plugin_design_notes.md), and the COG driver builds tiling,
compression AND pyramids inside a single Translate call rather than a
separate Translate-then-BuildOverviews pair - there is no second GDAL
call left to make.

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
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from osgeo import gdal
# Ships alongside the GDAL Python bindings themselves (confirmed present
# in this environment's GDAL 3.13.2/OSGeo4W install; not separately
# checked against every QGIS version/OS this plugin targets) - the
# official reference implementation for COG structural validation, used
# by _verify() below rather than re-implementing a byte-order check by
# hand. Imported unconditionally, same as gdal itself: if this is ever
# missing, that's an environment problem worth failing loudly on, not
# one to silently work around.
from osgeo_utils.samples.validate_cloud_optimized_geotiff import validate as _validate_cog

try:
    from .detector import (
        detect, DetectionResult, RECOMMENDED_SETTINGS, resolve_profile_reason,
        describe_detection, content_label, is_jpeg_compression,
    )
except ImportError:  # running as a plain script, not as part of the core package
    from detector import (
        detect, DetectionResult, RECOMMENDED_SETTINGS, resolve_profile_reason,
        describe_detection, content_label, is_jpeg_compression,
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
    cog_valid: bool = False
    passed: bool = False
    issues: list = field(default_factory=list)


@dataclass
class AppliedSettings:
    """What was actually passed to Translate, in a form that doesn't
    require knowing GDAL's creation-option key names (COMPRESS, LEVEL
    vs QUALITY, PREDICTOR) to read. Built from the same
    creation_options/overview_config dicts convert() already constructs
    for the GDAL call itself - not read back
    from VerificationResult, which stays an independent check of the
    output file so a mismatch between intended and actually-written
    settings stays detectable rather than getting silently papered over
    by this record agreeing with itself.
    """
    compression: str  # "ZSTD" | "JPEG"
    compression_detail: Optional[str] = None  # "level 9" | "quality 90 with YCbCr"
    predictor: Optional[str] = None  # "2" | "3" | None (JPEG has none)
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
    # already_optimised | converted | converted_unverified | blocked
    # | refused_upstream | cancelled | error
    # (converted_incomplete no longer occurs: that was Translate
    # succeeding while a separate BuildOverviews call then failed, and
    # the COG driver builds pyramids inside the one Translate call, so
    # there's no longer a separate step left to fail independently)
    action: str = "unknown"
    message: str = ""
    output_path: Optional[str] = None
    profile_used: Optional[str] = None
    primary_reason: Optional[str] = None  # "tiling" | "overviews" | "compression" | "cog_structure" | None
    translate_ok: Optional[bool] = None
    translate_seconds: Optional[float] = None
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
    API. Only the length of this list is used now - convert() passes it
    as the COG driver's OVERVIEW_COUNT creation option, which takes a
    plain count rather than an explicit factor list - but the count has
    to come from somewhere real: COG's own OVERVIEW_COUNT default,
    tested directly, stops one or more levels shallower than this
    function does (its cutoff tracks BLOCKSIZE, not this codebase's own
    min_dim), so leaving it unset would silently shrink the pyramid
    depth this tool has always produced. Keeping this function as the
    source of truth for the level *count* still ties it to the
    dimensions/gdaladdo-parity reasoning below, even though the
    explicit factor list itself is no longer passed anywhere. Halve
    repeatedly until the overview's larger dimension drops under
    min_dim, matching the doc's "down to thumbnail
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
    exception from Translate can be told apart: a deliberate user
    cancellation (the wrapped callback returned falsy - GDAL's own
    cancellation convention) versus a genuine GDAL failure. Only the
    former should delete the partial output; the latter must keep it,
    per this module's existing honest-failure-reporting behaviour (see
    the Translate except block below).
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


def _estimate_output_ceiling_bytes(
    detection: "DetectionResult", profile: str, source_bytes: int,
) -> int:
    """An estimate of the output's own size, for _check_free_space()
    below - real reasoning behind each profile's number, not the
    source file's on-disk size (see that function's docstring for why
    the source was the wrong basis).

    The two profiles need different reasoning, because they fail
    differently:

    Lossy always writes JPEG at quality 90 on RGB imagery, and this
    codebase already has a real, measured figure for what that does in
    practice - shortHelpString()/README's "What to expect" report a
    typical drone orthomosaic coming out around 80% smaller than
    source (roughly a fifth). This uses half that shrinkage (50%
    smaller, not 80%) rather than the full documented figure: it's a
    typical result from real testing, not a guaranteed worst case, and
    halving it builds in margin against less-compressible content
    without pretending to a precision this can't actually have before
    Translate runs.

    That 50% assumption only holds when there's real compression left
    to gain, which fails for a source that's already JPEG-compressed -
    most commonly this tool's own prior Viewing/lossy output, re-run
    for any reason (a different NoData mode, force_reprocess, and so
    on). Measured directly, twice, on two different real files:
    re-encoding already-JPEG-degraded pixels as JPEG again does not
    shrink further - it grows slightly (350.21MiB source -> 350.35MiB
    output; 367,221,417 -> 367,363,465 bytes on a second file), both
    +0.04%. Using the 50%-smaller assumption there would UNDER-estimate
    the real output by about 2x - worse than the old source-based
    over-estimate, since it could let a run start that then fails
    partway through on disk space rather than being refused up front.

    So an already-JPEG source uses the full source size PLUS a 5%
    margin, not the 50% discount. 5% is not itself a measurement - only
    +0.04% growth was ever observed, on two files - but using exactly
    that measured figure as the margin would mean trusting two data
    points to bound a real effect (JPEG re-encode growth) that plausibly
    varies with content and quantisation. 5% is comfortably above what
    was actually seen while still being a small, stated correction
    rather than a second large discount undoing the point of measuring
    this case separately at all.

    Lossless has no equivalent "typically X% smaller" figure to lean
    on - ZSTD's ratio is far more content-dependent, and this codebase
    has already measured the opposite failure: an already-JPEG-
    degraded source recompressed as lossless ZSTD came out 4.4x the
    SOURCE FILE's size (see _SIZE_INCREASE_EXPLANATION below). So
    lossless uses a real, provable ceiling instead of a guess: no
    codec this tool uses can write more base-image pixel data than the
    raster's own uncompressed size, raster_size x band count x
    bytes-per-sample - that 4.4x figure stays comfortably under this
    ceiling, since it's 4.4x an already-heavily-compressed source, not
    4.4x the raw pixel data.
    """
    if profile == "lossy":
        if is_jpeg_compression(detection.compression):
            return int(source_bytes * 1.05)
        return int(source_bytes * 0.5)
    bytes_per_sample = gdal.GetDataTypeSize(gdal.GetDataTypeByName(detection.dtype)) // 8
    xsize, ysize = detection.raster_size
    return xsize * ysize * detection.band_count * bytes_per_sample


def _check_free_space(output_path: str, estimated_output_bytes: int, factor: float = 1.3) -> Optional[str]:
    """Pre-flight guard for the COG driver's own working-space
    requirement - measured directly (watched a 516MB elevation file
    convert): the driver writes a temporary "<output>.tif.ovr.tmp" file
    alongside the destination while it builds overviews and reorganises
    the file into COG's IFDs-before-data layout, peaking at roughly a
    third of the final output size before being folded in and removed.
    Checked against the DESTINATION volume specifically, not the OS
    temp directory - that temp file was confirmed written next to the
    output, not in system temp.

    estimated_output_bytes should be _estimate_output_ceiling_bytes()'s
    result, not the source file's size - see that function's docstring
    for why the source is the wrong basis. Erring toward
    over-estimating the requirement is still the safer failure mode
    here even with a real ceiling rather than a guess: a false "not
    enough space" refusal costs a re-run, an out-of-space failure
    partway through a multi-hundred-megabyte conversion costs the time
    already spent and leaves a partial file behind. Whether GDAL's own
    out-of-space failure text is clear enough to drop this check
    entirely hasn't been tested - doing so deliberately would mean
    filling a real disk, which wasn't done here - so this stays as the
    proactive guard until that's been seen.

    Returns None if there's enough room (or the check itself couldn't
    run - see below), or a ready-to-show message naming the volume and
    the shortfall if not.
    """
    directory = os.path.dirname(os.path.abspath(output_path)) or "."
    needed = int(estimated_output_bytes * factor)
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        # Can't check (e.g. a destination directory that doesn't exist
        # yet) - don't block on a check that couldn't run. GDAL's own
        # failure during Translate remains the fallback if space
        # genuinely runs out.
        return None
    if free >= needed:
        return None
    drive = os.path.splitdrive(os.path.abspath(directory))[0] or directory
    shortfall = needed - free
    return (
        f"Not enough free space on {drive} to convert this file safely. "
        "Cloud Optimized GeoTIFF creation needs working space on top of "
        f"the output while it builds pyramids and reorganises the file - "
        f"estimated at roughly {_format_bytes(needed)} here (1.3x an "
        f"estimated {_format_bytes(estimated_output_bytes)} output). "
        f"{drive} has {_format_bytes(free)} free, which is "
        f"{_format_bytes(shortfall)} short. Free up space or choose a "
        "different output location, then run again."
    )


def _build_applied_settings(creation_options: dict, overview_config: dict) -> AppliedSettings:
    """Turns the creation_options/overview_config dicts actually passed
    to Translate into an AppliedSettings record - see that dataclass's
    docstring for why this reads the dicts, not the output file.
    COMPRESS is always either "ZSTD" (lossless, both dtypes) or "JPEG"
    (lossy) per RECOMMENDED_SETTINGS, so the two are handled explicitly
    rather than generically - a third compression would need this
    updated anyway, same as RECOMMENDED_SETTINGS itself would.
    Everything here is known before Translate even runs, unlike whether
    the file will actually verify afterwards - deliberately: this
    record only ever states settings that were decided, never an
    outcome that might not happen.
    """
    compress = creation_options["COMPRESS"]
    if compress == "ZSTD":
        detail = f"level {creation_options['LEVEL']}"
    elif compress == "JPEG":
        # No PHOTOMETRIC to read back - COG doesn't accept that option
        # at all (see RECOMMENDED_SETTINGS's comment). "with YCbCr" is
        # unconditional here rather than reading a creation option,
        # because it's unconditionally true: lossy is only ever offered
        # for RGB_8BIT content in this codebase, and a 3-band Byte
        # image with COMPRESS=JPEG comes out YCbCr-encoded on the COG
        # driver's own initiative, confirmed directly against the
        # driver (SOURCE_COLOR_SPACE=YCbCr in the output with no
        # colourspace option requested).
        quality = creation_options.get("QUALITY")
        detail = f"quality {quality} with YCbCr"
    else:
        detail = None
    return AppliedSettings(
        compression=compress,
        compression_detail=detail,
        predictor=creation_options.get("PREDICTOR"),
        block_size=(int(creation_options["BLOCKSIZE"]), int(creation_options["BLOCKSIZE"])),
        resampling=overview_config.get("OVERVIEW_RESAMPLING", "AVERAGE"),
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

    Leads with "Cloud Optimized GeoTIFF" rather than folding it into
    "tiled" - v1 always writes a COG (see
    docs/plugin_design_notes.md), and a reader with this file's
    metadata open but not the plugin's docs shouldn't have to infer COG
    compliance from tiling plus pyramids plus compression on their own.
    """
    parts = ["Cloud Optimized GeoTIFF (COG)",
             f"{_compression_display_label(applied.compression)} {applied.compression_detail}"]
    if applied.predictor:
        parts.append(f"predictor {applied.predictor}")
    if applied.alpha_reattached:
        parts.append("alpha reattached as mask")
    parts.append(f"tiled {applied.block_size[0]}x{applied.block_size[1]}")
    parts.append(f"pyramids resampled with {applied.resampling}")
    return ", ".join(parts)


def _format_reproduce_commands(full_args: list) -> str:
    """GEOKLEIN_7_REPRODUCE's text: the actual gdal_translate invocation
    for this run, built from full_args (exactly what was passed to
    gdal.Translate - the same co_args this module built, including
    -of COG and OVERVIEW_COUNT, plus whatever translate_extra_args/
    -a_nodata applied) - not a hand-written second copy of the
    settings, so this can't drift from what the run actually did.

    One command now, not two: v1 always writes a Cloud Optimized
    GeoTIFF (see docs/plugin_design_notes.md), and the COG driver
    builds pyramids inside the same Translate call rather than a
    separate BuildOverviews step - there is no gdaladdo invocation left
    to reproduce.

    "input.tif"/"output.tif" stand in for the real paths deliberately:
    the real source path embeds the local folder structure and the
    Windows username, and the real output path is frequently a temp
    directory that no longer exists by the time anyone reads this
    metadata back - neither is reproducible information, and whoever
    reproduces this will substitute their own paths anyway. The flags
    are the part nobody could reconstruct unaided.
    """
    translate_cmd = "gdal_translate " + " ".join(full_args) + ' "input.tif" "output.tif"'

    return (
        f"{translate_cmd}\n"
        "Same operation in QGIS: Raster > Conversion > Translate, with "
        "the output format set to COG. Full manual workflow: "
        "docs/GeoKlein_raster_optimisation_workflow.md."
    )


def _build_decision_metadata(
    path: str, detection: "DetectionResult", requested_profile: Optional[str],
    profile_used: str, decisions: "DecisionSummary", full_args: list,
) -> dict:
    """Builds the GEOKLEIN_* decision chain and TIFFTAG_IMAGEDESCRIPTION
    as a plain {key: value} dict - Phase 5 of the purpose-question
    rework. Everything it draws on (detection, decisions.profile_reason/
    nodata_message/applied, full_args) is known before Translate ever
    runs, which is deliberate: these values now become -mo KEY=VALUE
    arguments passed INTO the same Translate call that creates the
    file, not SetMetadataItem() calls made on it afterwards.

    That "afterwards" approach is what this function replaced, and it
    is not just a style choice: tested directly against the COG driver,
    calling SetMetadataItem() on an already-created COG dataset and
    closing it moves the main IFD to the end of the file to fit the
    grown tag data, which breaks the "IFDs before data" byte ordering a
    COG's entire point rests on - confirmed with the official
    validate_cloud_optimized_geotiff.py, which passed a file built this
    way (-mo at Translate time) and failed the same content written via
    the old post-Translate SetMetadataItem() approach. See
    docs/plugin_design_notes.md.

    full_args is passed in (rather than re-derived here) purely for
    GEOKLEIN_7_REPRODUCE - see _format_reproduce_commands()'s docstring
    for why reusing the exact value convert() already built matters,
    and why it's full_args (before the -mo flags this function's own
    output becomes) rather than the final Translate args: the printed
    reproduce command documents the conversion, not this tool's own
    self-description, so it doesn't re-include the metadata that
    describes it.

    GEOKLEIN_6_HIDDEN_PIXELS is always present, as an empty string when
    there's no message this run, rather than omitted - confirmed
    directly that "-mo KEY=" (empty) actually removes an inherited
    value with that key, not just blanks it. Translate inherits source
    metadata, so re-running this tool on a file it already produced
    (with a message that run, none this run) would otherwise leave a
    stale record sitting next to the fresh one.

    The same problem exists for any other GEOKLEIN_* key the source
    happens to carry that isn't one of the seven above, so this
    function also opens the source itself (metadata-only, no pixel
    read - see the scan below) and clears any inherited key starting
    with GEOKLEIN_ it doesn't already know about. This repo's own
    commit history has never held an eighth key or a differently-named
    one - confirmed directly against every commit, not assumed - but
    that's a fact about this repository, not about every build of this
    tool that has ever run: a build predating git init wrote
    GEOKLEIN_SUMMARY into real files, and files carrying it still
    exist. The scan below exists for exactly that gap - a stray key
    this repo's history has no record of, but a real file can still
    carry - not because an eighth key has ever turned up in anything
    reviewed here.
    """
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
        "GEOKLEIN_6_HIDDEN_PIXELS": decisions.nodata_message or "",
        "GEOKLEIN_7_REPRODUCE": _format_reproduce_commands(full_args),
        # Deliberately NOT the full GEOKLEIN_2_DETECTED/GEOKLEIN_4_DECISION
        # text - this tag exists for software that ignores GDAL's own
        # metadata domain (ArcGIS, ExifTool, Photoshop) and just needs a
        # short line, not the same paragraph repeated a second time in
        # Layer Properties. One sentence: tool and version, what the
        # file is, what compression was applied.
        "TIFFTAG_IMAGEDESCRIPTION": (
            f"Optimised by GeoKlein Raster Optimiser {version}. "
            f"{content_label(detection)}, written as "
            f"{_compression_display_label(decisions.applied.compression)}."
        ),
    }

    # Scan the source's own metadata for a stray GEOKLEIN_* key none of
    # the seven above already covers - see this function's docstring
    # for why the fixed seven aren't assumed to be the whole possible
    # set. A second gdal.Open() here (detect()'s own handle is already
    # closed by this point - see detector.py's "finally: ds = None"
    # blocks) but metadata-only, so cheap even on a large source: no
    # pixel data is touched, only the header. Never lets a scan failure
    # block the conversion itself - a source metadata read that fails
    # here is surprising (detection already opened this same file
    # successfully earlier in this same run) but not a reason to abort
    # a Translate that would otherwise succeed.
    try:
        src_ds = gdal.Open(path, gdal.GA_ReadOnly)
        src_metadata = src_ds.GetMetadata() if src_ds is not None else {}
    except Exception:  # noqa: BLE001 - never block the conversion on this scan
        src_metadata = {}
    finally:
        src_ds = None
    for key in src_metadata:
        if key.startswith("GEOKLEIN_") and key not in items:
            items[key] = ""

    return items


def _metadata_mo_args(items: dict) -> list:
    """Turns a {key: value} dict into ["-mo", "KEY=VALUE", ...] tokens
    for gdal.Translate's options list - see _build_decision_metadata()'s
    docstring for why these are passed at Translate time rather than
    SetMetadataItem() calls made afterwards.
    """
    args = []
    for key, value in items.items():
        args += ["-mo", f"{key}={value}"]
    return args


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
# than the source AND this run built the source's pyramids for the first
# time (detection.has_overviews was False going in - see convert()'s
# "pyramids_added_fresh" check). Only reachable with the Analysis profile
# in that case - Viewing compression is dramatically smaller than nearly
# any source, so this fires almost exclusively there. A source that
# arrives already compressed (DEFLATE is Metashape/Terra's typical
# default) can be close enough to ZSTD's size that seven fresh overview
# levels - which add roughly a third back on top of the base image,
# regardless of profile - push the total past the original. That is a
# real, expected outcome, not a failure: this plugin trades file size for
# pan/zoom speed on the base image, and pyramids are an unavoidable part
# of buying that speed.
#
# This explanation is specifically about pyramids being BUILT here for
# the first time - it must not fire when the source already had
# overviews, since then pyramids aren't what changed (see
# _SIZE_INCREASE_RESTRUCTURE_EXPLANATION below for that case), NOR when
# the profile decision was already consequential (see convert()'s
# `result.decisions.profile_consequential` check) - a JPEG-source
# profile_reason warning already explains the increase in that case, and
# this note's own "pyramids add back roughly a third" claim can be wrong
# in cause AND magnitude for it: confirmed live on a JPEG source with NO
# existing pyramids, re-run through Analysis - this note still fired
# (has_overviews was False, so the first condition alone didn't catch it)
# and claimed "roughly a third" under a real +837% increase, while the
# profile_reason warning two lines above had already correctly explained
# the real cause (re-encoding already-degraded pixels losslessly). A
# similar case (a 0.02% increase on a file that already had pyramids)
# is what prompted _SIZE_INCREASE_RESTRUCTURE_EXPLANATION below in the
# first place - both are the same underlying mistake: this note firing
# when something else already, correctly, accounts for the increase.
_SIZE_INCREASE_EXPLANATION = (
    "Output is larger than source. Expected when the source was already "
    "compressed, since pyramids add back roughly a third. Not a failure: "
    "the gain here is speed, not size."
)

# Growth on a file that already had pyramids before this run, below which
# _SIZE_INCREASE_RESTRUCTURE_EXPLANATION is skipped entirely rather than
# shown. 1%: comfortably above the kind of overhead pure COG restructuring
# itself can add (a small, fixed amount of header/IFD/ghost-area bytes
# relative to any real image - the confirmed real-world case that
# prompted this was 0.02%) while still well below a genuine, worth-
# explaining increase from a compression change or a deeper pyramid than
# the source already had. Not a measured boundary - there was only one
# real data point (0.02%) to calibrate against - but a round number
# comfortably on the "not worth a paragraph" side of it, chosen the same
# way _NODATA_PCT_DISPLAY_FLOOR above states a plain threshold rather
# than pretending to a precision this can't have.
_SIZE_INCREASE_RESTRUCTURE_THRESHOLD_PCT = 1.0

# Shown instead of _SIZE_INCREASE_EXPLANATION when the source already had
# pyramids (so building them isn't the cause), the profile decision
# was NOT already consequential (see convert()'s check - a JPEG-source
# profile_reason warning already covers that case, more specifically and
# correctly than a generic sentence here could), and the growth is large
# enough to be worth naming a cause for at all (see the threshold above).
# Deliberately does not name a single specific cause - restructuring a
# file that was already tiled/overviewed/correctly-compressed can still
# grow it either because this tool's own pyramid depth exceeds what the
# source already had, or because of the compression change itself - and
# claiming one specific cause here risked being just as wrong as the bug
# this replaced.
_SIZE_INCREASE_RESTRUCTURE_EXPLANATION = (
    "Output is larger than source. This file already had pyramids, so "
    "they are not the cause here - the increase comes from the "
    "compression change or Cloud Optimized restructuring made in this "
    "run, not from building pyramids that already existed."
)

# Appended to _SIZE_INCREASE_EXPLANATION only when Viewing was genuinely
# available for this file (detection.profile_mode == "choice") AND
# Analysis is the profile that actually ran. Both conditions are needed:
# profile_mode == "choice" alone doesn't distinguish "Viewing was offered
# but Analysis ran" from "Viewing was offered and Viewing ran" - without
# the second check, a Viewing run that happened to grow past its source
# (e.g. an already-JPEG source re-encoded) got told "a file written for
# viewing would be smaller" immediately after writing one for viewing,
# which is nonsensical advice about the very run that just happened.
# Confirmed live: PURPOSE=Viewing on Ortho_school_v1_optimised.tif
# printed exactly that contradiction.
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
    """True only when there's genuinely nothing left to gain: already a
    valid Cloud Optimized GeoTIFF, tiled, with overviews, AND already
    compressed with the target codec for the profile that would be
    used. A file that's tiled with overviews but still on LZW/DEFLATE/
    uncompressed does NOT count as "already optimised" here - and
    neither, now, does a file that happens to be tiled/overviewed/
    correctly-compressed without actually being a COG.

    That last case is not hypothetical: every file this tool produced
    before it switched to always writing COGs fails COG validation
    (confirmed directly with the official validator against this
    tool's own pre-COG test outputs - see docs/plugin_design_notes.md)
    despite passing the other three checks below. Without the LAYOUT
    check, re-running this tool on its own older output would wrongly
    report "already optimised" and never actually turn it into a real
    COG. The check reads DetectionResult.layout - populated from the
    same already-open metadata read as compression, in detect(), not a
    second file open here - deliberately cheap, since this runs inside
    the QGIS wrapper's checkParameterValues() pre-flight block, where
    speed matters. That's also why this checks the metadata tag alone
    rather than running the full validator: a cheap, mostly-reliable
    signal is the right trade here, not the same thoroughness
    convert()'s own _verify() needs once a file is actually about to
    be skipped.

    Public (no leading underscore) and imported by the QGIS wrapper's
    checkParameterValues() pre-flight block - this used to be a second,
    hand-mirrored copy of the rule living in algorithms/optimise_raster.py,
    with its own comment saying so. One function now, so the pre-flight
    block and convert()'s own decision below cannot drift apart.
    """
    if detection.layout != "COG":
        return False
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
    # trap is_jpeg_compression() in detector.py exists to document). An exact
    # match here meant a Viewing-profile output, re-run with Viewing,
    # was never recognised as already at target - it got reprocessed
    # instead, with a warning claiming the compression wasn't JPEG yet.
    # Matches _verify()'s compression_ok check below, the one comparison
    # in this file that already guarded against this.
    return target_compression.upper() in (detection.compression or "").upper()


def _verify(output_path: str, expected_compress: str, expected_block: tuple) -> VerificationResult:
    """Confirms the output is actually what it claims to be - including,
    now, that it's a genuinely valid Cloud Optimized GeoTIFF, not just a
    file that happens to be tiled/overviewed/correctly-compressed
    without being one.

    That last distinction is not academic: the -mo-after-Translate bug
    caught earlier in this same rework (see docs/plugin_design_notes.md)
    produced a file with LAYOUT=COG sitting right there in its own
    metadata, tiled, overviewed, and correctly compressed - passing
    every check this function had before this change - while the main
    IFD had actually been moved past the pixel data by a later metadata
    write, making it genuinely invalid. A LAYOUT tag read alone would
    have passed that file. Only running the real validator - the same
    one a client's own GIS software or a plugins.qgis.org reviewer
    might run - catches it, which is why this runs the full check here
    rather than the cheap tag read already_optimised_at_target() uses
    (that function has a different job: a fast, mostly-reliable signal
    for whether to skip a source file entirely, not the last word on
    whether a freshly-written file is actually correct).
    """
    vr = VerificationResult(ran=True, expected_compression=expected_compress,
                             expected_block_size=expected_block)
    ds = gdal.Open(output_path, gdal.GA_ReadOnly)
    band1 = ds.GetRasterBand(1)
    vr.block_size = tuple(band1.GetBlockSize())
    vr.overview_count = band1.GetOverviewCount()
    md = ds.GetMetadata("IMAGE_STRUCTURE")
    vr.compression = md.get("COMPRESSION")
    # full_check=True (byte-level tile/strip leader/trailer checks, not
    # just structural offsets) - the validator's own docs call this
    # possibly slow on remote files, but this is always a local path
    # straight after Translate wrote it, and measured directly on a
    # real 433MB output, full_check added under 5ms over the cheaper
    # default. Passed the still-open dataset handle rather than
    # output_path a second time - validate() accepts either, and this
    # avoids reopening a file already open two lines up.
    cog_warnings, cog_errors, _cog_details = _validate_cog(ds, full_check=True)
    ds = None

    vr.block_size_ok = vr.block_size == expected_block
    vr.overviews_ok = vr.overview_count > 0
    vr.compression_ok = expected_compress.upper() in (vr.compression or "").upper()
    vr.cog_valid = not cog_errors

    if not vr.block_size_ok:
        vr.issues.append(f"block size {vr.block_size} != expected {expected_block}")
    if not vr.overviews_ok:
        vr.issues.append("no overviews found")
    if not vr.compression_ok:
        vr.issues.append(
            f"compression tag '{vr.compression}' does not contain expected '{expected_compress}'"
        )
    if not vr.cog_valid:
        vr.issues.append("not a valid Cloud Optimized GeoTIFF: " + "; ".join(cog_errors))
    # cog_warnings (e.g. advisory notes the validator doesn't treat as
    # invalidating) deliberately don't affect vr.passed - only actual
    # errors do, matching how block/overview/compression checks above
    # only ever report hard mismatches, not advisories.
    vr.passed = vr.block_size_ok and vr.overviews_ok and vr.compression_ok and vr.cog_valid
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
    log_cb: Optional[Callable[[str, Optional[float]], None]] = None,
) -> ConversionResult:
    """Convert one file per the resolved detection/profile, or report why not.

    translate_progress_cb, if given, is passed through to gdal.Translate -
    GDAL's own callback(complete, message, cb_data) convention. There used
    to be a second, separate overview_progress_cb for a following
    BuildOverviews call; the COG driver builds pyramids inside the same
    Translate call, so this one callback's 0-100 already covers the whole
    operation - confirmed directly (GDAL's own progress meter sweeps
    continuously through both phases in one call, not two). Returning a
    falsy value cancels the operation - GDAL's own built-in cancellation
    mechanism - and this module then deletes whatever partial output
    exists before returning action="cancelled", so a cancelled run never
    leaves a file on disk that looks like a finished result. Nothing here
    imports QGIS; a caller with a progress dialog plugs in here without
    this module changing.

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

    log_cb, if given, is called once as log_cb("translate", elapsed_seconds)
    when the single Translate call finishes, including the dataset
    close/flush that immediately follows it (detection is timed by the
    caller, not here - this module never calls detect()). It is then
    called once more as log_cb("verify", None), right before this
    function runs _verify() - real work, with no progress percentage of
    its own, that would otherwise sit behind whatever status text the
    caller last set (typically still "Translating..."), looking
    finished/hung rather than busy. elapsed_seconds is None on this
    second call specifically so a caller can tell "phase starting, no
    number yet" from "phase finished, here's how long it took" without a
    second parameter. "translate" fires before "verify" deliberately, in
    that order: a caller building a log from these two events (e.g. QGIS
    feedback.pushInfo()/setProgressText()) gets "Translate finished"
    before "Checking the output", matching what actually happened - the
    close/flush between Translate returning and this callback firing has
    no event of its own, which is fine, since its cost is already folded
    into elapsed_seconds above rather than needing a separate label. Used
    to be called a second time for a separate "overviews" phase; there is
    only one Translate phase now, plus this verify marker. Kept separate
    from the GDAL progress callback since that fires many times per
    phase; this fires once per event, and structured rather than
    pre-formatted so a caller can drive its own UI off the phase name
    without parsing a string.
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
    # passed. Only its length is used, as the COG driver's
    # OVERVIEW_COUNT creation option below - see
    # _default_overview_levels()'s docstring for why COG's own default
    # can't be trusted to match this tool's existing pyramid depth.
    overview_levels = _default_overview_levels(detection.raster_size[0], detection.raster_size[1])

    is_tiled = _is_tiled(detection.block_size, detection.raster_size)
    has_overviews = detection.overview_count > 0
    already_optimised = is_tiled and has_overviews
    # compression_at_target/is_cog are broken out separately from
    # already_optimised_at_target() below (rather than only calling that
    # function) purely so the elif branch can tell WHY a tiled,
    # overviewed file still needs reprocessing - already_optimised_at_target()
    # only ever returns one bit, correct for its own job (the full
    # skip-or-not decision, shared with algorithms/optimise_raster.py's
    # checkParameterValues() pre-flight, which is why the full decision
    # still goes through that one function rather than being
    # recombined here).
    compression_at_target = target_compression.upper() in (detection.compression or "").upper()
    is_cog = detection.layout == "COG"
    fully_optimised = already_optimised_at_target(detection, profile)

    if already_optimised and fully_optimised:
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
        # deliberate choice (e.g. wanting a different NoData mode applied,
        # since that's resolved independently of this already-optimised
        # check - see _resolve_nodata_handling() below), not a normal
        # tiling/overviews/compression rebuild - primary_reason wouldn't
        # mean anything here, so this is a warning instead.
        result.warnings.append(
            "Already tiled, with overviews, and at the target compression, "
            "but reprocessing anyway - Force reprocess is ticked."
        )
    elif already_optimised and not compression_at_target:
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
    elif already_optimised and not is_cog:
        # already_optimised and compression_at_target both true here
        # (the not-compression_at_target case above already claimed
        # every already_optimised file that doesn't qualify for that
        # reason), so this is specifically a file every earlier version
        # of this tool produced, since v1 is the first to always write
        # a genuine COG (confirmed
        # directly: every pre-COG output this tool has ever produced
        # fails COG validation despite passing the other three checks -
        # see docs/plugin_design_notes.md). Unlike the compression case
        # above, this doesn't promise a smaller file or faster pan/zoom
        # - both should stay about the same - so it gets its own message
        # rather than reusing the compression one, and isn't suppressed
        # for profile_consequential, since it makes no size/speed claim
        # that case would falsify.
        result.primary_reason = "cog_structure"
        # profile == "lossy" implies the source is already JPEG here,
        # not just correlates with it: compression_at_target (checked
        # above to even reach this branch) already confirmed the
        # source's compression contains the lossy profile's target
        # (JPEG) - so this is never reached by a lossy profile on a
        # non-JPEG source. Restructuring that source into a COG still
        # requires a full Translate rewrite, and GDAL has no way to copy
        # already-compressed JPEG tiles into a COG unchanged - confirmed
        # directly (checksum mismatch even with matching COMPRESS/
        # QUALITY/BLOCKSIZE; no passthrough option exists on the COG
        # driver; cogger does this but is a separate unbundled binary,
        # rejected for that cost, not for lacking the capability - see
        # docs/plugin_design_notes.md, "A lossy source cannot be
        # restructured into a COG without re-encoding"). So unlike the
        # lossless case below, the pixel values here change slightly too,
        # not just the byte layout - the message has to say so plainly
        # rather than repeat the lossless case's "not the pixel data
        # itself" claim, which is false for this combination.
        #
        # No size claim: measured directly at +0.039% on a source this
        # tool itself had written at QUALITY=90, but +21% on a source
        # built at a different JPEG quality then re-encoded at this
        # tool's fixed 90 - true only for this tool's own prior output,
        # not in general, since the source's original quality isn't
        # known up front. Pan/zoom speed is the one thing this branch can
        # actually guarantee regardless of source quality: the file was
        # already tiled with overviews before this run.
        if profile == "lossy":
            result.warnings.append(
                "Already tiled, with overviews, and already compressed "
                f"with {target_compression}, but this isn't a valid "
                "Cloud Optimized GeoTIFF yet - reprocessing to add that "
                "structure. Restructuring to COG means rewriting the "
                "file, and a lossy source can't be rewritten without "
                "decoding and re-encoding it - there's no way to copy "
                "already-compressed JPEG data into a COG unchanged. So "
                "the pixel values change slightly here too, on top of "
                "the byte layout. Pan/zoom speed isn't affected."
            )
        else:
            result.warnings.append(
                "Already tiled, with overviews, and already compressed "
                f"with {target_compression}, but this isn't a valid "
                "Cloud Optimized GeoTIFF yet - reprocessing to add that "
                "structure. Pan/zoom speed and file size should both "
                "stay about the same; the only change is how the file's "
                "bytes are arranged, not the pixel data itself."
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

    source_bytes = os.path.getsize(path)
    estimated_output_bytes = _estimate_output_ceiling_bytes(detection, profile, source_bytes)
    space_problem = _check_free_space(output_path, estimated_output_bytes)
    if space_problem:
        result.action = "blocked"
        result.output_path = output_path
        result.message = space_problem
        return result

    # -of COG first, so GEOKLEIN_7_REPRODUCE's command reads naturally
    # ("gdal_translate -of COG -co ..."). OVERVIEW_COUNT comes from
    # overview_levels' length, not from leaving COG to pick its own
    # default - see _default_overview_levels()'s docstring.
    co_args = ["-of", "COG"]
    for k, v in creation_options.items():
        co_args += ["-co", f"{k}={v}"]
    for k, v in overview_config.items():
        co_args += ["-co", f"{k}={v}"]
    co_args += ["-co", f"OVERVIEW_COUNT={len(overview_levels)}"]
    full_args = co_args + list(profile_opt.translate_extra_args or [])

    # Built from creation_options/overview_config themselves (the same
    # dicts turned into co_args above), not read back from the output
    # file - see AppliedSettings' docstring for why that separation
    # matters. Nothing here states an outcome that hasn't happened yet:
    # resampling method is a decision already made, not a claim about
    # whether Translate will actually succeed - whether overviews
    # actually exist is left to the file itself to show, not restated
    # here.
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

    # GEOKLEIN_* metadata is passed as -mo arguments INTO the Translate
    # call below, not written afterwards with SetMetadataItem() - see
    # _build_decision_metadata()'s docstring for why the COG driver
    # specifically requires that ordering. full_args itself (without
    # these -mo flags) is what GEOKLEIN_7_REPRODUCE's own text is built
    # from, inside _build_decision_metadata() - the printed reproduce
    # command doesn't re-include the metadata that describes it.
    metadata_items = _build_decision_metadata(
        path, detection, chosen_profile, profile, result.decisions, full_args,
    )
    translate_args = full_args + _metadata_mo_args(metadata_items)

    # ---- Step 3: Translate ----
    translate_tracker = _ProgressTracker(translate_progress_cb, translate_progress_cb_data)
    t0 = time.perf_counter()
    try:
        translate_opts = gdal.TranslateOptions(
            options=translate_args, callback=translate_tracker, callback_data=None,
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
        # Not removed: unlike a cancellation (the user's own choice), a
        # file left behind by a genuine Translate failure might be
        # incomplete, but deleting it outright on this module's own
        # judgement risks discarding something the user could still
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

    out_ds = None  # flush/close - metadata was already written via -mo above
    result.translate_ok = True
    result.output_path = output_path
    result.translate_seconds = time.perf_counter() - t0
    if log_cb:
        log_cb("translate", result.translate_seconds)

    # _verify() below is real work with no progress percentage of its
    # own - see this function's log_cb docstring. Fired after
    # "translate", not before, so the log reads "Translate finished"
    # then "Checking the output" in that order - a log reporting the
    # wrong order is worse than the close/flush above going unlabelled
    # between the two (it's already folded into translate_seconds
    # above, not a separate silent gap of its own).
    if log_cb:
        log_cb("verify", None)

    # ---- Verify (doc Step 4 "Verify") ----
    # No separate Build Overviews step to fail independently any more -
    # base image, compression and pyramids all came from the one
    # Translate call above, so a failure there was already caught by
    # the try/except around it.
    expected_block = (
        int(creation_options["BLOCKSIZE"]), int(creation_options["BLOCKSIZE"]),
    )
    verification = _verify(output_path, creation_options["COMPRESS"], expected_block)
    result.verification = verification

    result.source_bytes = source_bytes
    result.output_bytes = os.path.getsize(output_path)
    result.size_summary = _size_summary(result.source_bytes, result.output_bytes)
    if result.output_bytes > result.source_bytes:
        size_note = None
        if result.decisions.profile_consequential:
            # A JPEG-source profile_reason warning already explains this
            # increase - see PROFILE_REASON_JPEG_SOURCE_ANALYSIS/_VIEWING
            # in detector.py - and explains it correctly, whether or not
            # this run also happened to build pyramids for the first
            # time. Checked first, before pyramids_added_fresh, and
            # unconditionally suppresses either explanation below:
            # confirmed live that has_overviews being False was not
            # enough on its own to route around this - a JPEG source
            # with no existing pyramids, re-run through Analysis, still
            # got told "pyramids add back roughly a third" under a real
            # +837% increase, directly beneath the profile_reason warning
            # that had already correctly explained it two lines above.
            pass
        else:
            pyramids_added_fresh = not detection.has_overviews
            if pyramids_added_fresh:
                size_note = _SIZE_INCREASE_EXPLANATION
            else:
                # Source already had pyramids, so they aren't the cause -
                # see _SIZE_INCREASE_RESTRUCTURE_EXPLANATION's docstring.
                pct_growth = (
                    (result.output_bytes - result.source_bytes) / result.source_bytes * 100
                    if result.source_bytes else 0.0
                )
                if pct_growth >= _SIZE_INCREASE_RESTRUCTURE_THRESHOLD_PCT:
                    size_note = _SIZE_INCREASE_RESTRUCTURE_EXPLANATION
        if size_note and detection.profile_mode == "choice" and profile == "lossless":
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
        print(f"  Cloud Optimized GeoTIFF: {'OK' if v.cog_valid else 'FAILED'}")
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
        translate_progress_cb=progress_cb,
        log_cb=log_cb,
    )

    if args.json:
        print(json.dumps(_to_jsonable(result), indent=2))
    else:
        _print_report(result)

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
