# Raster Optimiser: plugin design notes

Decisions and deferred ideas specific to the plugin's implementation, as
distinct from `GeoKlein_raster_optimisation_workflow.md`, which documents
the underlying manual workflow and stays the authoritative spec for *why*
each setting exists. This file is where the plugin's own architecture
diverges from, automates, or extends that workflow.

---

## Deferred: v2 classified/categorical raster support

**Status:** not built. Captured 2026-08-17. v1 detects classified rasters
and refuses cleanly (`CLASSIFIED` in `core/detector.py`) rather than risk
the wrong branch.

**Why this is a real category, not a niche skip:** classified rasters
can be large - national land cover products, big estate/council
segmentation outputs are gigabytes, not toy files. This is deferred
because handling it correctly needs a genuinely different processing
recipe from continuous imagery, not because it's rare or unimportant.

**The recipe, when built:**

- **ZSTD + predictor 2**, lossless. Class maps compress very well this
  way because they're long runs of identical integer codes - the same
  property that makes them a bad fit for JPEG makes them a great fit for
  a run-friendly lossless codec.
- **NEAREST-neighbour overviews, never average.** This is the whole
  reason v1 refuses these files today rather than routing them through
  the existing pipeline: pixel values are category *codes*, not colours
  or measurements. Average-resampled overviews blend a forest code and a
  water code at a boundary into some meaningless fractional value
  in-between - silent corruption, structurally identical to what JPEG
  would do to the same file (see next point).
- **Never lossy compression.** JPEG (or any lossy codec) would silently
  reassign boundary pixels between categories - a forest pixel smoothed
  toward its water neighbour becomes a wrong class, not a slightly
  different shade of a right one.
- **Colour table preserved** through the pipeline.

**The underlying principle, worth keeping in mind for any future v1.x
work near this boundary:** lossy compression and average resampling both
assume nearby values are interchangeable. That assumption is exactly
right for photographic imagery (a pixel one shade off is still
recognisably the same thing) and exactly wrong for category codes (a
code one integer off is a different category, full stop). Any new
content type considered for v1 scope should be checked against this
assumption before being routed through the existing continuous-imagery
recipe.

**Why deferring it is low risk:** the large classified products above are
the reason to support this eventually, but the typical classified raster
is a single band with few distinct values, and that shape already
compresses well with a run-friendly lossless codec. Files like that are
among the least likely to arrive slow and oversized in the first place,
and many are small enough that tiling and pyramids add little. The common
case loses almost nothing by waiting.

---

## Deferred: visual NoData preview (magenta-fill diff)

**Status:** not built. Captured 2026-08-17 for later; v1 uses automated
sampling only (see `core/detector.py`).

**The idea:** when NoData=0 is detected on 8-bit RGB, render a preview
where NoData pixels are filled with a colour that never occurs in real
imagery (magenta/cyan), shown against the normal render. The user sees
exactly what NoData is hiding: magenta speckled through shadows means a
real problem; magenta only as a clean edge collar means it's safe to
clear. This is QGIS's own manual "NoData toggle test"
(workflow doc, "The NoData toggle test") done inside the plugin, so the
user never needs to know to go run it themselves.

**Why it matters:** any automated sampling threshold is inherently
arguable and can miss localised clusters: proven on a real file
(`Ortho_school_v1_nick.tif`, a 1.74 GB ortho): a shadow region under one
structure held real black content, but as a *fraction of the whole
image* it diluted to 0.01%, under any sane global threshold. A visual
preview sidesteps the whole question of where to draw the threshold line
by letting the user look, rather than trust a number.

