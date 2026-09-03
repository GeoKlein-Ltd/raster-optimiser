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
