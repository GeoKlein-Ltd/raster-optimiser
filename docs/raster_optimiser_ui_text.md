# Raster Optimiser UI text

The user-facing strings as shipped in the code - this doc is kept in sync with `algorithms/optimise_raster.py`, `core/converter.py`, and `core/detector.py`, not the other way round. If you're changing wording, change the code first and update this file to match, not the reverse.

`core/detector.py` holds user-facing strings too now (`forced_reason`, the Reveal-on-NoData-only messages it feeds into `core/converter.py`, `describe_detection()`): it has no QGIS imports by design (pure GDAL/Python, no QGIS dependency), so none of its strings can be wrapped in `QCoreApplication.translate()` the way `algorithms/optimise_raster.py`'s can. They're untranslatable for now, not an oversight.

The NoData parameter is the three-option dropdown described below (Automatic / Reveal hidden pixels / Keep as-is), not a checkbox.

---

## shortDescription() (toolbox hover tooltip)

> Makes slow, oversized rasters load and pan quickly in QGIS or any other GDAL-based software.

---

## displayName()

> Optimise raster

---

## shortHelpString()

QGIS renders this as HTML. Section headings are plain `<p>` text, not `<b>`: QGIS's own help-panel template applies a fixed, non-theme-aware colour to bold text that reads as dark grey on dark grey in the Night Mapping theme (and likely other dark themes), and no single inline colour can fix it (it can't satisfy WCAG contrast against both a near-white default theme and Night Mapping's `#535353` background at once). Lists use real `<ul>`/`<li>` tags. Headings are shown in **bold** below purely as a doc-formatting convention to mark them as headings, it doesn't mean the code wraps them in `<b>`.

---

**What this does**

Makes large rasters load and pan quickly in QGIS or any other GDAL-based software, and reduces their file size.

**Why your file is slow**

Most orthomosaics and elevation rasters come out of processing software missing two things GDAL needs in order to draw them quickly:

- **Pyramids**, also called overviews: pre-built smaller copies of the image. Without them, QGIS has to read every pixel in the file just to draw a zoomed-out view. On a 470-megapixel ortho, that's the entire file, on every pan and every zoom.
- **Tiling**: storing pixels as small squares rather than full-width rows, so software can read one part of the image without touching the rest.

This tool adds both, and compresses the file sensibly on the way through.

**What will you use this file for**

*Analysis*: keeps every pixel value exactly as it is. Use it for anything you extract numbers from: vegetation indices, crown segmentation, classification, change detection. Elevation data always uses this, whatever you select.

*Viewing*: produces a much smaller file, by discarding detail the eye won't notice. A typical drone orthomosaic came out around 80% smaller. Still a GeoTIFF either way, never a .jpg file. Use it for basemaps, client copies, QField backdrops and site context.

Both load and pan at the same speed. The choice only affects file size and whether pixel values survive unchanged.

Some files can't be compressed for viewing without changing the numbers they hold, or without losing their transparent collar. Elevation and 16-bit or multispectral imagery are always in that group, whatever they contain. An 8-bit RGB file can be too, if its collar is marked by a NoData value rather than a real alpha band, since compressing for viewing shifts pixel values slightly and a NoData-marked collar doesn't survive that the way a real alpha band does. Where any of this applies, the tool preserves the values instead, and records what it did in the log and in the file itself.

**What it won't process**

Some rasters can't be optimised safely with these settings, so the tool detects them and stops rather than producing something quietly wrong:

- **Classified rasters**: land cover, species class, or any map where pixel values are category codes rather than measurements. Building pyramids averages neighbouring pixels, and averaging two categories produces a third that doesn't exist.
- **Files with no coordinate reference system.**

Your source file is never modified. The tool always writes a new file. (No longer `<b>` in code either, for the same reason as the section headings above.)

**Glossary**