**Intended eventual role:** primary decision mechanism for the NoData
question. The automated sampling in `core/detector.py` becomes a hint
that pre-fills the recommendation ("looks like real content is hidden,
check the preview") rather than the sole decider.

**Architectural constraint this places on the code now:** the NoData
decision must stay a separate step from detection, not baked into it.
`DetectionResult.nodata_risk` (a `NoDataRisk` dataclass) already holds
this correctly: it carries the raw sample stats, an `assessment`, and a
`needs_user_decision` flag as advisory output. It does not by itself
block or force a profile choice (the lossy profile's availability is never
gated on the sampling assessment, only on `nodata_only_transparency`,
which is a structural fact, not a sampling judgement call). Keep it this
way: nothing downstream should assume the sampling assessment is final,
so that a future preview step can slot in as an alternative or
confirming path to the same decision point without detection needing to
change.

---

## v1 sampling: localized black-pixel detection

**Status:** implemented, `core/detector.py`.

The Stage 2A black-pixel sample originally computed one fraction over
the whole image (decimated read, black pixels / valid pixels). Real
testdata (`Ortho_school_v1_nick.tif`) showed this dilutes a real,
localised problem below any reasonable threshold: a shadow-hole cluster
confined to one structure's footprint was ~0.4-1.2% black locally but
0.01% as a fraction of a 1.5-gigapixel image.

Empirical testing against that file (44488x34255 px, no overviews)
ruled out two alternatives:

- **N random full-resolution windows**: fast (0.46s for 30x
  1024x1024 windows) but *less* reliable than the naive global read on
  this file: random placement happened to mostly miss the cluster
  (0.0056% aggregate, worse than the 0.012% global decimated read it
  was meant to improve on). Sparse random coverage has the same
  dilution problem as decimation, just stochastic instead of
  systematic.
- **Reading at a denser decimated target resolution alone**: no help
  either, and notably no extra I/O cost: with no overviews present,
  GDAL's decimated `ReadAsArray` already has to touch nearly every
  source tile regardless of requested output size (1000px and 4000px
  targets both cost ~47s on this file). Decimated-read cost here is
  I/O-bound by the source file, not by the output resolution.

The fix: keep the single decimated whole-image read (its cost is fixed
regardless of target size, so there's no reason not to make it
reasonably dense), but stop aggregating it into one global fraction.
Instead bin the same in-memory sample into a spatial grid and flag on
the worst single cell's fraction. This costs no extra I/O (it re-uses
the array already read) and caught the real cluster cleanly (worst
cell 0.37-1.2% depending on grid density, both well above the 0.1%
per-cell threshold, versus 0.012% globally).

---

## Deferred: internal mask band for NoData-only-transparency files

**Status:** not built. Captured 2026-08-22 for later, alongside the
magenta-fill preview above.

