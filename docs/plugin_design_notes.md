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

## Deferred: a separate name for the loaded output layer

**Status:** not built, 2026-09-02. QGIS names the layer it loads on
completion after the output parameter's description, and that name is
what persists in the saved project (the field label itself is read once
and gone). So the `OUTPUT` parameter's label is kept short - "Optimised
raster". "Save optimised raster as" was tried and reverted because it
produced a layer literally called "Save optimised raster as".

The layer name can be set independently of the label from
`processAlgorithm()`: after resolving the output path, guard with
`context.willLoadLayerOnCompletion(path)`, then
`context.layerToLoadOnCompletionDetails(path)` returns a mutable
`QgsProcessingContext.LayerDetails` whose `.name` and `.forceName` are
both writable (confirmed on 3.44 LTR and 4.2).

The reason to do this eventually: "Optimised raster" is unhelpful when
several are loaded in one session - they all get the same name plus a
numeric suffix. Deriving the layer name from the source filename (e.g.
the source stem, or the source stem plus a short suffix) would make a
batch readable. Deferred only because the short label works as both for
now.

---

## History-based rules versus nature-based rules

**Status:** implemented, 2026-08-24 (`core/detector.py`'s
`PROFILE_REASON_JPEG_SOURCE_ANALYSIS` and `resolve_profile_reason()`'s
`consequential` return value). Captured here because it is the first
rule of its kind the tool has, and future rules should be checked
against the same distinction before being written.

Detection reads what a file *is*. Every rule the tool had before this
one maps that nature to what can safely be done with it: floating point
elevation cannot be lossy, at Float32 or Float64, because height
measurements have no "close enough" shade the way colours do. Five bands cannot be YCbCr, because YCbCr JPEG is
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

## The profile-decision message is logged before Translate

**Status:** implemented, 2026-09-03.

`_log_profile_decision()` (`algorithms/optimise_raster.py`) pushes the
profile-decision string - `resolve_profile_reason()`'s return value, one
of the `PROFILE_REASON_*` honoured constants or a per-file
`forced_reason` - into the run log immediately after detection, *before*
`convert()` runs. The same string is then embedded in the output file as
`GEOKLEIN_4_DECISION` once the file exists.

**Why the push stays before Translate.** The placement is deliberate:
the user sees which profile was used, and why, before waiting through a
conversion that can take minutes on a large file. For the two
consequential JPEG-source cases the timing matters more than
convenience - those strings end "run this tool on the original file
instead", and a user who reads that mid-run can still Cancel before the
expensive Translate has run. Moving the push to after `convert()`
returns would make a past tense ("the file *was* written...") literally
true, but it would close that cancel window exactly where cancelling
before the expensive work is the response the warning invites. The
tense is the smaller problem, and it was solved in the wording instead.

**Why the strings are timeless present tense.** Because each is shown at
two moments - pre-Translate in the log, post-write in the file's
metadata - any tense anchored to the write is wrong at one of them.
"The file was written losslessly" is false in the log (nothing written
yet); "will be written" would be false in the metadata. So every clause
describing what was written is phrased as a property that holds whenever
it is read: "Lossless compression preserves every pixel value", "Lossy
compression discards detail the eye will not notice", "The file is
written for analysis instead. ... The pixel values are preserved
instead." Clauses about the *source's* history stay past ("This file
was already compressed for viewing before it reached this tool" is
genuinely past at both moments), and counterfactual clauses stay
conditional ("Viewing would only have made it smaller, not faster"). All
seven strings were brought into line in one pass, 2026-09-03: three of
them (the `forced_reason` set) had been left in write-anchored past
tense a round earlier, before the tense problem was identified - leaving
part of a set in the wrong tense while the rest was fixed is the drift
these notes exist to catch. `GEOKLEIN_5_APPLIED` follows the same rule
for the same reason: it states decisions made before Translate ran,
never an outcome that might not happen.

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
`convert()` explains the slow start so the wait reads as expected rather
than hung. Three gates:

- `_resolved_profile_for_target(detection, purpose_choice) == "lossy"`.
  Analysis climbs fairly evenly, and gating on the *resolved* profile
  means a Viewing request coerced to Analysis (elevation, 16-bit,
  NoData-only RGB) does not get a message about a slowdown it will not
  see.
