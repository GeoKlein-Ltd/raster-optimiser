# Optimising Orthomosaics for QGIS

A GeoKlein working reference

---

## Premise

Photogrammetry software gives you a technically correct file that is practically unusable. A 7 hectare site at 1.3 cm ground sample distance is around 470 million pixels and close to 2 GB of raw pixel data. QGIS will open it, then crawl.

The default export is built for archival correctness, not for someone panning around a map canvas. Nothing is wrong with the file. It just has no structure that lets software read part of it without reading all of it.

This workflow adds that structure. It takes about five minutes per file, all through Processing Toolbox dialogs, and turns a sluggish ortho into one that pans instantly.

**Two things to be clear about before starting.**

Speed comes from **tiling and pyramids**, not from compression. Compression changes file size. If you only do one thing, build pyramids.

Whether you can compress lossily depends entirely on **what the file is for**. Looking at imagery and measuring imagery are different jobs with different answers. That decision is Step 2 and everything else follows from it.

---

## Background

Six concepts. Skip if you already know them.

### What a TIFF actually is

A TIFF is a container. It holds pixel data plus metadata about coordinates, projection, band meaning and so on. It does not specify how the pixels are stored.

Inside that container, the pixels can be compressed in different ways. The container stays a TIFF either way, and the file extension stays `.tif`. So "should I use TIFF or JPEG" is the wrong question. The real question is which compression method to use *inside* the TIFF.

Common options:

| Method | Lossless? | Notes |
|---|---|---|
| None | Yes | Fastest to read, enormous |
| LZW | Yes | Old, universally supported, weak on imagery |
| DEFLATE | Yes | Same algorithm as ZIP. What Metashape and Terra usually output |
| **ZSTD** | Yes | Modern. Smaller and faster than DEFLATE |
| **JPEG** | No | Photographic. Dramatically smaller, throws data away |
| LERC | Controlled | For elevation. You set the maximum error |

A GeoTIFF with JPEG compression inside is still a GeoTIFF. It keeps its coordinates, its projection, all of it. It is not the same thing as a `.jpg` file, which has no georeferencing at all.

### Pyramids (overviews)

The single most important concept here.

When you zoom out so the whole ortho fits your screen, you are asking for maybe 1,500 pixels across. The file has 19,828. Without pyramids, QGIS has no choice but to read and decompress all 470 million pixels, then throw away 99.99% of them to produce the picture. It does this every single time you pan, zoom, or refresh.

Pyramids are pre-built smaller copies stored alongside the full-resolution data. Half size, quarter size, eighth size, and so on down. QGIS picks whichever is closest to what your screen actually needs and reads that instead.

Reading a 2,500 pixel wide copy instead of a 19,828 pixel wide one is roughly 60 times less work. That is the difference between laggy and instant.

**Pyramids do not reduce your data.** The full resolution image is untouched. Zoom in and you get every original pixel. Overviews only get used when you are zoomed out far enough that the screen could not show the detail anyway.

They add roughly a third to the file size, because each level is a quarter the pixels of the one above and the series adds up to about 1/3 of the original.

Pyramids can be stored two ways:

- **Internal** — written inside the TIFF. One self-contained file. Modifies the source, cannot be undone.
- **External** — written to a sidecar file called `yourfile.tif.ovr`. Source untouched. But if the `.ovr` gets separated from the `.tif`, it stops working entirely. There is no search path. It has to sit in the same folder with the same base name.

Use External on someone else's original. Use Internal on files you produce.

### Tiling and block size

Pixels have to be stored in some order. Two options:

- **Stripped** — stored in horizontal rows spanning the full width. Reading anything in a row means reading the whole row.
- **Tiled** — stored as independent squares, typically 256x256 or 512x512. Software can read any square without touching the rest.

For map navigation, tiled is essential. Stripped files are painful regardless of what else you do.

You will see this in `gdalinfo` output as `Block=`. `Block=512x512` is tiled. `Block=19828x1` is stripped.

512 is a slightly better tile size than 256 for local disk, because you get fewer separate reads per screenful. Both work. Dimensions must be multiples of 16.

### Lossy versus lossless

**Lossless** compression (ZSTD, DEFLATE, LZW) is a smarter way of writing the same numbers. Decompress it and you get back exactly what went in, every value identical. It works by spotting patterns and repetition. Aerial imagery has fairly little of either, which is why lossless typically only gets you 2x to 4x.

