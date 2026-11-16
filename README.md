# Raster Optimiser

![A large raster panning jerkily in QGIS, then the Optimise raster algorithm running on it, then the same file panning smoothly](docs/images/raster-optimiser_gif.gif)

*Before, during and after: a slow raster, the plugin run, and the same file panning smoothly.*

![Poster: large orthomosaics, elevation models and multispectral rasters pan slowly in QGIS until they are rewritten as Cloud Optimized GeoTIFFs](docs/images/poster.png)

*Why the tool exists: field and survey rasters are often slow to pan in
QGIS, and the fix is a set of GDAL settings that are easy to get wrong by
hand.*

A QGIS Processing plugin that converts slow, unoptimised orthomosaics,
elevation and multispectral rasters into fast-panning Cloud Optimized
GeoTIFFs (COGs) - the right tiling, pyramid and compression settings
applied automatically, no GDAL flags to memorise.

**What it processes.** 8-bit RGB imagery (3 or 4 band) gets a real choice
between 'Analysis' and 'Viewing'. Float32, Float64 and integer data of any
band count is written losslessly, at its own data type, once detection has
confirmed it is not classified. Classified rasters and files with no
coordinate reference system are refused with a clear reason rather than
processed into something quietly wrong.

Float64 elevation is kept as Float64 by default, and there is one opt-in
exception to the otherwise-absolute promise that 'Analysis' preserves every
value: ticking **Store Float64 elevation as Float32** under Advanced
parameters stores a single-band Float64 DSM, DTM or CHM at Float32 instead -
about a third of the finished file size, for a height change the run states
exactly and verifies pixel by pixel afterwards. On a real 16000 x 16000 LiDAR
DSM that is 1.02 GiB down to 358 MiB, with no height moving more than
0.0038 mm. Leave the box clear and nothing is rounded.

See `docs/GeoKlein_raster_optimisation_workflow.md` for the full workflow and
design notes.

![Layer Properties for the source raster and the optimised output side by side, with the file size and compression fields marked](docs/images/before_and_after.png)

*Source on the left and optimised output on the right, with the size and
compression fields marked.*

## How to use it

**Install.** From the QGIS Plugin Manager: Plugins, Manage and Install
Plugins, then search for GeoKlein Raster Optimiser. This is the primary
install path now that the plugin is listed on
https://plugins.qgis.org/plugins/raster_optimiser/.

Alternatively, download the release zip `raster_optimiser-1.1.1.zip` from
https://github.com/GeoKlein-Ltd/raster-optimiser/releases, then Plugins,
Manage and Install Plugins, Install from ZIP.

Do not use the green Code button's Download ZIP. It produces
`raster-optimiser-main.zip`, which nests the plugin one folder deep and
bundles the docs and tools, and QGIS cannot install it: the install fails
with ModuleNotFoundError. Use the Plugin Manager or the release zip above.

**Run.** Processing, Toolbox, GeoKlein, Optimise raster. Or the Raster menu,
GeoKlein Raster Optimiser, Optimise raster. Pick the input raster, choose
'Analysis' or 'Viewing', choose where to save the output, and run.

![The Optimise raster algorithm open in the QGIS Processing Toolbox, with the input raster, the 'Analysis' or 'Viewing' choice and the output path fields](docs/images/plugin_dialog.png)

*The Optimise raster algorithm in the Processing Toolbox, with the input
raster, the 'Analysis' or 'Viewing' choice, and the output path.*

![The QGIS Processing log after a completed run, with the detection, profile decision and end-of-run summary lines marked](docs/images/log_after_run.png)

*The Log tab after a run, with the detection, profile decision and
end-of-run summary lines marked.*

![Layer Properties, Information tab, for the output file, listing the GEOKLEIN_1 to GEOKLEIN_7 metadata keys](docs/images/file_metadata.png)

*Layer Properties, Information: the GEOKLEIN_1 to GEOKLEIN_7 keys record
what was detected, what was decided, and the command that reproduces the
file.*

All seven keys are written on every run, so a file found a year later
explains itself without the log that produced it. Where a check did not
apply, the key says so rather than being left out: `GEOKLEIN_6` reads
"Not applicable: this check is only for 8-bit imagery." on an elevation
raster, for instance. If you are comparing outputs
from 1.0.x, that one key was absent rather than explicit on most
files.

The output is always a real file on disk: an ordinary Cloud Optimized GeoTIFF
that opens in any software that can read GeoTIFFs. You can send it to a client
or put it on a server exactly as it is, with no separate export step. By
default it is written to a temporary location that QGIS may clear later, which
is why you should set a proper output path before you run.

Use that Processing algorithm, not the right-click Export, Save As in the
Layers panel. Save As is a separate QGIS tool that writes a plain GeoTIFF
with none of the tiling, pyramids or compression this plugin adds, so its
result is not the plugin's output and should not be judged as such.

## What to expect

Seven things that look like problems and aren't.

**The output looks slightly different in colour.** On 16-bit and multispectral
imagery, QGIS calculates its own contrast stretch for each layer, and two
layers can end up with slightly different minimum and maximum values even when
their pixels are identical. Copy the symbology from one layer to the other and
the difference should disappear.

**How to confirm the data is unchanged.** Use the Processing tool **Raster layer statistics** (Processing Toolbox, under QGIS > Raster analysis, or type its name in the Toolbox search box) and run it on the source and on the output, then compare. Count, Minimum and Maximum will match exactly. Mean and Standard deviation will agree to about twelve digits but not every digit: the two files store their pixels in differently shaped blocks, so the totals get added up in a different order, and that last-digit difference is arithmetic rather than a change to your data.

