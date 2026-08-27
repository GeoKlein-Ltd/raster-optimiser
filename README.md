# Raster Optimiser

A QGIS Processing plugin that detects slow, unoptimised orthomosaics and
elevation rasters and converts them to fast-panning, correctly-compressed
GeoTIFFs - the right tiling, pyramid and compression settings applied
automatically, no GDAL flags to memorise.

See `docs/GeoKlein_raster_optimisation_workflow.md` for the full workflow and
design notes.

## What to expect

Five things that look like problems and aren't.

**The output looks slightly different in colour.** On 16-bit and multispectral
imagery, QGIS calculates its own contrast stretch for each layer, and two
layers can end up with slightly different minimum and maximum values even when
their pixels are identical. Copy the symbology from one layer to the other and
the difference disappears. To confirm the data is unchanged, compare the band
statistics in Layer Properties: they will match exactly.

**The output is larger than the source.** This happens when the source was
already compressed and you chose Analysis. If the source did not already have
pyramids, building them adds roughly a third to the base image size. If it
already had pyramids, the increase instead comes from the compression change
or from restructuring the file into a valid Cloud Optimized GeoTIFF. Either
way, this is the cost of the speed improvement: the file is faster to pan,
not smaller.

**The output has slightly different georeferencing text.** Some files store
their CRS as a BOUNDCRS, an EPSG code wrapped in a datum transformation. GDAL
does not preserve that wrapper when writing, so the output carries the bare
EPSG code instead. The origin, pixel size and extent are byte-identical, and
the difference in where QGIS draws the two files is under two centimetres. This
is GDAL's behaviour rather than this plugin's, and the bare EPSG code is
arguably the more accurate of the two.

**Running the plugin on its own output.** If you take a file written for
viewing and run it again for analysis, the result will be substantially larger
with no gain in accuracy: the pixel values were already changed by the first
pass, and preserving them now keeps those changed values rather than recovering
the originals. The plugin warns when it detects this. For measurement work, run
it on the original file.

**Running the plugin again with the same purpose on a file it already
optimised.** If you run the plugin a second time with the same purpose on a
file it has already converted, it will not redo the work silently. It stops
before converting and tells you the file is already tiled, has pyramids, and
is already using the target compression, so converting it again would not
make it faster or smaller. To force it anyway, tick "Reprocess even if
already optimised" under Advanced parameters.

## Licence

The source code is licensed under the GPL. The GeoKlein name and the cheetah
logo are not covered by that licence and remain the property of GeoKlein. If
you fork this plugin, please replace the branding - the code is yours to use.

Not required, but very welcome: a credit back to GeoKlein, and a message
telling me what you're building. I'd genuinely like to see where this ends up.

Full license text: `LICENSE`.

## Contact

- Email: mail@geoklein.com
- Issues: https://github.com/GeoKlein-Ltd/raster-optimiser/issues