- source pixel count at least `_SLOW_START_NOTE_MIN_PIXELS` (250
  megapixels). Below that the slow phase was under a second in the crop
  measurements below, so the note would describe a wait that does not
  happen. 250 MP sits above the largest "fast" point (171 MP) and below
  the smallest "slow" one (513 MP); the constant's own comment carries
  the limits (one point either side of the crossover, nothing between
  264 and 612 MB, and the step is a memory boundary that moves with
  RAM, `GDAL_CACHEMAX` and disk speed, so this is a single-machine
  calibration).
- `not detection.has_overviews`. Added 2026-09-02 after the note was
  reported firing on three reprocess runs where there was no wait at
  all: reprocess of a fresh COG output (Translate 31.9s), of a pre-COG
  file (30.4s), and of an already-optimised DSM (4.6s), none of which
  stalled near 10%. All three had a source that was already tiled with
  overviews; every measurement behind both the note's existence and the
  250 MP knee was taken on a source with *no* overviews
  (`Ortho_school_v1.tif` and its crops). The slow phase is GDAL decoding
  a full-resolution image with no overviews to work from - exactly the
  case those runs did not have - so a source that already carries
  overviews is being restructured, not read from scratch, and does not
  get the note.

**Why the message no longer names a duration.** The first version named
"a rough 20 to 30 second figure" on the reasoning that "slowly" alone
leaves someone guessing whether the run has hung, while a number lets
them wait. The figure was dropped because it was measured on a single
machine (~10s at 513 MP, ~45s at 1524 MP here) and this same section
already establishes the slow phase as a memory boundary that moves with
RAM, `GDAL_CACHEMAX` and disk speed. A slower or RAM-starved machine can
sit near 10% for well over 30 seconds, at which point the figure stops
reassuring and starts reading as "something is wrong". "For a while" is
honest on every machine; the figure was honest only on the one it came
from.

**Why the 250 MP gate stays despite the same portability problem.** The
knee moves with the same RAM/`GDAL_CACHEMAX`/disk variables, so 250 MP is
no more portable than the discarded figure was. It stays because the two
failure modes are not equivalent. A wrong *duration* misinforms - it
asserts a specific false fact the user measures against. A wrong
*threshold* only shows or withholds one hedged sentence at the wrong
boundary: a slightly-early trigger adds a mild, non-alarming line to a
run that turned out quick; a slightly-late one falls back to the
pre-existing "bar looks stuck, no explanation" behaviour, no worse than
before the note existed. The floor also still does a job the wording
change does not remove: it suppresses the note on the many small lossy
runs where the crop data shows the slow phase is under a second.

A first version pushed the line on every run and phrased it to
distinguish the two paths in prose; that read as a warning about
something that was not happening on Analysis runs. An earlier attempt
still, to carry the message in the status text and swap it for
"Translating..." on GDAL's first callback tick, was reverted too: the
first tick lands at about 20ms, so the message flashed and vanished
before the slow phase it described.

### Crop measurements, 2026-09-02

Lossy path, on nested crops of `Ortho_school_v1.tif` (DEFLATE, 4-band
Byte, no overviews) plus the full file. Two crops each at 43 and 171
megapixels: one from the near-empty top-left corner (low bytes per
pixel) and one from the dense centre (high), to tell pixel count apart
from compressed size. Slow phase = first callback tick until the first
inter-tick gap under about a second.

| crop | source MB | MP | bytes/px | slow phase | slow phase ends at `complete` | Translate total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| corner 43 MP | 2.1 | 43 | 0.05 | ~0.1s | 0.01 | 0.7s |
| centre 43 MP | 58.3 | 43 | 1.36 | ~0.16s | 0.01 | 1.5s |
| corner 171 MP | 121.4 | 171 | 0.71 | ~0.4s | 0.01 | 4.3s |
| centre 171 MP | 263.6 | 171 | 1.54 | ~0.7s | 0.01 | 5.8s |
| corner 513 MP | 611.8 | 513 | 1.19 | ~10.1s | 0.062 | 26.5s |
| full | 1782.2 | 1524 | 1.17 | ~45.0s | 0.180 | 86.7s |

First tick was 15 to 20 ms in every run.

