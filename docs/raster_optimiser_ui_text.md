# Raster Optimiser — UI text

Polished replacements for all user-facing strings. Reconcile parameter names against the actual code; the wording is what matters.

**Note:** this assumes the NoData checkbox becomes a three-option dropdown (Automatic / Reveal / Keep as-is). If it stays a checkbox, adjust the NoData entries accordingly.

---

## shortDescription() — toolbox hover tooltip

> Makes slow, oversized rasters load and pan quickly in QGIS.

---

## displayName()

> Optimise raster

---

## shortHelpString()

QGIS renders this as HTML. Use `<b>` for headings and `<ul>`/`<li>` for lists.

---

**What this does**

Makes large rasters load and pan quickly in QGIS, and reduces their file size.

**Why your file is slow**

Most orthomosaics and elevation rasters come out of processing software missing two things GDAL needs in order to draw them quickly:

- **Pyramids**, also called overviews — pre-built smaller copies of the image. Without them, QGIS has to read every pixel in the file just to draw a zoomed-out view. On a 470-megapixel ortho, that's the entire file, on every pan and every zoom.
- **Tiling** — storing pixels as small squares rather than full-width rows, so software can read one part of the image without touching the rest.

This tool adds both, and compresses the file sensibly on the way through.

**Choosing a compression profile**

*Preserve pixel values (lossless)* — a smaller file with every pixel value exactly as it was. Use this whenever you'll measure something from the image: vegetation indices, classification, crown segmentation, change detection. Elevation data always uses this, whatever you select.

*Smallest file size (lossy)* — typically 15 to 30 times smaller, but pixel values shift slightly. Invisible on screen, measurable in analysis. Use it for basemaps, client copies, QField backdrops — anything you look at rather than measure.

Both profiles produce a file that loads at the same speed. The choice only affects file size and whether pixel values survive unchanged.

**What it won't process**

Some rasters can't be optimised safely with these settings, so the tool detects them and stops rather than producing something quietly wrong:

- **Classified rasters** — land cover, species class, or any map where pixel values are category codes rather than measurements. Building pyramids averages neighbouring pixels, and averaging two categories produces a third that doesn't exist.
- **Multispectral rasters**, or anything with more than four bands.
- **Files with no coordinate reference system.**

**Your source file is never modified.** The tool always writes a new file.

**Glossary**

- **NoData** — a pixel value the file declares to mean "nothing here". Safe on elevation data, where you can pick a value no real height could ever be, such as -9999. Risky on 8-bit imagery, where every value from 0 to 255 is a legitimate colour and 0 is simply black.
- **Alpha band** — an extra band recording which pixels fall inside the surveyed area. It works by position rather than by value, so it never mistakes a black pixel for an empty one.
- **Collar** — the transparent border around a survey area, where the image doesn't fill the rectangular file.
- **Pyramids / overviews** — pre-built smaller copies of the image at successive zoom levels.
- **Tiling** — storing the image as small squares instead of full-width rows.

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
> Lossless keeps every pixel value exactly as it is — use it for anything you'll measure or analyse. Lossy produces a much smaller file by discarding detail the eye won't notice — use it for basemaps and anything you only look at.
>
> Both load at the same speed. Elevation data is always processed losslessly, whatever you choose here.

---

### Hidden pixels (NoData)

Label: **Hidden pixels (NoData)**

Options:
1. **Automatic — decide per file (recommended)**
2. **Reveal hidden pixels**
3. **Keep as-is**

Help:
> Many orthomosaics mark transparency using a NoData value of 0. On 8-bit imagery that's unsafe, because 0 is also the value of a genuinely black pixel — so deep shadow, dark water and wet tarmac get treated as empty and punched out as holes.
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
> Inspects the file before converting and stops with an explanation if you've chosen lossy compression for elevation data, or if the file is already optimised.
>
> These are quick checks that read the file's structure rather than its contents, so leaving this on costs nothing.

---

### Reprocess even if already optimised

Label: **Reprocess even if already optimised**

Help:
> By default, a file that's already tiled with pyramids is left alone, since converting it again wouldn't make it any faster.
>
> Tick this to convert it anyway — for example to switch an existing file from lossless to lossy compression to save disk space.

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

### Lossy compression on elevation data

Blocks execution. No override.

> This is elevation data — a DSM, DTM or CHM.
>
> Lossy compression works by discarding detail the eye won't notice. That's fine for photographs, but elevation pixels are height measurements, not colours, so discarding detail changes the actual heights.
>
> Choose **Preserve pixel values (lossless)** instead.

---

### File is already optimised

Blocks execution. Escapable via Reprocess.

> This file is already tiled and has pyramids built, so it should already load and pan quickly in QGIS.
>
> Converting it again won't make it any faster — it would just produce a second large file.
>
> If you're reconverting deliberately, for example to switch from lossless to lossy compression, tick **Reprocess even if already optimised** under Advanced parameters.

---

### Real content hidden behind NoData

Fires only when the user has explicitly chosen **Keep as-is**. Escapable by changing the dropdown.

> Around **{pct}** of the interior of this image is pure black and currently hidden by a NoData value of 0 — usually shadow or water that happens to sit on the same value the file uses to mean "nothing here".
>
> You've chosen to keep NoData as it is, so those pixels will stay hidden in the output.
>
> Choose **Automatic** or **Reveal hidden pixels** to bring them back.

If `{pct}` would round to 0.00%, write "a small but detectable amount" instead of a figure.

---

### Log message when Automatic clears NoData

Not a warning — informational, but should stand out. This is the most educational thing the plugin says.

> Cleared NoData: around **{pct}** of this image's interior was pure black and hidden behind a NoData value of 0 — real content, usually shadow or water, not just the transparent collar. Those pixels are now visible in the output.

---

### Log message when Automatic leaves NoData alone

Routine. No emphasis needed.

> Kept NoData: this file's NoData value only marks the transparent collar, so nothing real was hidden. Left unchanged.

---

## Writing conventions used here

- Headings are bold, never followed by a colon.
- Technical terms are explained the first time they appear, or defined in the glossary.
- Every warning states what was found, why it matters, and what to do about it, in that order.
- Second person throughout. "Your source file is never modified", not "the source file is not modified".
- No exclamation marks, no "simply", no "just".
- British spelling: optimise, colour, behaviour.
