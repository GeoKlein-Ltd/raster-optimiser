# Code review checklist

A full-read review of the plugin package, to be run after any significant
change. Every question below comes from a defect this codebase actually
produced. None of them are hypothetical, and none should be pruned as generic
advice.

This is additional to normal testing, not a replacement for it. Tests confirm
the code does what it was built to do. This finds the places where two parts of
the codebase quietly stopped agreeing with each other.

The examples attached to each question are historical. Most describe defects
that have since been fixed, and they are kept because they explain what the
question is protecting against, not because they are still present.

---

## When to run it

- After any round of changes that touches more than one file
- After any change to how a decision is made, named, or explained
- Before submitting a new version to plugins.qgis.org
- After a GDAL or QGIS major version upgrade
- Any time a defect is found that a targeted search would not have caught

Do not run it in the middle of a change. One earlier review produced six
findings, five of which rewrote the files being reviewed, which made the report
stale before it could be fully acted on.

---

## How to run it

Give this instruction:

> Work through `docs/code_review_checklist.md`. Read every file in the plugin
> package completely rather than searching for patterns. Report findings
> section by section, with file and line. Say "clean" where you find nothing.
> Change nothing until I have seen the whole report.

The report-first rule matters and has earned itself twice. Two findings that
looked like live bugs turned out, on investigation, to be clean: one was
version-specific behaviour that did not reproduce on the shipped GDAL, and one
was already handled correctly by code elsewhere. Both would have been "fixed"
into unnecessary complexity if the instruction had been to fix rather than
report.

---

## Why a full read, not a search

Two rounds of targeted auditing on this codebase both missed defects that a
full read then found. The searches were not badly written. They found what they
were told to look for, and the misses were in the same files they had already
opened.

The defects that get missed share a shape: they need two distant parts of the
codebase held in mind at once. A function whose return value stopped being used
by a call site three hundred lines away. A rule implemented in three places
that drifted apart. A comment that contradicts another comment eighty lines
below it in the same function. No search term describes any of those, because
the problem is a relationship, not a string.

Anyone tempted to turn this checklist into a set of grep patterns should read
this section again first.

---

## Section 1: comments and docstrings that no longer match the code

The largest single category of defect found in this codebase, and the one most
likely to recur. Seven were found across two reviews. Every one described
behaviour that existed before a design change, which means comments go stale in
the same rounds the design changes, and the files most worth checking are the
ones just edited.

- Does every module docstring still describe what its module does? One claimed
  the plugin called `execAlgorithmDialog()` well after that was replaced.
- Does any comment describe a gate, block, or refusal that has since become a
  log message or a coercion? Four separate comments did.
- Does any comment contradict another comment in the same file? One listed
  three checks in a function that had only one left, contradicting a comment
  eighty lines below it.
- Do any user-facing strings outside the UI layer name options that no longer
  exist? A CLI debug printer named "Clear" and "Keep" long after the dropdown
  labels became Automatic, Reveal hidden pixels, and Keep as-is.
- Where a comment says a branch is defensive or unreachable, is that still
  true?
- Does each message's stated reason survive the condition that produces it?
  Read the guard, then the message, and check the message does not offer a
  cause the guard has already excluded. Three separate strings offered
  "switch from Analysis to Viewing" as a reason to tick Reprocess, which
  `already_optimised_at_target()` can never be true for.
- When a user-facing string changes, which prose files describe that
  behaviour? `docs/raster_optimiser_ui_text.md` is meant to be kept in sync
  code-first, but "kept in sync" is a discipline, not a guarantee: the same
  review that added this question found a gap in that file too (the
  End-of-run summary format block listed two consequential cases where
  `resolve_profile_reason()` produces three - fixed 2026-08-28). Check it
  along with the others, not instead of them. `plugin_design_notes.md` and
  `README.md` are not covered by that discipline at all, and both retained
  claims the code had already dropped.