**Lossy** compression (JPEG) discards information permanently. It converts small blocks of the image into frequency components, then throws away the high-frequency ones your eye barely registers. This gets you 15x to 30x, but the pixel values that come back out are not the ones that went in. They are close. They are not the same.

That difference is invisible on screen and measurable in analysis. Which is the trap: it looks fine, so people assume it is fine.

### YCbCr

Stands for luminance and two chrominance channels. Y is brightness, Cb and Cr are colour difference values.

Normal RGB stores three colour channels at full resolution. YCbCr splits the image into brightness and colour instead, then stores the **colour at half resolution in both directions** while keeping brightness at full resolution.

This works because human vision resolves brightness far better than colour. You will not notice colour detail being halved. You would immediately notice brightness detail being halved.

On top of JPEG itself, this is roughly another 2x saving. It is the single most effective option in the whole recipe.

It comes with a hard constraint that shapes everything else: **YCbCr requires exactly three bands, 8-bit, with JPEG compression.** Give it four bands and it refuses to run.

### Predictor

A lossless compression helper. It does not compress anything itself, it rearranges the data so the actual compressor does better.

Neighbouring pixels in aerial imagery are usually similar. A patch of grass might read 82, 84, 83, 85, 84. Those are five different numbers with little repetition, so a compressor struggles.

Predictor 2, horizontal differencing, stores each pixel as the difference from the one before it: 82, +2, -1, +2, -1. Now you have a long run of tiny numbers clustered around zero, which compresses far better.

| Predictor | Use for |
|---|---|
| 1 | None. The default |
| 2 | Integer data. Byte and UInt16 rasters |
| 3 | Floating point data. Float32 rasters like DSM, DTM, CHM |

Getting this wrong on Float32 is a common mistake. Predictor 2 on floats can make the file *bigger*. Elevation rasters take predictor 3.

Predictor is irrelevant with JPEG, which does not use it.

### Bit depth: Byte versus UInt16

How much space each pixel value gets per band.

| Type | Range | Notes |
|---|---|---|
| Byte | 0 to 255 | 8-bit. Standard for RGB imagery |
| UInt16 | 0 to 65,535 | 16-bit. Twice the file size |
| Float32 | Decimals | Elevation, indices, continuous values |

You will see this in gdalinfo as `Type=Byte` or `Type=UInt16`.

Most drone RGB cameras produce 8-bit output. You get 16-bit if the source was raw or if the processing software was set to preserve radiometric range.

**Converting down** means rescaling 16-bit values into the 0 to 255 range. A pixel reading 41,203 out of 65,535 becomes 161 out of 255. Same relative brightness, less precision, half the file.

It matters because **JPEG only works on 8-bit data**. If you want Profile A on a 16-bit file, you must convert down first. If you want Profile B, leave it alone.

You do this in the Translate dialog by setting Output data type to Byte and adding `-scale` to the extra parameters. Bare `-scale` uses the band's own minimum and maximum, which is usually what you want. You can specify explicitly with `-scale 0 65535 0 255`, but only do that if you know the data genuinely spans the full range, otherwise you will flatten the contrast.

### NoData, alpha bands and mask bands

Three ways of saying "this pixel is not real data". They work differently and only two of them belong on imagery.

| | NoData | Alpha band | Mask band |
|---|---|---|---|
| What it is | A number written in the metadata | A full extra band of pixel data | A separate channel outside the band structure |
| How it decides | **Value**: does this pixel equal the number? | **Position**: what does the alpha say at this location? | **Position**, same as alpha |
| Storage cost | Zero, it is a metadata tag | A whole band, same bit depth as the others | About 1 bit per pixel, compresses to nothing |
| Counts toward band total | No | **Yes** | No |
| Partial transparency | No, binary | Yes, 0 to 255 | No, binary |
| Support across software | Universal, oldest mechanism | Good | Patchy, some readers ignore it |

Alpha and mask are the same idea at different fidelity and cost. The only reason to prefer a mask is that it does not count as a band, which is what unblocks YCbCr. That is the entire justification for the `-mask` option and nothing else.

Two details that catch people out:

**A mask treats any non-zero value as fully valid.** An alpha value of 1 out of 255, which is almost completely transparent, becomes fully opaque when converted to a mask. This is what causes the black fringe described in Troubleshooting.

**NoData is a value test, not a location test.** GDAL does not know where your survey boundary is. It only knows the number you told it to treat as missing, and it applies that test to every pixel in the raster regardless of position. See the next section.

### Why NoData is a bad fit for 8-bit RGB

