# Raster Optimiser UI text

The user-facing strings as shipped in the code - this doc is kept in sync with `algorithms/optimise_raster.py` and `core/converter.py`, not the other way round. If you're changing wording, change the code first and update this file to match, not the reverse.

The NoData parameter is the three-option dropdown described below (Automatic / Reveal hidden pixels / Keep as-is), not a checkbox.

---

## shortDescription() (toolbox hover tooltip)

> Makes slow, oversized rasters load and pan quickly in QGIS.

---

## displayName()

> Optimise raster

---

## shortHelpString()

QGIS renders this as HTML. Section headings are plain `<p>` text, not `<b>`: QGIS's own help-panel template applies a fixed, non-theme-aware colour to bold text that reads as dark grey on dark grey in the Night Mapping theme (and likely other dark themes), and no single inline colour can fix it (it can't satisfy WCAG contrast against both a near-white default theme and Night Mapping's `#535353` background at once). Lists use real `<ul>`/`<li>` tags. Headings are shown in **bold** below purely as a doc-formatting convention to mark them as headings, it doesn't mean the code wraps them in `<b>`.

---

**What this does**

Makes large rasters load and pan quickly in QGIS, and reduces their file size.

**Why your file is slow**

Most orthomosaics and elevation rasters come out of processing software missing two things GDAL needs in order to draw them quickly:

- **Pyramids**, also called overviews: pre-built smaller copies of the image. Without them, QGIS has to read every pixel in the file just to draw a zoomed-out view. On a 470-megapixel ortho, that's the entire file, on every pan and every zoom.
- **Tiling**: storing pixels as small squares rather than full-width rows, so software can read one part of the image without touching the rest.

This tool adds both, and compresses the file sensibly on the way through.

**Choosing a compression profile**

*Preserve pixel values (lossless)*: a smaller file with every pixel value exactly as it was. Use this whenever you'll measure something from the image: vegetation indices, classification, crown segmentation, change detection. Elevation data always uses this, whatever you select.

*Smallest file size (lossy)*: typically 15 to 30 times smaller, but pixel values shift slightly. Invisible on screen, measurable in analysis. Still a GeoTIFF either way, never a .jpg file. Use it for basemaps, client copies, QField backdrops: anything you look at rather than measure.

Both profiles produce a file that loads at the same speed. The choice only affects file size and whether pixel values survive unchanged.

Multispectral and 16-bit imagery are always processed losslessly too, the same as elevation: the lossy option is built for colour photographs, which need exactly three 8-bit colour bands - that doesn't apply to either.

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

---

## Parameter labels and help text

### Input layer

Label: **Input layer**

Help:
> The raster to optimise. Any format GDAL can read. The output is always a GeoTIFF.

---

### Compression profile

Label: **Compression profile**

Options:
1. **Preserve pixel values (lossless)**
2. **Smallest file size (lossy)**

Help:
> Lossless keeps every pixel value exactly as it is: use it for anything you'll measure or analyse. Lossy produces a much smaller file by discarding detail the eye won't notice: use it for basemaps and anything you only look at.
>
> Both load at the same speed, and both are always written as GeoTIFF - lossy never means a .jpg file. Elevation data is always processed losslessly, whatever you choose here.

---

### Hidden pixels (NoData)

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

### Warn me before running

Label: **Warn me before running if something looks wrong**

Help:
> Inspects the file before converting and stops with an explanation if you've chosen lossy compression for elevation data, or if the file already has nothing left to gain (tiled, with pyramids, and already at the target compression). A file that's tiled with pyramids but on a less efficient compression still gets reprocessed, since there's real file size to save there.
>
> These are quick checks that read the file's structure rather than its contents, so leaving this on costs nothing.

---

### Reprocess even if already optimised

Label: **Reprocess even if already optimised**

Help:
> By default, a file that's already tiled with pyramids and already at the target compression is left alone, since converting it again wouldn't make it any faster or smaller. A file that's tiled with pyramids but still on a less efficient compression is reprocessed regardless of this setting, since there's real file size to save there.
>
> Tick this to convert an already-optimal file anyway, for example to switch it from lossless to lossy compression to save disk space.

---

### Replace existing output file

