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
settings BEFORE writing anything, and does nothing at all if the file
is already tiled with overviews, regardless of what compression it
currently uses. The speed problem (the plugin's core promise) is what
that check is protecting against re-doing unnecessarily; compression
choice is a separate, optional concern once tiling+pyramids are in place.
See docs/plugin_design_notes.md for the reasoning.

Run directly against a file:

    C:\\OSGeo4W\\bin\\python-qgis.bat core\\converter.py path\\to\\file.tif [--profile lossy|lossless]

Needs a GDAL Python environment (see core/detector.py's docstring).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from osgeo import gdal

try:
    from .detector import detect, DetectionResult, RECOMMENDED_SETTINGS
except ImportError:  # running as a plain script, not as part of the core package
    from detector import detect, DetectionResult, RECOMMENDED_SETTINGS

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
    clear_nodata_requested: bool = False  # echoes the request
    nodata_cleared: bool = False  # what actually happened
    nodata_message: Optional[str] = None  # human explanation of what happened and why
    warnings: list = field(default_factory=list)


def _settings_key(detection: "DetectionResult", profile: str) -> str:
    if profile == "lossy":
        return "lossy"
    return "lossless_float" if detection.content_type == "FLOAT32_CONTINUOUS" else "lossless_integer"


# clear_requested=False (unticked) is the default - asymmetric harm runs
# the same direction as the profile default: kept-when-it-should-have-
# cleared is visible speckle a user can see and rerun to fix; cleared-
# when-it-shouldn't-have-been puts a black collar back on a file that
# may already have shipped. There is deliberately no automatic mode:
# an earlier version of this had a third "auto" option that cleared
# whenever detect() reported it was confident (assessment ==
# "collar_only") - reverted, because even that confident case is still
# detect() making a collar-vs-shadow judgement call, which is exactly
# what the advisory design (see docs/plugin_design_notes.md) says this
# plugin can't reliably make. Detection can tell the user the choice is
# worth considering; it can't make the choice.
def _resolve_nodata_handling(detection: "DetectionResult", clear_requested: bool):
    """Decide whether to append -a_nodata none, and what to tell the
    caller about it. Returns (should_clear: bool, message: Optional[str]).

    Profile-independent by design - this used to be baked separately
    into each ProfileOption's translate_extra_args in detector.py (lossy
    cleared when structurally safe, lossless never did, regardless of
    the detected risk); that inconsistency is gone, replaced by this one
    decision point every profile goes through identically.
    """
    nodata_risk = detection.nodata_risk

    if nodata_risk is None or not nodata_risk.applies:
        # No NoData=0 condition on this file at all (elevation always
        # lands here, since detect_metadata_only never even creates a
        # NoDataRisk for FLOAT32_CONTINUOUS - and plenty of RGB files
        # have no NoData=0 either). The QGIS wrapper's checkParameterValues
        # refuses this combination outright before execution starts (so
        # nobody thinks they've cleared something they haven't) - this is
        # the defensive fallback for any caller that skips that check
        # (direct API use, the CLI). Only worth a log line if the caller
        # actually asked for clearing; the no-op default stays quiet.
        if clear_requested:
            return False, (
                "NoData handling: clearing has no effect on this file - "
                "no NoData=0 condition was detected."
            )
        return False, None

    if not nodata_risk.clear_possible:
        # nodata_only_transparency: clearing would reveal a border - this
        # is a structural block, not a judgement call, and it cannot be
        # overridden by the caller. This case is also already reflected
        # in whichever profile options are blocked for the same reason
        # where relevant.
        if clear_requested:
            return False, (
                "NoData handling: clearing requested but not possible on "
                "this file - " + (nodata_risk.message or (
                    "NoData is the only transparency here; clearing it "
                    "would reveal a border."
                ))
            )
        return False, None

    if clear_requested:
        return True, "NoData handling: cleared, as requested."

    return False, "NoData handling: kept, as requested."


def _default_overview_levels(xsize: int, ysize: int, min_dim: int = 256) -> list:
    """Replicate gdaladdo's "leave blank" default level series.

    That behaviour lives in the gdaladdo CLI utility, not the core GDAL
    API - BuildOverviews itself requires an explicit level list and
    raises on None. Halve repeatedly until the overview's larger
    dimension drops under min_dim, matching the doc's "down to thumbnail
    size" description.
    """
    levels = []
    factor = 2
    while max(xsize // factor, ysize // factor) > min_dim:
        levels.append(factor)
        factor *= 2
    return levels or [2]


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


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}TB"  # unreachable in practice, keeps the loop total


# Shown after a successful conversion whenever the output ends up larger
# than the source. Only reachable with the lossless profile - lossy
# compression is dramatically smaller than nearly any source, so this
# fires almost exclusively there. A source that arrives already
# compressed (DEFLATE is Metashape/Terra's typical default) can be close
# enough to ZSTD's size that seven overview levels - which add roughly a
# third back on top of the base image, regardless of profile - push the
# total past the original. That is a real, expected outcome, not a
# failure: this plugin trades file size for pan/zoom speed on the base
# image, and pyramids are an unavoidable part of buying that speed.
_SIZE_INCREASE_EXPLANATION = (
    "Output is larger than source - expected with Lossless if the "
    "source was already compressed, since pyramids add back roughly a "
    "third. Not a failure: the gain here is speed, not size (use Lossy "
    "instead if size matters more)."
)


def _size_summary(source_bytes: int, output_bytes: int) -> str:
    pct = ((output_bytes - source_bytes) / source_bytes * 100) if source_bytes else 0.0
    sign = "+" if pct >= 0 else ""
    return (
        f"Source: {_format_bytes(source_bytes)}  Output: {_format_bytes(output_bytes)}  "
        f"Change: {sign}{pct:.1f}%"
    )


def _is_tiled(block_size: tuple, raster_size: tuple) -> bool:
    # A stripped/untiled band's block spans the full image width (and
    # usually just a few rows tall). Tiled data has both dimensions
    # smaller than the image itself.
    return block_size[0] < raster_size[0] and block_size[1] < raster_size[1]


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
    clear_nodata: bool = False,
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

    clear_nodata (default False - keep) - see _resolve_nodata_handling
    for exactly what this does and doesn't do. Profile-independent:
    whether NoData=0 gets cleared no longer depends on which profile is
    picked, only on this. Never clears without being asked to - there is
    no automatic mode, because even detect()'s most confident read is
    still a collar-vs-shadow guess this plugin's design says it can't
    reliably make (see docs/plugin_design_notes.md).

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

    # ---- compare current state to target, decide whether to do anything ----
    is_tiled = _is_tiled(detection.block_size, detection.raster_size)
    has_overviews = detection.overview_count > 0
    already_optimised = is_tiled and has_overviews

    if already_optimised and not force_reprocess:
        result.ok = True
        result.action = "already_optimised"
        result.message = (
            "Already tiled with overviews - the speed problem this plugin "
            "exists to fix is already solved here. Not touching it."
        )
        return result

    if already_optimised:
        # already_optimised and force_reprocess both true: a deliberate
        # override (e.g. switching profile after the fact), not a normal
        # tiling/overviews rebuild - primary_reason wouldn't mean anything
        # here (both are already true), so this is recorded as a warning
        # instead of a reason.
        result.warnings.append(
            "Already tiled with overviews, but reprocessing anyway - "
            "Force reprocess is ticked."
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

    settings_key = _settings_key(detection, profile)
    settings = RECOMMENDED_SETTINGS[settings_key]
    creation_options = settings["creation_options"]
    overview_config = settings["overview_config"]

    co_args = []
    for k, v in creation_options.items():
        co_args += ["-co", f"{k}={v}"]
    full_args = co_args + list(profile_opt.translate_extra_args or [])

    # ---- resolve NoData handling (profile-independent - see
    # _resolve_nodata_handling and convert()'s own docstring) ----
    should_clear_nodata, nodata_message = _resolve_nodata_handling(detection, clear_nodata)
    result.clear_nodata_requested = clear_nodata
    result.nodata_cleared = should_clear_nodata
    result.nodata_message = nodata_message
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
        result.message = f"Translate failed: {exc}"
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
            levels = _default_overview_levels(out_ds.RasterXSize, out_ds.RasterYSize)
            out_ds.BuildOverviews(
                overview_config.get("RESAMPLING", "AVERAGE"),
                overviewlist=levels,
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
            # honestly rather than silently calling this success.
            result.action = "converted_incomplete"
            result.message = (
                f"Translate succeeded but building overviews failed: {exc}. "
                f"The output at {output_path} exists but is NOT optimised yet "
                "- retry overviews on it directly, no need to re-convert."
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
        result.size_note = _SIZE_INCREASE_EXPLANATION

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
    parser.add_argument("--clear-nodata", action="store_true",
                         help="Clear NoData=0 on RGB imagery where safe (default: keep)")
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
        output_path=args.output, force=args.force, clear_nodata=args.clear_nodata,
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