**Finding 1: pixel count drives the slow phase; compressed bytes only
nudge it.** At a fixed 43 MP, 28x more bytes (2.1 to 58 MB) moved the
slow phase from ~0.1 to ~0.16s. At 171 MP, 2.2x more bytes (121 to 264
MB) moved it from ~0.4 to ~0.7s. So decode cost adds a small multiplier
but never turns a fast run slow. Pixel count is what carries it into
"worth warning" territory, non-linearly: near-linear to ~170 MP, then a
sharp step between 170 and 510 MP (0.7s to 10s), then roughly linear
again. That step is the full-resolution working set crossing a
memory/cache boundary, which is why the MP threshold is a single-machine
calibration.

**Finding 2: the `complete` value where the slow phase ends is not
stable.** It is 0.01 on every file up to ~260 MB, then 0.062 at ~610 MB,
then 0.180 on the full file - the base-image phase's share of the
progress bar grows with pixel count, from about 1 to about 18 per cent,
and would also move with band count, overview count and codec.

**Status text during the slow phase: left as "Translating...", no
swap.** It is set once before `convert()` and stays put. The idea was to
show a different string during the slow phase and swap it for
"Translating..." once `complete` crossed a threshold around 0.05.
Finding 2 rules that out: there is no stable `complete` value to swap
on, so any threshold would be calibrated to how this one GDAL version
apportioned progress on this one file. Tick-cadence detection instead -
swap once two consecutive ticks arrive under about a second apart -
would survive a different file, but it adds state and logic to
`make_progress_cb` for a marginal gain over the gated log line, which
already tells a large-Viewing user what to expect. Not worth the
machinery.

---

## Float64 uses the Float32 predictor