This is worth understanding properly, because it affects nearly every ortho you will ever be handed.

An 8-bit image stores values from 0 to 255 per band. **Every one of those is a legitimate colour.** There is no spare value you can reserve to mean "nothing here", because black is 0 and black is real.

So when a file declares `NoData Value=0` on all three colour bands, any pixel where red, green and blue all read zero gets reported as missing. Deep shadow under canopy, dark water, wet tarmac in shade, a black vehicle roof. Those become holes in the ortho.

Contrast that with elevation data. A Float32 DSM can use -9999, because no real elevation on Earth is ever minus nine thousand nine hundred and ninety-nine metres. The sentinel is physically impossible, so it can never collide with real data. That is what nodata is designed for and it works perfectly there.

Note that bit depth alone does not solve it. UInt16 is unsigned, running 0 to 65,535, so -9999 is not available. You need a signed or floating point type to use a negative sentinel. What actually makes nodata safe is having a value range wider than the data can physically occupy, not simply having more values.

**Why exporters set it anyway.** When photogrammetry software writes an ortho it allocates a zero-filled output buffer and writes valid pixels into it. Everywhere outside the survey footprint stays at zero because nothing was written there. From the writer's point of view, "0 means nothing was put here" is literally true at the moment of writing. It just cannot account for the pixels it *did* write that also happen to be zero.

Most exporters then write an alpha band as well, because alpha support has historically been patchy across GIS software and belt-and-braces makes the file behave sensibly wherever it lands. Reasonable logic, known failure mode.

**This is normal behaviour, not a defect.** Metashape, DJI Terra, Pix4D, ODM and RealityCapture all do some version of it. It is not a sign the client processed anything wrongly, and with Terra in particular there is no export setting they could have changed. Worth saying out loud if a client asks.

**The rule.** On 8-bit RGB, transparency should be positional. Use an alpha band or a mask, and clear the nodata. Keep nodata for continuous data where a physically impossible sentinel exists.

### Alpha bands and mask bands in practice

Both mark which pixels are real and which are outside the survey area. They work differently.

An **alpha band** is a full extra band, same bit depth as the others, holding a transparency value per pixel. It can hold intermediate values, so edges can fade out gradually. It counts toward your band total, which is why it blocks YCbCr.

A **mask band** is a separate channel outside the normal band structure, holding a simple valid/invalid flag. It does not count toward the band total.

Important detail that catches people out: **a mask treats any non-zero value as fully valid.** An alpha value of 1 out of 255, which is almost completely transparent, becomes fully opaque when converted to a mask. This is what causes the black fringe described at the end of this document.

---

## Step 1: Check the file

**Raster > Miscellaneous > Raster Information**

Set your ortho as input, run, read the output. Five things to find.

| Find this | Meaning | Action |
|---|---|---|
| `ColorInterp=Alpha` on the last band | The file has a transparency band | Note the band number. Profile A needs it converted to a mask |
| No alpha band | Three bands only | Profile A needs no extra parameters at all |
| `NoData Value=0` on an 8-bit RGB file | Value-based transparency on data where every value is legitimate. Almost certainly eating real black pixels | Run the toggle test below, then add `-a_nodata none` |
| `NoData Value=` something else, or on elevation data | May well be correct | Leave it alone |
| `Type=Byte` | 8-bit, standard | Either profile works |
| `Type=UInt16` | 16-bit | Profile A needs conversion to Byte first. Profile B does not |
| `Block=256x256` or `512x512` | Already tiled | Good. Pyramids alone may fix everything |
| `Block=<something>x1` | Stripped | Must rebuild through Translate. Pyramids alone will not save it |
| No `Overviews:` line | No pyramids | This is almost certainly your lag |

### The NoData toggle test

Five seconds, non-destructive, and it answers the question definitively.

1. Layer Properties > **Transparency**
2. Under "No data value", untick the source nodata checkbox
3. Apply, and look at the shadows

| What happens | Meaning |
|---|---|
| Holes fill in, Identify now reports 0 instead of "no data" | The nodata was hiding real pixels. Clear it |
| The survey edge stays crisp, no black box appears | The alpha or mask is handling the boundary on its own. Safe to clear the nodata |
| A black rectangle appears round the survey area | Nodata is the *only* thing providing transparency. Do not simply clear it, see below |
| Nothing changes | The nodata is not doing anything. Clear it or leave it, no consequence |

This is a display override only. Nothing is written to disk and you can untoggle it.