Label: **Replace existing output file**

Help:
> Unticked, the tool stops rather than overwriting a file that already exists at the output path, and tells you what it found.
>
> Ticked, the existing file is replaced. Your source file is never modified either way.

---

### Output

Label: **Optimised raster**

Help:
> Where to save the result. Always written as a GeoTIFF, tiled with pyramids built in.

---

## Warning messages

Three things block execution: lossy compression on elevation data, lossy compression on anything else that isn't 8-bit RGB (16-bit imagery, multispectral), and reprocessing an already-optimised file. Each has a real, different parameter to change (Profile, or Force reprocess). NoData never blocks, however consequential the finding: `checkParameterValues()` can only refuse, not accept-with-acknowledgement, and every NoData choice (Automatic, Reveal hidden pixels, Keep as-is) is a legitimate one. What NoData gets instead is a prominent log message when the finding is consequential, see below.

### Lossy compression on elevation data

Blocks execution. No override.

> This is elevation data: a DSM, DTM or CHM.
>
> Lossy compression works by discarding detail the eye won't notice. That's fine for photographs, but elevation pixels are height measurements, not colours, so discarding detail changes the actual heights.
>
> Choose **Preserve pixel values (lossless)** instead.

---

### Lossy compression on 16-bit or multispectral imagery

Blocks execution. No override. Doesn't lead with the codec name - "JPEG" first reads as "this might output a .jpg file", which it never does.

> The lossy option compresses colour photographs, so it needs exactly three 8-bit colour bands. This file doesn't fit that, so only lossless is offered.

---

### File is already optimised

Blocks execution. Escapable via Reprocess. Compression-aware: only fires when the file is tiled, has pyramids, AND is already using the target compression for the profile that would be used (ZSTD for lossless, YCbCr JPEG for lossy). A file that's tiled with pyramids but still on a less efficient compression (LZW, DEFLATE, uncompressed) does NOT hit this, see the log entry below instead.

> This file is already tiled and has pyramids built, so it should already load and pan quickly in QGIS. It's also already using the target compression, so reprocessing wouldn't shrink it either.
>
> Converting it again won't make it any faster or smaller. It would just produce a second large file.
>
> If you're reconverting deliberately, for example to switch from lossless to lossy compression, tick **Reprocess even if already optimised** under Advanced parameters.

---

### Log message when tiled and has overviews, but compression isn't the target yet

Not a warning, doesn't block: this is a warning-severity log line (`feedback.pushWarning()`), not a `checkParameterValues()` refusal. Fires when the file is tiled with pyramids (so pan/zoom speed is already fine) but the current compression isn't the target one for the profile in use, e.g. MSTIFF.tif arriving as LZW when the target is ZSTD. Runs regardless of Force reprocess: unlike the fully-optimised case above, there's a real file-size gain here, so this isn't a redundant rebuild that needs an explicit override.

> Already tiled with overviews, so pan/zoom speed was already fine. Reprocessing anyway because the current compression ({current}) isn't {target} yet - expect a smaller file, not a faster one.

---

### Log message when Automatic clears NoData

Not a warning: informational, but should stand out. This is the most educational thing the plugin says. `processAlgorithm()`, not `checkParameterValues()` - see the note below on why NoData never blocks execution.

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

## Writing conventions used here

- Headings are never followed by a colon. In `shortHelpString()`'s actual HTML they're plain `<p>` text rather than `<b>`, because QGIS's own help-panel template gives bold text a fixed colour that's unreadable in dark themes; this doc still shows them in **bold** as a doc-only convention for marking a heading.
- Technical terms are explained the first time they appear, or defined in the glossary.
- Every warning states what was found, why it matters, and what to do about it, in that order.
- Second person throughout. "Your source file is never modified", not "the source file is not modified".
- No exclamation marks. No "just" or "simply" as minimisers ("simply tick the box") - they imply the task is trivial and make people feel stupid when it isn't. "Just" meaning "only" or "merely" ("it would just produce a second large file") is fine and often the clearest word available.
- British spelling: optimise, colour, behaviour.
- No em dashes, anywhere. Use a comma, colon, bracket or full stop, whichever reads best for that sentence.