**The idea:** on an 8-bit RGB file whose only transparency is a NoData
value (no alpha band), building an internal mask band from the collar
would let lossy compression become genuinely safe: the mask marks the
collar by position once, up front, the same way a real alpha band
already does for files that have one, and the border stops depending
on NoData surviving pixel-value shifts intact. This is the correct fix
for the case `detector.py`'s `nodata_only_transparency` structural
check currently forces to lossless instead (see `_finish_rgb_8bit()` -
Phase 2 of the purpose-question rework made that case coerce-and-log
rather than block, but a mask band would let it actually get the
smaller file too, not just a clearer explanation of why it doesn't).

**Why it's deferred, not built now:** the mask cannot simply be "every
pixel equal to zero" - that's indistinguishable from the interior
black-pixel-cluster problem the grid sampling in `core/detector.py`
already exists to catch (deep shadow, dark water, wet tarmac are all
genuinely NoData-valued pixels too). Building this mask correctly needs
actual collar detection: tracing the empty border inward from the
image's edges, so only pixels connected to the outside count as collar,
not an interior region that merely happens to share the same value.
That's a materially different (and more expensive) piece of image
processing than anything currently in the detection pipeline, not a
small addition to the existing sampling.

**Update, 2026-08-22:** explicit `Reveal hidden pixels` on these files
now actually clears NoData (`core/converter.py`'s
`_resolve_nodata_handling()`), matching what
`docs/raster_optimiser_ui_text.md` already documented - it was
previously a hardcoded refusal regardless of NODATA_MODE. Automatic is
unaffected and still stays conservative on this file type, since never
clearing without being asked is the whole point of Automatic. This
removes the first of the two blockers on offering lossy here when
Reveal is chosen: NoData genuinely can be cleared now, on request. The
remaining blocker is unchanged: the lossy `ProfileOption` for this file
type isn't populated with real creation options/translate args (it's
currently just absent, since the file is `profile_mode == "forced"`),
and reconstructing one would need the resolution layer
(`_log_profile_decision()` in `algorithms/optimise_raster.py`, and
`convert()`'s own profile lookup in `core/converter.py`) to read
NODATA_MODE and override the structural forced-lossless decision
post-hoc - detection itself still can't take NODATA_MODE as an input
without crossing the "detection stays pure structural classification"
boundary this document already protects for the visual-preview feature
above.

---

## History-based rules versus nature-based rules

**Status:** implemented, 2026-08-24 (`core/detector.py`'s
`PROFILE_REASON_JPEG_SOURCE_ANALYSIS` and `resolve_profile_reason()`'s
`consequential` return value). Captured here because it is the first
rule of its kind the tool has, and future rules should be checked
against the same distinction before being written.

Detection reads what a file *is*. Every rule the tool had before this
one maps that nature to what can safely be done with it: Float32 cannot
be lossy, because height measurements have no "close enough" shade the
way colours do. Five bands cannot be YCbCr, because YCbCr JPEG is
defined for exactly three colour channels. Categorical pixel values
cannot be averaged, because a code one integer off is a different
category, not a similar one. All three are facts about the data itself,
true regardless of where the file came from or what happened to it
before it arrived.

Requesting Analysis on a file whose source compression is already a
JPEG variant is a different kind of rule. It doesn't map the data's
nature to what can be done with it - lossless ZSTD applies to this file
exactly as well as to any other. It maps the file's *history* - a prior
lossy pass already changed some pixel values - to whether preserving
those values losslessly now still means what the user thinks it means.
Nothing about the file's current structure says so; only knowing what
was previously done to it does. That's a genuinely new axis, which is
why `resolve_profile_reason()` needed `consequential` as its own axis,
separate from whether the request was honoured: this case is honoured
(Analysis was requested and Analysis is exactly what ran) and
consequential at the same time, a combination no purely nature-based
rule ever produces.

Two things are worth noting about how this surfaced. First, the source
compression value itself was already available in detection
(`DetectionResult.compression`, read cheaply from `IMAGE_STRUCTURE`
metadata) well before this rule was written - having the fact on hand
did not by itself produce the check, because nobody had yet asked
whether a file's compression history, as opposed to its current
structure, was something a rule should act on. Second, this case was
masked for a while by the "already tiled and has pyramids" check, which
used to be broad enough to catch a re-run of this same file and refuse
it outright for an unrelated reason; Phase 3 of the purpose-question
rework correctly narrowed that check to stop over-refusing, and doing
so is what let a re-run of an already-optimised file reach this far
into the pipeline at all. It only became visible once a measurement
table happened to include a prior Viewing-profile output of this same
tool run back through Analysis, and the resulting 4.4x size growth had
nowhere left to hide behind a broader refusal.

Any future rule proposed for this tool should be examined for which
category it falls into before being written: a fact about what the file
*is*, derivable from its current structure alone, or a fact about what
was *previously done to it*, which detection cannot see just by reading
the file's current state and which needs its own explicit check, the
way this one now has.

---

## Comments go stale in the same rounds the design changes

**Status:** audit done, 2026-08-24. Five stale comments/docstrings found
and fixed across `plugin.py`, `algorithms/optimise_raster.py`,
`core/detector.py` and `core/converter.py` (one CLI debug string fixed
alongside them). All five described behaviour from before the
purpose-question rework: a removed `execAlgorithmDialog()` call, two
`checkParameterValues()` checks that were coerced into log-only Bucket A
behaviour, a forced-lossless case still described as a refusal, and an
already-optimised short-circuit described as compression-blind after it
became compression-aware.

None of these were random drift - every one was left behind by a design
change that touched the code but not the comment sitting next to it.
That makes this a predictable failure mode, not a one-off cleanup: the
next significant design change to this codebase should be followed by
the same kind of pass (read every docstring and comment in the changed
files against what the code now does) rather than assuming comments
updated themselves alongside the behaviour they describe.

---

## BOUNDCRS is not preserved through conversion - confirmed as a GDAL
## limitation, not this tool's behaviour

**Status:** investigated, 2026-08-25. Not fixed, because no fix was found to
apply, and because the current (unpreserved) behaviour is not clearly worse
than the alternative - see the reasoning below.

A source file whose CRS is a `BOUNDCRS` - a base projected CRS (here,
`EPSG:27700`, OSGB36 / British National Grid) wrapped in an explicit
`ABRIDGEDTRANSFORMATION` to WGS 84 via seven-parameter (Helmert) datum-shift
values - comes out of this tool as a plain `PROJCRS` for the same base EPSG
code, with the wrapper and its datum-shift parameters gone. Geometrically
this is invisible: the geotransform (origin, pixel size) is byte-identical
between source and output to eighteen decimal places, and so are all four
corner coordinates. What changes is which transformation a later consumer
uses to relate that geometry to WGS 84 for reprojection or a basemap
underlay - with the `BOUNDCRS` present, GDAL/QGIS use the embedded
seven-parameter transformation; without it, they fall back to whatever PROJ
selects as the best available OSGB36-to-WGS84 operation on its own (for
Great Britain, that is ordinarily the OSTN15 grid shift, a spatially-varying
correction rather than a single fixed offset). The two disagree by up to
about 2cm on the file this was measured against, an 0.5m-GSD ortho (roughly
12mm at the pixel-corner level once resampled), varying by location rather
than in one fixed direction - a signature consistent with a grid-based
correction (OSTN15) diverging slightly, non-uniformly, from a fixed Helmert
transform, rather than with anything reprojecting incorrectly. Below the
image's own pixel resolution, and invisible at any normal working zoom;
it only shows up as a hairline offset with both files open together and
zoomed in past the point the imagery itself resolves.

**Is this plugin's creation options doing it?** No. Reproduced with a bare
CLI `gdal_translate` carrying zero `-co` flags at all - run outside this
plugin, outside Python, against `testdata/MSTIFF.tif` (a real file already in
this repo whose CRS is a genuine `BOUNDCRS`, confirmed by opening it
directly) - and the wrapper was dropped identically to a run using this
plugin's exact `lossless_integer` creation options. Both produce the same
plain `PROJCRS`. The drop happens with or without a single one of this
tool's settings involved.

**Can the wrapper be preserved at all?** Not with anything tried. Beyond the
default write, tested `-co GEOTIFF_VERSION=1.1` (the newer, WKT-capable
GeoTIFF mode) and `-co GEOTIFF_KEYS_FLAVOR=ESRI_PE` (the flavour real-world
software most often uses to embed a full WKT text blob when the classic
numeric GeoTIFF keys can't represent a CRS) - both on the real `BOUNDCRS`
file and reproduced independently by constructing a synthetic OSGB36/BNG
`BOUNDCRS` via `osr.SpatialReference.SetTOWGS84()` and writing it fresh.
Neither preserved the wrapper; in fact the synthetic file didn't survive as
a `BOUNDCRS` even on its own *first* write via GDAL's `Create()`/
`SetSpatialRef()`, before any Translate was involved at all. That means
whatever software produced the real ortho this was first noticed on wrote
its GeoTIFF through a path this GDAL version's own writer doesn't
reproduce - as far as testing here shows, `BOUNDCRS` support in GDAL's GTiff
writer is a genuine current upstream limitation, not a setting this tool
declined to use.

**Should it be preserved, if a way is ever found?** Not by default, and not
reflexively. Two reasons, not one:

- It is not established that keeping the file's own embedded transformation
  would be *more* correct. A fixed seven-parameter Helmert transform is
  itself one specific choice among several possible OSGB36-to-WGS84
  operations, generally less accurate across Great Britain than the
  OSTN15 grid shift that PROJ selects by default once no `BOUNDCRS` locks
  the choice - and the spatially-varying nature of the ~2cm measured
  difference is itself evidence that OSTN15 (not a cruder fallback) is what
  PROJ is actually using in the current, wrapper-free case. Restoring the
  wrapper could just as easily make output *less* accurate as more, file by
  file, depending on how good that file's own embedded parameters happen to
  be.
- Even if a real, working way to preserve it were found, `GEOTIFF_VERSION=1.1`
  is a newer specification with less universal support than classic
  GeoTIFF - a real compatibility cost against other GDAL-based consumers
  this tool explicitly targets (QField in particular), for a correctness
  gain that isn't itself confirmed. That trade needs to be made on purpose,
  with both sides measured, not adopted as an incidental side effect of
  fixing something else.

Recorded here specifically so a future change that happens to preserve this
(a GDAL upgrade, a creation-option change made for an unrelated reason) gets
noticed and evaluated deliberately, rather than silently starting to embed a
fixed datum-shift transform nobody decided to keep.

---

## v1 always writes Cloud Optimized GeoTIFFs

**Status:** implemented, 2026-08-26. The output is always a COG now - no
parameter, no tick box. A COG is a valid tiled GeoTIFF with overviews plus a
specific header byte layout (IFDs before pixel data), so it is strictly
better than what this tool produced before, with no trade-off to expose in
the dialog.

**Writing metadata onto a COG handle after Translate silently breaks it, and
a cheap check would not have caught it.** The `GEOKLEIN_*` decision-chain
metadata used to be written with `SetMetadataItem()` calls on the dataset
handle Translate returned, after Translate had already finished writing the
file - safe under classic GTiff, where metadata can sit anywhere in the one
IFD. Under COG it is not safe: tested directly, calling `SetMetadataItem()`
on an already-created COG dataset and closing it forces GDAL to grow the IFD
to fit the new tag data, and the enlarged IFD gets appended to the end of the
file rather than rewritten in place. That moves the main IFD past the pixel
data it's supposed to precede, which is exactly the ordering a COG's validity
depends on - and the file still carries `LAYOUT=COG` in its own metadata,
because that tag reflects how the file was *created*, not how it now
happens to be laid out. A cheap tag read would have reported this file as a
valid COG. Only running the real validator
(`osgeo_utils.samples.validate_cloud_optimized_geotiff.validate()`) caught
it, with a concrete offset mismatch: the main IFD reported at byte 42540
while an overview's IFD sat at byte 1430, ahead of it.

This is why the fix has two parts, not one: `GEOKLEIN_*` metadata (and
`TIFFTAG_IMAGEDESCRIPTION`) is now passed as `-mo KEY=VALUE` arguments
*into* the same `gdal.Translate()` call that creates the file
(`core/converter.py`'s `_build_decision_metadata()`/`_metadata_mo_args()`),
never written afterwards - and `_verify()` runs the full validator on every
output, not a `LAYOUT` tag read, specifically because a tag read is exactly
the check this bug would have passed. The cheap tag read is still the right
tool elsewhere: `already_optimised_at_target()`'s fourth condition (see
below) only needs a fast, mostly-reliable signal for whether to skip a
*source* file entirely, not the last word on whether a *freshly-written*
file is actually correct - those are different jobs with different
correctness requirements, which is why they use different checks on purpose
rather than one being a shortcut for the other.

**Every file this tool produced before this change will be reprocessed, not
skipped, and that is correct.** `already_optimised_at_target()` gained a
fourth condition alongside tiled/overviews/target-compression: the source
must itself report `LAYOUT=COG`. Confirmed directly against real pre-COG
outputs from earlier in this same rework (`Ortho_school_v1_nick_optimised.tif`,
`dsm_nick_optimised.tif`) - both are tiled, have overviews, and are already on
their target compression, yet both fail COG validation outright (wrong
overview/main-image block ordering), the same structural defect a plain
`LAYOUT` tag read cannot see either. Without the fourth condition, this
tool's own earlier output would wrongly read as "already optimised" forever
and never actually become a real COG. The user-facing consequence is a new,
distinct message rather than silence or a misleading one: a file that is
tiled, overviewed, and already correctly compressed but not yet a valid COG
now reports "not a valid Cloud Optimized GeoTIFF yet - reprocessing to add
that structure" rather than either being skipped or hitting the
compression-mismatch message (which would falsely claim the compression
itself is wrong). See `docs/raster_optimiser_ui_text.md`'s "Already tiled,
overviews and compression right, but not a valid COG" for the exact text.

---

## A lossy source cannot be restructured into a COG without re-encoding

**Status:** confirmed, 2026-08-27. When a JPEG-compressed source is
restructured into a Cloud Optimized GeoTIFF, GDAL always fully decodes and
re-encodes the pixel data - there is no way, in this toolchain, to copy
already-compressed tiles across unchanged. This was checked four ways rather
than assumed, since the wrong assumption here would mean building an
incorrect "no-op recompression" code path on top of it:

1. **The COG driver's own creation-option list** (tested directly:
   `gdal.GetDriverByName('COG').GetMetadataItem('DMD_CREATIONOPTIONLIST')`
   against GDAL 3.13.2, the version bundled with this QGIS install) has
   nothing resembling passthrough, copy, or reuse of source-compressed bytes.
   The closest candidate, `OVERVIEWS=FORCE_USE_EXISTING`, was tested earlier
   in this same investigation and disproven by per-overview-level checksum
   mismatch - it matches the source's overview *count and dimensions*, not
   its bytes.
2. **`cogger`** (tested by reading its documentation and GDAL's own COG
   driver docs, not installed or run): a real, separate tool
   (<https://github.com/airbusgeo/cogger>) that genuinely does what's being
   asked - it restructures an already internally-tiled, standard-compressed
   GeoTIFF with existing overviews into a COG by "reshuffling of the
   original geotiff's bytes", explicitly without pixel manipulation. It
   exists and works; it is also a separate, unbundled, unsigned Go binary
   with no relationship to this project's GDAL/QGIS toolchain today.
   Rejected for that dependency cost, not for lacking the capability -
   bundling a third-party compiled binary to cover one edge case (an
   already-JPEG source) was judged not worth it.
3. **`rio-cogeo`** (read documentation only, not installed, per instruction):
   no passthrough mode is documented anywhere. Its default path predates the
   COG driver and builds output via GDAL's own `CreateCopy`/`Translate`; its
   `--use-cog-driver` flag routes to the exact GDAL COG driver already
   tested in (1). It adds no independent capability here.
4. **`gdal_edit.py`/`tiffcp`** (read documentation for both; neither is
   present anywhere in this OSGeo4W install, confirmed by searching the
   whole tree): `gdal_edit.py` is explicitly scoped to
   georeferencing/metadata/nodata/statistics only and never touches pixel
   data, compression, tiling, or IFD structure - it cannot restructure
   anything. `tiffcp` can retile/restrip a TIFF and documents itself as not
   altering image data content while doing so, but that claim is about
   pixel *values* surviving a lossless round-trip, not about avoiding
   recompression - it does not and cannot apply to JPEG-in-TIFF anyway: JPEG
   data is chunked in DCT blocks aligned to the tile grid, so changing tile
   boundaries requires decoding to pixels regardless of what any tool
   intends. `tiffcp` is also not COG-aware - it has no concept of the
   leading-IFD/ghost-area layout a valid COG requires, so even a
   byte-preserving retile from it would not itself be a valid COG.

The consequence: restructuring an already-JPEG source into a COG with the
lossy profile always changes pixel values slightly (second-generation JPEG
loss). Size also changes, and not predictably: measured directly at +0.039%
on a source this tool itself had written at QUALITY=90, but +21% on a source
built at a different JPEG quality (GDAL's own default, 75) then re-encoded at
this tool's fixed 90. The gap between a source's original quality and this
tool's fixed one is not known up front, so the size effect cannot be
predicted either. What stays genuinely unaffected is pan and zoom speed,
since the file was already tiled with overviews before this run. The chosen
response is to warn, not refuse - see
`docs/raster_optimiser_ui_text.md` for the resulting warning text and
`core/converter.py`'s `resolve_profile_reason()`/cog_structure branch for
where it's produced. Refusing would leave someone whose only asset is a JPEG
basemap with no route to a COG through this tool at all, for a trade-off (a
second generation of loss, and possibly a size change) the user can
reasonably judge for themselves once told about it plainly.

---

## The output-larger-than-source note's 1.0% suppression threshold

**Status:** implemented, 2026-08-27 (`core/converter.py`'s
`_SIZE_INCREASE_RESTRUCTURE_THRESHOLD_PCT`). On a file that already had
pyramids, the note is shown only when the increase is at least 1.0%, and
suppressed entirely below that. The number sits above the one confirmed
pure-COG-restructure case (a 0.02% increase with no other cause) and below
any increase judged worth naming a cause for, rather than being measured
directly. It is a round number chosen to sit comfortably on the right side
of that single data point, not a boundary calibrated against several.

---

## "What it will not process" lists a subset of refusals on purpose

**Status:** deliberate, 2026-08-31. `shortHelpString()`'s "What it will not
process" section names only classified rasters and files with no CRS, and
leaves out three other refusal codes in `core/detector.py`: UNREADABLE,
NO_BANDS and UNSUPPORTED_DTYPE. The section exists to save someone time on
a file they can recognise in advance; nobody looks at a raster and can
tell it has zero bands or an unsupported data type, so those three already
explain themselves in the refusal message when they happen and do not need
advance warning here.

---

## Progress bar sits near 10% early in a large Viewing conversion

**Status:** cause found, 2026-09-02. This section was previously headed
"Progress bar pauses around 10% early on QGIS 4.2, cause not found" and
attributed the symptom to a Qt repaint or event-loop effect. That was
wrong. It is GDAL's own progress reporting.

Instrumented on `Ortho_school_v1.tif` (1.66 GiB, 4-band Byte, DEFLATE,
no overviews) by recording the timestamp and `complete` value of every
`gdal.Translate` callback tick, once written for Viewing and once for
Analysis:

- The first tick arrives about 20ms after the `gdal.Translate()` call,
  at `complete` 0.0. There is no pre-callback startup gap.
- The slow stretch is inside Translate, after the first callback. The
  COG driver works through the full-resolution image before it builds
  the pyramids, and reports that phase as `complete` 0.00 to 0.05, a few
  per cent of the bar, while it takes a large share of the wall time, in
  silent 3 to 7 second steps between ticks.
- Written for Viewing (JPEG encode of a roughly 1.5 gigapixel image,
  plus the alpha drop and remask the lossy path does) that phase took
  about 25 of the run's 84 seconds, with the bar between 10 and 13 per
  cent throughout. Written for Analysis it is mild: the worst gap
  between consecutive ticks was 1.7 seconds, at about 24 per cent, and
  the bar climbs fairly evenly.

The 2026-08-31 instrumentation measured the wrong span. It timed the gap
from the `gdal.Translate()` call to the first callback tick, found it was
0.020s, saw no plateau there, and stopped. It also ran on a test file
that did not reproduce the symptom, and concluded a Qt repaint effect
that does not exist.

**What was done about it:** a `processAlgorithm()` log line before
`convert()` explains the slow start and names a rough 20 to 30 second
figure, so the wait reads as expected rather than hung. It is gated to
`_resolved_profile_for_target(detection, purpose_choice) == "lossy"`:
Analysis climbs fairly evenly, and gating on the *resolved* profile
means a Viewing request coerced to Analysis (elevation, 16-bit,
NoData-only RGB) does not get a message about a slowdown it will not
see. A first version pushed the line on every run and phrased it to
distinguish the two paths in prose; that read as a warning about
something that was not happening on Analysis runs. An earlier attempt
still, to carry the message in the status text and swap it for
"Translating..." on GDAL's first callback tick, was reverted too: the
first tick lands at about 20ms, so the message flashed and vanished
before the slow phase it described.

---

## Bold headings in shortHelpString() render low-contrast on dark themes

**Status:** known and accepted, 2026-09-02. `shortHelpString()` uses real
`<b>` for its section headings, glossary terms and (as of this date)
parameter names. QGIS's Processing help panel applies a fixed,
non-theme-aware colour to `<b>` text, so on dark themes - Night Mapping
(`#535353` background) and similar - the bold text renders as
low-contrast dark-grey-on-dark-grey: legible but dull rather than
prominent. No single inline colour fixes it, since nothing satisfies WCAG
contrast against both a dark background and the default theme's near-white
one at once. An earlier version of the string avoided `<b>` headings for
exactly this reason. Bold is kept anyway: the default light theme is
where most users are, and the heading/parameter structure earns its keep
there. Reverting is mechanical - swap each `<b>...</b>` back to a plain
`<p>` heading (and drop the `<b>` from the `<b><i>` parameter names).