- **NoData**: a pixel value the file declares to mean "nothing here". Safe on elevation data, where you can pick a value no real height could ever be, such as -9999. Risky on 8-bit imagery, where every value from 0 to 255 is a legitimate colour and 0 is simply black.
- **Alpha band**: an extra band recording which pixels fall inside the surveyed area. It works by position rather than by value, so it never mistakes a black pixel for an empty one.
- **Collar**: the transparent border around a survey area, where the image doesn't fill the rectangular file.
- **Pyramids / overviews**: pre-built smaller copies of the image at successive zoom levels.
- **Tiling**: storing the image as small squares instead of full-width rows.

**(closing block, separated by a horizontal rule)**

Made by GeoKlein Ltd, Edinburgh. Built on GDAL.

Report problems: tracker link to be added once the repository is public.

---

## Parameter labels and help text

Order follows `initAlgorithm()`: Main (Input layer, What will you use this file for, Optimised raster) then Advanced (Hidden pixels (NoData), Reprocess even if already optimised, Replace existing output file).

### Input layer

Label: **Input layer**

Help:
> The raster to optimise. Any format GDAL can read. The output is always a GeoTIFF.

---

### What will you use this file for

Label: **What will you use this file for**

Options:
1. **Analysis: every pixel value preserved**
2. **Viewing: smallest possible file**

Help:
> Analysis keeps every pixel value exactly as it is. Use it for anything you extract numbers from: vegetation indices, crown segmentation, classification, change detection.
>
> Viewing produces a much smaller file, by discarding detail the eye will not notice. A typical drone orthomosaic came out around 80% smaller. Use it for basemaps, client copies, QField backdrops and site context.
>
> Both load and pan at the same speed. Both are always written as GeoTIFF, never a .jpg file.
>
> Some files cannot be compressed for viewing without changing the numbers they hold, or without losing their transparent collar. Elevation and 16-bit or multispectral imagery are always in that group, whatever they contain. An 8-bit RGB file can be too, if its collar is marked by a NoData value rather than a real alpha band, since compressing for viewing shifts pixel values slightly and a NoData-marked collar doesn't survive that the way a real alpha band does. Where any of this applies, the tool preserves the values instead, and records what it did in the log and in the file itself.
>
> If you are not sure, choose Analysis. It costs disk space and nothing else.

---

### Optimised raster (Output)

Label: **Optimised raster**

Help:
> Where to save the result. Always written as a GeoTIFF, tiled with pyramids built in.

---

### Hidden pixels (NoData)

Advanced.

Label: **Hidden pixels (NoData)**

Options:
1. **Automatic: decide per file (recommended)**
2. **Reveal hidden pixels**
3. **Keep as-is**

Help:
> Many orthomosaics mark transparency using a NoData value of 0. On 8-bit imagery that's unsafe, because 0 is also the value of a genuinely black pixel, so deep shadow, dark water and wet tarmac get treated as empty and punched out as holes.
>
> **Automatic** checks each file and only clears NoData when real content is hidden behind it. Files where NoData marks nothing but the transparent collar are left alone.
>
> **Reveal hidden pixels** always clears NoData. Hidden content comes back, but the collar may render as a solid black border rather than transparent.
>
> **Keep as-is** leaves the file's NoData setting untouched.

---

### Reprocess even if already optimised

Advanced.

Label: **Reprocess even if already optimised**

Help:
> By default, a file that's already tiled with pyramids and already at the target compression is left alone, since converting it again wouldn't make it any faster or smaller. A file that's tiled with pyramids but still on a less efficient compression is reprocessed regardless of this setting, since there's real file size to save there.
>
> Tick this to convert an already-optimal file anyway, for example to switch it from Analysis to Viewing to save disk space.

---

### Replace existing output file

Advanced.

Label: **Replace existing output file**

Help:
> Unticked, the tool stops rather than overwriting a file that already exists at the output path, and tells you what it found.
>
> Ticked, the existing file is replaced. Your source file is never modified either way.

---

## Warning messages

Exactly one thing blocks execution: reprocessing a file that's already tiled, has pyramids, and is already at the target compression. It has a real, different parameter to change (Reprocess), which is why it's the one case `checkParameterValues()` can refuse rather than merely note.