- When a behavioural rule is stated in prose or user-facing text, enumerate
  every place it appears *before* changing any of them, and fix the whole
  set in one pass. Fixing the locations one session happens to open, and
  trusting a later search to surface the rest, is how a rule ends up stated
  several different ways at once. The four-condition "already optimised"
  rule - a file is left alone only when it is a valid COG *and* tiled *and*
  has pyramids *and* is at the target compression - was corrected in five
  separate places across three sessions: `_already_optimised_message()`,
  `shortHelpString()`'s Reprocess paragraph, `FORCE_REPROCESS`'s
  `setHelp()`, `metadata.txt`'s `about=`, and `README.md`'s intro. Each
  session fixed only what it had loaded and left the others asserting three
  conditions. The full set for a claim like this is: the string constants
  and helper methods that build the message, every parameter `setHelp()`
  and `shortHelpString()`, `metadata.txt` (`description=` and `about=`),
  `README.md`, `docs/raster_optimiser_ui_text.md`, and
  `plugin_design_notes.md`. A `grep` for one distinctive phrase from the
  claim, run across all of those before editing, is the cheap version of
  this - the expensive version is the three sessions it actually took.
- Does any comment or docstring assert something about every version of this
  plugin that has ever run, rather than every version in this repository? The
  git history is not a complete record of that: a build predating `git init`
  wrote `GEOKLEIN_SUMMARY`, a key no commit in this repository contains, and
  files carrying it still exist. A claim about what past builds did or did not
  write needs to be scoped explicitly to the repository, or defended against
  files the repository has no record of, not stated as a fact about every
  build that has ever run. Found 2026-09-01 in `_build_decision_metadata()`'s
  own docstring, which claimed "there has never been an eighth key or a
  differently-named one" - true of every commit, confirmed directly against
  the full git history, and false of at least one real file; fixed by scoping
  the claim to what the repository's history can actually attest to, and
  adding a scan for exactly the case the old claim had ruled out.

Note that a targeted audit for stale comments found five and missed a sixth in
a file it had already opened twice. This section needs the full read as much as
any other.

## Section 2: things written more than once

- Is any rule or decision implemented in more than one place rather than
  written once and imported? The already-optimised-at-target rule was written
  three times: once in the wrapper, once as a helper, and once inline inside
  `convert()`.
- Is any user-facing sentence constructed at more than one call site rather
  than produced once and passed outward? The honoured-Analysis case had two
  different wordings for the same decision, one in the log and one in the
  file's metadata.
- `metadata.txt`'s `tags=` and the algorithm's `tags()` claim to be kept in
  sync, but nothing enforces it. Do they still match?

## Section 3: functions whose contract has drifted

- Any function whose return value is discarded at every call site?
  `_resolve_profile()` kept two `return` statements after its result stopped
  being read, and its name outlived what it did.
- Any function whose name no longer describes what it does?
- Any parameter passed but never read, or branch that can no longer be reached?
- Any import left behind after the code that used it moved elsewhere?

## Section 4: exceptions and failure paths

- `gdal.UseExceptions()` is on at module level in both core files. List every
  GDAL call that can raise under it, and for each say which handler catches it
  on the `processAlgorithm()` path specifically. The `checkParameterValues()`
  path has a bare `except` that hides this class of problem rather than solving
  it.
  - `GetGeoTransform()` was checked against GDAL 3.13.2 in August 2026 and does
    not raise when no geotransform is set: GDAL reports no error at all, so
    there is nothing for `UseExceptions()` to escalate. Older and other
    versions have returned `CE_Failure` here. Re-check after any GDAL upgrade.
    No guard was added, deliberately: a guard against something that cannot
    currently happen becomes a comment nobody can verify.
  - Known open item, 2026-08-24: the broader point above is still true beyond
    `GetGeoTransform()`. Every other GDAL call after `gdal.Open()` in
    `detect()`'s pipeline - `GetSpatialRef()`, `GetRasterBand()`,
    `GetBlockSize()`, `GetOverviewCount()`, `GetColorTable()`, and, once pixel
    sampling starts, `ReadAsArray()` inside `_classified_unique_count()` and
    `_black_pixel_sample()` - sits inside a bare `try/finally`, not
    `try/except`, and `processAlgorithm()` wraps none of it. `_verify()`'s
    post-conversion reopen in `convert()` has the same gap, unguarded at both
    ends. Unlike `GetGeoTransform()`, none of these have been individually
    checked against the shipped GDAL - this is a known, deliberately deferred
    gap, not a confirmed-clean one, so do not treat its absence from a future
    report as new information.