**Status:** implemented, 2026-11-13. Float64 was previously refused
outright at the data-type gate (`UNSUPPORTED_DTYPE`, "Data type Float64
isn't supported in v1"), which mattered because Float64 is what PDAL's
`writers.gdal` produces by default - so a DSM straight out of a LiDAR
pipeline was refused by a tool whose single best-fitting content type is
elevation.

Float64 is now handled identically to Float32 at every decision point:
`FLOAT_DTYPES` in `core/detector.py` is the one set both widths belong
to, Stage 1a detects single-band Float64 as elevation, the CONTINUOUS
bucket and its classified-raster check take multi-band Float64 on the
same terms, `_settings_key()` in both modules selects `lossless_float`
by membership of that set, and the output is written at the source's own
type with no conversion.

**Predictor 3 is the right default at both widths.** Measured directly,
ZSTD level 9, same COG settings, overviews included:

| File | No predictor | Predictor 2 | Predictor 3 |
|---|---|---|---|
| `dsm_nick.tif` crop, Float32 | 11,631,632 | 8,546,457 (73.5%) | 7,297,708 (**62.7%**) |
| same crop promoted to Float64 | 11,711,216 | 10,908,424 (93.1%) | 8,617,226 (**73.6%**) |
| Hamilton Smeaton 0.025m DSM, real Float64 LiDAR, 16000x16000 | 1,449,149,703 | 1,124,976,997 (77.6%) | 1,093,744,868 (**75.5%**) |
| `dsm_float64_pdal.tif`, synthetic Float64 | 21,922,455 | 21,592,494 (98.5%) | 34,014,012 (**155.2%**) |
| same synthetic file demoted to Float32 | 11,547,298 | 10,623,308 (92.0%) | 11,893,330 (103.0%) |

Predictor 3 is the smallest option on every **real** elevation file
measured, at either width. The one file it loses on is the synthetic
one, and badly - 155% of no-predictor.

**Correction, recorded deliberately.** An earlier version of this
section explained that outlier by trailing zero mantissa bits: Float64
values that came from Float32 carry 29 of them for the byte shuffle to
exploit, values rasterised from LAS Z at 0.001 scaling carry none. The
Hamilton row falsifies it. That file is real client LiDAR, its values
carry a mean of 1.0 trailing zero bits (versus 30.2 on Float32-derived
values), and predictor 3 is still the best of the three on it. Two
further candidates were then measured and both also predict the
opposite of what happened:

| | Hamilton (p3 wins) | synthetic (p3 loses) |
|---|---|---|
| pixel size | 0.025 m | 0.5 m |
| mean trailing zero mantissa bits | 1.0 | 1.6 |
| mean leading bytes shared with horizontal neighbour, of 8 | 2.34 | 5.84 |
| NoData share | 40% | 32% |

More shared leading bytes and less NoData would both predict predictor 3
doing *better* on the synthetic file, not 80 percentage points worse.
**So the cause is not established**, and nothing in this file should be
read as explaining it.

What that leaves is a default validated on real data and one synthetic
counter-example nobody has reproduced on a real product. Predictor 3
stays. Splitting it per dtype width would add a fourth entry to
`RECOMMENDED_SETTINGS` keyed on something the measurements do not
actually separate on - the two Float64 files disagree with each other,
not with the Float32 ones - and a predictor chosen from a property the
detector cannot observe would be a guess wearing a measurement's
clothes. Revisit only if a real client file reproduces the synthetic
file's result; `testdata/make_points.py` builds that file if the
outlier is worth chasing.

**The advisory addresses the size question from the other end**, and
needs no guess about predictors at all:
`_float64_advisory()` in `core/detector.py` tells the user that
re-exporting the source as Float32 halves the pixel data and, measured,
brings the finished file down to about a third of its size, and what the
worst-case rounding of doing so would be for their specific file.

That saving was measured 2026-11-13 by taking the route the advisory
recommends, both legs through the plugin on 'Analysis': the Hamilton
Float64 DSM produced a 1,093,746,390-byte output, and the same DSM
translated to Float32 first produced 375,248,930 - 34.3%, a 66% saving,
against a largest height change of 0.00381470 mm, exactly the bound the
advisory predicted. `testdata/dsm_float64_pdal.tif` gives 34.8% by the
same route. The string said "would halve the base image size" until
those measurements existed, which understated the saving by nearly a
factor of two and named a quantity (the base image) that nobody sees in
a folder listing. It now states the halving as the arithmetic certainty
it is and the third as the measurement it is, and says "about a third"
rather than "34.3%" because two files cannot carry a decimal place. On `dsm_float64_pdal.tif` that advice is worth more than the predictor
question either way: Float32 brings this tool's own output from
34,210,554 to 11,893,330 bytes, 34.8%.

The rounding figure is `2^(floor(log2(max_abs_valid)) - 24)` metres,
reported in millimetres. That is half the spacing between representable
Float32 values in the binade of the file's largest absolute valid value -
Float32's significand is 24 bits, so the spacing at magnitude 2^e is
2^(e-23) and round-to-nearest is out by at most half of it. Taking the
exponent from the file's *largest* value makes it a worst case for the
whole raster, since that is where Float32's spacing is coarsest.
Confirmed as a true and tight bound on `dsm_float64_pdal.tif`: predicted
0.00095367 mm, largest rounding actually measured across 7,841,625 valid
pixels 0.00094604 mm.

The advisory is resolved in `detect()`, never in
`detect_metadata_only()` - it needs an exact `ComputeRasterMinMax`, and
that function's contract is that it reads no pixels so the QGIS dialog
can refuse out-of-scope files instantly. It is deliberately NOT folded
into `needs_pixel_sampling`, which means "classification is not resolved
yet": the advisory resolves nothing and gates nothing. `ComputeRasterMinMax`
rather than `GetStatistics` because the latter persists to PAM, which
would write a sidecar next to a source file this tool promises never to
modify; exact rather than approximate because the figure is a power of
two and one binade out is a factor of two wrong in a number shown to the
user as a fact about their file.

---

## Storing Float64 elevation as Float32 is opt-in, and verified

**Status:** implemented, 2026-11-13, in the same release that added
Float64 support. The advisory added alongside that support told users
what re-exporting as Float32 would save; this does it for them, on
request only.

**Why opt-in and not automatic.** Every other setting in this tool
either preserves pixel values or asks about them explicitly
("Viewing"). This one rounds heights on a file the tool otherwise
guarantees exactly, so the default has to be the one where nothing is
rounded: an opt-in nobody ticks costs disk space, an opt-out somebody
misses costs them precision they were never asked about. The whole
trade-off therefore sits in the checkbox label rather than in a tooltip
- "Store Float64 elevation as Float32 (about a third of the size;
heights change by a fraction of a millimetre)" - because the cost is
the informed half of informed consent.