Everything else that used to block here doesn't any more:

- **Classified rasters and files with no coordinate reference system** are refused, but not by `checkParameterValues()` - they're raised as a `QgsProcessingException` at the top of `processAlgorithm()` instead. No parameter in this dialog can turn a classified raster into a non-classified one, so refusing it here would only repeat on every press of Run without a way out; raising instead produces one clean error, and lets a batch run's other files still process.
- **Viewing requested on a file that can't take it** (elevation, 16-bit/multispectral imagery, or an 8-bit RGB file with NoData-only transparency and no alpha band) no longer blocks - the tool uses Analysis settings instead and logs why. See "Log messages: what was used and why" below.
- **Reveal hidden pixels requested where it wouldn't do what you'd expect** (no NoData=0 condition at all, or a file where clearing it also affects the collar) no longer blocks either - see "Log messages: NoData handling" below.

NoData never blocked in the first place, however consequential the finding: `checkParameterValues()` can only refuse, not accept-with-acknowledgement, and every NoData choice (Automatic, Reveal hidden pixels, Keep as-is) is a legitimate one. What NoData gets instead is a prominent log message when the finding is consequential.

### File is already optimised

Blocks execution. Escapable via Reprocess. Compression-aware: only fires when the file is tiled, has pyramids, AND is already using the target compression for the profile that would be used (ZSTD for Analysis, YCbCr JPEG for Viewing). A file that's tiled with pyramids but still on a less efficient compression (LZW, DEFLATE, uncompressed) does NOT hit this, see the log entry below instead.

> This file is already tiled and has pyramids built, so it should already load and pan quickly in QGIS. It's also already using the target compression, so reprocessing wouldn't shrink it either.
>
> Converting it again won't make it any faster or smaller. It would just produce a second large file.
>
> If you're reconverting deliberately, for example to switch it from Analysis to Viewing, tick **Reprocess even if already optimised** under Advanced parameters.

---

## Log messages: what was used and why

Never a block. Each is produced once, in `core/detector.py`'s `forced_reason` (set at detection time) or `resolve_profile_reason()` (the honoured-but-worth-flagging case below), or in `core/converter.py` (the compression-not-yet-at-target case, resolved once conversion settings are known), and reused verbatim wherever it's shown: the log during the run (`feedback.pushWarning()`), the end-of-run summary, and the file's own embedded metadata (`GEOKLEIN_4_DECISION`) all read the identical text - none of them reword it.

### Viewing requested on elevation data

`detection.forced_reason` when `content_type == "FLOAT32_CONTINUOUS"` and Viewing was requested but Analysis was used.

> Written for analysis instead. This is elevation data, a DSM, DTM or CHM. Compressing for viewing works by discarding detail the eye will not notice, but these pixels are height measurements rather than colours, so discarding detail would change the actual heights. Lossless ZSTD was used instead. The file still loads and pans at full speed - Viewing would only have made it smaller, not faster.

---

### Viewing requested on 16-bit or multispectral imagery

`detection.forced_reason` when `content_type == "CONTINUOUS"` and Viewing was requested but Analysis was used. `{n}` and `{dtype}` are the file's actual band count and pixel type. Ends by naming the actual route to a small visual copy, since none of this bucket's files can get one from this tool directly.

> Written for analysis instead. Compressing for viewing works on colour photographs, so it needs exactly three 8-bit colour bands. This file has {n} bands at {dtype}, so the pixel values were preserved instead. The file still loads and pans at full speed - Viewing would only have made it smaller, not faster.
>
> If a small visual copy is genuinely wanted, export an 8-bit RGB composite of the bands you want to see, then run this tool on that file with Viewing.

---

### Viewing requested on an 8-bit RGB file with NoData-only transparency

`detection.forced_reason` for an `RGB_8BIT` file whose collar is marked by a NoData value with no alpha band, when Viewing was requested but Analysis was used. Unlike the two entries above, this is a fact about this specific file, not its whole content type - a different RGB_8BIT file with a real alpha band gets a genuine Viewing/Analysis choice, never this message.