- Does every return path close its dataset, including early refusals?
- Does every failure path either remove the partial output or tell the user it
  exists and what to do with it?
- Does `convert()` set any global GDAL state (`gdal.SetConfigOption()` or
  similar) that needs restoring afterward? Checked 2026-08-28: no - it does
  not any more. The pre-COG version set overview compression/photometric
  config options globally around a separate `BuildOverviews()` call and
  restored them in a `finally` block afterward; the COG rework replaced
  that with one `Translate()` call using `-co` creation options scoped to
  that call alone, so there is nothing global left to restore. Re-check if
  global config options are ever reintroduced - the original form of this
  question asked whether `SetConfigOption(k, None)` genuinely unsets a
  value that `GetConfigOption` returned `None` for, which would need
  re-verifying then, not assumed from this note.

## Section 5: cancellation

- `_black_pixel_sample()` breaks out of its grid loop when the progress
  callback returns falsy, then computes an assessment from whatever it sampled.
  Does cancelling detection produce a partial result presented as a real one?
  What does the user see?
  - Fixed, 2026-08-24: the stats dict now carries a `cancelled` flag, forced
    onto `NoDataRisk.assessment = "insufficient_sample"` in `_detect_body()`
    rather than letting a partial read report `"meaningful"`/`"collar_only"`.
    `processAlgorithm()` also checks `feedback.isCanceled()` immediately after
    `detect()` returns and stops the run there, matching a cancelled
    Translate - previously a cancel during "Detecting raster type..." was
    never honoured and the run continued into Translate regardless.
  - Known deferred item, 2026-08-24: the fix above only covers the RGB/NoData
    path. `_detect_body()`'s CONTINUOUS branch loops `_classified_unique_count()`
    over every band and polls no callback at all, so a Cancel during a
    many-band detection (a multispectral file with many bands, in
    particular) is only caught once that loop finishes on its own -
    `processAlgorithm()`'s `isCanceled()` check still stops the run
    afterwards, so this is a latency problem, not a correctness one, and the
    files where it's noticeable are narrow. Left unbuilt on that basis.
    Design constraint worth keeping if this is ever built: a cancelled
    classified scan must leave the result unresolved
    (`needs_pixel_sampling` stays `True`) and must never be allowed to read
    as "confirmed not classified". This is unlike the NoData sample, which
    can degrade safely to `"insufficient_sample"` because nothing is gated
    on it - the classified check is a safety refusal against silent
    corruption, so a partial scan concluding "not classified" would be
    exactly the wrong kind of wrong.
- For each of the two phases (detection, Translate - the COG driver builds
  pyramids inside the same Translate call now, so there is no separate
  BuildOverviews phase left to check): what is left on disk after a cancel,
  and what is the user told?

## Section 6: comparisons and matching

- Any `==` against a string GDAL reports, where GDAL might report a variant?
  GDAL returns `"YCbCr JPEG"` rather than `"JPEG"`, which nearly made the
  JPEG-source warning dead code against this tool's own output.
- Any check testing one condition where two must both hold?
  `_has_georeferencing()` accepted a geotransform on its own and so never
  refused a file with its CRS stripped.
- Is case guarded everywhere strings are compared, or only in the one place it
  was noticed at the time?

## Section 7: numbers shown to users

- Any place two numbers are displayed that let a reader derive a third, where
  independent rounding makes that derivation come out wrong? Source size,
  output size and percentage change did exactly this, and a user checking the
  arithmetic concluded the tool was broken.
- Are all size units labelled to match the arithmetic actually used? The
  numbers were base-1024 while the labels read as decimal units.
- Any division without a zero guard?
- Does any claim about typical results still match measured results? The help
  panel claimed 15 to 30 times smaller when the measured figure was closer to
  five.