**Scope is narrow on purpose.** Single-band Float64 detected as
elevation, and nothing else. A multi-band Float64 raster could hold
anything, and "a height's worth-keeping precision" is the only
judgement that makes the rounding safe to offer, so the option is not
extended to data where that judgement does not apply. Out-of-scope
input is not refused - the box is ignored, one log line says why, and
the run continues, so a batch mixing file types still processes.

**Two refusals, both measured rather than assumed.** The bound is the
same half-ULP arithmetic as the advisory, `2^(floor(log2(
max_abs_valid)) - 24)`, and the type is kept at Float64 when it exceeds
1 mm or when the NoData value would not survive the cast.

**The 1 mm threshold first applies at a height of 32,768.** That is the
number to remember, and it is not the number it looks like it should
be. The bound steps by binade rather than smoothly, so:

| Largest absolute value | Exponent | Half-ULP bound | Verdict |
|---|---|---|---|
| up to 8,191 | <= 12 | <= 0.24 mm | stored as Float32 |
| 8,192 - 16,383 | 13 | 0.49 mm | stored as Float32 |
| 16,384 - 32,767 | 14 | 0.98 mm | stored as Float32 |
| **32,768 - 65,535** | **15** | **1.95 mm** | **kept at Float64** |
| 65,536 and above | >= 16 | >= 3.91 mm | kept at Float64 |

So every real elevation dataset on Earth passes - Everest is 8,848 m,
bound 0.49 mm - and the gate engages only on magnitudes no elevation
raster should hold. That is the point of it: it catches a Float64 raster
that is not really elevation, before its values are quantised.

The specification this was built from expected heights "above 16,384"
to be kept at Float64. That corresponds to the FULL ULP, `2^(e-23)`,
not the half-ULP `2^(e-24)` the advisory maths uses and which the same
specification said to reuse. The half-ULP rule was kept exactly as
implemented, per instruction, and the discrepancy is recorded here
rather than split the difference. Confirmed by test: 16,385 is stored
as Float32 at a 0.98 mm bound, 40,000 is kept at Float64. Moving the
gate to the full ULP, if that is ever wanted, is a one-line change to
`FLOAT32_MAX_BOUND_METRES` or to the shift - not a redesign.

The NoData refusal exists for a real sentinel, not a hypothetical one:
-1.7976931348623157e308, Float64's most negative finite value, is a
defensible choice of "impossible" and casts to -inf. Every NoData pixel
would be written as -inf while the header still declared
-1.797...e308, and nothing in the file would read as NoData any more.
NaN counts as round-tripping, since it casts to NaN and a NoData marker
only has to go on marking the same pixels.

**The bound is verified, not asserted.** This is the only thing the
tool does that changes pixel values on purpose, and what it promises
the user is a number, so `_verify_type_change()` reads every valid
pixel of the output against the source in row blocks and fails the run
if the measured maximum exceeds the bound or if any pixel changed
between data and NoData in either direction. Measured at about 7s for
256 million pixels, only on the ticked path.

**A failed check renames the output rather than leaving it.** This is
the one verification failure in the tool that leaves behind a file
which actively misreports itself, so it is the one that gets the file
moved: `_quarantine_failed_output()` renames it to `<output>.failed`
and the error names the new path.

The reasoning is a narrow distinction and worth keeping straight. Every
other verification failure - wrong block size, wrong compression, not a
valid COG - leaves a file whose pixel values are still exactly the
source's and whose embedded metadata claims nothing that has been
disproved. It is structurally disappointing, not misleading, and this
module's existing stance applies: leave it, say so, let the user
decide, because "silently losing data is worse than leaving an orphan
they've been told about". A failed type change is different in kind.
That file holds deliberately altered values AND states the worst-case
height change inside itself, in `GEOKLEIN_4_DECISION`, as a measured
fact - which is precisely the claim the check just refuted. Left at a
`.tif` path, it reads as an authoritative record of a bound it does not
honour, and nothing a later reader opens it with would say otherwise.
The suffix is the only thing that travels with the file.

Renamed rather than deleted for the same reason the Translate failure
path keeps its partial output: the user may want to inspect it, and
this module deleting a multi-gigabyte file on its own judgement is the
more expensive mistake. An existing `.failed` from an earlier attempt
is removed first - it was already established as invalid, and refusing
to overwrite it would leave the newer invalid file sitting at the
valid-looking path, which is the whole thing being prevented. If the
rename itself fails (a lock, a permissions problem), the file stays
where it is and both the message and a warning say so plainly and tell
the user to delete it by hand; reporting a move that did not happen
would be worse than either.