> Written for analysis instead. This file marks its transparent collar with a NoData value and has no alpha band. Compressing for viewing shifts pixel values slightly, so a collar marked by value rather than by position stops being reliable, and the border would render as solid black. The pixel values were preserved instead. The file still loads and pans at full speed - Viewing would only have made it smaller, not faster.
>
> To get the smaller file, re-export with an alpha band and run this again.

---

### Analysis requested on a file whose source was already compressed for viewing

`resolve_profile_reason()` when Analysis is requested (or forced) and the source file's `IMAGE_STRUCTURE` `COMPRESSION` tag names a JPEG variant - most commonly a prior Viewing-profile output from this same tool, re-run through Analysis. Unlike the three entries above, the request here genuinely is honoured exactly as asked: nothing is overridden, lossless ZSTD is applied. It is flagged anyway because the source pixels are not what they were before that earlier JPEG pass, so preserving them losslessly now preserves already-altered values rather than recovering the originals - see `docs/plugin_design_notes.md` for why this needed a new category of rule. Confirmed on a real file: 367MiB in, 1.55GiB out (4.41x), the "compression not yet the target" message below suppressed in favour of this one.

> This file was already compressed for viewing, so some pixel values were changed before it reached this tool. Preserving them now keeps those changed values rather than recovering the originals, and the file will be substantially larger for no gain in accuracy. For measurement work, run this tool on the original file instead.

---

### Already tiled and has pyramids, but compression isn't the target yet

Fires when the file is tiled with pyramids (so pan/zoom speed is already fine) but the current compression isn't the target one for the profile in use, e.g. a file arriving as LZW when the target is ZSTD. Runs regardless of Reprocess: unlike "File is already optimised" above, there's a real file-size gain here, so it isn't a redundant rebuild that needs an explicit override. Suppressed specifically when the entry above also fires (Analysis requested on an already-JPEG-compressed source): that case is not a size gain, typically the opposite, so promising "expect a smaller file" there would be false, and the entry above already explains what's actually happening.

> Already tiled with overviews, so pan and zoom speed was already fine. Reprocessing anyway because the current compression ({current}) is not {target} yet. Expect a smaller file, not a faster one.

---

### Size summary

Routine, always logged after a successful conversion. Units are binary (1024-based) and labelled accordingly - `GiB`/`MiB`, not `GB`/`MB` - since the arithmetic was always base-1024 and the old decimal-looking labels didn't match what was actually shown. The percentage is rounded to a whole number, not one decimal place, because the sizes above it are only shown to 2 decimal places: at 1 decimal, the percentage could disagree with what a reader computes by hand from the rounded sizes shown next to it (a real example this was fixed against: "Source: 1.66GiB Output: 1.76GiB" alongside a 1-decimal change figure of "+5.8%" contradicts the +6.0% those two numbers actually give; whole-number rounding removes the contradiction).

> Source: {source size} Output: {output size} Change: {sign}{whole-number percentage}%

### Output larger than source

Routine, logged only when the output ends up bigger than the source - almost always with Analysis, since a source that arrived already compressed can be close enough to ZSTD's size that pyramids (which add roughly a third back on top of the base image) push the total past the original. Not a failure: this tool trades file size for pan/zoom speed on the base image, and pyramids are an unavoidable part of buying that speed.

> Output is larger than source - expected with Analysis if the source was already compressed, since pyramids add back roughly a third. Not a failure: the gain here is speed, not size (choose Viewing instead if size matters more).

---

## Log messages: NoData handling

### Reveal hidden pixels requested on elevation

Elevation never has a NoData=0 condition to reveal (`detect_metadata_only()` never even creates a `NoDataRisk` for it), so this is always a no-op - but it's still worth a clear, elevation-specific explanation rather than the generic one below, since elevation's own NoData convention (a value like -9999 marking genuinely empty ground) is worth restating here.