**Do not use the histogram for this.** GDAL excludes nodata pixels from statistics by definition, so a file with nodata=0 can never show a spike at 0 no matter how many zero pixels it contains. The test cannot detect the thing it is meant to detect.

### If nodata is the only transparency

If the toggle test gives you a black rectangle, the file has no alpha and no mask. You cannot just clear the nodata. You need to build positional transparency first.

**If you have or can draw an AOI polygon:** Raster > Extraction > **Clip Raster by Mask Layer**, and tick **"Create and output alpha band"**. That gives you a real positional alpha built from your polygon. Then clear the nodata.

**If you need to derive the footprint:** from OSGeo4W Shell, `gdal_footprint input.tif footprint.gpkg` produces a polygon of the valid area. Search the Processing Toolbox for "footprint" first in case your version exposes it as an algorithm. Fall back to Raster > Conversion > **Polygonize** if not.

Watch out: `gdal_footprint` derives the footprint from the nodata, so if the nodata is already eating black pixels you get holes in the polygon and bake the error in permanently. Use the hole-removal option if the tool offers one, or just draw the boundary by hand. On a 250 metre site that is a two minute job and it is guaranteed correct.

Also worth checking: whether a `.aux.xml` file exists next to the raster. If it does not, QGIS cannot cache band statistics and is recomputing them across every pixel on every load. Usually means the folder is read-only. Move the file somewhere writable.

---

## Step 2: Pick a profile

| | **A: Looking at it** | **B: Measuring it** |
|---|---|---|
| For | Basemaps, client copies, QField backdrops, site context, presentations | Anything you extract numbers from |
| Examples | Showing a client their site, background for digitising | Crown segmentation, vegetation indices, classification, change detection |
| Compression | JPEG, lossy | ZSTD, lossless |
| Size | 15 to 30x smaller | 2 to 4x smaller |
| Pixel values | Changed slightly | Identical to source |
| Speed | Same | Same |

**Speed is identical.** Tiling and pyramids deliver that. The profile choice only affects file size and whether pixel values survive.

### Why forestry work needs Profile B

Two things happen under JPEG, and the second is the one people miss.

**Frequency discarding** softens fine detail. On open ground that detail is mostly sensor noise. At a tree crown boundary it *is* the signal. Segmentation algorithms keying on local contrast get mushier, less reliable edges.

**YCbCr chroma subsampling** stores colour at half resolution. Every vegetation index is a ratio between colour channels, so you would be computing them from interpolated colour data. The error is small per pixel and systematic across the whole scene, which is the worst kind, because it looks like a real spatial pattern rather than noise.

**Note on NDVI specifically.** True NDVI needs a near-infrared band, which a standard RGB drone camera does not capture. Zenmuse P1, the L2's RGB, M4E, Mavic 3E all record visible light only. With RGB alone you use visible-band indices instead:

| Index | Full name | Notes |
|---|---|---|
| VARI | Visible Atmospherically Resistant Index | Most common RGB substitute for NDVI |
| ExG | Excess Green | Simple, good for vegetation extraction |
| GLI | Green Leaf Index | Similar behaviour to VARI |
| VDVI | Visible-band Difference Vegetation Index | Direct NDVI analogue for RGB |

If you do have a multispectral sensor and real NIR, the output is normally a multi-band or multi-file product and JPEG is not an option anyway. Either way: Profile B.

---

## Step 3: Translate (Convert Format)

**Raster > Conversion > Translate (Convert Format)**, then expand Advanced Parameters.

### Creation options

These control how the output file is physically built. Enter them as name/value rows in the table.

| Option | A | B | Does what |
|---|---|---|---|
| TILED | YES | YES | Store as squares, not strips |
| BLOCKXSIZE | 512 | 512 | Tile width |
| BLOCKYSIZE | 512 | 512 | Tile height |
| COMPRESS | JPEG | ZSTD | Which compressor |
| JPEG_QUALITY | 90 | – | 1 to 100. Below 75 shows blocking, above 95 wastes space |
| PHOTOMETRIC | YCBCR | – | Half-resolution colour storage |
| ZSTD_LEVEL | – | 9 | 1 to 22. Higher is smaller and slower to write |
| PREDICTOR | – | 2 | Differencing. 2 for integers, 3 for floats |
| BIGTIFF | YES | YES | Allows files over 4 GB |
| NUM_THREADS | ALL_CPUS | ALL_CPUS | Use all cores. Affects build time only |

