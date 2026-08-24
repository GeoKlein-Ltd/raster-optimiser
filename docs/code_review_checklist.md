# Code review checklist

A full-read review of the plugin package, to be run after any significant
change. Every question below comes from a defect this codebase actually
produced. None of them are hypothetical, and none should be pruned as generic
advice.

This is additional to normal testing, not a replacement for it. Tests confirm
the code does what it was built to do. This finds the places where two parts of
the codebase quietly stopped agreeing with each other.

---

## When to run it

- After any round of changes that touches more than one file
- After any change to how a decision is made, named, or explained
- Before submitting a new version to plugins.qgis.org
- Any time a defect is found that a targeted search would not have caught

Do not run it in the middle of a change. Five of the six findings from the last
review rewrote the files being reviewed, which made the report stale before it
was acted on.

---

## How to run it

Give this instruction:

> Work through `docs/code_review_checklist.md`. Read every file in the plugin
> package completely rather than searching for patterns. Report findings
> section by section, with file and line. Say "clean" where you find nothing.
> Change nothing until I have seen the whole report.

The report-first rule matters. Several past findings turned out to need a
decision rather than a fix, and one turned out not to be a defect at all once
investigated.

---

## Why a full read, not a search

Two rounds of targeted auditing on this codebase both missed defects that a
full read then found. The searches were not badly written. They found what they
were told to look for, and the misses were in the same files they had already
opened.

The defects that get missed share a shape: they need two distant parts of the
codebase held in mind at once. A function whose return value stopped being used
by a call site three hundred lines away. An exception raised in one module with
no handler anywhere between there and the top. A rule implemented in two files
that drifted apart. No search term describes any of those, because the problem
is a relationship, not a string.

Anyone tempted to turn this checklist into a set of grep patterns should read
this section again first.

---

## Section 1: things written more than once

- Is any rule or decision implemented in two places rather than written once
  and imported? `_already_optimised_at_target()` was, in both the wrapper and
  `convert()`, with a comment admitting it mirrored the other.
- Is any user-facing sentence constructed at more than one call site rather
  than produced once and passed outward? The honoured-Analysis case had two
  different wordings for the same decision, one in the log and one in the
  file's metadata.
- `metadata.txt`'s `tags=` and the algorithm's `tags()` claim to be kept in
  sync, but nothing enforces it. Do they still match?

## Section 2: functions whose contract has drifted

- Any function whose return value is discarded at every call site?
  `_resolve_profile()` kept two `return` statements after its result stopped
  being read.
- Any function whose name no longer describes what it does?
- Any parameter passed but never read, or branch that can no longer be reached?
- Where a branch is genuinely unreachable now, is the comment beside it honest
  about that?

## Section 3: exceptions and failure paths

- `gdal.UseExceptions()` is on at module level in both core files. List every
  GDAL call that can raise under it, and for each say which handler catches it
  on the `processAlgorithm()` path specifically. The `checkParameterValues()`
  path has a bare `except` that hides this class of problem rather than
  solving it.
- Does every return path close its dataset, including early refusals?
- Does every failure path either remove the partial output or tell the user it
  exists and what to do with it?
- `convert()` restores prior GDAL config options in a `finally` block. If
  `GetConfigOption` returned `None` for an option that was unset, does
  `SetConfigOption(k, None)` unset it again, or leave the value set?

## Section 4: cancellation

- `_black_pixel_sample()` breaks out of its grid loop when the progress
  callback returns falsy, then computes an assessment from whatever it sampled.
  Does cancelling detection produce a partial result presented as a real one?
  What does the user see?
- For each of the three phases (detection, Translate, BuildOverviews): what is
  left on disk after a cancel, and what is the user told?

## Section 5: comparisons and matching

- Any `==` against a string GDAL reports, where GDAL might report a variant?
  GDAL returns `"YCbCr JPEG"` rather than `"JPEG"`, which nearly made the
  JPEG-source warning dead code against this tool's own output.
- Any check testing one condition where two must both hold?
  `_has_georeferencing()` accepted a geotransform on its own and so never
  refused a file with its CRS stripped.
- Is case guarded everywhere strings are compared, or only in the one place it
  was noticed at the time?

## Section 6: numbers shown to users

- Any place two numbers are displayed that let a reader derive a third, where
  independent rounding makes that derivation come out wrong? Source size,
  output size and percentage change did exactly this, and a user checking the
  arithmetic concluded the tool was broken.
- Are all size units labelled to match the arithmetic actually used? The
  numbers were base-1024 while the labels read as decimal units.
- Any division without a zero guard?

## Section 7: edge-case inputs

- What happens on a raster smaller than `NODATA_WINDOW_SIZE`, or smaller than
  `NODATA_GRID_SIZE` cells on a side? Walk through `_cell_window()` and the
  margin calculation for a 50x50 image.
- What happens on a 1x1 raster, and on one where every pixel is NoData?
- `GEOKLEIN_7_REPRODUCE` quotes `input.tif` and `output.tif`, but `full_args`
  values are joined unquoted. Can any creation option value contain a space or
  a shell-significant character that would break the command if pasted?

## Section 8: translation consistency

- `self.tr()` is applied to some strings in `optimise_raster.py`, but the enum
  options passed as `options=PURPOSE_OPTIONS` and `options=NODATA_OPTIONS` are
  raw. List every user-facing string in that file that is not wrapped, and say
  whether each is deliberate.
- Strings in `core/detector.py` cannot be wrapped, since that module has no
  QGIS imports. Confirm that is still the only reason any of them are
  unwrapped.

---

## Keeping this current

Add a question whenever a defect is found that none of the existing questions
would have caught. Write it with the real defect attached, the way every
question above carries one. A question with no provenance reads as generic
advice and will eventually be pruned by someone who does not know what it was
protecting against.

Remove a question only when the thing it guards against has become structurally
impossible, not merely fixed once.

Specific line references and function names in this file will go stale. That is
acceptable: the question is the durable part, and the example only has to be
recognisable enough to explain what the question is for.