Verified by forcing real breaches rather than stubbing the check: the
bound on the assessment was shrunk from the test so the production
comparison genuinely exceeded it, and the mask check was pointed at a
different source so valid/NoData positions genuinely disagreed. In both
cases the `.tif` was gone, the `.failed` file was a readable Float32
raster of full size still carrying its disproved claim, and
`result.output_path` pointed at the renamed file. With the rename
blocked, the message switched to "could not be renamed out of the way"
and the warning told the user to remove it. No plugin source was
modified to run any of that.

**An ordering constraint worth knowing about.** The metadata has to
record the measured maximum, and metadata is passed as `-mo` arguments
INTO the Translate call that creates the file - writing it afterwards
with `SetMetadataItem()` moves the main IFD past the pixel data and
breaks COG validity, confirmed directly and recorded under "v1 always
writes Cloud Optimized GeoTIFFs". So the figure has to exist before
Translate runs. It does, because `-ot Float32` is a deterministic
round-to-nearest of each value independently: `_measure_float32_cast()`
casts the source's values in memory and gets exactly the number the
output will hold. The post-write verification then confirms the file
against that prediction, and fails the run if they disagree - at which
point the metadata would be describing a file that does not exist.

**Measured, on the 16000x16000 Hamilton Smeaton LiDAR DSM:**

| | Float64 | Float32 |
|---|---|---|
| plugin output | 1,093,746,390 | 375,248,984 |
| share of the Float64 output | 100% | 34.3% |
| guaranteed bound | - | 0.0038 mm |
| largest change measured across 153,589,056 valid pixels | - | 0.0038 mm (100% of the bound) |
| worst-case volume across the whole raster | - | 0.37 m3 |

The bound being reached exactly is expected on a file this large: with
153 million values, some of them land as far from a representable
Float32 as it is possible to land.

---

## Performance knobs deliberately left unset

**Status:** investigated, 2026-09-03. Two GDAL performance settings were
weighed against the 170-510 MP knee documented in "Progress bar sits
near 10%" above. Neither is used.

### `GDAL_CACHEMAX` is not set

The plugin never calls `gdal.SetCacheMax()` and never sets the
`GDAL_CACHEMAX` config option, so GDAL's block cache stays at whatever
the process inherits. GDAL's own default is 5% of physical RAM; inside a
QGIS process the effective value can differ, and would have to be read
at runtime with `gdal.GetCacheMax()` to be known rather than assumed.

Raising it for the duration of the Translate call was considered and
rejected:

- **It can only help one of the knee's three candidate causes.** The
  step between 170 and 510 MP is the full-resolution working set
  crossing a memory boundary, but that boundary could be (a)
  block-cache-bound re-reads during the COG full-resolution pass and the
  overview read-back, (b) the first-time decode of the compressed
  source, or (c) OS page-cache pressure. A larger GDAL block cache helps
  only (a). It cannot help (b) - a first read is never already in cache
  - and for (c) it makes things worse, because the block cache then
  competes with the OS page cache for the same RAM and can move the knee
  the wrong way.
- **It is process-global in a long-lived session.** `SetCacheMax()`
  changes the cache for the whole QGIS process, and layer rendering on
  worker threads shares that same block cache. Raising it around one
  Translate changes cache pressure for concurrent rendering; restoring
  it afterwards evicts blocks other layers were using and forces them to
  re-read. Transient and non-corrupting, but a real side effect that a
  genuinely Translate-scoped knob would not have.
- **A naive save-and-restore is unsafe.** The classic
  `gdal.GetCacheMax()` binding returns a 32-bit int and overflows above
  2 GiB, so reading the current value in order to restore it later
  corrupts the value it means to preserve whenever the baseline is
  already large. `GetCacheMaxAsInt64()` avoids that, but the obvious
  implementation is wrong.

Not worth the side effects for a gain that is unconfirmed and, on two of
the three candidate causes, absent or negative.

### `NUM_THREADS` covers the overview build