## Section 8: edge-case inputs

- What happens on a raster smaller than `NODATA_WINDOW_SIZE`, or smaller than
  `NODATA_GRID_SIZE` cells on a side? Walk through `_cell_window()` and the
  margin calculation for a 50x50 image.
- What happens on a 1x1 raster, and on one where every pixel is NoData?
- `GEOKLEIN_7_REPRODUCE` quotes `input.tif` and `output.tif`, but `full_args`
  values are joined unquoted. Can any creation option value contain a space or
  a shell-significant character that would break the command if pasted?
- Does re-running the tool on its own output behave correctly in every
  combination of purpose and NoData mode? The metadata strip-and-rewrite was
  verified this way, two generations deep. The JPEG-source warning exists
  because one such combination produced a file 4.4 times larger with no gain
  in accuracy.

## Section 9: translation consistency

- `self.tr()` needs a live algorithm instance to resolve against - any
  user-facing string built at module level, before an `OptimiseRasterAlgorithm`
  instance exists, can never reach it. List every user-facing string in
  `optimise_raster.py`, say whether it is built inside an instance method or
  at module scope, and say whether that placement is deliberate. First
  audited 2026-08-28: both dropdowns' option lists (`PURPOSE_OPTIONS` and
  `NODATA_OPTIONS`) were module-level constants for exactly this reason, so
  both were untranslatable outright - fixed by building the translated list
  inside `initAlgorithm()` instead, where it is resolved fresh on every
  instance rather than once at import.
- Where a template is wrapped in `self.tr()` and a value is spliced into it
  afterward with `.format()`, is the spliced value translated too? Both
  halves can look correct read on their own - the template's `self.tr()`
  call is fine, and the constant is fine wherever it's translated at its
  own point of use - and still be wrong together: an untranslated splice
  leaves the surrounding sentence in the target language but the spliced
  text in English, and where that text names a widget the user has to go
  find, the sentence points at something that is not there. Found
  2026-08-28 in `_already_optimised_message()` and the NoData parameter's
  `setHelp()`, both of which spliced a raw label constant in after
  translating the sentence around it; fixed by translating the constant at
  the splice site too, not only at its other point of use.
- Strings in `core/detector.py` and `core/converter.py` cannot be wrapped,
  since neither module imports QGIS. Confirm that is still the only reason any
  of them are unwrapped.

---

## Before a release

What must be true before `raster_optimiser.*.zip` gets uploaded to
plugins.qgis.org. Nothing here is enforced by the build - check it by hand
before every submission. (Folded in from `pre_submission_checklist.md` on
2026-08-25, once most of its items were done: the repo is public and the
placeholder URLs are filled in.)

- [x] `GEOKLEIN_1_TOOL`'s URL: settled 2026-09-03. The GitHub repository
  (`https://github.com/GeoKlein-Ltd/raster-optimiser`) is the plugin's
  canonical home and is embedded as-is; the earlier "(placeholder until the
  plugins.qgis.org listing exists)" note has been removed. A plugins.qgis.org
  listing URL, if one is ever wanted, belongs *alongside* the repository URL
  in `_build_decision_metadata()`, not as a replacement for it. Files
  produced before this change still carry the old parenthetical in their
  embedded metadata - fixable only going forward, not in files already
  handed to a client.
- [ ] A changelog entry has been added to `metadata.txt` for this release.
- [ ] The zip has been rebuilt (`python tools/build_zip.py`) after either of
  the above.

---

## Keeping this current

Add a question whenever a defect is found that none of the existing questions
would have caught. Write it with the real defect attached, the way every
question above carries one. A question with no provenance reads as generic
advice and will eventually be pruned by someone who does not know what it was
protecting against.

Where a question has been investigated and found clean, record that inline with
the date and the version it was checked against, as Section 4 does for
`GetGeoTransform()`. Clean is often version-specific rather than permanent.

Remove a question only when the thing it guards against has become structurally
impossible, not merely fixed once.

Specific line references and function names in this file will go stale. That is
acceptable: the question is the durable part, and the example only has to be
recognisable enough to explain what the question is for.
