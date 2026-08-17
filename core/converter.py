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

    C:\\OSGeo4W\\bin\\python-qgis.bat core\\converter.py path\\to\\file.tif [--profile A|B]

Needs a GDAL Python environment (see core/detector.py's docstring).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

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
    # | blocked | refused_upstream | error
    action: str = "unknown"
    message: str = ""
    output_path: Optional[str] = None
    profile_used: Optional[str] = None
    primary_reason: Optional[str] = None  # "tiling" | "overviews" | None
    translate_ok: Optional[bool] = None
    overviews_ok: Optional[bool] = None
    verification: Optional[VerificationResult] = None
    warnings: list = field(default_factory=list)


def _settings_key(detection: "DetectionResult", profile: str) -> str:
    if profile == "A":
        return "A"
    return "B_float" if detection.content_type == "FLOAT32_CONTINUOUS" else "B_integer"


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
    progress_cb=None,
    progress_cb_data=None,
) -> ConversionResult:
    """Convert one file per the resolved detection/profile, or report why not.

    progress_cb, if given, is passed straight through to gdal.Translate and
    BuildOverviews - it's GDAL's own callback(complete, message, cb_data)
    convention. Returning False from it cancels the running operation, which
    is GDAL's built-in cancellation mechanism. Nothing here wires it to
    anything (no QGIS dependency); a caller with a progress dialog plugs in
    here later without this module changing.
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
        if chosen_profile not in ("A", "B"):
            result.action = "error"
            result.message = (
                "This file needs an explicit profile choice (A or B) - "
                "detection did not force one."
            )
            return result
        profile = chosen_profile

    profile_opt = next((o for o in detection.profile_options if o.profile == profile), None)
    if profile_opt is None or not profile_opt.available:
        reason = profile_opt.reason_blocked if profile_opt else "not offered for this file"
        result.action = "blocked"
        result.message = f"Profile {profile} is not available: {reason}"
        return result

    result.profile_used = profile

    # ---- compare current state to target, decide whether to do anything ----
    is_tiled = _is_tiled(detection.block_size, detection.raster_size)
    has_overviews = detection.overview_count > 0

    if is_tiled and has_overviews:
        result.ok = True
        result.action = "already_optimised"
        result.message = (
            "Already tiled with overviews - the speed problem this plugin "
            "exists to fix is already solved here. Not touching it."
        )
        return result

    result.primary_reason = "tiling" if not is_tiled else "overviews"

    # ---- resolve output path, with hard guards ----
    if output_path is None:
        stem, _ext = os.path.splitext(path)
        output_path = f"{stem}_optimised.tif"

    # Never write over the source. Not a check-and-warn: this cannot be
    # bypassed by force=True or any other flag.
    if os.path.abspath(output_path) == os.path.abspath(path):
        result.action = "error"
        result.message = (
            "Refusing to write the output over the source file. This guard "
            "cannot be bypassed."
        )
        return result

    if os.path.exists(output_path) and not force:
        result.action = "blocked"
        result.output_path = output_path
        result.message = (
            f"{output_path} already exists. Not overwriting silently - "
            "delete it, choose a different output path, or pass force=True."
        )
        return result

    settings_key = _settings_key(detection, profile)
    settings = RECOMMENDED_SETTINGS[settings_key]
    creation_options = settings["creation_options"]
    overview_config = settings["overview_config"]

    co_args = []
    for k, v in creation_options.items():
        co_args += ["-co", f"{k}={v}"]
    full_args = co_args + list(profile_opt.translate_extra_args or [])

    # ---- Step 3: Translate ----
    try:
        translate_opts = gdal.TranslateOptions(
            options=full_args, callback=progress_cb, callback_data=progress_cb_data,
        )
        out_ds = gdal.Translate(output_path, path, options=translate_opts)
    except Exception as exc:  # noqa: BLE001 - surface any GDAL failure to the caller
        result.action = "error"
        result.translate_ok = False
        result.message = f"Translate failed: {exc}"
        return result

    if out_ds is None:
        result.action = "error"
        result.translate_ok = False
        result.message = "Translate failed (no output produced)."
        return result

    out_ds = None  # flush/close before reopening for BuildOverviews
    result.translate_ok = True
    result.output_path = output_path

    # ---- Step 4: Build Overviews ----
    prior_config = {k: gdal.GetConfigOption(k) for k in overview_config if k != "RESAMPLING"}
    for k, v in overview_config.items():
        if k != "RESAMPLING":
            gdal.SetConfigOption(k, v)
    try:
        try:
            out_ds = gdal.Open(output_path, gdal.GA_Update)
            levels = _default_overview_levels(out_ds.RasterXSize, out_ds.RasterYSize)
            out_ds.BuildOverviews(
                overview_config.get("RESAMPLING", "AVERAGE"),
                overviewlist=levels,
                callback=progress_cb, callback_data=progress_cb_data,
            )
            out_ds = None
            result.overviews_ok = True
        except Exception as exc:  # noqa: BLE001
            result.overviews_ok = False
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
    parser.add_argument("--profile", choices=["A", "B"], default=None,
                         help="Required when detection offers a choice")
    parser.add_argument("--output", default=None, help="Output path (default: <stem>_optimised.tif)")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing output file")
    parser.add_argument("--quiet", action="store_true", help="No terminal progress bar")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a human report")
    args = parser.parse_args(argv)

    detection = detect(args.path)
    progress_cb = None if args.quiet else gdal.TermProgress_nocb
    result = convert(
        args.path, detection=detection, chosen_profile=args.profile,
        output_path=args.output, force=args.force, progress_cb=progress_cb,
    )

    if args.json:
        print(json.dumps(_to_jsonable(result), indent=2))
    else:
        _print_report(result)

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
