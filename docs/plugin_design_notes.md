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