`NUM_THREADS=ALL_CPUS` is set once in each profile's `creation_options`
(`core/detector.py`'s `RECOMMENDED_SETTINGS`). It covers base-image tile
compression for certain. Whether it also covers the overview build rests
on the COG driver reusing the setting internally.

The GDAL COG driver documentation answers this with no measurement
needed. The `NUM_THREADS` creation option is described - verbatim in
both the release-3.9 and the current docs - as: "Enable multi-threaded
compression by specifying the number of worker threads. Default is
compression in the main thread. This also determines the number of
threads used when reprojection is done with the TILING_SCHEME or
TARGET_SRS creation options. (Overview generation is also multithreaded
since GDAL 3.2)". That parenthetical is present continuously across the
3.8 to 3.13 range, so on the bundled GDAL 3.13.2 the single
`NUM_THREADS` creation option this plugin sets is documented to drive
base-image compression, overview generation, and - if ever used -
reprojection.

This is the documentation, not a repository measurement: the overview
phase has not been independently timed here with and without threading.
It does close the open question, and it is worth noting that if the
overview build ever did prove serial there is no additional knob to
reach for - `NUM_THREADS` is the only control the COG driver exposes for
this - so such a finding would be a documentation note, not a fix.

---

## The file speaks in the third person

**Status:** implemented 2026-11-15, after a client-facing output was
read and found to be addressing its reader as "you".

The log and the embedded metadata have different readers, and until now
they shared their strings. The log is read by the person who pressed
Run, where "at your request" is exactly right. A `GEOKLEIN_*` value is
read by whoever opens the file - a client, a contractor, an auditor, or
the same person two years later - where "your" has no referent and
quietly asserts that the reader is the one who chose. Every metadata
value is third person now; the log keeps second person.

Only one string needed two forms, the honoured-Analysis-Float32
opening. Both come from `resolve_profile_reason()` under a
`third_person` flag rather than one being a later rewording of the
other, so they cannot drift - the same discipline the rest of this
codebase's message handling follows.

**Saying it once was the harder half.** `GEOKLEIN_4_DECISION` is the
profile reason followed by the storage paragraph, and both attributed
the change, so the file said "with heights stored as Float32 at the
user's request" and then "Stored as Float32 at the user's request"
immediately after. The paragraph can drop its lead - but only when the
opening carries the attribution, which is true of exactly one opening.
A first attempt dropped it unconditionally and left the coerced run's
metadata with no attribution at all: its opening is the Viewing
refusal, which mentions no data type. Caught by a test that counted the
attribution phrase per key and expected exactly one. The condition is
`lead=not profile_request_honoured`.

---

## The advisory stopped paying for an exact scan nobody asked for

**Status:** changed 2026-11-14, after GUI testing on QGIS 4.2.1 reported
detection taking 10 to 20 seconds on a DEFLATE-compressed, tiled,
pyramided Float64 DSM with the progress bar at 0% throughout.

**Where the time went**, measured by timing each stage rather than
inferring it, and with the data held constant by re-encoding one file
into both shapes:

| | uncompressed | DEFLATE + tiled + pyramids |
|---|---|---|
| `detect_metadata_only()` | 0.01s | 0.01s |
| `_max_abs_valid()` exact | **1.16s** | **4.09s** |
| `ComputeRasterMinMax(approx)` | 0.01s | 0.00s |
| `detect()` total | 1.36s | 4.16s |

All of it was the exact max-abs scan, and the multiplier is
decompression: identical data, 3.5x the time once every tile has to be
inflated. The bar sat at 0% because `_max_abs_valid` polled no callback,
and detection's callback only ever reached the RGB black-pixel sampler.

**What it was buying:** a per-file figure in an advisory log line, on a
file that was going to be written at Float64 regardless. That is the
whole of it. The scan ran on every Float64 input whether or not anyone
had asked for the storage option.

**The split.** Box unticked, the advisory now uses approximate
statistics and quotes no figure, saying instead that the exact figure is
worked out if you tick the box. Box ticked, the exact scan runs exactly
as before, because its figure is written into the output file and has to
be right - and it now reports progress and announces itself first.
Measured on a purpose-built 14640x14400 Float64 DEFLATE/tiled/pyramided
file, 1.82 GB, matching the reported shape:

| | detection |
|---|---|
| box unticked | **0.02s** |
| box ticked | 7.81s, 29 progress callbacks, bar 0.034 to 1.000 |

**Why approximate statistics may gate but never quote.** They read
averaged pyramid pixels, so the maximum they report is systematically
low: 115.998 against 116.014 exact on that file, and 116.39 or 115.07
against 117.46 on Hamilton depending on the pyramid set. A file whose
true maximum sits just over 32,768 - where the real 1 mm gate bites -
could report an approximate maximum well under it, and an advisory built
on that would promise a sub-millimetre rounding the gate would refuse.
So the advisory is suppressed entirely at an approximate maximum of
16,384, half the real threshold, which allows a factor of two of damping
that no averaging of real heights can exceed. Being wrong the other way
costs only a missing advisory on a file holding values above 16,384,
which is not elevation in metres.

**One thing left on the table.** The ticked path now makes three full
passes over the source: this exact scan, `_measure_float32_cast()`
before Translate, and `_verify_type_change()` after it. The first two
could be merged - max-abs and the cast delta are computable in one pass
- which would cut a third off the ticked path's pixel reading. Not done
here, because the instruction was to keep the exact scan as it is and
only wire the callback into it, and merging it is a wider change than
that. Recorded as the obvious next saving if the ticked path ever needs
to be faster.

---

## Dark-theme colours for the help panel: not done, and why

**Status:** investigated and skipped, 2026-11-13. Asked for as
"keep the current light-theme colours exactly as they are, add
dark-theme variants chosen at render time from the panel background's
lightness, use lighter versions of the same colours". Half of that is
straightforwardly available and the other half has no defined input.

What IS available: `shortHelpString()` runs when the panel renders, so
`QgsApplication.palette()` is readable from it, and
`QPalette.ColorRole.Base` gives the text panel's own background. On
this machine it read `#2d2d2d`, lightness 45 of 255, which a
`lightness() < 128` test classifies as dark correctly. A light/dark
decision at render time is therefore perfectly possible.

What is NOT available is the thing to vary. Checked directly against
the rendered string: it contains **41** `<b>` tags and **zero** colour
declarations - no `color:`, no `color=`, no `<font>`, no `style=`. This
plugin has never set a colour in the help panel. The colours are
QGIS's own, applied by the C++ help-widget stylesheet, which Python
cannot read. So "lighter versions of the same colours" has no input
here: there is no colour of ours to lighten, and no way from Python to
discover the one QGIS used in order to lighten it.

Inventing colours instead would be a different change from the one
asked for, and the wrong one: our bold text would then be styled by us
while every other element in the panel stayed styled by QGIS, which
looks inconsistent precisely on the themes it was meant to help.
Verifying it would also need pixels sampled from a rendered C++ widget,
which is not something this project can check headlessly, so the result
would be asserted rather than measured - against this codebase's
standing preference everywhere else.

Recorded here as a known limitation rather than held against the
release, per instruction. If it is wanted later, the shape that would
work is QGIS-side: a theme-aware colour for `<b>` in the help panel's
own stylesheet, which fixes it for every plugin at once rather than
one plugin overriding it locally. See the entry below, which reached
the same conclusion from the other direction before the storage option
existed.

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

---

## Deferred: forced_reason fallback names the internal token, not the user's choice

**Status:** known, deferred, 2026-09-16, found during the 1.0.1 full-read
review. `_log_profile_decision()`'s defensive fallback
(`algorithms/optimise_raster.py:1144-1149`, itself documented as "should
be unreachable in practice") raises
`QgsProcessingException(opt.reason_blocked or self.tr("The {} option is
not available for this file.").format(requested))`. Two problems, not
one: `requested` is the raw internal identifier (`"lossy"` or
`"lossless"`), spliced untranslated into an otherwise-translated
template; and even translated, it would be the wrong label regardless -
the user's actual choice is `'Analysis'` or `'Viewing'`, never "lossy"
or "lossless", so a reader would see an internal token they never
selected.

Left as-is for 1.0.1: every choice-mode `ProfileOption` is currently
`available=True`, so this branch cannot be reached today, and this
release is scoped to the plugins.qgis.org scanner fixes and the
`GEOKLEIN_1_TOOL` URL, not a general cleanup pass. Fix, when made: map
`requested` through the same `"lossy" -> 'Viewing'` / `"lossless" ->
'Analysis'` naming used by `_log_profile_decision()`'s non-fallback
branches, and translate the substituted label along with the template.