Save each set as a named profile using the dropdown at the top of the creation options widget. You never type this again.

### Additional command-line parameters

This is the one field that needs typing, because these change the data rather than the file structure and the dialog has no fields for them.

| Situation | Enter |
|---|---|
| Profile A, alpha band present, nodata=0 (the usual case) | `-b 1 -b 2 -b 3 -mask 4 -a_nodata none` |
| Profile A, alpha band present, no nodata declared | `-b 1 -b 2 -b 3 -mask 4` |
| Profile A, no alpha band, nodata=0 | `-a_nodata none`, but build an alpha first, see Step 1 |
| Profile A, three bands, nothing to fix | leave blank |
| Profile A, 16-bit source | add `-scale` and set Output data type to Byte |
| Profile B | leave blank, always |

**What `-b 1 -b 2 -b 3` does.** Selects which bands to copy into the output, in order. Band 1, band 2, band 3. Band 4 is not on the list, so it is excluded. This is what gets you down to the three bands YCbCr demands.

**What `-mask 4` does.** Takes band 4, the one you just excluded, and reattaches it as a mask band instead. Your transparency survives, but it no longer counts as a band. Without this you would get a black rectangle around the survey area.

**What `-a_nodata none` does.** Clears the declared nodata value. The `-a_` prefix means "assign", and these options only rewrite metadata without touching a single pixel.

Use it on any 8-bit RGB ortho carrying nodata=0, which is most of them. Every value from 0 to 255 is a legitimate colour, so the sentinel collides with real black pixels and punches holes through shadows, dark water and anything genuinely black. Confirm with the toggle test in Step 1 first, mainly to check that something else is handling the survey boundary before you remove it.

Do this **before** building pyramids, not after. Average resampling combines each 2x2 block, so four dark pixels reading 1, 0, 1, 0 average to 0, which is the nodata value. That block becomes a hole in the overview even though three of the four source pixels were valid. Repeat down each level and near-black regions erode progressively. Clear the nodata first and the problem never arises.

**Profile B keeps all four bands deliberately.** ZSTD does not care about band count, so there is no reason to convert. A real alpha band is also easier to handle in R and lidR than a GDAL mask, which some readers ignore entirely.

---

## Step 4: Build Overviews (Pyramids)

**Raster > Miscellaneous > Build Overviews (Pyramids)**

Run this on the file you just produced.

| Field | Set to | Why |
|---|---|---|
| Input layer | Your new file | |
| Overview levels | Leave blank | GDAL picks a sensible series down to thumbnail size |
| Resampling method | **Average** | Averages each 2x2 block. Nearest picks one pixel and discards three, which causes speckle and shimmer zoomed out |
| Overview format | **Internal** for your own files, **External** for someone else's original | Internal modifies the file and cannot be undone. External writes a `.ovr` sidecar |
| Remove existing overviews | Only if rebuilding | |

### Additional command-line parameters

| Profile | Enter |
|---|---|
| A | `--config COMPRESS_OVERVIEW JPEG --config PHOTOMETRIC_OVERVIEW YCBCR --config INTERLEAVE_OVERVIEW PIXEL` |
| B | `--config COMPRESS_OVERVIEW ZSTD` |

These tell GDAL to compress the pyramids the same way as the base image. Without them the overviews are written uncompressed, which adds roughly a third of the *uncompressed* file size back on. On a 470 megapixel ortho that is around 600 MB of sidecar for no reason.

`--config` sets a GDAL runtime setting rather than a file creation option, which is why the syntax is different from the table in Step 3.

`INTERLEAVE_OVERVIEW PIXEL` matches how bands are woven together in the pyramids to how they are in the base image. Mismatched interleaving loses most of the compression benefit.

### Verify

Run Raster Information on the output. You want:

- `Block=512x512`
- An `Overviews:` line under each band listing several sizes
- `COMPRESSION=YCbCr JPEG` or `COMPRESSION=ZSTD` in Image Structure Metadata

Then remove the layer from the project and add it back. Refreshing does not pick up structural changes.

---

## Reference: other data types