Do not use the statistics in Layer Properties > Information for this. Those can be approximate, computed from a shrunk copy of the image rather than every pixel, and they can also be read from a `.aux.xml` file saved next to the raster by an earlier session. Either way they can differ between two files whose pixels are identical. On one real 24000 x 24000 DSM the approximate minimum and maximum differed from the true ones in the second decimal place, and the reported share of valid pixels differed by more than a percentage point.

If you ticked **Store Float64 elevation as Float32**, Minimum and Maximum will not match either, by up to the rounding the run reported. That is the option working as described, not a fault.

**The output is larger than the source.** This happens when the source file
was already compressed and you chose 'Analysis', or when you chose 'Viewing'
on a file that was already compressed for viewing. If the source did not already
have pyramids, building them adds roughly a third to the base image size. If
it already had pyramids, the increase instead comes from the compression
change or from restructuring the file into a valid Cloud Optimized GeoTIFF.
Either way, this is the cost of the speed improvement: the file is faster to
pan, not smaller.

**The output has slightly different georeferencing text.** Some files store
their CRS as a BOUNDCRS: an EPSG code wrapped in a particular datum
transformation to WGS 84. GDAL does not keep that wrapper when it writes a
GeoTIFF, so the output carries the bare EPSG code instead. This is GDAL's
behaviour, not this plugin's: a plain gdal_translate with no options does the
same thing. The origin, pixel size and extent are byte-identical between
source and output. What changes is the datum transformation a later consumer
uses when it reprojects, because it now picks one itself rather than following
the one named in the file. That shift is usually small, but how small depends
entirely on which transformation was dropped, and for some files it is larger.
To check your own case, open the source and the output together, reproject
both to WGS 84 or a web basemap, and zoom in on a known point: any gap between
them is the difference between the two transformations. The bare EPSG code is
often the better default, since it lets PROJ choose the best transformation
for the area rather than a fixed one from the source software.

**Running the plugin on its own output.** If you take a file written for
viewing and run it again for analysis, the result will be substantially larger
with no gain in accuracy: the pixel values were already changed by the first
pass, and preserving them now keeps those changed values rather than recovering
the originals. If you run it again for viewing instead, the file is compressed
a second time rather than the first, so a little more detail is lost each
time, though pan and zoom speed stay the same either way. The plugin warns
when it detects either case. For measurement work, or for a clean copy, run it
on the original file instead.

**Running the plugin again with the same purpose on a file it already
optimised.** If you run the plugin a second time with the same purpose on a
file it has already converted, it will not redo the work silently. It stops
before converting and tells you the file is already tiled, has pyramids, is
already using the target compression, and is already a valid Cloud Optimized
GeoTIFF, so converting it again would not make it faster or smaller. To force
it anyway, tick "Reprocess even if already optimised" under Advanced
parameters.

**Running the plugin on a file optimised by an earlier version of the
plugin.** This version is the first to always write a genuine Cloud Optimized
GeoTIFF. A file from an earlier version can already be tiled, have pyramids,
and be on the target compression, and still not be a valid COG, because a COG
also requires the file's index to sit at the front rather than after the
image data. The plugin reprocesses it anyway, even without ticking Reprocess,
to add the missing structure. For a file that was written for viewing, this
means decoding and re-encoding the JPEG data, so the pixel values change
slightly, the same as running the plugin twice for viewing above. For any
other file, pan and zoom speed and file size stay about the same, and only
the byte layout changes.

**The log mentions Float32 on a Float64 file.** Nothing is wrong, and
nothing was changed. A Float64 raster is written back out as Float64, with
every value preserved exactly; Raster layer statistics on the two files gives
the same count, minimum and maximum, as described above. The note appears
because Float64 records heights far more finely than any survey can measure
them, so the smaller option is worth knowing about. Acting on it means
ticking **Store Float64 elevation as Float32** under Advanced parameters, on
the run itself - it is this plugin's own option, and there is nothing to do
in other software.

That note deliberately quotes no figure for your file. Working out the exact
worst-case rounding means reading every pixel, which is not worth doing on a
run where nobody asked for the option. Tick the box and the tool measures it
properly, keeps Float64 anyway if the rounding would exceed a millimetre, and
records both the guaranteed bound and the largest change it actually made in
the file's own metadata. The bound doubles each time the heights double, so
it stays slight across any real survey: about 0.00095 mm on a site DSM
reaching 24 m, and still only about 0.49 mm at Everest's 8848 m. Where the
option cannot apply to a file - a multi-band Float64 raster, which is not
elevation - the log says so instead, so you are never left ticking a box
that would do nothing.

## Licence

The source code is licensed under the GPL. The GeoKlein name and the cheetah
logo are not covered by that licence and remain the property of GeoKlein Ltd.
If you fork this plugin, please replace the branding - the code is yours to
use.

Not required, but very welcome: a credit back to GeoKlein, and a message
telling me what you're building. I'd genuinely like to see where this ends up.

Full license text: `LICENSE`. The name and logo carve-out is also recorded
in `NOTICE`, which ships alongside `LICENSE` in the plugin package. Keep the
two in step if either changes.

## Contact

- Email: mail@geoklein.com
- Issues: https://github.com/GeoKlein-Ltd/raster-optimiser/issues