> Hidden pixels: left as they are. Reveal applies to 8-bit imagery, where every value from 0 to 255 is a legitimate colour and a NoData value of 0 can hide real pixels. This is elevation data, where NoData marks genuinely empty ground using a value no real height could take, such as -9999. Clearing it would turn the collar into real height values.

---

### Reveal hidden pixels requested on a file with no NoData=0 condition at all

Routine. Fires for any non-elevation file (most often 8-bit RGB) with no NoData value of 0 set at all - selecting Reveal here is harmless, the output is identical to any other setting, and blocking it would fail files in batch that would otherwise process correctly.

> Hidden pixels: nothing to reveal. This file has no NoData value of 0, so nothing was being hidden behind one. The output is the same as it would have been with any other setting.

---

### Reveal hidden pixels requested on a file whose only transparency is NoData

An 8-bit RGB file with no alpha band, whose collar is marked by a NoData value of 0. Explicit Reveal now genuinely clears NoData here (previously this was a hard refusal regardless of NODATA_MODE - Automatic still stays conservative and never clears on this file type, unaffected by this change, since staying conservative by default is the whole point of Automatic).

> Hidden pixels: cleared, as requested. This file marked its transparent collar with a NoData value of 0 and has no alpha band, so clearing it removes the collar's transparency as well as any interior holes. The border may now render as solid black. Every pixel value is unchanged. To keep the collar transparent, run again with Automatic or Keep as-is.

---

### Log message when Automatic clears NoData

Not a warning: informational, but should stand out. This is the most educational thing the plugin says. `processAlgorithm()`, not `checkParameterValues()` - see the note above on why NoData never blocks execution.

> Cleared NoData: around **{pct}** of this image's interior was pure black and hidden behind a NoData value of 0, real content, usually shadow or water, not just the transparent collar. Those pixels are now visible in the output.

---

### Log message when Keep as-is finds real content

Not a warning either, for the same reason: `checkParameterValues()` can only refuse, never accept-with-acknowledgement, and Keep as-is is a legitimate choice, not a mistake. Gating this in `checkParameterValues()` was tried twice and produced an unclosable modal loop both times (OK dismisses it, Run fires it again). Given the same emphasis as the message above: the finding is exactly as consequential either way, only the outcome (kept vs cleared) differs.

> NoData handling: kept, as requested. Around **{pct}** of this image's interior is pure black and hidden behind a NoData value of 0, usually shadow or water, not the transparent collar. Those pixels stay hidden in this output. Run again with Reveal hidden pixels or Automatic to bring them back.

---

### Log message when Automatic confirms nothing was hidden

Routine. No emphasis needed. Fires when detection sampled the file and found NoData marks only the collar.

> Kept NoData: this file's NoData value only marks the transparent collar, so nothing real was hidden. Left unchanged.

---

### Log message when Automatic couldn't tell

Routine. No emphasis needed, but must not be conflated with the message above: this fires when detection *couldn't determine* whether real content was hidden, not when it confirmed there wasn't any. Saying "nothing real was hidden" here would be a false reassurance the file never earned.

Reachable for two different reasons, not just a small file: the image's dimensions can be too small for the sampling grid to have any interior cells at all, or a large file can still have sparse, thin coverage (an oddly-shaped survey area) where every interior cell individually falls under the minimum valid-pixel threshold. The wording below is deliberately neutral about which.

> Kept NoData: this file didn't have enough interior area to sample reliably, so it wasn't possible to tell whether real content is hidden behind NoData=0. Left unchanged. If dark areas look like they have holes in them, run again with **Reveal hidden pixels**.

If `{pct}` would round to 0.00% in any of the messages above, write "a small but detectable amount" instead of a figure.

---

## End-of-run summary

The last thing logged before a successful run completes, at warning severity (`feedback.pushWarning()`) so it carries colour. Re-states the consequential decisions from the run, which otherwise scroll away behind Translate/BuildOverviews' own progress output - not every decision, only the ones worth repeating: a plain file with nothing surprising (Viewing honoured on an RGB file, no NoData finding) gets just the closing line below, not a restatement of "used as asked", which would be noise on every run.

