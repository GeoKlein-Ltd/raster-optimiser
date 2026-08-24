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
why `resolve_profile_reason()` needed a second, separate `consequential`
return value alongside `honoured`: this case is honoured (Analysis was
requested and Analysis is exactly what ran) and consequential at the
same time, a combination no purely nature-based rule ever produces.

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
