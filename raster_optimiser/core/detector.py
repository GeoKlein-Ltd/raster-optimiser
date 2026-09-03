"""Raster Optimiser detection module.

Pure GDAL/Python, no QGIS imports. Classifies a raster against the v1
content-type scope, resolves the lossy/lossless profile decision, and
flags the lossy-profile NoData risk conditions from
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

# Long-side pixel count for the decimated sample used for the classified-
# raster unique-value count. "Decimated" here really does mean cheap: a
# single-band decimated ReadAsArray at this size costs a couple of
# seconds even on a 1.5-gigapixel source. Deliberately much smaller than
# the old shared 2000px target - a truly classified raster's handful of
# category codes will turn up in a far smaller sample (a value spread
# across a large area survives heavy downsampling), and the failure mode
# of decimating too hard here is a continuous raster occasionally reading
# as classified because too few distinct values got sampled, which is
# self-correcting: 500px still yields hundreds of thousands of sampled
# pixels, far more than needed to separate "~8 codes" from "thousands of
# elevation-like values" reliably.
CLASSIFIED_SAMPLE_TARGET_DIM = 500

# At or under this many unique values in the sample reads as
# classified/categorical - originally single-band only, now checked per
# band across the continuous bucket too (see CONTINUOUS content type
# below), whichever band count the file has.
#
# Known gap, deliberately not fixed: a genuinely categorical raster with
# MORE than this many codes and no embedded colour table reads as
# continuous instead, since a higher unique-value count means "looks
# less classified" under this heuristic - the wrong direction for that
# case. Real products with this shape exist (e.g. USDA Cropland Data
# Layer, 100+ crop-type codes, not always shipped with a GDAL-readable
# colour table). This isn't a new risk from supporting the continuous
# bucket - it already existed for single-band data - but that bucket
# does widen exposure to it, since band count and dtype used to keep
# some file shapes (5+ bands, non-Byte 3-4 band) away from this check
# entirely, and no longer do. Candidate fix if this ever bites in
# practice: check value DENSITY, not just count - real continuous
# measurements sample as a dense spread across their range, while
# classified codes sample as sparse, isolated integers (e.g.
# 1,2,3,4,5,11,12,21,22...) regardless of how many of them there are.
# Not built speculatively - recorded here instead.
#
# Known gap in the OTHER direction, also deliberately not fixed: a 1x1
# raster, or one that's genuinely continuous but locally uniform (a tiny
# crop, a flat single-value DEM tile), can sample down to a single
# unique value - always <= this threshold, so it always reads as
# CLASSIFIED regardless of what the file actually is. No minimum
# sample-size guard is worth adding for this on its own: the threshold
# such a guard would need has no real failing case to calibrate it
# against (nobody runs this tool on a 1x1 file), and a refused file with
# a clear reason is a far cheaper mistake than a silently wrong guess
# would be. If a real case does turn up, it fails the same way the
# too-many-codes gap above does - loudly, with a reason, not silently -
# so it stays a documented limitation rather than a built guard.
CLASSIFIED_MAX_UNIQUE_VALUES = 100

# Spatial bins per axis when checking for localized NoData=0 clusters.
NODATA_GRID_SIZE = 20

# NoData=0 black-pixel check: per grid cell, read NODATA_SUBGRID x
# NODATA_SUBGRID small native-resolution windows (no decimation at all)
# instead of one whole-image decimated pass. This is the fix for
# detection being slower than the actual conversion (67s measured on a
# 1.7GB, no-overview ortho, more than Translate+overviews combined):
# empirically, a whole-image decimated ReadAsArray on a source with no
# overviews costs ~12s PER BAND regardless of the requested output
# resolution (confirmed flat from 500px to 4000px) - GDAL has to touch
# nearly every source tile to build ANY decimated output without
# overviews to fall back on, so 4 bands (RGB + alpha) cost ~48s no matter
# how coarse the result is. A small native-resolution window, by
# contrast, only touches the handful of tiles under it - reading 400
# grid cells x 9 sub-windows x 96px (3600 small reads) measured 4.0s
# total on that same file, a 12x speedup, while actually catching MORE
# of the known real cluster (worst-cell fraction 0.55% vs. 0.27% for the
# old decimated approach) because it's genuine full-resolution data, not
# NEAREST-decimated pixels that can skip over a run of black pixels
# between sample points.
#
# One window per cell (rejected as insufficiently robust before settling
# on this) risks missing a cluster confined to a corner of that cell -
# a real coverage gap, distinct from (but related to) the sparse-random-
# window approach an earlier round of this project already measured as
# WORSE than a decimated pass (it could miss entire cells, not just parts
# of one). Spreading NODATA_SUBGRID x NODATA_SUBGRID windows across each
# cell keeps the systematic full-grid coverage that fixed that earlier
# problem while adding within-cell coverage the single-window version
# lacked, all still for ~4s total. See docs/plugin_design_notes.md.
NODATA_SUBGRID = 3
NODATA_WINDOW_SIZE = 96

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

# COG creation-option names, not GTiff's. v1 always writes a Cloud
# Optimized GeoTIFF (see docs/plugin_design_notes.md) - there is no
# classic-GTiff code path left for these to serve, so the dicts hold the
# final -co keys directly rather than a GTiff vocabulary translated
# later. Differences from the GTiff names these replaced, confirmed
# against the real COG driver (GDAL 3.13.2) before this switch:
#   - TILED is gone outright - COG has no such option (a COG is always
#     tiled; passing TILED=YES produces "driver COG does not support
#     creation option TILED").
#   - BLOCKXSIZE/BLOCKYSIZE collapse to one BLOCKSIZE (COG tiles are
#     always square).
#   - ZSTD_LEVEL/JPEG_QUALITY become LEVEL/QUALITY (COG names these
#     uniformly across codecs rather than per-codec).
#   - PREDICTOR keeps its existing numeric values ("2"/"3") unchanged -
#     confirmed by testing both directly against the COG driver and
#     reading them back from IMAGE_STRUCTURE metadata. COG's option list
#     also documents STANDARD/FLOATING_POINT as aliases for the same
#     values, but the numeric form was never actually GTiff-only, so
#     nothing here needed translating.
#   - PHOTOMETRIC is gone outright, not mapped - COG does not accept it
#     ("driver COG does not support creation option PHOTOMETRIC") and
#     doesn't need it: tested directly, a 3-band Byte image with
#     COMPRESS=JPEG comes out YCbCr-encoded on its own
#     (SOURCE_COLOR_SPACE=YCbCr in the output's own metadata) with no
#     creation option asking for it. Lossy is only ever offered for
#     RGB_8BIT content in this codebase, so that auto-conversion applies
#     to every JPEG output this tool writes - see
#     _build_applied_settings() below, which relies on this being
#     unconditional rather than reading back a PHOTOMETRIC value that no
#     longer exists.
# overview_config's keys are also COG creation options now, not
# GDAL config options set around a separate BuildOverviews() call - the
# _OVERVIEW suffix (COMPRESS_OVERVIEW) becomes an OVERVIEW_ prefix
# (OVERVIEW_COMPRESS) for the same reason: COG builds overviews inside
# the one Translate call, so there is no separate call left to configure
# with the old suffixed config-option names. OVERVIEW_QUALITY is new -
# the lossy profile sets it explicitly to match QUALITY rather than
# accepting COG's own default (75) for overviews specifically, same
# "documented intent over undocumented default" reasoning
# PREDICTOR_OVERVIEW/OVERVIEW_PREDICTOR already used below. There is no
# COG equivalent for PHOTOMETRIC_OVERVIEW or INTERLEAVE_OVERVIEW - both
# dropped for the same reason PHOTOMETRIC was.
RECOMMENDED_SETTINGS = {
    "lossy": {
        "creation_options": {
            "BLOCKSIZE": "512",
            "COMPRESS": "JPEG", "QUALITY": "90",
            "BIGTIFF": "YES", "NUM_THREADS": "ALL_CPUS",
        },
        "overview_config": {
            "OVERVIEW_RESAMPLING": "AVERAGE", "OVERVIEW_COMPRESS": "JPEG",
            "OVERVIEW_QUALITY": "90",
        },
    },
    "lossless_integer": {
        "creation_options": {
            "BLOCKSIZE": "512",
            "COMPRESS": "ZSTD", "LEVEL": "9", "PREDICTOR": "2",
            "BIGTIFF": "YES", "NUM_THREADS": "ALL_CPUS",
        },
        # OVERVIEW_PREDICTOR matches the base image's PREDICTOR above,
        # kept explicit as documented intent rather than relying on
        # GDAL's own behaviour: measured directly (isolated GDAL test,
        # base predictor with vs without this set) that GDAL 3.13.2's
        # internal overview builder already inherits the base image's
        # predictor on its own - this line currently changes nothing
        # (0.00% size difference, confirmed on real elevation and RGB
        # test files, prior to the COG switch). It stays anyway, in
        # case that inheritance behaviour is ever removed or changes in
        # a future GDAL version - explicit here means the correct value
        # doesn't depend on undocumented default behaviour continuing
        # to hold.
        "overview_config": {
            "OVERVIEW_RESAMPLING": "AVERAGE", "OVERVIEW_COMPRESS": "ZSTD",
            "OVERVIEW_PREDICTOR": "2",
        },
    },
    "lossless_float": {
        "creation_options": {
            "BLOCKSIZE": "512",
            "COMPRESS": "ZSTD", "LEVEL": "9", "PREDICTOR": "3",
            "BIGTIFF": "YES", "NUM_THREADS": "ALL_CPUS",
        },
        # OVERVIEW_PREDICTOR=3: same "documented intent, currently a
        # no-op" reasoning as lossless_integer's above.
        "overview_config": {
            "OVERVIEW_RESAMPLING": "AVERAGE", "OVERVIEW_COMPRESS": "ZSTD",
            "OVERVIEW_PREDICTOR": "3",
        },
    },
}


# ---------------------------------------------------------------------------
# Result structures
# ---------------------------------------------------------------------------

@dataclass
class ProfileOption:
    profile: str  # "lossy" or "lossless"
    available: bool
    reason_blocked: Optional[str] = None
    recommended_settings: Optional[dict] = None
    translate_extra_args: Optional[list] = None


@dataclass
class NoDataRisk:
    applies: bool = False
    # True only when a real alpha/mask already provides positional
    # transparency independent of NoData=0 (i.e. applies=True but the
    # assessment isn't "nodata_only_transparency") - the single source
    # of truth for whether clearing NoData is even structurally
    # possible on this file. False whenever applies=False (no NoData=0
    # condition at all) or the file relies on NoData as its only
    # transparency (clearing would reveal a border, so it's blocked
    # outright regardless of what the caller asks for).
    clear_possible: bool = False
    nodata_value: Optional[float] = None
    sample_pixels_checked: int = 0
    black_pixel_count: int = 0
    black_pixel_fraction: float = 0.0  # whole-sample average - kept for visibility, not used to decide
    interior_max_cell_fraction: float = 0.0  # worst INTERIOR grid-cell fraction - this decides the assessment
    interior_flagged_cells: int = 0
    interior_cells_checked: int = 0
    edge_max_cell_fraction: float = 0.0  # diagnostic only, never decides anything
    grid_cells: int = 0
    # True if the black-pixel grid sample was cut short (progress_cb
    # returned falsy, e.g. the user cancelled) before every row was
    # read - see _black_pixel_sample()'s returned "cancelled" key.
    # assessment is forced to "insufficient_sample" whenever this is
    # True, in _detect_body(): a cancelled sample is not evidence of
    # anything, however the partial numbers above happen to look.
    cancelled: bool = False
    assessment: str = "not_applicable"  # not_applicable | collar_only | meaningful | insufficient_sample
    needs_user_decision: bool = False
    message: Optional[str] = None


@dataclass
class DetectionResult:
    """Classification output for one file. `refused` MUST be checked
    before reading any other field.

    Several refusal codes (NO_CRS chief among them) return early, before
    fields like band_count/dtype/block_size/raster_size are ever set -
    they stay at their dataclass defaults (usually None) on a refused
    result. There is no ordering within this class that guarantees
    those fields are populated; the only reliable signal is `refused`
    itself. A caller that reads e.g. `block_size[0]` without checking
    `refused` first will crash on a None subscript for exactly the
    refusal codes where that field was never reached. This isn't
    hypothetical: it's why checkParameterValues() in the QGIS wrapper
    keeps an early `if detection.refused: return True, ""` even though
    it no longer acts on the refusal itself - see that function's
    comment for the concrete crash it guards against.
    """

    path: str
    ok: bool = False
    refused: bool = False
    refusal_code: Optional[str] = None
    refusal_reason: Optional[str] = None

    # True when the metadata-only pass (detect_metadata_only) could not
    # fully resolve classification and a pixel read is genuinely required -
    # either a CONTINUOUS-bucket raster (any band count) with no colour
    # table on band 1 (classified vs. genuinely continuous needs a
    # per-band unique-value count) or an RGB_8BIT file with NoData=0
    # whose black-pixel risk hasn't been sampled yet. detect() always
    # resolves this to False before returning; only a result returned
    # directly by detect_metadata_only() can still have it True.
    needs_pixel_sampling: bool = False

    content_type: Optional[str] = None  # RGB_8BIT | FLOAT32_CONTINUOUS | CONTINUOUS
    band_count: Optional[int] = None
    dtype: Optional[str] = None
    has_alpha: bool = False
    alpha_band_index: Optional[int] = None
    transparency_source: Optional[str] = None  # alpha | mask | nodata_only | none

    profile_mode: Optional[str] = None  # "forced" | "choice"
    forced_profile: Optional[str] = None  # "lossy" or "lossless"
    # Plain-English explanation of why forced_profile was substituted for
    # whatever was actually requested, set once here (never regenerated
    # by a caller) so the QGIS wrapper's pushWarning() during the run,
    # the eventual end-of-run summary, and the eventual output-file
    # metadata (Phases 4/5 of the purpose-question rework) all read the
    # identical sentence rather than three independently-worded ones.
    # Only meaningful when profile_mode == "forced"; None otherwise.
    # Set per file, not per content type: FLOAT32_CONTINUOUS and
    # CONTINUOUS force every file in the bucket identically, but
    # RGB_8BIT can also force a specific file (nodata_only_transparency,
    # no alpha band) while other RGB_8BIT files stay "choice" - the
    # reason text differs accordingly, not just by content_type.
    forced_reason: Optional[str] = None
    profile_options: list = field(default_factory=list)

    nodata_risk: Optional[NoDataRisk] = None

    block_size: Optional[tuple] = None
    has_overviews: bool = False
    overview_count: int = 0
    compression: Optional[str] = None  # IMAGE_STRUCTURE COMPRESSION tag, e.g. "LZW", "ZSTD", None if uncompressed
    layout: Optional[str] = None  # IMAGE_STRUCTURE LAYOUT tag, "COG" if the source is itself a valid Cloud Optimized GeoTIFF, None otherwise
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


# Plain-English explanations for the cases where the requested profile
# WAS honoured - the counterpart to forced_reason, which only ever
# covers the case where it wasn't. Fixed constants, not per-file text:
# unlike forced_reason (which needs per-file specifics - band count,
# dtype, "no alpha band") none of these depend on anything about the
# individual file, only on which profile ended up used (and, for the
# JPEG one, the source compression detection already read). Defined
# once here and selected (never reworded) by resolve_profile_reason()
# below, so GEOKLEIN_4_DECISION and the end-of-run summary read
# identical text to what a mismatch would have produced via
# forced_reason - one mechanism covering every case.
PROFILE_REASON_ANALYSIS_HONOURED = (
    "Analysis, as asked. The file was written losslessly, so no pixel "
    "value was changed."
)
PROFILE_REASON_VIEWING_HONOURED_RGB = (
    "Viewing, as asked. The file was written with lossy compression, "
    "for the smallest possible file."
)

# Distinct from PROFILE_REASON_ANALYSIS_HONOURED: this is also a
# genuinely-honoured Analysis request (nothing was overridden), but a
# history-based fact about the file makes it worth flagging anyway - a
# category of rule this tool didn't have before this case (see
# docs/plugin_design_notes.md). Surfaced via consequential=True below,
# not honoured=False: the mechanism did match what was asked for.
PROFILE_REASON_JPEG_SOURCE_ANALYSIS = (
    "Analysis, as asked. This file was already compressed for viewing "
    "before it reached this tool, so some pixel values were changed. "
    "Preserving them now keeps those changed values rather than "
    "recovering the originals, and the file will be substantially "
    "larger for no gain in accuracy. For measurement work, run this "
    "tool on the original file instead."
)

# Counterpart to PROFILE_REASON_JPEG_SOURCE_ANALYSIS for the other
# direction: Viewing/lossy requested on a source that's already JPEG-
# compressed. Confirmed directly (see docs/plugin_design_notes.md, "A
# lossy source cannot be restructured into a COG without re-encoding")
# that GDAL has no way to copy already-compressed JPEG tiles through
# unchanged, so re-running Viewing here re-encodes pixels that were
# already lossy-compressed once - a second generation of loss, with
# previously no warning at all. That silence was worse than the
# Analysis case above, which already warned.
#
# No size claim: an earlier version said "at roughly the same file
# size, not a smaller file", which only holds when the source's own
# JPEG quality happens to match this tool's fixed QUALITY=90. Measured
# directly: +0.04%/+0.039% on two sources this tool itself had written
# at quality 90, but +21% on a source built at GDAL's own JPEG default
# (quality 75) re-encoded at 90 - a real, not a rounding-level,
# difference, and one this tool cannot predict up front since it never
# reads the source's original quality setting. True for this tool's own
# output, false in general, so dropped rather than caveated. What's
# actually guaranteed regardless of source quality is pan/zoom speed,
# which this file already had before this run and keeps either way.
PROFILE_REASON_JPEG_SOURCE_VIEWING = (
    "This file was already compressed for viewing, so re-running "
    "Viewing on it discards detail a second time rather than the "
    "first. Pan and zoom speed isn't affected either way. For a clean "
    "copy, run this tool on the original file instead."
)


def is_jpeg_compression(compression: Optional[str]) -> bool:
    """True if the source file's IMAGE_STRUCTURE COMPRESSION tag names a
    JPEG variant. Not a plain "== JPEG" check: GDAL reports "YCbCr JPEG"
    (not "JPEG") for the common RGB case where PHOTOMETRIC=YCBCR - which
    is exactly the combination this tool's own "lossy"/Viewing profile
    writes (see RECOMMENDED_SETTINGS["lossy"] above), so an exact match
    would silently miss the tool's own prior output, the precise case
    PROFILE_REASON_JPEG_SOURCE_ANALYSIS exists to catch. Confirmed
    directly against a real Viewing-profile output file.

    .upper() guards case the same way core/converter.py's own
    compression comparisons do (already_optimised_at_target(), _verify())
    - GDAL's own convention for this tag is consistently uppercase, but
    nothing enforces that convention, and this function had been the one
    compression comparison in the codebase without the guard.
    """
    return compression is not None and "JPEG" in compression.upper()


def resolve_profile_reason(detection: DetectionResult, requested: Optional[str]) -> tuple:
    """Returns (reason: str, consequential: bool) explaining what profile
    was used and why - covering every case a run can produce: forced and
    matched, forced and overridden, and choice (RGB_8BIT only, always
    honoured now that Phase 2 of the purpose-question rework removed the
    one file-specific block that used to sit in "choice" mode).

    consequential means "worth surfacing prominently" (pushWarning
    rather than pushInfo, in the QGIS wrapper) - the same distinction
    core/converter.py's NoData handling already makes between what
    happened and whether it's worth surfacing (nodata_cleared vs.
    nodata_consequential). Whether the request was honoured - the user
    got the profile they asked for, versus the file's nature requiring
    otherwise - isn't returned as a value of its own: every case where
    it wasn't honoured is already consequential (there's no scenario
    where the file's nature overrode the request and that's NOT worth
    mentioning), so consequential alone is enough to decide severity.
    A genuine match can still be consequential, though: requesting
    Analysis on a file whose source compression is already some JPEG
    variant (see is_jpeg_compression) is honoured exactly as asked -
    lossless ZSTD is applied, nothing overridden - and still worth a
    warning, because the pixel values being preserved were already
    changed by that prior JPEG pass. The same applies in the other
    direction: requesting Viewing on an already-JPEG source is also
    honoured exactly as asked, and still worth a warning, because
    re-encoding already-lossy pixels as JPEG again is a second
    generation of loss with no size benefit (confirmed no passthrough
    route exists - see docs/plugin_design_notes.md). Routine matches
    (anything else) are consequential=False.

    requested may be None (a caller that never expressed a preference,
    e.g. the CLI's --profile is optional) - never a mismatch on its
    own, since nothing was denied.
    """
    if detection.profile_mode == "forced":
        actual = detection.forced_profile
        if requested is not None and requested != actual:
            return detection.forced_reason, True
        if actual == "lossless":
            if requested == "lossless" and is_jpeg_compression(detection.compression):
                return PROFILE_REASON_JPEG_SOURCE_ANALYSIS, True
            return PROFILE_REASON_ANALYSIS_HONOURED, False
        # actual == "lossy" - not reachable today (no content type ever
        # forces lossy, only lossless), kept for symmetry with the
        # lossless branch above so a future forced-lossy case doesn't
        # silently skip this check.
        if requested == "lossy" and is_jpeg_compression(detection.compression):
            return PROFILE_REASON_JPEG_SOURCE_VIEWING, True
        return PROFILE_REASON_VIEWING_HONOURED_RGB, False

    # profile_mode == "choice" (RGB_8BIT only): never blocked or
    # overridden any more, so always honoured.
    if requested == "lossy":
        if is_jpeg_compression(detection.compression):
            return PROFILE_REASON_JPEG_SOURCE_VIEWING, True
        return PROFILE_REASON_VIEWING_HONOURED_RGB, False
    if is_jpeg_compression(detection.compression):
        return PROFILE_REASON_JPEG_SOURCE_ANALYSIS, True
    return PROFILE_REASON_ANALYSIS_HONOURED, False


def content_label(detection: DetectionResult) -> str:
    """The short "what this file is" phrase, shared by describe_detection()
    (the full GEOKLEIN_2_DETECTED sentence) and TIFFTAG_IMAGEDESCRIPTION's
    one-line summary in core/converter.py's _write_decision_metadata() -
    factored out so the two don't describe the same file two different
    ways.
    """
    if detection.content_type == "FLOAT32_CONTINUOUS":
        return "Float32 elevation (DSM, DTM or CHM)"
    if detection.content_type == "RGB_8BIT":
        return "8-bit RGB imagery"
    return f"{detection.dtype} imagery"


def describe_detection(detection: DetectionResult) -> str:
    """Plain-English one-liner describing what was detected before
    conversion ran - GEOKLEIN_2_DETECTED (Phase 5 of the purpose-
    question rework). Built once here from DetectionResult fields
    already known before convert() is ever called, so nothing
    downstream needs to regenerate this text - see
    core/converter.py's _write_decision_metadata().

    Leads with "Source file was" rather than a bare comma list: this is
    read on the OUTPUT file (GEOKLEIN_2_DETECTED describes the file
    before conversion, but is only ever seen embedded in the file
    after), so "tiled, without pyramids" on its own reads as a claim
    about the file in front of the reader, when it's actually a
    statement about the source. An explicit subject removes that
    ambiguity.

    Does not import _is_tiled from converter.py (that would be a
    reverse dependency - converter.py imports FROM this module, never
    the other way) - the tiled/stripped check is inlined instead, the
    same one-line comparison _is_tiled itself is.
    """
    content = content_label(detection)

    if detection.content_type == "RGB_8BIT" and detection.has_alpha:
        # Matches how a real alpha band is described elsewhere: 3
        # colour bands, alpha called out separately rather than folded
        # into "4 bands".
        bands = "3 bands plus alpha"
    else:
        n = detection.band_count
        bands = f"{n} band" if n == 1 else f"{n} bands"

    is_tiled = (
        detection.block_size[0] < detection.raster_size[0]
        and detection.block_size[1] < detection.raster_size[1]
    )
    layout = "tiled" if is_tiled else "stripped"
    pyramids = "with pyramids" if detection.has_overviews else "without pyramids"

    return f"Source file was {content}, {bands}, {layout}, {pyramids}."


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------

def _sample_dims(xsize: int, ysize: int, target: int = CLASSIFIED_SAMPLE_TARGET_DIM):
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


def _cell_window(c0: int, c1: int, sub_index: int, sub_count: int, win: int, axis_size: int):
    """Native-resolution window offset for one sub-sample within grid
    cell [c0, c1), spreading sub_count windows evenly across the cell
    (not stacked at its centre) so within-cell coverage isn't limited to
    one small patch. Clamped to the cell first, then to the raster
    extent, so it degrades gracefully on small images/cells rather than
    requesting an out-of-bounds read.
    """
    pos = c0 + int((sub_index + 0.5) * (c1 - c0) / sub_count) - win // 2
    pos = max(c0, min(pos, c1 - win))
    return max(0, min(pos, axis_size - win))


def _black_pixel_sample(ds: "gdal.Dataset", rgb_band_indices, alpha_index,
                         grid_size: int = NODATA_GRID_SIZE,
                         edge_margin: int = NODATA_EDGE_MARGIN_CELLS,
                         min_cell_valid: int = NODATA_MIN_CELL_VALID_PIXELS,
                         sub_grid: int = NODATA_SUBGRID,
                         window_size: int = NODATA_WINDOW_SIZE,
                         progress_cb=None, progress_cb_data=None):
    """Black-pixel-under-NoData check, sampled via small native-resolution
    windows spread across a spatial grid, rather than one whole-image
    decimated read. See the NODATA_SUBGRID/NODATA_WINDOW_SIZE module
    comment for why: this is a straight speed fix (12x measured), and
    incidentally a sensitivity improvement too, since real full-resolution
    pixels can't skip over a run of black pixels the way a NEAREST-
    decimated sample point can.

    A single whole-image fraction dilutes a cluster confined to one part
    of a large raster below any sane threshold (a shadow under one
    structure can be ~100% black within its own footprint but ~0.01% of
    a multi-gigapixel ortho) - that's still the reason this is binned by
    grid cell rather than reported as one number.

    The outermost ring of cells is tracked separately (edge_*) and never
    drives the decision: a clean survey-boundary collar is SUPPOSED to
    show up there. Only an INTERIOR cell exceeding threshold means NoData
    is eating real content, not just the boundary it was meant to mark.
    Cells with too few valid pixels to trust are skipped entirely rather
    than allowed to swing the result on one or two stray pixels.
    See docs/plugin_design_notes.md.

    progress_cb, if given, is called once per completed grid row as
    progress_cb(fraction_complete, "", progress_cb_data) - GDAL's own
    callback shape, so a caller already using that convention for
    Translate/BuildOverviews (see core/converter.py) can reuse it here.
    Returning a falsy value stops sampling early (the grid rows already
    read still count).
    """
    import numpy as np

    xsize, ysize = ds.RasterXSize, ds.RasterYSize
    grid = max(1, min(grid_size, xsize, ysize))
    margin = min(edge_margin, (grid - 1) // 2)  # never eat the whole grid on tiny images
    band_list = list(rgb_band_indices) + ([alpha_index] if alpha_index is not None else [])
    n_rgb = len(rgb_band_indices)

    total_valid = 0
    total_black = 0
    interior_max_fraction = 0.0
    interior_flagged = 0
    interior_checked = 0
    edge_max_fraction = 0.0
    cancelled = False

    for gy in range(grid):
        cy0, cy1 = gy * ysize // grid, (gy + 1) * ysize // grid
        is_edge_row = gy < margin or gy >= grid - margin
        win_y = max(1, min(window_size, cy1 - cy0, ysize))
        for gx in range(grid):
            cx0, cx1 = gx * xsize // grid, (gx + 1) * xsize // grid
            win_x = max(1, min(window_size, cx1 - cx0, xsize))

            cell_valid = 0
            cell_black = 0
            for sy in range(sub_grid):
                wy0 = _cell_window(cy0, cy1, sy, sub_grid, win_y, ysize)
                for sx in range(sub_grid):
                    wx0 = _cell_window(cx0, cx1, sx, sub_grid, win_x, xsize)
                    arr = ds.ReadAsArray(
                        xoff=wx0, yoff=wy0, xsize=win_x, ysize=win_y, band_list=band_list,
                    )
                    rgb = arr[:n_rgb]
                    black = np.all(rgb == 0, axis=0)
                    if alpha_index is not None:
                        valid = arr[n_rgb] > 0  # mask semantics: any non-zero = valid
                    else:
                        valid = np.ones_like(black, dtype=bool)
                    black = black & valid
                    cell_valid += int(valid.sum())
                    cell_black += int(black.sum())

            total_valid += cell_valid
            total_black += cell_black
            if cell_valid >= min_cell_valid:
                cell_fraction = cell_black / cell_valid
                is_edge = is_edge_row or gx < margin or gx >= grid - margin
                if is_edge:
                    edge_max_fraction = max(edge_max_fraction, cell_fraction)
                else:
                    interior_checked += 1
                    interior_max_fraction = max(interior_max_fraction, cell_fraction)
                    if cell_fraction >= NODATA_INTERIOR_CELL_RISK_FRACTION:
                        interior_flagged += 1

        if progress_cb is not None and not progress_cb((gy + 1) / grid, "", progress_cb_data):
            cancelled = True
            break

    return {
        "total_valid": total_valid,
        "black_count": total_black,
        "global_fraction": (total_black / total_valid) if total_valid else 0.0,
        "interior_max_fraction": interior_max_fraction,
        "interior_flagged": interior_flagged,
        "interior_checked": interior_checked,
        "edge_max_fraction": edge_max_fraction,
        "grid_cells": grid * grid,
        # True if progress_cb returned falsy before every grid row was
        # sampled - the stats above are then computed from a genuinely
        # incomplete pass, not a deliberately coarse one. _detect_body()
        # uses this to keep a cut-short sample from being reported as a
        # confident "meaningful"/"collar_only" finding.
        "cancelled": cancelled,
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
    # Both conditions required, not either: a real geotransform (pixel
    # size and origin) and a defined SRS are independent facts about a
    # file, and a file can have one without the other. Confirmed as a
    # real gap in GUI testing - a file with its WKT stripped but a real
    # geotransform still left (matching what removing a CRS from an
    # existing file actually looks like) previously read as "has
    # georeferencing" here, purely because the geotransform alone was
    # enough to pass. Requiring both means neither a real geotransform
    # with no SRS, nor a defined SRS with no real geotransform (an
    # identity transform - a file with a CRS tag but never actually
    # located in it), passes on its own.
    srs = ds.GetSpatialRef()
    gt = ds.GetGeoTransform()
    identity_gt = gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    return srs is not None and not identity_gt


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def detect_metadata_only(path: str) -> DetectionResult:
    """Everything detect() can determine from gdal.Open() + band/dataset
    metadata alone - no pixel reads, so this runs in milliseconds even on
    a multi-gigapixel file. Shared by detect() (which finishes the two
    cases below that genuinely need pixels) and by the QGIS wrapper's
    checkParameterValues (algorithms/optimise_raster.py), which needs an
    instant refusal for the scope checks that don't require sampling
    rather than waiting through a full detect() run before the dialog can
    tell the user "this won't work".

    Two things are deliberately left unresolved here, flagged via
    result.needs_pixel_sampling=True:

    - CONTINUOUS-bucket data (any band count) with no colour table on
      band 1: classified vs. genuinely continuous needs a per-band
      unique-value count over a decimated read, checked across every
      non-alpha band by detect() - see that function and CONTINUOUS
      below.
    - RGB_8BIT with NoData=0 and real (non-nodata-only) transparency: the
      black-pixel cluster risk assessment depends on the same kind of
      sample. This never blocks the lossy profile (nodata risk is
      advisory, see module docstring/docs/plugin_design_notes.md) so it
      doesn't affect what checkParameterValues can decide - it only
      means nodata_risk is incomplete on a result returned from here.

    Every other refusal (no CRS, unsupported dtype, colour-table-
    classified) is fully resolved here, as is the RGB_8BIT
    nodata-only-transparency case, which forces lossless rather than
    refusing (see _finish_rgb_8bit()).
    """
    result = DetectionResult(path=path)

    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
    except Exception as exc:  # noqa: BLE001 - surfacing any GDAL open failure
        return _refuse(result, "UNREADABLE", f"Could not read this file: {exc}")

    if ds is None:
        return _refuse(result, "UNREADABLE", "Could not read this file.")

    try:
        return _detect_metadata_only_body(result, ds)
    finally:
        # Explicit close rather than relying on scope-exit refcounting -
        # matches converter.py's discipline of never leaving a dataset
        # open past the point it's still needed, on every return path
        # including early refusals inside the body below.
        ds = None


def _is_alpha_band(band: "gdal.Band") -> bool:
    return gdal.GetColorInterpretationName(band.GetColorInterpretation()) == "Alpha"


def _finish_rgb_8bit(result: DetectionResult, band1: "gdal.Band", has_alpha: bool, alpha_index) -> DetectionResult:
    """Shared profile-resolution tail for RGB_8BIT, reached from either
    the 3-band or the 4-band-with-real-alpha case in Stage 1 below - the
    two differ only in has_alpha/alpha_index, everything after that is
    identical. Kept as its own function specifically so that difference
    doesn't get duplicated: a 4-band Byte file WITHOUT real alpha in
    band 4 must never reach this function (it falls through to
    CONTINUOUS instead - see Stage 1), since offering JPEG/YCbCr lossy on
    an unflagged 4th band would silently mishandle it, not because the
    band count itself is unsafe.
    """
    result.has_alpha = has_alpha
    result.alpha_band_index = alpha_index
    result.content_type = "RGB_8BIT"

    nodata_value = band1.GetNoDataValue()
    transparency_source = _transparency_source(band1, nodata_value, has_alpha)
    result.transparency_source = transparency_source

    nodata_risk = NoDataRisk()
    nodata_only_transparency = False

    if nodata_value == 0:
        nodata_risk.applies = True
        nodata_risk.nodata_value = nodata_value
        if transparency_source == "nodata_only":
            nodata_only_transparency = True
            nodata_risk.assessment = "nodata_only_transparency"
            nodata_risk.message = (
                "This file uses NoData as its only transparency and has no "
                "alpha band. Clearing it would leave a black border. "
                "Building a proper footprint mask isn't in v1 - see "
                "'If nodata is the only transparency' in the workflow doc "
                "for the manual route."
            )
        else:
            # Black-pixel cluster risk needs a pixel read - can't resolve
            # from metadata alone. Never blocks the lossy profile either
            # way (see docs/plugin_design_notes.md - advisory only), so
            # it's safe to leave incomplete here; detect() finishes it.
            # clear_possible=True: a real alpha/mask already exists here
            # (that's what the "else" of nodata_only means), so clearing
            # NoData can never reintroduce a border - the only remaining
            # question is whether it reveals real content, which is what
            # the pixel sample below decides. This is the single source
            # of truth converter.py uses to decide whether the NoData
            # handling choice (Automatic/Reveal/Keep) is even actionable
            # on this file - see convert()'s "resolve NoData handling" step.
            result.needs_pixel_sampling = True
            nodata_risk.clear_possible = True

    result.nodata_risk = nodata_risk

    # No -a_nodata handling here any more: whether NoData gets cleared is
    # now driven entirely by the caller's nodata_mode choice in convert(),
    # independent of which profile is picked - see that function's
    # "resolve NoData handling" step. This used to be baked in here
    # (lossy always cleared when structurally safe, lossless never did,
    # regardless of the detected risk) - an inconsistency nobody chose
    # and the new parameter replaces outright. The band-selection/mask
    # args below are still genuinely profile-specific (only the lossy
    # profile needs to drop the alpha band to unlock YCbCr) and stay here.
    translate_extra_lossy = []
    if has_alpha:
        translate_extra_lossy = ["-b", "1", "-b", "2", "-b", "3", "-mask", str(alpha_index)]

    if nodata_only_transparency:
        # File-specific forced-lossless, unlike elevation/CONTINUOUS
        # (whole content types, forced unconditionally): this is
        # otherwise a genuine RGB_8BIT choice file, forced here only
        # because THIS file has no alpha band and marks its collar via
        # NoData instead of position. Lossy compression shifts pixel
        # values slightly, so a value-based collar marker doesn't
        # survive that intact (a position-based alpha marker does,
        # which is why files WITH real alpha never reach this branch).
        # See docs/plugin_design_notes.md's deferred internal-mask-band
        # item for the actual fix that would let this get lossy too.
        result.profile_mode = "forced"
        result.forced_profile = "lossless"
        result.forced_reason = (
            "Written for analysis instead. This file marks its "
            "transparent collar with a NoData value and has no alpha "
            "band. Compressing for viewing shifts pixel values "
            "slightly, so a collar marked by value rather than by "
            "position stops being reliable, and the border would "
            "render as solid black. The pixel values were preserved "
            "instead. The file still loads and pans at full speed - "
            "Viewing would only have made it smaller, not faster.\n"
            "\n"
            "To get the smaller file, re-export with an alpha band "
            "and run this again."
        )
        result.profile_options = [
            ProfileOption(
                profile="lossless",
                available=True,
                recommended_settings=RECOMMENDED_SETTINGS["lossless_integer"],
                translate_extra_args=[],
            )
        ]
        result.ok = True
        return result

    result.profile_mode = "choice"
    lossy_option = ProfileOption(
        profile="lossy",
        available=True,
        recommended_settings=RECOMMENDED_SETTINGS["lossy"],
        translate_extra_args=translate_extra_lossy,
    )
    lossless_option = ProfileOption(
        profile="lossless",
        available=True,
        recommended_settings=RECOMMENDED_SETTINGS["lossless_integer"],
        translate_extra_args=[],
    )
    result.profile_options = [lossy_option, lossless_option]
    result.ok = True
    return result


def _detect_metadata_only_body(result: DetectionResult, ds: "gdal.Dataset") -> DetectionResult:
    result.raster_size = (ds.RasterXSize, ds.RasterYSize)
    result.has_crs = _has_georeferencing(ds)
    result.has_aux_xml = os.path.exists(result.path + ".aux.xml")

    if not result.has_crs:
        return _refuse(
            result,
            "NO_CRS",
            "No coordinate reference system found. This looks like a plain "
            "image, not a georeferenced raster.",
        )

    band_count = ds.RasterCount
    result.band_count = band_count
    if band_count < 1:
        return _refuse(result, "NO_BANDS", "This raster has no readable bands.")

    band1 = ds.GetRasterBand(1)
    dtype = gdal.GetDataTypeName(band1.DataType)
    result.dtype = dtype
    result.block_size = tuple(band1.GetBlockSize())
    result.overview_count = band1.GetOverviewCount()
    result.has_overviews = result.overview_count > 0
    image_structure_md = ds.GetMetadata("IMAGE_STRUCTURE")
    result.compression = image_structure_md.get("COMPRESSION")
    # Cheap - already-open handle, same metadata domain as COMPRESSION
    # above, no extra file access. Lets already_optimised_at_target()
    # in converter.py tell a genuine COG apart from a file that's
    # merely tiled/overviewed/correctly-compressed without being one -
    # see that function's docstring for why the difference matters.
    result.layout = image_structure_md.get("LAYOUT")

    for i in range(2, band_count + 1):
        other_dtype = gdal.GetDataTypeName(ds.GetRasterBand(i).DataType)
        if other_dtype != dtype:
            result.warnings.append(
                f"Band {i} has data type {other_dtype}, differs from band 1 "
                f"({dtype}). Detection uses band 1's type."
            )

    # ---- Stage 1a: elevation - single-band Float32, unconditionally
    # forced lossless. Kept as its own case, deliberately not folded into
    # CONTINUOUS below: this forced-profile-plus-optional-warning
    # behaviour was tuned deliberately over several rounds and must not
    # change as a side effect of CONTINUOUS existing.
    if band_count == 1 and dtype == "Float32":
        result.content_type = "FLOAT32_CONTINUOUS"
        result.profile_mode = "forced"
        result.forced_profile = "lossless"
        result.forced_reason = (
            "Written for analysis instead. This is elevation data, a "
            "DSM, DTM or CHM. Compressing for viewing works by "
            "discarding detail the eye won't notice, but these "
            "pixels are height measurements rather than colours, so "
            "discarding detail would change the actual heights. "
            "The pixel values were preserved instead. The file still "
            "loads and pans at full speed - Viewing would only have "
            "made it smaller, not faster."
        )
        settings = RECOMMENDED_SETTINGS["lossless_float"]
        result.profile_options = [
            ProfileOption(
                profile="lossless",
                available=True,
                recommended_settings=settings,
                translate_extra_args=[],
            )
        ]
        result.ok = True
        return result

    # ---- Stage 1b: RGB imagery - 3-4 band Byte, the only content type
    # where lossy is ever actually offered (JPEG/YCbCr genuinely needs
    # exactly three 8-bit visible bands, which is what this is). A
    # 4-band Byte file whose 4th band ISN'T real alpha does NOT enter
    # this branch - it falls through to Stage 1d/CONTINUOUS below,
    # lossless only, rather than being refused (as it was in v1) or
    # silently offered a lossy path that would mishandle its 4th band.
    if band_count == 3 and dtype == "Byte":
        return _finish_rgb_8bit(result, band1, has_alpha=False, alpha_index=None)

    if band_count == 4 and dtype == "Byte":
        band4 = ds.GetRasterBand(4)
        if _is_alpha_band(band4):
            return _finish_rgb_8bit(result, band1, has_alpha=True, alpha_index=4)
        # else: falls through below.

    # ---- Stage 1c: single-band integer with an embedded colour table -
    # definitely classified, resolvable from metadata alone, no pixel
    # read needed.
    if band_count == 1 and dtype in INTEGER_DTYPES:
        has_color_table = band1.GetColorTable() is not None
        if has_color_table:
            return _refuse(
                result,
                "CLASSIFIED",
                "This looks like a classified/categorical raster (a "
                "colour table is present). Building pyramids with "
                "average resampling blends category codes into "
                "meaningless fractional values - silent corruption. "
                "Nearest-neighbour handling for classified rasters "
                "isn't in v1 yet.",
            )

    # ---- Stage 1d: CONTINUOUS - everything else this plugin already
    # trusts the dtype of: any other band count (1 without a colour
    # table, 2, non-Byte 3-4, 5+) with integer or Float32 data. Absorbs
    # what used to be three separate cases - unrecognised single-band
    # integer data, 16-bit RGB (whose "choice" was already a no-op, since
    # lossy was never actually available on it), and multispectral (5+
    # bands) - because none of them need band count or "what this raster
    # represents" to process correctly: only dtype (for the predictor)
    # and "is it classified", which is still unknown at this point and
    # needs an actual pixel read per band - exactly what
    # detect_metadata_only() can't do. detect() resolves it; see
    # CLASSIFIED_MAX_UNIQUE_VALUES's comment for the one known gap in
    # how it resolves it.
    if dtype not in INTEGER_DTYPES and dtype != "Float32":
        return _refuse(
            result,
            "UNSUPPORTED_DTYPE",
            f"Data type {dtype} isn't supported in v1. Supported: 8-bit "
            "RGB (3-4 band) for lossy/lossless, or Float32/integer data "
            "elsewhere for lossless once it's confirmed not classified.",
        )

    result.content_type = "CONTINUOUS"
    result.needs_pixel_sampling = True
    # Forced, not choice: every file that reaches here is content-type-
    # wide forced to lossless, same as elevation (FLOAT32_CONTINUOUS)
    # above - lossy is never available for this bucket regardless of
    # this specific file's other properties, so "choice" with lossy
    # permanently unavailable was a vestigial state once
    # checkParameterValues() stopped hard-blocking on it (Bucket A of
    # the purpose-question rework). needs_pixel_sampling above is
    # independent of this - the file can still turn out to be refused
    # as CLASSIFIED once pixel-sampled; that's unaffected by profile_mode.
    result.profile_mode = "forced"
    result.forced_profile = "lossless"
    # Leads with what the lossy option is FOR, not the codec name - "JPEG"
    # first reads as "this plugin might output a .jpg file", which it
    # never does (always GeoTIFF, whichever profile is used).
    result.forced_reason = (
        "Written for analysis instead. Compressing for viewing works "
        "on colour photographs, so it needs exactly three 8-bit colour "
        f"bands. This file has {band_count} bands at {dtype}, so the "
        "pixel values were preserved instead. The file still loads and "
        "pans at full speed - Viewing would only have made it smaller, "
        "not faster.\n"
        "\n"
        "If a small visual copy is genuinely wanted, export an 8-bit "
        "RGB composite of the bands you want to see, then run this "
        "tool on that file with Viewing."
    )
    settings_key = "lossless_float" if dtype == "Float32" else "lossless_integer"
    result.profile_options = [
        ProfileOption(
            profile="lossless",
            available=True,
            recommended_settings=RECOMMENDED_SETTINGS[settings_key],
            translate_extra_args=[],
        ),
    ]
    result.ok = True
    return result


def detect(path: str, progress_cb=None, progress_cb_data=None) -> DetectionResult:
    """Full detection: the metadata-only pass, plus whatever pixel
    sampling it flagged as still needed (see
    detect_metadata_only.__doc__). This is the entry point for anything
    that wants a complete, final answer - the QGIS wrapper's
    processAlgorithm uses this, not detect_metadata_only, since by the
    time it runs the fast checkParameterValues refusals have already
    passed and the actual conversion needs the full picture (including
    the advisory NoData message).

    progress_cb, if given, is GDAL's own callback(complete, message,
    cb_data) shape and is only meaningful for the RGB/NoData path (the
    classified-check path is a single cheap decimated read with nothing
    worth reporting progress on) - see _black_pixel_sample's docstring.
    """
    result = detect_metadata_only(path)

    if result.refused or not result.ok or not result.needs_pixel_sampling:
        return result

    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
    except Exception as exc:  # noqa: BLE001
        return _refuse(result, "UNREADABLE", f"Could not read this file: {exc}")
    if ds is None:
        return _refuse(result, "UNREADABLE", "Could not read this file.")

    try:
        return _detect_body(result, ds, progress_cb, progress_cb_data)
    finally:
        # Explicit close, matching converter.py's discipline - see
        # detect_metadata_only's identical finally block.
        ds = None


def _detect_body(result: DetectionResult, ds: "gdal.Dataset", progress_cb, progress_cb_data) -> DetectionResult:
    if result.content_type == "CONTINUOUS":
        # Deferred from detect_metadata_only: classified vs. genuinely
        # continuous needs a unique-value count over pixels, checked
        # across every non-alpha band - not just band 1 - so a hidden
        # categorical band (a QA/quality-flag band bundled into an
        # otherwise continuous multispectral product, for example)
        # doesn't slip through undetected. Alpha/mask bands are skipped
        # outright: a real alpha channel is expected to have very few
        # unique values (a near-binary mask), which would otherwise
        # misread as classified - confirmed on this project's own
        # MSTIFF.tif test file, whose alpha band sampled to exactly 2
        # unique values.
        for i in range(1, result.band_count + 1):
            band = ds.GetRasterBand(i)
            if _is_alpha_band(band):
                continue
            unique_count = _classified_unique_count(ds, band)
            if unique_count <= CLASSIFIED_MAX_UNIQUE_VALUES:
                result.needs_pixel_sampling = False
                return _refuse(
                    result,
                    "CLASSIFIED",
                    f"Band {i} looks classified/categorical (only "
                    f"{unique_count} unique values in a "
                    f"{CLASSIFIED_SAMPLE_TARGET_DIM}px sample). Building "
                    "pyramids with average resampling blends category "
                    "codes into meaningless fractional values - silent "
                    "corruption. Nearest-neighbour handling for "
                    "classified rasters isn't in v1 yet.",
                )
        result.needs_pixel_sampling = False
        return result

    # Deferred from detect_metadata_only: RGB_8BIT with NoData=0 and real
    # transparency - black-pixel cluster risk needs the pixel sample.
    stats = _black_pixel_sample(
        ds, [1, 2, 3], alpha_index=result.alpha_band_index,
        progress_cb=progress_cb, progress_cb_data=progress_cb_data,
    )
    nodata_risk = result.nodata_risk
    nodata_risk.sample_pixels_checked = stats["total_valid"]
    nodata_risk.black_pixel_count = stats["black_count"]
    nodata_risk.black_pixel_fraction = stats["global_fraction"]
    nodata_risk.interior_max_cell_fraction = stats["interior_max_fraction"]
    nodata_risk.interior_flagged_cells = stats["interior_flagged"]
    nodata_risk.interior_cells_checked = stats["interior_checked"]
    nodata_risk.edge_max_cell_fraction = stats["edge_max_fraction"]
    nodata_risk.grid_cells = stats["grid_cells"]
    nodata_risk.cancelled = stats["cancelled"]
    result.needs_pixel_sampling = False
    # A cut-short sample (cancelled) is forced into insufficient_sample
    # regardless of what the partial numbers happen to show - it hasn't
    # earned "collar_only" (confirmed nothing hidden) or "meaningful"
    # (confirmed something hidden), only whatever fraction of the grid
    # was actually read before the callback returned falsy.
    if stats["cancelled"] or stats["interior_checked"] == 0:
        nodata_risk.assessment = "insufficient_sample"
        nodata_risk.needs_user_decision = True
        nodata_risk.message = (
            "Too little interior area was sampled to judge collar vs. "
            "real content here. Check visually before deciding on Clear "
            "NoData."
        )
    elif stats["interior_max_fraction"] >= NODATA_INTERIOR_CELL_RISK_FRACTION:
        nodata_risk.assessment = "meaningful"
        nodata_risk.needs_user_decision = True
        pct = stats["interior_max_fraction"] * 100
        nodata_risk.message = (
            f"Up to {pct:.2f}% of pixels in an interior area are pure "
            "black and hidden by NoData=0 - possibly real content "
            "(shadow, water), not just the collar. Tick Clear NoData to "
            "reveal them - the collar will render solid black instead "
            "of transparent."
        )
    else:
        nodata_risk.assessment = "collar_only"
        nodata_risk.message = (
            "No interior black content detected - NoData=0 appears to "
            "only be catching the collar. Clearing it is likely "
            "unnecessary."
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
    if result.content_type == "RGB_8BIT":
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
        print(f"\nNoData risk (lossy option only): {nr.assessment}")
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
            print("  -> needs a decision: [Automatic / Reveal hidden pixels / Keep as-is]")

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