A decision line is also dropped if it would sit immediately under the message it's restating, with nothing genuinely buried in between - the summary exists to resurface a decision that's scrolled out of view, not to echo the line directly above it. In practice this only ever affects the NoData line: the NoData dispatch is always the last thing logged before the summary starts, so that line is always adjacent to its own original and is dropped every time it would otherwise appear. The profile-decision line doesn't have this problem - it's logged in `_resolve_profile()`, well before Translate/BuildOverviews' own progress output, so real content genuinely separates it from the summary. The location line always stays, even when both decision lines are dropped.

Format:

> Summary
> {the profile-decision message, only if consequential - either Viewing was requested but Analysis was used, or Analysis was requested (and honoured) on a source already compressed for viewing - see the Log messages above}
> {the NoData message, only if it was consequential AND not immediately adjacent to its own original - see above; under the current message order this line never actually appears}
> These decisions are also recorded in the file, under Layer Properties > Information > More information.

The location line's exact wording was verified against the running application in both QGIS 3.44 LTR and 4.2 (via `QgsRasterLayer.htmlMetadata()`, not a static resource-string search, which misses it - the label is generated by the GDAL provider, not `qgis_core`/`qgis_app`): both versions show a section labelled **More information** under Layer Properties' Information tab, listing the file's custom GDAL metadata - identical text in both, so no version-specific wording was needed.

---

## Embedded metadata (output file)

Six items in the output file's own metadata, plus the standard TIFF description tag, written with `SetMetadataItem()` once conversion has finished writing pixels but before pyramids are built (the header has to settle before hundreds of megabytes of pyramid data are appended behind it). Keys are numbered because QGIS renders the key verbatim as the visible label, and the number keeps the sequence readable regardless of display order. Any `GEOKLEIN_*` key already present (from a source file this tool already produced) is stripped before the fresh set is written - overwrite, never append, so re-running this tool never leaves a stale record sitting next to a current one.

| Key | Content |
|---|---|
| `GEOKLEIN_1_TOOL` | `GeoKlein Raster Optimiser {version}, {D Month YYYY}` - version read from `metadata.txt`, never hardcoded; date is the day the conversion ran. |
| `GEOKLEIN_2_DETECTED` | What was detected before conversion ran - content type, band count, tiled/stripped, pyramids or not. Leads with an explicit subject ("Source file was...") rather than a bare comma list: this key is only ever read on the OUTPUT file, so "tiled, without pyramids" on its own would read as a claim about the file in front of the reader, not the source it was made from. See `describe_detection()` in `core/detector.py`. |
| `GEOKLEIN_3_REQUESTED` | **Analysis** or **Viewing** - whichever was actually requested (the dropdown choice, or whatever profile was used if the caller never specified one, e.g. via the CLI without `--profile`). |
| `GEOKLEIN_4_DECISION` | The identical text from "Log messages: what was used and why" above, whether or not the request was honoured. |
| `GEOKLEIN_5_APPLIED` | The compression, predictor, tiling and overview resampling actually applied - see `_format_applied_settings()`. States decisions made before Translate ran, never an outcome that might not happen: it does not say whether pyramids exist, since that's directly observable from the file itself; it names the resampling method used to build them instead, which is a real decision and isn't recoverable from the file afterwards. |
| `GEOKLEIN_6_HIDDEN_PIXELS` | The current NoData-handling message. Present whenever `_resolve_nodata_handling()` reached a conclusion worth recording - not only when real content was found, but also the routine "nothing hidden" and "kept as requested" outcomes. A routine finding is still a result: the key shows that the check ran and what it concluded, which is different information from the file simply not having a problem, and a missing key in a file written by an older version can't be told apart from a check that ran and found nothing - recording the routine case too keeps files self-describing across versions on their own, without needing to know which version wrote them. Absent only when Automatic or Keep as-is was chosen and there was nothing to assess or act on: either the file has no NoData=0 condition at all, or NoData marks only the collar with no alpha band (clearing isn't possible without also affecting the collar) and nothing was asked that would surface that fact. |

