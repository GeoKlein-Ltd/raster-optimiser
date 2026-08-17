"""Raster Optimiser detection module.

Pure GDAL/Python, no QGIS imports. Classifies a raster against the v1
content-type scope, resolves the Profile A/B decision, and flags the
Profile-A NoData risk conditions from
docs/GeoKlein_raster_optimisation_workflow.md, without writing anything.

Run directly against a file:

    python core/detector.py path/to/file.tif
    python core/detector.py path/to/file.tif --json

Needs a GDAL Python environment. On this machine that means running it
through the QGIS-bundled interpreter, e.g.:

    C:\\OSGeo4W\\bin\\python-qgis.bat core\\detector.py path\\to\\file.tif
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

gdal.UseExceptions()

# ---------------------------------------------------------------------------
# Tunable heuristics. Nothing in the spec doc pins these numbers down -
# calibrate against real files in testdata/ once some exist.
# ---------------------------------------------------------------------------

# Long-side pixel count for the decimated sample used for both the
# classified-raster unique-value count and the NoData=0 black-pixel count.
# When overviews exist GDAL serves this cheaply from them. When they
# don't (the common case pre-optimisation), GDAL still has to touch
# nearly every source tile to build a decimated read regardless of the
# requested output size - confirmed empirically on a 1.74GB, no-overview
# ortho: 1000px and 4000px targets both cost ~47s. So there's no reason
# to keep this small; it doesn't buy speed, only precision loss.
#
# Known characteristic, not a v1 blocker: on a large source with no
# overviews, this read alone takes on the order of a minute (measured
# ~47s on a 1.74GB, 1.5-gigapixel ortho). A future version could check
# for existing overviews first and adjust strategy; v1 just accepts the
# wait, since it's a one-off per detection run.
SAMPLE_TARGET_DIM = 2000

# Single-band integer raster with no colour table: at or under this many
# unique values in the sample reads as classified/categorical.
CLASSIFIED_MAX_UNIQUE_VALUES = 100

# Spatial bins per axis when checking for localized NoData=0 clusters.
# Applied to the same decimated sample already read - no extra I/O.
NODATA_GRID_SIZE = 20

# Outermost ring(s) of grid cells to exclude from the "is this a real
# interior cluster" decision. A clean survey-boundary collar is EXPECTED
# to light up edge cells - that's not a problem, it's exactly what NoData
# is supposed to catch. Only an interior cell lighting up means NoData is
# eating real content. 1 ring is enough at NODATA_GRID_SIZE=20 (each cell
# is a ~20th of the image on a side) to separate collar from interior on
# every real file tested so far.
NODATA_EDGE_MARGIN_CELLS = 1

# Minimum alpha/mask-valid pixels a grid cell must contain before its
# fraction is trusted. Without this, a near-empty cell (e.g. a sliver of
# valid pixels near the alpha edge, or any cell on a small/low-res file)
# can swing to 50-100% black on one or two stray pixels and false-positive.
NODATA_MIN_CELL_VALID_PIXELS = 25

# Fraction of valid pixels within a single INTERIOR grid cell that are
# pure black, at or above which NoData=0 is judged to be hiding real
# content rather than just the collar. Calibration data point: on
# Ortho_school_v1_nick.tif, a shadow-hole cluster confined to one
# structure's footprint diluted to ~0.01% as a whole-image average but
# hit 0.37% (denser sample: 1.16%) in its worst interior cell - both
# comfortably above this threshold, which was deliberately kept separate
# from (and much stricter than) a whole-image average would ever need to
# be. See docs/plugin_design_notes.md.
NODATA_INTERIOR_CELL_RISK_FRACTION = 0.001

INTEGER_DTYPES = {
    "Byte", "Int8", "UInt16", "Int16", "UInt32", "Int32", "UInt64", "Int64",
}

RECOMMENDED_SETTINGS = {
    "A": {
        "creation_options": {
            "TILED": "YES", "BLOCKXSIZE": "512", "BLOCKYSIZE": "512",
            "COMPRESS": "JPEG", "JPEG_QUALITY": "90", "PHOTOMETRIC": "YCBCR",
            "BIGTIFF": "YES", "NUM_THREADS": "ALL_CPUS",
        },
        "overview_config": {
            "RESAMPLING": "AVERAGE", "COMPRESS_OVERVIEW": "JPEG",
            "PHOTOMETRIC_OVERVIEW": "YCBCR", "INTERLEAVE_OVERVIEW": "PIXEL",
        },
    },
    "B_integer": {
        "creation_options": {
            "TILED": "YES", "BLOCKXSIZE": "512", "BLOCKYSIZE": "512",
            "COMPRESS": "ZSTD", "ZSTD_LEVEL": "9", "PREDICTOR": "2",
            "BIGTIFF": "YES", "NUM_THREADS": "ALL_CPUS",
        },
        "overview_config": {"RESAMPLING": "AVERAGE", "COMPRESS_OVERVIEW": "ZSTD"},
    },
    "B_float": {
        "creation_options": {
            "TILED": "YES", "BLOCKXSIZE": "512", "BLOCKYSIZE": "512",
            "COMPRESS": "ZSTD", "ZSTD_LEVEL": "9", "PREDICTOR": "3",
            "BIGTIFF": "YES", "NUM_THREADS": "ALL_CPUS",
        },
        "overview_config": {"RESAMPLING": "AVERAGE", "COMPRESS_OVERVIEW": "ZSTD"},
    },
}


# ---------------------------------------------------------------------------
# Result structures
# ---------------------------------------------------------------------------

@dataclass
class ProfileOption:
    profile: str  # "A" or "B"
    available: bool
    reason_blocked: Optional[str] = None
    recommended_settings: Optional[dict] = None
    translate_extra_args: Optional[list] = None


@dataclass
class NoDataRisk:
    applies: bool = False
    nodata_value: Optional[float] = None
    sample_pixels_checked: int = 0
    black_pixel_count: int = 0
    black_pixel_fraction: float = 0.0  # whole-sample average - kept for visibility, not used to decide
    interior_max_cell_fraction: float = 0.0  # worst INTERIOR grid-cell fraction - this decides the assessment
    interior_flagged_cells: int = 0
    interior_cells_checked: int = 0
    edge_max_cell_fraction: float = 0.0  # diagnostic only, never decides anything
    grid_cells: int = 0
    assessment: str = "not_applicable"  # not_applicable | collar_only | meaningful | insufficient_sample
    needs_user_decision: bool = False
    message: Optional[str] = None


@dataclass
class DetectionResult:
    path: str
    ok: bool = False
    refused: bool = False
    refusal_code: Optional[str] = None
    refusal_reason: Optional[str] = None

    content_type: Optional[str] = None  # RGB_8BIT | RGB_16BIT | FLOAT32_CONTINUOUS
    band_count: Optional[int] = None
    dtype: Optional[str] = None
    has_alpha: bool = False
    alpha_band_index: Optional[int] = None
    transparency_source: Optional[str] = None  # alpha | mask | nodata_only | none

    profile_mode: Optional[str] = None  # "forced" | "choice"
    forced_profile: Optional[str] = None
    profile_options: list = field(default_factory=list)

    nodata_risk: Optional[NoDataRisk] = None

    block_size: Optional[tuple] = None
    has_overviews: bool = False
    overview_count: int = 0
    has_aux_xml: Optional[bool] = None
    has_crs: Optional[bool] = None
    raster_size: Optional[tuple] = None

    warnings: list = field(default_factory=list)


def _refuse(result: DetectionResult, code: str, message: str) -> DetectionResult:
    result.ok = True
    result.refused = True
    result.refusal_code = code
    result.refusal_reason = message
    return result


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------

def _sample_dims(xsize: int, ysize: int, target: int = SAMPLE_TARGET_DIM):
    long_side = max(xsize, ysize)
    if long_side <= target:
        return xsize, ysize
    scale = target / long_side
    return max(1, int(xsize * scale)), max(1, int(ysize * scale))


def _read_sample(band: "gdal.Band", w: int, h: int):
    return band.ReadAsArray(buf_xsize=w, buf_ysize=h)


def _classified_unique_count(ds: "gdal.Dataset", band: "gdal.Band") -> int:
    import numpy as np

    w, h = _sample_dims(ds.RasterXSize, ds.RasterYSize)
    arr = _read_sample(band, w, h)
    return int(np.unique(arr).size)


def _black_pixel_sample(ds: "gdal.Dataset", rgb_band_indices, alpha_index,
                         grid_size: int = NODATA_GRID_SIZE,
                         edge_margin: int = NODATA_EDGE_MARGIN_CELLS,
                         min_cell_valid: int = NODATA_MIN_CELL_VALID_PIXELS):
    """Decimated black-pixel-under-NoData check, binned spatially.

    A single whole-image fraction dilutes a cluster confined to one part
    of a large raster below any sane threshold (a shadow under one
    structure can be ~100% black within its own footprint but ~0.01% of
    a multi-gigapixel ortho). Bin the same decimated sample into a grid
    instead - no extra I/O, since the sample array is already in memory.

    The outermost ring of cells is tracked separately (edge_*) and never
    drives the decision: a clean survey-boundary collar is SUPPOSED to
    show up there. Only an INTERIOR cell exceeding threshold means NoData
    is eating real content, not just the boundary it was meant to mark.
    Cells with too few valid pixels to trust are skipped entirely rather
    than allowed to swing the result on one or two stray pixels.
    See docs/plugin_design_notes.md.
    """
    import numpy as np

    w, h = _sample_dims(ds.RasterXSize, ds.RasterYSize)
    stacked = np.stack(
        [_read_sample(ds.GetRasterBand(i), w, h) for i in rgb_band_indices], axis=0
    )
    black_mask = np.all(stacked == 0, axis=0)

    if alpha_index is not None:
        alpha_arr = _read_sample(ds.GetRasterBand(alpha_index), w, h)
        valid_mask = alpha_arr > 0  # mask semantics: any non-zero = valid
    else:
        valid_mask = np.ones_like(black_mask, dtype=bool)
    black_mask = black_mask & valid_mask

    total_valid = int(valid_mask.sum())
    black_count = int(black_mask.sum())
    global_fraction = (black_count / total_valid) if total_valid else 0.0

    grid = max(1, min(grid_size, w, h))
    margin = min(edge_margin, (grid - 1) // 2)  # never eat the whole grid on tiny images

    interior_max_fraction = 0.0
    interior_flagged = 0
    interior_checked = 0
    edge_max_fraction = 0.0

    for gy in range(grid):
        y0, y1 = gy * h // grid, (gy + 1) * h // grid
        is_edge_row = gy < margin or gy >= grid - margin
        for gx in range(grid):
            x0, x1 = gx * w // grid, (gx + 1) * w // grid
            cell_valid_n = int(valid_mask[y0:y1, x0:x1].sum())
            if cell_valid_n < min_cell_valid:
                continue
            cell_black_n = int(black_mask[y0:y1, x0:x1].sum())
            cell_fraction = cell_black_n / cell_valid_n

            is_edge = is_edge_row or gx < margin or gx >= grid - margin
            if is_edge:
                edge_max_fraction = max(edge_max_fraction, cell_fraction)
            else:
                interior_checked += 1
                interior_max_fraction = max(interior_max_fraction, cell_fraction)
                if cell_fraction >= NODATA_INTERIOR_CELL_RISK_FRACTION:
                    interior_flagged += 1

    return {
        "total_valid": total_valid,
        "black_count": black_count,
        "global_fraction": global_fraction,
        "interior_max_fraction": interior_max_fraction,
        "interior_flagged": interior_flagged,
        "interior_checked": interior_checked,
        "edge_max_fraction": edge_max_fraction,
        "grid_cells": grid * grid,
    }


# ---------------------------------------------------------------------------
# Transparency source (Stage 2A sub-detect)
# ---------------------------------------------------------------------------

def _transparency_source(band: "gdal.Band", nodata_value, alpha_present: bool) -> str:
    # GDAL's own GetMaskFlags() gives NoData priority over an alpha band
    # when both are present on a band - not what we want here, since an
    # alpha band already answered "is there positional transparency" in
    # Stage 1. Check that first and only fall through to the mask-flag
    # heuristic for a genuine separate mask (.msk sidecar / internal mask).
    if alpha_present:
        return "alpha"
    flags = band.GetMaskFlags()
    if (flags & gdal.GMF_PER_DATASET) and not (flags & gdal.GMF_ALPHA):
        return "mask"
    if nodata_value is not None:
        return "nodata_only"
    return "none"


def _has_georeferencing(ds: "gdal.Dataset") -> bool:
    srs = ds.GetSpatialRef()
    gt = ds.GetGeoTransform()
    identity_gt = gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    return not (srs is None and identity_gt)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def detect(path: str) -> DetectionResult:
    result = DetectionResult(path=path)

    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
    except Exception as exc:  # noqa: BLE001 - surfacing any GDAL open failure
        return _refuse(result, "UNREADABLE", f"Could not read this file: {exc}")

    if ds is None:
        return _refuse(result, "UNREADABLE", "Could not read this file.")

    result.raster_size = (ds.RasterXSize, ds.RasterYSize)
    result.has_crs = _has_georeferencing(ds)
    result.has_aux_xml = os.path.exists(path + ".aux.xml")

    if not result.has_crs:
        return _refuse(
            result,
            "NO_CRS",
            "No coordinate reference system found. This looks like a plain "
            "image, not a georeferenced raster.",
        )

    band_count = ds.RasterCount
    result.band_count = band_count
    band1 = ds.GetRasterBand(1)
    dtype = gdal.GetDataTypeName(band1.DataType)
    result.dtype = dtype
    result.block_size = tuple(band1.GetBlockSize())
    result.overview_count = band1.GetOverviewCount()
    result.has_overviews = result.overview_count > 0

    for i in range(2, band_count + 1):
        other_dtype = gdal.GetDataTypeName(ds.GetRasterBand(i).DataType)
        if other_dtype != dtype:
            result.warnings.append(
                f"Band {i} has data type {other_dtype}, differs from band 1 "
                f"({dtype}). Detection uses band 1's type."
            )

    # ---- Stage 1: content-type classification ----

    if band_count == 1:
        if dtype == "Float32":
            result.content_type = "FLOAT32_CONTINUOUS"
        elif dtype in INTEGER_DTYPES:
            has_color_table = band1.GetColorTable() is not None
            unique_count = None if has_color_table else _classified_unique_count(ds, band1)
            is_classified = has_color_table or (unique_count is not None and unique_count <= CLASSIFIED_MAX_UNIQUE_VALUES)
            if is_classified:
                detail = (
                    "a colour table is present"
                    if has_color_table
                    else f"only {unique_count} unique values in a {SAMPLE_TARGET_DIM}px sample"
                )
                return _refuse(
                    result,
                    "CLASSIFIED",
                    "This looks like a classified/categorical raster "
                    f"({detail}). Building pyramids with average resampling "
                    "blends category codes into meaningless fractional "
                    "values - silent corruption. Nearest-neighbour handling "
                    "for classified rasters isn't in v1 yet.",
                )
            return _refuse(
                result,
                "SINGLE_BAND_UNRECOGNIZED",
                f"Single-band {dtype} data that isn't Float32 elevation and "
                "doesn't look classified isn't a recognised v1 case yet.",
            )
        else:
            return _refuse(
                result,
                "UNSUPPORTED_DTYPE",
                f"Single-band data type {dtype} isn't supported in v1. "
                "Supported: Float32 elevation, or classified integer "
                "rasters (which are refused with a different message).",
            )

    elif band_count in (3, 4):
        if dtype not in ("Byte", "UInt16"):
            return _refuse(
                result,
                "UNSUPPORTED_DTYPE",
                f"{band_count}-band data of type {dtype} isn't supported in "
                "v1. Supported: 8-bit or 16-bit RGB (3-4 band).",
            )

        alpha_index = None
        if band_count == 4:
            band4 = ds.GetRasterBand(4)
            if gdal.GetColorInterpretationName(band4.GetColorInterpretation()) == "Alpha":
                alpha_index = 4
            else:
                return _refuse(
                    result,
                    "MULTISPECTRAL",
                    f"This raster has {band_count} bands and band 4 isn't "
                    "flagged as alpha, so it reads as multispectral rather "
                    "than RGB imagery. Multispectral isn't in v1. "
                    "Supported: 8-bit/16-bit RGB (3-4 band), single-band "
                    "Float32 elevation.",
                )

        result.has_alpha = alpha_index is not None
        result.alpha_band_index = alpha_index
        result.content_type = "RGB_8BIT" if dtype == "Byte" else "RGB_16BIT"

    elif band_count > 4:
        return _refuse(
            result,
            "MULTISPECTRAL",
            f"This raster has {band_count} bands - reads as multispectral "
            "rather than RGB imagery. Multispectral isn't in v1. Supported: "
            "8-bit/16-bit RGB (3-4 band), single-band Float32 elevation.",
        )

    else:
        return _refuse(result, "NO_BANDS", "This raster has no readable bands.")

    # ---- Stage 2: profile resolution ----

    if result.content_type == "FLOAT32_CONTINUOUS":
        result.profile_mode = "forced"
        result.forced_profile = "B"
        settings = RECOMMENDED_SETTINGS["B_float"]
        result.profile_options = [
            ProfileOption(
                profile="B",
                available=True,
                recommended_settings=settings,
                translate_extra_args=[],
            )
        ]
        result.ok = True
        return result

    # RGB_8BIT or RGB_16BIT: profile is a live choice, subject to Stage 2A checks.
    result.profile_mode = "choice"
    nodata_value = band1.GetNoDataValue()
    transparency_source = _transparency_source(band1, nodata_value, result.has_alpha)
    result.transparency_source = transparency_source

    rgb_indices = [1, 2, 3]
    nodata_risk = NoDataRisk()
    profile_a_blocked_reason = None

    if result.content_type == "RGB_16BIT":
        profile_a_blocked_reason = (
            "JPEG needs 8-bit data. Converting your 16-bit values down "
            "means guessing a scale, which risks wrecking contrast - not "
            "something this plugin will do silently. Choose Profile B "
            "(lossless), or convert to 8-bit yourself first if you're sure "
            "of the intended range."
        )
    elif nodata_value == 0:
        nodata_risk.applies = True
        nodata_risk.nodata_value = nodata_value
        if transparency_source == "nodata_only":
            profile_a_blocked_reason = (
                "This file uses NoData as its only transparency and has no "
                "alpha band. Clearing it would leave a black border. "
                "Building a proper footprint mask isn't in v1 - see "
                "'If nodata is the only transparency' in the workflow doc "
                "for the manual route."
            )
            nodata_risk.assessment = "nodata_only_transparency"
            nodata_risk.message = profile_a_blocked_reason
        else:
            stats = _black_pixel_sample(
                ds, rgb_indices, alpha_index=result.alpha_band_index
            )
            nodata_risk.sample_pixels_checked = stats["total_valid"]
            nodata_risk.black_pixel_count = stats["black_count"]
            nodata_risk.black_pixel_fraction = stats["global_fraction"]
            nodata_risk.interior_max_cell_fraction = stats["interior_max_fraction"]
            nodata_risk.interior_flagged_cells = stats["interior_flagged"]
            nodata_risk.interior_cells_checked = stats["interior_checked"]
            nodata_risk.edge_max_cell_fraction = stats["edge_max_fraction"]
            nodata_risk.grid_cells = stats["grid_cells"]
            if stats["interior_checked"] == 0:
                nodata_risk.assessment = "insufficient_sample"
                nodata_risk.needs_user_decision = True
                nodata_risk.message = (
                    "Too little interior area was sampled to reliably tell "
                    "collar from real content on this file - check manually "
                    "before clearing NoData."
                )
            elif stats["interior_max_fraction"] >= NODATA_INTERIOR_CELL_RISK_FRACTION:
                nodata_risk.assessment = "meaningful"
                nodata_risk.needs_user_decision = True
                pct = stats["interior_max_fraction"] * 100
                nodata_risk.message = (
                    f"Up to {pct:.2f}% of pixels are pure black within a "
                    "localized interior area (away from the image edge) "
                    "and being hidden by NoData=0 - this looks like real "
                    "content (deep shadow, water, dark surfaces), not just "
                    "the collar. Clear NoData so it shows?"
                )
            else:
                nodata_risk.assessment = "collar_only"
                nodata_risk.message = (
                    "NoData=0 only catches the collar outside the survey "
                    "area. Safe to clear, no localized black content "
                    "pixels detected away from the edge."
                )

    result.nodata_risk = nodata_risk

    settings_key = "B_integer"
    translate_extra_a = []
    if result.has_alpha:
        translate_extra_a = ["-b", "1", "-b", "2", "-b", "3", "-mask", str(result.alpha_band_index)]
        if nodata_value == 0 and transparency_source != "nodata_only":
            translate_extra_a += ["-a_nodata", "none"]
    elif nodata_value == 0 and transparency_source != "nodata_only":
        translate_extra_a = ["-a_nodata", "none"]

    profile_a = ProfileOption(
        profile="A",
        available=profile_a_blocked_reason is None,
        reason_blocked=profile_a_blocked_reason,
        recommended_settings=RECOMMENDED_SETTINGS["A"] if profile_a_blocked_reason is None else None,
        translate_extra_args=translate_extra_a if profile_a_blocked_reason is None else None,
    )
    profile_b = ProfileOption(
        profile="B",
        available=True,
        recommended_settings=RECOMMENDED_SETTINGS[settings_key],
        translate_extra_args=[],
    )
    result.profile_options = [profile_a, profile_b]
    result.ok = True
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


def _print_report(result: DetectionResult) -> None:
    print(f"File: {result.path}")
    if not result.ok:
        print("  Could not run detection.")
        return
    if result.raster_size:
        print(f"  Size: {result.raster_size[0]} x {result.raster_size[1]} px")
    print(f"  CRS present: {result.has_crs}")
    print(f"  .aux.xml present: {result.has_aux_xml}")

    if result.refused:
        print(f"\nREFUSED [{result.refusal_code}]")
        print(f"  {result.refusal_reason}")
        for w in result.warnings:
            print(f"  Warning: {w}")
        return

    print(f"  Bands: {result.band_count}  Type: {result.dtype}")
    print(f"  Block size: {result.block_size[0]}x{result.block_size[1]}")
    print(f"  Overviews: {result.overview_count}")
    print(f"  Content type: {result.content_type}")
    if result.content_type in ("RGB_8BIT", "RGB_16BIT"):
        print(f"  Alpha band: {result.alpha_band_index if result.has_alpha else 'none'}")
        print(f"  Transparency source: {result.transparency_source}")

    print(f"\nProfile mode: {result.profile_mode}"
          + (f" ({result.forced_profile})" if result.forced_profile else ""))
    for opt in result.profile_options:
        status = "available" if opt.available else "BLOCKED"
        print(f"  Profile {opt.profile}: {status}")
        if opt.reason_blocked:
            print(f"    {opt.reason_blocked}")
        if opt.translate_extra_args:
            print(f"    extra args: {' '.join(opt.translate_extra_args)}")

    if result.nodata_risk and result.nodata_risk.applies:
        nr = result.nodata_risk
        print(f"\nNoData risk (Profile A only): {nr.assessment}")
        if nr.message:
            print(f"  {nr.message}")
        if nr.sample_pixels_checked:
            print(f"  sampled {nr.sample_pixels_checked} valid px, "
                  f"{nr.black_pixel_count} pure black "
                  f"(whole-image avg {nr.black_pixel_fraction * 100:.4f}%)")
        if nr.grid_cells:
            print(f"  worst interior cell: {nr.interior_max_cell_fraction * 100:.3f}% "
                  f"black ({nr.interior_flagged_cells}/{nr.interior_cells_checked} "
                  f"interior cells over threshold, {nr.grid_cells} cells total)")
            print(f"  worst edge cell (diagnostic only, not decisive): "
                  f"{nr.edge_max_cell_fraction * 100:.3f}% black")
        if nr.needs_user_decision:
            print("  -> needs a decision: [Clear / Keep / not sure]")

    for w in result.warnings:
        print(f"\nWarning: {w}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Raster Optimiser detection report")
    parser.add_argument("path", help="Path to a raster file")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a human report")
    args = parser.parse_args(argv)

    result = detect(args.path)

    if args.json:
        print(json.dumps(_to_jsonable(result), indent=2))
    else:
        _print_report(result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