| Data | Compression | Predictor | Notes |
|---|---|---|---|
| RGB 8-bit, display | JPEG quality 90 + YCBCR | – | Profile A |
| RGB 8-bit, analysis | ZSTD level 9 | 2 | Profile B |
| RGB 16-bit, display | Convert to Byte first, then JPEG | – | `-ot Byte -scale` |
| RGB 16-bit, analysis | ZSTD level 9 | 2 | Keep 16-bit |
| DSM, DTM, CHM (Float32) | ZSTD level 9 | **3** | Never JPEG |
| DSM where size matters | LERC_ZSTD, MAX_Z_ERROR 0.01 | – | Guarantees no height error above 1 cm |
| Classified raster, integer | ZSTD level 9 | 2 | Never JPEG, values are categories |
| Multispectral | ZSTD level 9 | 2 | Never JPEG |

Two things worth flagging.

**Float32 takes predictor 3.** Using 2 on floating point data can make the file larger. This is the easiest mistake in the whole document to make.

**LERC is worth knowing about for elevation.** It is controlled loss rather than uncontrolled: you specify the maximum error you will tolerate and it guarantees nothing exceeds it. `MAX_Z_ERROR 0.01` means no height is off by more than 1 cm. That is a number you can put in a method statement, which is a completely different proposition from JPEG's "trust me, it looks fine".

---

## Troubleshooting

### Holes in dark shadows, water or other black areas

Almost always nodata=0 on 8-bit RGB. Run the toggle test in Step 1 to confirm, then clear it with `-a_nodata none`.

One other possibility if you have already built pyramids on a file that still has nodata=0: average resampling can manufacture holes that are not in the source. Four dark pixels reading 1, 0, 1, 0 average to 0, which is the nodata value, so the block becomes a hole in the overview.

To tell them apart, zoom right in past native resolution so QGIS reads the base image rather than an overview. If the hole closes as you zoom in, the pyramids created it. If it is still there at full resolution, it was in the source.

Either way the fix is the same: clear the nodata, then rebuild the pyramids on the cleaned file.

### Black fringe around the ortho edge

**Cause.** Your alpha band had feathered edges with intermediate transparency values. A mask band only understands valid or invalid, so those partial values had to be forced one way or the other. Any non-zero alpha counts as fully valid, so the feathered fringe came through as opaque. Underneath, those pixels are near-black because there was never real image data there.

This only affects Profile A. Profile B keeps the alpha band intact and never has this problem.

Note that this is a separate mechanism from the shadow holes. If `-a_nodata none` also brought back genuine dark pixels in shadows and water, that is correct behaviour and unrelated to the fringe. A thin edge line you can crop is a better outcome than holes scattered through the scene.

**Option 1: leave it.** On a basemap it is a dark line at the survey boundary. Nobody comments.

**Option 2: clip it back.**

1. **Raster > Conversion > Polygonize (Raster to Vector)** on the original file's alpha band. Gives you the footprint as a polygon.
2. **Vector > Geoprocessing Tools > Buffer** with a negative distance. Try -0.05, which is 5 cm, roughly four pixels at 1.3 cm GSD.
3. **Raster > Extraction > Clip Raster by Mask Layer** using that buffered polygon.

Losing 5 cm off the boundary of a 250 metre site is nothing.

**Option 3: avoid it next time.** Have the processing software export a hard-edged alpha rather than a feathered one. Metashape has a setting for this in the ortho export options.

### A `.msk` file appeared next to the output

The mask was written externally instead of inside the TIFF. Add `--config GDAL_TIFF_INTERNAL_MASK YES` to the Translate extra parameters and run again. Recent GDAL usually handles this on its own.

### Pyramids seem to have stopped working

If you used External format, check the `.ovr` is still sitting next to the `.tif` with the exact same base name. There is no search path and no way to point at a different location. Move it and the pyramids are simply not used.

### It got faster but you are not sure why

Windows caches recently read files in RAM, so anything that pulled the whole file through memory will make the next few loads feel fast regardless of what you changed. Restart QGIS, or better reboot, then load it cold. That is the only honest test.

### The whole QGIS instance is slow even with the layer unticked

Unticking does not close the dataset. Check for `yourfile.tif.aux.xml` in the folder. If it is missing, QGIS cannot cache band statistics and is recomputing across every pixel repeatedly. Usually a permissions problem. Also set Contrast enhancement to **No enhancement** in the Symbology tab, since any stretch on 8-bit RGB gains nothing and forces a full statistics pass.

---

## Two rules

**Keep the original whenever you use Profile A.** You made a lossy copy. Do not delete the source, and do not hand a client a JPEG-compressed file as their only version.

**Batch it.** Right-click any algorithm in the toolbox and choose Execute as Batch Process. Drop in a folder of orthos, apply the same profile to all of them, then run overviews as a second batch. That is a whole flying season's post-processing in two dialogs.