Two full worked examples, both taken from a real run against the current code (elevation with Viewing requested; 8-bit RGB with Viewing requested and a real NoData finding):

> GEOKLEIN_2_DETECTED = Source file was Float32 elevation (DSM, DTM or CHM), 1 band, tiled, without pyramids.
> GEOKLEIN_4_DECISION = Written for analysis instead. This is elevation data, a DSM, DTM or CHM. Compressing for viewing works by discarding detail the eye will not notice, but these pixels are height measurements rather than colours, so discarding detail would change the actual heights. Lossless ZSTD was used instead. The file still loads and pans at full speed - Viewing would only have made it smaller, not faster.
> GEOKLEIN_5_APPLIED = Lossless ZSTD level 9, predictor 3, tiled 512x512, pyramids resampled with AVERAGE

> GEOKLEIN_2_DETECTED = Source file was 8-bit RGB imagery, 3 bands plus alpha, tiled, without pyramids.
> GEOKLEIN_4_DECISION = Lossy compression suits this data, so it was used as asked.
> GEOKLEIN_5_APPLIED = JPEG quality 90 with YCbCr, alpha reattached as mask, tiled 512x512, pyramids resampled with AVERAGE
> GEOKLEIN_6_HIDDEN_PIXELS = Cleared NoData: around 0.55% of this image's interior was pure black and hidden behind a NoData value of 0, real content, usually shadow or water, not just the transparent collar. Those pixels are now visible in the output.

Also set, the standard TIFF tag other tools (ArcGIS, ExifTool, Photoshop) read where GDAL's own metadata domain is ignored. Deliberately NOT the full `GEOKLEIN_2_DETECTED`/`GEOKLEIN_4_DECISION` text concatenated - that produced the same paragraph appearing twice in Layer Properties. One short sentence instead: tool and version, what the file is (`content_label()`, the same short phrase `GEOKLEIN_2_DETECTED`'s longer sentence is built from), and what compression was applied:

> TIFFTAG_IMAGEDESCRIPTION = Optimised by GeoKlein Raster Optimiser 0.1.0. Float32 elevation (DSM, DTM or CHM), written as Lossless ZSTD.

> TIFFTAG_IMAGEDESCRIPTION = Optimised by GeoKlein Raster Optimiser 0.1.0. 8-bit RGB imagery, written as JPEG.

---

## Writing conventions used here

- Headings are never followed by a colon. In `shortHelpString()`'s actual HTML they're plain `<p>` text rather than `<b>`, because QGIS's own help-panel template gives bold text a fixed colour that's unreadable in dark themes; this doc still shows them in **bold** as a doc-only convention for marking a heading.
- Technical terms are explained the first time they appear, or defined in the glossary. Analysis and Viewing are a deliberate exception: they're the purpose question's two options, and each already gets its own full explanation right where it's introduced, in `shortHelpString()`'s "What will you use this file for" section - a Glossary one-liner would say less than that paragraph already does, not more. Everything actually in the Glossary is a term that gets *used* elsewhere (in warnings, in other parameters' help text) without being re-explained on the spot; Analysis and Viewing don't have that pattern - wherever they appear, they're either the dropdown label itself or paired with their own explanation.
- Every warning states what was found, why it matters, and what to do about it, in that order.
- Second person throughout. "Your source file is never modified", not "the source file is not modified".
- No exclamation marks. No "just" or "simply" as minimisers ("simply tick the box") - they imply the task is trivial and make people feel stupid when it isn't. "Just" meaning "only" or "merely" ("it would just produce a second large file") is fine and often the clearest word available.
- British spelling: optimise, colour, behaviour.
- No em dashes, anywhere. Use a comma, colon, bracket or full stop, whichever reads best for that sentence.
