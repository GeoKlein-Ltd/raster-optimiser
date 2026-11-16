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

- [x] `GEOKLEIN_1_TOOL`'s URL: settled 2026-09-03, updated 2026-09-16 now
  that a plugins.qgis.org listing exists (1.0.0 went live there). The string
  now carries both, labelled: `Source: https://github.com/GeoKlein-Ltd/
  raster-optimiser. Listing: https://plugins.qgis.org/plugins/raster_optimiser/`
  - the plugins.qgis.org URL was added *alongside* the repository URL in
  `_build_decision_metadata()`, not as a replacement, per the 2026-09-03
  decision recorded here. Files produced before this change still carry only
  the GitHub URL in their embedded metadata - fixable only going forward, not
  in files already handed to a client.
- [x] A changelog entry has been added to `metadata.txt` for this release:
  1.1.2, added 2026-11-16 (the help panel's description of the Float32 option
  corrected to match how 1.1 works; no change to processing). 1.1.1 was added
  2026-11-16 (the Bandit B112 that blocked the 1.1.0 upload), 1.1.0 on
  2026-11-13 (Float64 elevation support, the Float64 advisory, the CLI
  log-callback crash and the dropped detection warnings), 1.0.1 on
  2026-09-16.
- [x] The zip has been rebuilt (`python tools/build_zip.py`) after either of
  the above: `raster_optimiser-1.1.2.zip`, 15 files, 135.8 KB, built
  2026-11-16 from a clean tree at the tagged commit. Verified its internal
  layout is a single top-level `raster_optimiser/` folder and that the
  `metadata.txt` inside it reads `version=1.1.2`. Also verified entry by
  entry against `git show v1.1.2:<path>`: all 15 identical once line endings
  are normalised, which is the comparison that matters, because git stores
  text as LF while the build copies working-tree CRLF. The superseded 1.0.1,
  1.1.0 and 1.1.1 zips are still in the repo root; all are gitignored, so
  none is tracked.
- [x] **Bandit scans clean.** `plugins.qgis.org` runs Bandit on every
  upload and **blocks on any finding, at any severity** - a Low is enough
  to reject the package. Run it before building the zip:

  ```
  pip install bandit
  bandit -r raster_optimiser
  ```

  Every severity must read 0. This is not advisory: 1.1.0 was rejected on
  a single Low/High-confidence **B112 (try, except, continue)** in
  `core/detector.py`, which had been sitting there behind a `# noqa:
  BLE001` comment that satisfied the linter and meant nothing to Bandit.

  Two things learned from that rejection, worth not rediscovering:

  - **Do not reach for `# nosec`.** The site may ignore it, and a
    suppression comment is not a fix. Restructure instead.
  - **A typed exception is not reliably enough.** Measured against
    Bandit 1.9.4: B112 and B110 flag `except Exception:` with a bare
    `continue`/`pass` body under default settings, and flag a TYPED
    `except RuntimeError: continue` as well once `check_typed_exception`
    is enabled in the Bandit config. The repository's own configuration
    is not visible from here, so the shape to write is one that neither
    setting flags: put the `except` in a small helper whose body is a
    `return None`, and have the loop test `if ... is None: continue`.
    That is what `_band_min_max_or_none()` in `core/detector.py` is, and
    its docstring records the measurement.

  Scan the package, not just the files just edited - and note that
  `tools/` is not in the zip, so a finding there does not block an
  upload, though it is still worth fixing.

  Last run 2026-11-16 for 1.1.2: zero findings at every severity, with no
  `#nosec` markers anywhere, on all three of the package (4,087 lines), the
  extracted zip - which is what the site actually scans - and the whole
  repository including `tools/`. Re-run with `check_typed_exception: true`,
  stricter than the default, and clean there too.

- [x] `README.md`'s install section names the zip by filename, so it has to
  follow every version bump. Reads `raster_optimiser-1.1.2.zip` as of
  2026-11-16. Updated to `raster_optimiser-1.1.0.zip` in the
  same round. Checked 2026-11-13 by grepping every version string in the
  repo: `README.md` is the only one that has to FOLLOW a bump. No code
  hardcodes a version - `GEOKLEIN_1_TOOL` reads it from `metadata.txt` at
  runtime via `_read_plugin_version()`. The rest are deliberately fixed to
  a past version and must NOT be updated: `plugin_design_notes.md`'s dated
  "found during the 1.0.1 full-read" entries, and
  `raster_optimiser_ui_text.md`'s worked `GEOKLEIN_*` transcripts, which
  are now labelled with the version they were captured on (see the next
  box) rather than implying they came from the current build.

---

## Section 10: what a failure leaves behind

- When a check fails, what is left on disk, and does it describe itself
  honestly? Found 2026-11-13: the data-type-change verification set
  `passed = False` and raised correctly, and left the output sitting at
  its ordinary `.tif` path with `GEOKLEIN_4_DECISION` inside it stating
  a worst-case height change as a measured fact - the exact claim the
  check had just refuted. The run reported failure; the file did not.
  Anyone who opened it later, or found it in a folder a week on, had
  nothing to tell them it was the rejected one. Now renamed to
  `<output>.failed` with the new path in the error.
  - The question generalises past this one case: for each failure path,
    ask not only "was the user told" but "will the artifact still say
    so after the message has scrolled away". A file that outlives the
    log needs to carry its own status.
  - Note the distinction that keeps this from becoming "delete
    everything on any failure": a file with exact pixel values and a
    structural shortfall is disappointing, and a file with altered
    values that advertises a bound it broke is misleading. Only the
    second needs moving. Getting that backwards would mean discarding
    recoverable outputs over a block-size mismatch.

## Section 11: callbacks and the values they are passed

- When a function documents that a callback parameter can be `None`, does
  every implementation of that callback actually handle `None`? Found
  2026-11-13: `convert()`'s docstring states plainly that `log_cb` is
  called as `log_cb("verify", None)`, and the QGIS wrapper's
  implementation handles it correctly - but `core/converter.py`'s own CLI
  passed a one-line lambda that formatted the value with `":.1f"`
  unconditionally, so every successful CLI run without `--quiet` ended in
  `TypeError: unsupported format string passed to NoneType.__format__`
  *after* writing and verifying a correct output file. The contract was
  documented, the caller honoured it, and the only implementation that
  did not was the one in the same file. A lambda is the warning sign
  here: it has nowhere to put a branch, so a contract with two shapes
  cannot be expressed in one.
- For each list or dict a core module populates for the caller's benefit,
  name the call site that reads it. Found 2026-11-13:
  `DetectionResult.warnings` had exactly one producer (`detector.py`'s
  band-dtype mismatch loop) and no consumer on any QGIS route - the
  wrapper looped over `ConversionResult.warnings`, a different object on
  a different class, which made the gap invisible to a reader checking
  that warnings were "handled". Every warning that list ever carried was
  silently dropped for GUI, batch, model and `processing.run()` users
  alike. Two near-identical attribute names on two result classes, one
  surfaced and one not, is the shape to look for.

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
