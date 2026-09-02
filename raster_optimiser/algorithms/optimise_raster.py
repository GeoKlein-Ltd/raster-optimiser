"""QGIS Processing algorithm wrapping the Raster Optimiser engine.

Thin wrapper only. All detection and conversion logic lives in core/
(detector.py, converter.py), which have no QGIS dependency and are
untouched by this file - it declares Processing parameters, translates
them into calls into core/, and maps the results back onto QGIS
feedback/exceptions. See docs/plugin_design_notes.md for why an
already-optimised file's OUTPUT resolves to the source path rather
than a copy.

NoData handling (NODATA_MODE below) is a three-option dropdown -
Automatic / Reveal hidden pixels / Keep as-is. Automatic decides per
file from detect()'s own assessment; Reveal and Keep are full manual
overrides. Choosing Keep as-is on a file where detection found real
content behind NoData used to be a checkParameterValues() hard gate,
tried twice, reverted twice - both times it produced an unclosable
modal loop in GUI testing (OK dismisses the QMessageBox, Run fires it
again, forever), because checkParameterValues() has no "I've considered
this and want to proceed anyway" state to escape into: it can only
refuse. Picking a genuinely different value from the SAME dropdown that
the refusal is about doesn't fix that, no matter how it reads on paper
- the user has already made their real choice by the time the modal
reappears identically. Keep as-is is a fully legitimate, supported
choice, so it belongs where every other legitimate choice this plugin
doesn't second-guess lives: a prominent processAlgorithm() log message,
not a gate. Automatic being the default is what protects a novice here
- selecting Keep as-is at all is already the deliberate override, not
something that then also needs blocking.

QGIS renders a checkParameterValues() failure as a modal QMessageBox
with only an OK button (confirmed via processing/gui/algorithm_widget.py),
not a dismissible inline banner - framework behaviour, not something
this file can change. That rules out checkParameterValues() for
anything a user might legitimately want to proceed past, or that the
tool can already handle correctly on its own. Per the "purpose-question
rework"'s three-bucket rule (Phase 2): a check belongs in
checkParameterValues() only when changing a parameter in THIS dialog
fixes the exact problem being reported; everything else either coerces
and logs (Bucket A - lossy/Viewing requested on a forced-lossless file,
"Reveal hidden pixels" with nothing to reveal) or raises a
QgsProcessingException in processAlgorithm() (Bucket C - classified
rasters, no CRS, and the other detection.refused codes, none of which
any dialog parameter can fix). Only one case still blocks here:
already-optimised-with-nothing-left-to-gain (Bucket B), escapable via
Force reprocess, the one parameter that actually changes the outcome
being reported. It's compression-aware, not just tiling/overviews: a
file that's tiled with overviews but still on a non-target codec (LZW,
DEFLATE, uncompressed) has real file size to gain, so that case
proceeds instead of blocking - see core/converter.py's
already_optimised_at_target(), which this module imports rather than
mirroring, and its use in convert(). Bucket A and Bucket C
both live in the pure-GDAL core or at the top of processAlgorithm(), so
they run on every entry route (GUI, processing.run(), batch, Processing
models) - no parameter switches either off.

Cross-version note (QGIS 3.x / PyQt5 vs QGIS 4.x / PyQt6): this file
deliberately touches no raw Qt widget classes and no QVariant - only
core Processing framework classes (QgsProcessingAlgorithm and its
parameter types), which are far more stable across the Qt5->Qt6
migration than UI code would be. That's been true in practice too:
everything used here has behaved identically on both a 3.44 LTR and a
4.2 install tested side by side on this machine.
"""

import os
import time

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from ..core.converter import (
    convert, output_exists_message, output_same_as_source_message,
    _same_file, already_optimised_at_target,
)
from ..core.detector import detect, detect_metadata_only, resolve_profile_reason
from ..icon_utils import plugin_icon

# No letters anywhere: "A"/"B" imply an order (A primary, B fallback)
# that's backwards from how this choice should be reached for, force
# learning a mapping before the choice means anything, and - the sharper
# risk - a letter-to-meaning mapping is something a future edit can get
# backwards without it showing up anywhere except the output. An earlier
# version of this file kept detector.py/converter.py's profile identity
# as "A"/"B" and bridged it here with a label dict; that was reverted
# because the bridge itself was the hazard - once the UI listed lossless
# first while "A" still meant lossy internally, anyone reading the dict
# would see letters running opposite to the displayed order and could
# "fix" it, silently inverting which compression each choice produces.
# detector.py/converter.py's profile identity strings are "lossy" and
# "lossless" now too (see their RECOMMENDED_SETTINGS keys and
# ProfileOption.profile) - self-describing everywhere, so there's no
# mapping left for anyone to get wrong. PURPOSE_ANALYSIS/PURPOSE_VIEWING
# below are only this wrapper's own dropdown-index constants (renamed
# from PROFILE_LOSSLESS/PROFILE_LOSSY when the PROFILE parameter itself
# became PURPOSE - see the PURPOSE class attribute - since those old
# names described the mechanism the dropdown used to ask about, not the
# purpose it asks about now). Same reasoning applies to
# NODATA_AUTO/REVEAL/KEEP further down: this file hardcodes the literal
# "auto"/"reveal"/"keep" strings at the one point they cross into
# convert(), rather than importing converter.py's NODATA_MODE_*
# constants under a second, index-shaped name here.
PURPOSE_ANALYSIS = 0
PURPOSE_VIEWING = 1
_PURPOSE_ANALYSIS_NAME = "Analysis: every pixel value preserved"
_PURPOSE_VIEWING_NAME = "Viewing: smallest possible file"
# No PURPOSE_OPTIONS list here: self.tr() needs a live algorithm
# instance to resolve against, and there is none yet at module level,
# only from the moment an OptimiseRasterAlgorithm() actually exists -
# see initAlgorithm(), which builds the translated options list fresh
# on every instance rather than once at import time.

# Index order matches the NODATA dropdown's options list, built the
# same way in initAlgorithm() below, exactly - see _NODATA_MODE_STRINGS
# for the one place this crosses into convert()'s plain
# "auto"/"reveal"/"keep" strings.
NODATA_AUTO = 0
NODATA_REVEAL = 1
NODATA_KEEP = 2
_NODATA_AUTO_SHORT_NAME = "Automatic"
_NODATA_AUTO_NAME = f"{_NODATA_AUTO_SHORT_NAME}: decide per file (recommended)"
_NODATA_REVEAL_NAME = "Reveal hidden pixels"
_NODATA_KEEP_NAME = "Keep as-is"
NODATA_MODE_LABEL = "Hidden pixels (NoData)"
_NODATA_MODE_STRINGS = ("auto", "reveal", "keep")  # index -> convert()'s nodata_mode

FORCE_REPROCESS_LABEL = "Reprocess even if already optimised"

# ConversionResult.action values that mean "this run did not succeed" -
# see core/converter.py's ConversionResult docstring for the full set.
# converted_unverified is deliberately in here, not treated as a soft
# warning: verification failing means "say so loudly rather than
# reporting success" (the explicit instruction this was built against),
# so it fails the Processing run rather than returning a green checkmark
# with a caveat buried in the log.
_HARD_FAILURE_ACTIONS = (
    "error", "blocked", "converted_unverified", "refused_upstream",
)


def _resolved_profile_for_target(detection, purpose_choice):
    """The actual lossy/lossless identity that will be used, needed to
    look up the target compression. Any forced case (profile_mode ==
    "forced": elevation, CONTINUOUS, or RGB_8BIT's nodata_only_
    transparency) always resolves via detection.forced_profile
    regardless of what PURPOSE is set to; everything else resolves from
    the dropdown."""
    if detection.profile_mode == "forced":
        return detection.forced_profile
    return "lossless" if purpose_choice == PURPOSE_ANALYSIS else "lossy"


class OptimiseRasterAlgorithm(QgsProcessingAlgorithm):

    INPUT = "INPUT"
    PURPOSE = "PURPOSE"
    NODATA_MODE = "NODATA_MODE"
    FORCE_REPROCESS = "FORCE_REPROCESS"
    OVERWRITE = "OVERWRITE"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        # QgsProcessingAlgorithm.tr() is not reliably inherited - confirmed
        # missing outright on QGIS 4.2 (PyQt6). PyQt5 (QGIS 3.x) injects a
        # tr() automatically on QObject subclasses in a way PyQt6 doesn't,
        # so this can't be left implicit. QCoreApplication.translate() is
        # the version-independent form.
        return QCoreApplication.translate("OptimiseRasterAlgorithm", string)

    def createInstance(self):
        return OptimiseRasterAlgorithm()

    def icon(self):
        return plugin_icon()

    def name(self):
        return "optimise_raster"

    def displayName(self):
        # Sentence case (title case reads off-convention next to QGIS's
        # own algorithms), and no parenthetical - it truncated in Model
        # Designer. What it does belongs in shortHelpString(), not here.
        return self.tr("Optimise raster")

    def shortDescription(self):
        # One sentence, shown as the toolbox tree's hover tooltip -
        # deliberately not shortHelpString() (the full help panel) or
        # displayName() (just the name). Default implementation falls
        # back to the first line of shortHelpString(), which isn't
        # useful as a tooltip on its own - this overrides it.
        return self.tr(
            "Makes slow, oversized rasters load and pan quickly in "
            "QGIS or any other GDAL-based software."
        )

    def group(self):
        # Empty on purpose: with only one algorithm, an extra group
        # level is redundant either way - Toolbox collapses straight to
        # {provider name} -> Optimise raster (see provider.py's name(),
        # "GeoKlein" as of Phase 6 of the purpose-question rework - was
        # "Raster Optimiser" when this was originally written, hence
        # this staying empty rather than repeating that name a second
        # time). Doesn't touch id() / name(), so this can't affect
        # anything that references the algorithm by ID (Model Designer,
        # qgis_process, saved models).
        return ""

    def groupId(self):
        return ""

    def tags(self):
        # Plain keywords for toolbox search, not translated - written for
        # someone typing their symptom ("slow", "large") or their gear
        # ("drone", "lidar", "qfield"), not the mechanism.
        # Kept in sync with metadata.txt's tags= line (that one drives
        # Plugin Manager search; this one drives Processing toolbox
        # search - they're separate mechanisms QGIS never cross-checks).
        return [
            "raster", "orthomosaic", "ortho", "optimise", "optimize",
            "optimiser", "optimizer", "slow", "slow raster", "large",
            "large file", "reduce file size", "shrink", "pyramids",
            "overviews", "compress", "compression", "tiling", "tiled",
            "tiles", "geotiff", "cog", "cloud optimised geotiff",
            "cloud optimized geotiff", "jpeg", "zstd", "performance",
            "speed", "speed up", "basemap", "drone", "photogrammetry",
            "lidar", "dsm", "dtm", "chm", "elevation", "qfield",
        ]

    def shortHelpString(self):
        # QGIS renders this as HTML (the Processing help panel is a
        # rich-text view, not plain text), so this uses real <p>,
        # <ol>/<ul>/<li>, <b> and <i> tags. Bold IS used for section
        # headings and for inline emphasis here, by explicit content
        # decision. Parameter names are <b><i> (bold + italic); option
        # VALUES - Analysis / Viewing, and the Automatic / Reveal /
        # Keep NoData choices - stay <i> only, so the parameter-vs-value
        # distinction reads at a glance. Known tradeoff, kept on record:
        # QGIS's own
        # help-panel template gives <b> text a fixed, non-theme-aware
        # colour that reads as low-contrast dark-grey-on-dark-grey in
        # the Night Mapping theme (background #535353) and other dark
        # themes - an earlier version of this string dropped <b>
        # headings for exactly that reason. No single inline colour
        # fixes it (nothing satisfies WCAG contrast against both Night
        # Mapping's dark background and the default theme's near-white
        # one at once), so on dark themes the bold text is legible but
        # dull rather than prominent. Accepted deliberately: most users
        # are on the default light theme, where the bold structure
        # earns its keep, and swapping <b> back out is a one-line
        # revert if that ever changes. docs/raster_optimiser_ui_text.md
        # mirrors this string and must be updated with it.
        return self.tr(
            "<p><b>What does it do?</b></p>"
            "<p>Makes rasters load and pan quickly in QGIS, QField, or "
            "any other GDAL-based software, and also reduces their file "
            "size. A raster does not have to be large to be slow. The "
            "output is always a Cloud Optimized GeoTIFF (COG), never a "
            ".jpg file.</p>"

            "<p><b>Reasons your file is slow</b></p>"
            "<p>Most orthomosaics and elevation rasters come out of "
            "processing software without two things GDAL needs to draw "
            "them quickly.</p>"
            "<ol>"
            "<li><b>No pyramids.</b> QGIS reads every pixel in the "
            "raster to draw a zoomed-out view, and does it again on "
            "every pan and every zoom.</li>"
            "<li><b>No tiling.</b> QGIS cannot read one corner of the "
            "image without reading the full width of every row that "
            "corner sits in.</li>"
            "</ol>"
            "<p>Both mean QGIS has to do more work than the view on "
            "screen actually requires. This tool adds pyramids and "
            "tiling, and picks a compression setting appropriate to the "
            "nature of the raster, for example an orthomosaic against a "
            "DTM, so it also takes up less disk space.</p>"

            "<p><b>What does each parameter do?</b></p>"
            "<p><b><i>Input layer</i></b>: the raster to optimise. Anything "
            "GDAL can read, apart from classified rasters and files "
            "with no coordinate reference system. Your source file is "
            "never modified.</p>"
            "<p><b><i>What will you use this file for</i></b>: <b><i>Analysis</i></b> "
            "or <b><i>Viewing</i></b>.</p>"
            "<p><b><i>Analysis</i></b> compresses the raster and keeps every "
            "pixel value exactly as the original. Use it for anything "
            "you take pixel values from: height measurements, "
            "vegetation indices, segmentation, classification, change "
            "detection, raster calculations.</p>"
            "<p><b><i>Viewing</i></b> compresses the raster and discards "
            "detail the eye will not notice, which makes the file "
            "considerably smaller. A typical drone orthomosaic comes "
            "out 80 to 90% smaller. Use it for basemaps, client "
            "copies, QField backdrops and site context, where what "
            "matters is how the raster looks rather than what its "
            "pixels measure.</p>"
            "<p>Since both profiles add pyramids and tiling, there is "
            "no difference in loading, panning or zooming speed. The "
            "choice affects file size, and whether pixel values are "
            "preserved.</p>"
            "<p>Not every file can be written for Viewing. Elevation "
            "(DEMs), 16-bit and multispectral imagery are always "
            "written for Analysis, because Viewing would alter the "
            "pixel values and silently distort the measurements. You "
            "might only notice once you compare against the "
            "original.</p>"
            "<p>8-bit RGB files are also forced to Analysis when their "
            "transparent border is marked by a NoData value rather than "
            "an alpha band, because Viewing shifts pixel values "
            "slightly and a border marked by value would no longer "
            "match. A border marked by an alpha band is defined by "
            "position, so it survives.</p>"
            "<p>Where Analysis is forced, the tool says why in the log "
            "and in the file's own metadata, under Layer Properties, "
            "Information.</p>"
            "<p>If you are not sure, choose Analysis. It is the safest "
            "option: your file still ends up smaller and faster, just "
            "not as small as Viewing would make it.</p>"
            "<p>The three settings below are under <b>Advanced "
            "parameters</b>.</p>"
            "<p><b><i>Hidden pixels (NoData)</i></b>: what to do when a file "
            "marks transparency with a NoData value of 0. On 8-bit "
            "imagery that is unsafe, because 0 is also the value of a "
            "black pixel, so deep shadow and dark water can be treated "
            "as empty and punched out as holes.</p>"
            "<ul>"
            "<li><b><i>Automatic</i></b> checks each file and only clears "
            "NoData where real content is hidden behind it.</li>"
            "<li><b><i>Reveal hidden pixels</i></b> always clears it, which "
            "brings the content back but can render the collar as a "
            "solid black border.</li>"
            "<li><b><i>Keep as-is</i></b> leaves the file's NoData setting "
            "untouched.</li>"
            "</ul>"
            "<p><b>Note:</b> even though it may appear that way, this "
            "operation does not fill holes. It uncovers hidden pixels "
            "that were already there. It does not interpolate or create "
            "data. Where a raster has holes because the data is "
            "genuinely missing rather than hidden, this plugin cannot "
            "fill them.</p>"
            "<p><b><i>Reprocess even if already optimised</i></b>: by default "
            "a file is left alone only when it is already tiled, has "
            "pyramids, is at the target compression, and is already a "
            "valid Cloud Optimized GeoTIFF (COG), since converting it "
            "again would not make it faster or smaller. A file that "
            "meets only the first three, tiled and pyramided at the "
            "target compression but not yet a valid COG, is reprocessed "
            "anyway. Tick this to convert an already-valid file again, "
            "for instance to change how NoData is handled.</p>"
            "<p><b><i>Replace existing output file</i></b>:</p>"
            "<ul>"
            "<li><b><i>Unticked</i></b>, the tool stops rather than "
            "overwriting a file that already exists at the output path, "
            "and tells you what it found.</li>"
            "<li><b><i>Ticked</i></b>, the file is replaced.</li>"
            "</ul>"

            "<p><b>What it will not process</b></p>"
            "<p>Some rasters cannot be optimised safely with these "
            "settings. The tool detects them and stops, rather than "
            "handing you compromised data that looks fine.</p>"
            "<ul>"
            "<li>Classified rasters: land cover, species class, "
            "or any map where pixel values are category codes rather "
            "than measurements. Building pyramids averages neighbouring "
            "pixels, and averaging two category codes produces a third "
            "that means nothing.</li>"
            "<li>Files with no coordinate reference system.</li>"
            "</ul>"

            "<p><b>Things that look wrong but are not</b></p>"
            "<p><b><i>Why does the zoomed-out view look slightly "
            "different?</i></b> Flick between your source and the output "
            "zoomed out and you may see pixels shift or shimmer "
            "slightly. This is the pyramids. Your source has none, so "
            "QGIS builds its zoomed-out view on the fly each time. The "
            "output has real pyramids, built by averaging. Two different "
            "ways of shrinking the same image, so they will not match "
            "exactly. Zoom in to full resolution and the difference "
            "goes. It happens on Analysis too, where every pixel value "
            "is preserved.</p>"
            "<p><b><i>Why do the colours look slightly different?</i></b> "
            "On 16-bit and multispectral imagery, QGIS works out its "
            "own contrast stretch for each layer, so two layers can "
            "look different even when their pixels are identical. Copy "
            "the symbology from one to the other and the difference "
            "disappears.</p>"
            "<p><b><i>Why is the output larger than the source?</i></b> "
            "This happens when the source was already compressed. "
            "Pyramids and the COG structure add bytes back. The file is "
            "faster to pan, not smaller.</p>"

            "<p><b>Glossary</b></p>"
            "<ul>"
            "<li><b>Alpha band</b>: an extra band recording which "
            "pixels fall inside the surveyed area. It works by position "
            "rather than by value, so it never mistakes a black pixel "
            "for an empty one.</li>"
            "<li><b>Band</b>: one layer of values in a raster. A colour "
            "photograph has three, red, green and blue. An elevation "
            "model has one. Multispectral imagery has more, often "
            "including light the eye cannot see.</li>"
            "<li><b>Bit depth</b>: how much range each pixel value has. "
            "8-bit holds 0 to 255, which is enough for colour. 16-bit "
            "and Float32 hold far more, which is what rasters holding "
            "more complex measurements need.</li>"
            "<li><b>Cloud Optimized GeoTIFF (COG)</b>: a GeoTIFF that "
            "is tiled, has pyramids, and keeps its index at the front "
            "of the file rather than after the image data. Software can "
            "then read one small part of it without reading the whole "
            "thing, including over a network, which is what the format "
            "was designed for.</li>"
            "<li><b>Collar</b>: the transparent border around a survey "
            "area, where the image does not completely fill the "
            "rectangular bounding box of the file when loaded. Keeping "
            "it transparent is the alpha band's job. Without one you "
            "get a black box around your raster.</li>"
            "<li><b>Lossless and lossy</b>: lossless compression makes "
            "a file smaller with every pixel value still recoverable "
            "exactly. Lossy compression makes it much smaller by "
            "discarding detail, and the discarded detail does not come "
            "back.</li>"
            "<li><b>NoData</b>: a pixel value the file declares to mean "
            "\"nothing here\". Safe on elevation data, where you can "
            "pick a value no real height could be, such as -9999. Risky "
            "on 8-bit imagery, where every value from 0 to 255 is a "
            "legitimate colour and 0 means black.</li>"
            "<li><b>Pyramids, also called overviews</b>: pre-built "
            "smaller copies of the whole image at successive zoom "
            "levels. Faster because QGIS loads one small copy instead "
            "of the full image.</li>"
            "<li><b>Resampling</b>: working out the pixel values for a "
            "smaller, lower resolution copy of an image from the pixels "
            "it replaces. Averaging is the usual method, and it is why "
            "pyramids cannot be built safely on classified "
            "rasters.</li>"
            "<li><b>Tiling</b>: storing the image as small squares "
            "instead of full-width rows.</li>"
            "</ul>"

            "<hr>"
            "<p>Made by GeoKlein Ltd, Edinburgh. Built on GDAL.<br>"
            "Report problems: "
            "https://github.com/GeoKlein-Ltd/raster-optimiser/issues</p>"
        )

    def _already_optimised_message(self):
        return self.tr(
            "This file is already tiled and has pyramids built, so it "
            "should already load and pan quickly in QGIS or any other "
            "GDAL-based software. It's also "
            "already using the target compression, so reprocessing "
            "wouldn't shrink it either.\n"
            "\n"
            "Converting it again won't make it any faster or smaller. "
            "It would produce a second large file with the structure "
            "unchanged.\n"
            "\n"
            "If you're reconverting deliberately, to change how NoData "
            "is handled, tick '{}' under Advanced parameters."
        ).format(self.tr(FORCE_REPROCESS_LABEL))

    def checkParameterValues(self, parameters, context):
        # Runs before execution and can refuse instantly, with nothing run
        # yet - QGIS 4.2 and 3.44 LTR both confirmed (via local source
        # inspection, not memory/docs) to expose this as
        # checkParameterValues(self, parameters, context) -> (bool, str).
        #
        # Deliberately calls detect_metadata_only(), never detect(): the
        # already-optimised check below - the only gate left in this
        # function (see further down in this same method for why the
        # 16-bit/multispectral block and the Reveal-has-no-effect no-op
        # that used to also live here are log-only now) - is resolvable
        # from gdal.Open() + band metadata alone, in milliseconds even on
        # a multi-gigapixel file. Running the full detect() here - which
        # pixel-samples for the classified check and the NoData
        # black-pixel risk - is exactly the bug this fixes: it cost 181s
        # on a 1.7GB ortho before the user ever saw a refusal. No
        # detection logic is duplicated here; this reads the same
        # DetectionResult shape detect() produces, just via detector.py's
        # metadata-only code path.
        #
        # detection.refused (unreadable file, no bands, unsupported
        # dtype, classified-by-colour-table, no CRS) is deliberately NOT
        # checked here any more - confirmed via a direct processing.run()
        # call on a classified raster (bypassing this function entirely)
        # that processAlgorithm()'s own "if detection.refused: raise
        # QgsProcessingException(...)" already produces the identical
        # message on its own. Checking it here too was pure duplication:
        # every one of those codes is a case checkParameterValues() can
        # never make surmountable (no parameter in this dialog turns a
        # classified raster into a non-classified one), so per this
        # module's "only gate what a parameter fixes" principle it
        # belongs solely in processAlgorithm() as a raised exception, not
        # echoed here as a returned refusal too.
        ok, msg = super().checkParameterValues(parameters, context)
        if not ok:
            return ok, msg

        # Cheapest possible checks first, before anything raster-related:
        # os.path.exists()/os.path.samefile(), not even a gdal.Open().
        # These used to only surface as a "blocked"/"error" failure from
        # convert() - after a full detection run had already cost over a
        # minute on a large file. Same messages convert() itself would
        # use (see core/converter.py's output_exists_message and
        # output_same_as_source_message docstrings for why they're
        # shared functions, not copied text).
        overwrite = self.parameterAsBoolean(parameters, self.OVERWRITE, context)
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        input_layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if input_layer is None:
            # No/invalid input yet - let the base parameter validation or
            # processAlgorithm produce that error, don't duplicate it here.
            return True, ""
        source_path = input_layer.source()

        # Unconditional - matches convert()'s own guard, which cannot be
        # bypassed by Replace existing output file either. Checked before
        # the exists check below since it's the same "never touch the
        # source" family of guard and should win regardless of overwrite.
        if output_path and _same_file(output_path, source_path):
            return False, output_same_as_source_message(output_path)

        if not overwrite:
            if output_path and os.path.exists(output_path):
                return False, output_exists_message(output_path)

        try:
            detection = detect_metadata_only(source_path)
        except Exception:  # noqa: BLE001 - never block the dialog on a quick-check crash
            return True, ""

        if detection.refused:
            # Deliberately NOT the classified/no-CRS/etc. gate any more
            # (see the comment above) - this allows the dialog to
            # proceed, and processAlgorithm() raises the actual
            # exception once it runs. This early-out exists only
            # because a refused DetectionResult can have block_size/
            # raster_size left at their None defaults (some refusal
            # codes fire before those fields are read at all, e.g.
            # NO_CRS), and already_optimised_at_target() below would
            # crash indexing into a None block_size/raster_size rather
            # than reading a meaningful "not tiled" answer.
            return True, ""

        # PURPOSE always has a genuine value (defaulted to Analysis if
        # untouched - see the PURPOSE_ANALYSIS comment), so this is safe
        # to read unconditionally.
        purpose_choice = self.parameterAsEnum(parameters, self.PURPOSE, context)

        # Bucket A of the purpose-question rework (Phase 2): Viewing
        # requested on a forced-lossless file (elevation, 16-bit/
        # multispectral, or an RGB file with NoData-only transparency
        # and no alpha band) no longer blocks here - it coerces to
        # Analysis and logs why, in processAlgorithm()'s
        # _log_profile_decision(). Likewise "Reveal hidden pixels" on a file
        # with no NoData=0 condition at all (elevation always lands
        # here, and plenty of RGB files with no NoData=0 do too) no
        # longer blocks - it's a no-op either way, so blocking it would
        # fail files in batch that would otherwise process correctly.
        # Both are now log-only, produced in core/converter.py's
        # _resolve_nodata_handling() and _log_profile_decision() respectively
        # so every entry route (GUI, processing.run(), batch, Processing
        # models, direct API) gets the identical message, not just this
        # dialog. detection.profile_mode == "choice" (RGB_8BIT, the only
        # bucket with a genuine two-option choice remaining) never has
        # an unavailable option any more either - the one file-specific
        # case that used to block it (nodata_only_transparency) is now
        # "forced" instead, per detector.py's _finish_rgb_8bit().
        #
        # Only one case still blocks here: file is already optimised,
        # below. It's the only one where changing a parameter in THIS
        # dialog (Reprocess) fixes the exact problem being reported.
        force_reprocess = self.parameterAsBoolean(parameters, self.FORCE_REPROCESS, context)
        if not force_reprocess and already_optimised_at_target(
            detection, _resolved_profile_for_target(detection, purpose_choice)
        ):
            return False, self._already_optimised_message()

        return True, ""

    def initAlgorithm(self, config=None):
        input_param = QgsProcessingParameterRasterLayer(
            self.INPUT, self.tr("Input layer"),
        )
        input_param.setHelp(self.tr(
            "The raster to optimise. Any format GDAL can read."
        ))
        self.addParameter(input_param)

        # Built here rather than at module level (see the PURPOSE_
        # constants' own comment above): self.tr() needs self, and
        # resolving the translation here means it happens fresh on
        # every algorithm instance - each dialog open, batch run, or
        # model load - not once at import, before this plugin's
        # translator is necessarily installed.
        purpose_options = [self.tr(_PURPOSE_ANALYSIS_NAME), self.tr(_PURPOSE_VIEWING_NAME)]
        purpose_param = QgsProcessingParameterEnum(
            self.PURPOSE, self.tr("What will you use this file for"),
            options=purpose_options,
            defaultValue=PURPOSE_ANALYSIS,
        )
        # Mandatory with a real default now (Analysis, index 0) -
        # supersedes the earlier "optional, no default, force a
        # conscious choice" design from a previous round. Reasoning is
        # asymmetric harm: an untouched dropdown giving a larger-than-
        # optimal file is visible and recoverable (rerun with Viewing);
        # one that silently alters pixel values is invisible and may
        # never be found. Every forced case (elevation, CONTINUOUS,
        # RGB_8BIT's nodata_only_transparency) still forces the Analysis
        # (lossless) settings outright regardless of this value - see
        # _log_profile_decision - so this default only ever matters for
        # files where it's a genuine choice. Asks about purpose rather
        # than mechanism (lossy/lossless): the workflow doc's own
        # framing is "whether you can compress lossily depends entirely
        # on what the file is for", and asking it that way is also what
        # stops the tool choosing Analysis settings on a DSM reading
        # like a contradiction of what was asked for - it's serving the
        # same purpose, just via the only settings that purpose allows
        # on that data.
        # Paragraph breaks are "<br><br>", not "\n\n": QGIS builds the
        # parameter tooltip by wrapping this help() text in a single
        # <p>...</p> and rendering it as rich text, so literal newlines
        # collapse to spaces and the whole thing shows as one unbroken
        # block (confirmed on QGIS 4.2 and 3.44 LTR - <br><br> renders
        # an identical gap on both). Kept short deliberately: the info
        # panel on the right (shortHelpString()) now carries the full
        # explanation, including which files are forced to Analysis and
        # why, so this only has to cover the choice itself.
        purpose_param.setHelp(self.tr(
            "Analysis keeps every pixel value exactly as it is. Use it "
            "for anything you take numbers from: height measurements, "
            "vegetation indices, segmentation, classification, change "
            "detection."
            "<br><br>"
            "Viewing discards detail the eye will not notice, which "
            "makes the file much smaller. A typical drone orthomosaic "
            "came out 80 to 90% smaller in testing. Use it for "
            "basemaps, client copies, QField backdrops and site context."
            "<br><br>"
            "Both load and pan at the same speed, and both are written "
            "as a Cloud Optimized GeoTIFF (COG)."
            "<br><br>"
            "Some files cannot be written for Viewing and are written "
            "for Analysis instead. The tool says why when that happens. "
            "See the panel on the right for which files and why."
            "<br><br>"
            "If you are not sure, choose Analysis. It is the safest "
            "option: your file still ends up smaller and faster, just "
            "not as small as Viewing would make it."
        ))
        self.addParameter(purpose_param)

        output_param = QgsProcessingParameterRasterDestination(
            self.OUTPUT, self.tr("Save optimised raster as"),
        )
        output_param.setHelp(self.tr(
            "Where to save the result. Always written as a Cloud "
            "Optimized GeoTIFF (COG), tiled with pyramids built in."
        ))
        self.addParameter(output_param)

        # Everything below is Advanced: Input/Purpose/Output is the
        # whole decision most runs need. Hidden pixels (NoData) moved
        # here too (previously Main) - Automatic already makes the
        # right call per file without anyone touching it, so it reads
        # as an override, not a routine choice, same as Reprocess and
        # Replace existing output file already were.

        # Same reasoning as purpose_options in initAlgorithm() above.
        nodata_options = [
            self.tr(_NODATA_AUTO_NAME), self.tr(_NODATA_REVEAL_NAME), self.tr(_NODATA_KEEP_NAME),
        ]
        nodata_param = QgsProcessingParameterEnum(
            self.NODATA_MODE, self.tr(NODATA_MODE_LABEL),
            options=nodata_options,
            defaultValue=NODATA_AUTO,
        )
        nodata_param.setFlags(nodata_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        # Automatic by default - see this module's docstring for why
        # that's no longer the rejected design it once was. Meaningless
        # on elevation and on RGB files without a NoData=0 condition,
        # same as PURPOSE is meaningless on elevation - always visible
        # anyway, since Processing parameter widgets are declared in
        # initAlgorithm() before any input is chosen and can't react to
        # it. No hard gate on this parameter any more: 'Reveal hidden
        # pixels' on a file where it would be a pure no-op, and 'Keep
        # as-is' on a file with real content behind NoData, are both
        # log-only now - see core/converter.py's _resolve_nodata_handling,
        # the one place both are decided. The run log always states what
        # happened, including "not applicable" as the defensive fallback
        # for callers that skip checkParameterValues.
        # Paragraph breaks are "<br><br>", not "\n\n" (QGIS renders this
        # tooltip as rich text in one <p>, so newlines collapse - same
        # as PURPOSE / FORCE_REPROCESS). The three option names are
        # written literally rather than spliced in from
        # _NODATA_*_NAME via .format(): keeping everything inside one
        # self.tr() means there is nothing left to translate at a splice
        # site. They must stay in step with the dropdown option labels
        # by hand - docs/raster_optimiser_ui_text.md flags that.
        nodata_param.setHelp(self.tr(
            "Many orthomosaics mark transparency using a NoData value "
            "of 0. On 8-bit imagery that's unsafe, because 0 is also "
            "the value of a genuinely black pixel, so deep shadow, "
            "dark water and wet tarmac get treated as empty and "
            "punched out as holes."
            "<br><br>"
            "'Automatic' checks each file and only clears NoData when "
            "real content is hidden behind it. Files where NoData marks "
            "nothing but the transparent collar are left alone."
            "<br><br>"
            "'Reveal hidden pixels' always clears NoData. Hidden "
            "content comes back, but the collar may render as a solid "
            "black border rather than transparent."
            "<br><br>"
            "'Keep as-is' leaves the file's NoData setting untouched."
            "<br><br>"
            "This does not fill holes. It uncovers pixels that were "
            "already there. Nothing is interpolated or created."
        ))
        self.addParameter(nodata_param)

        force_reprocess_param = QgsProcessingParameterBoolean(
            self.FORCE_REPROCESS, self.tr(FORCE_REPROCESS_LABEL),
            defaultValue=False,
        )
        force_reprocess_param.setFlags(
            force_reprocess_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced
        )
        # Unticked by default: redoing an already-optimised file is
        # normally exactly the wasted work this plugin exists to avoid
        # (see core/converter.py's already-optimised short-circuit).
        # This is its explicit override, plumbed through to convert()
        # itself (not just gated in the UI layer), so it works
        # identically via the CLI (--force-reprocess) and direct API use.
        # Paragraph breaks are "<br><br>", not "\n\n": QGIS renders this
        # tooltip as rich text wrapped in one <p>, so literal newlines
        # collapse to spaces (same constraint as PURPOSE's setHelp
        # above - confirmed on QGIS 4.2 and 3.44 LTR).
        force_reprocess_param.setHelp(self.tr(
            "By default, a file is left alone only when it's already "
            "tiled with pyramids, already at the target compression, "
            "and already a valid Cloud Optimized GeoTIFF - converting "
            "it again wouldn't make it any faster or smaller."
            "<br><br>"
            "A file that's tiled with pyramids but still on a less "
            "efficient compression is reprocessed regardless of this "
            "setting, since there's real file size to save. So is a "
            "file that's tiled, pyramided and already correctly "
            "compressed but isn't a valid COG yet - the case for every "
            "file written by an earlier version of this plugin."
            "<br><br>"
            "Tick this to convert an already-valid file again, to "
            "change how NoData is handled."
        ))
        self.addParameter(force_reprocess_param)

        # Advanced, unlike an earlier version of this file: that version
        # argued overwriting an output while iterating is routine enough
        # that the Advanced section had hidden it well enough to go
        # unnoticed in testing. It's grouped with the other overrides now
        # because leaving Input/Profile/Hidden pixels (NoData)/Output as
        # the entire main panel matters more, and both this and Reprocess
        # even if already optimised are already right there when a rerun
        # onto the same path needs them.
        overwrite_param = QgsProcessingParameterBoolean(
            self.OVERWRITE, self.tr("Replace existing output file"),
            defaultValue=False,
        )
        overwrite_param.setFlags(overwrite_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        # Label alone doesn't say what happens when left unticked - fixed
        # via setHelp() rather than lengthening the label itself.
        overwrite_param.setHelp(self.tr(
            "Unticked, the tool stops rather than overwriting a file "
            "that already exists at the output path, and tells you "
            "what it found.\n"
            "\n"
            "Ticked, the existing file is replaced. Your source file "
            "is never modified either way."
        ))
        self.addParameter(overwrite_param)

    def processAlgorithm(self, parameters, context, feedback):
        input_layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if input_layer is None:
            raise QgsProcessingException(self.tr("Could not load the input raster."))
        source_path = input_layer.source()

        # PURPOSE has a real default now (Analysis), so parameterAsEnum()
        # always resolves to a genuine value - no more need to read the
        # raw dict to detect "not set" (see the PURPOSE_ANALYSIS comment).
        purpose_choice = self.parameterAsEnum(parameters, self.PURPOSE, context)
        nodata_choice = self.parameterAsEnum(parameters, self.NODATA_MODE, context)
        nodata_mode = _NODATA_MODE_STRINGS[nodata_choice]
        force_reprocess = self.parameterAsBoolean(parameters, self.FORCE_REPROCESS, context)
        overwrite = self.parameterAsBoolean(parameters, self.OVERWRITE, context)
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        # One continuous 0-100 bar across both phases: detection 0-10,
        # Translate 10-90. The remaining 90-100 is reserved for
        # convert()'s own close/flush and _verify() afterwards, which
        # have no progress percentage of their own (see log_cb below) -
        # left at 90 rather than jumped to 100 the moment Translate's
        # own callback reports done, so the bar doesn't sit at 100
        # while there's real, unfinished work still happening; 100 is
        # only set explicitly once convert() has actually returned,
        # further down. Used to be three phases (detection, Translate,
        # BuildOverviews) - the COG driver builds pyramids inside the
        # same Translate call now, so there's no separate overviews
        # phase left to give its own span. Each phase gets its own
        # closure so cancellation and scaling are independent - see
        # core/converter.py's _ProgressTracker for why returning False
        # here is what makes Cancel actually stop the running GDAL call,
        # not just stop future progress updates. detect()'s own
        # progress_cb (core/detector.py's _black_pixel_sample) uses the
        # same GDAL-style callback shape, so the same closure factory
        # covers both phases.
        def make_progress_cb(base, span):
            def cb(complete, message, cb_data):
                if feedback.isCanceled():
                    return False
                feedback.setProgress(base + complete * span)
                return True
            return cb

        # Timing instrumentation (measure-first, per the user's explicit
        # request): the previous version had no visibility into where a
        # run's time actually went - a failed run logged 181s total with
        # no breakdown, so it was impossible to tell whether that was
        # detection, Translate, or overviews. Each phase is timed and
        # logged as it completes, not batched to the end, so the dialog
        # isn't silent for minutes at a time either. Detection itself
        # used to be the actual bottleneck (67s measured on a 1.7GB
        # ortho, more than Translate+overviews combined) - fixed in
        # core/detector.py's sampling strategy, not here.
        feedback.setProgressText(self.tr("Detecting raster type..."))
        t_detect = time.perf_counter()
        detection = detect(source_path, progress_cb=make_progress_cb(0, 10))
        detect_seconds = time.perf_counter() - t_detect
        feedback.pushInfo(self.tr("Detection finished in {:.1f}s").format(detect_seconds))

        # detect() has no cancelled action of its own to check (unlike
        # convert()'s result.action == "cancelled") - make_progress_cb
        # already returned False into it the moment isCanceled() went
        # true, so checking the same flag here directly is what actually
        # stops the run, matching how a cancelled Translate is handled
        # below. Without this, a user cancelling during "Detecting
        # raster type..." was never actually stopped - the run carried
        # on into Translate regardless.
        if feedback.isCanceled():
            feedback.pushInfo(self.tr("Cancelled during detection."))
            return {}

        if detection.refused:
            raise QgsProcessingException(detection.refusal_reason)

        # Defensive fallback for callers that bypass checkParameterValues()
        # entirely (processing.run(), qgis_process, batch, Model Designer
        # - confirmed via a direct processing.run() call during this
        # phase that these do skip it). checkParameterValues() already
        # blocks unconditionally on this same condition for the GUI path,
        # so in that path this is unreachable (already-optimised already
        # stopped the run before processAlgorithm() started) - it only
        # fires for a caller that never went through that gate. Force
        # reprocess still bypasses it here too: an informed, deliberate
        # choice, not worth re-flagging.
        if not force_reprocess:
            resolved_profile = _resolved_profile_for_target(detection, purpose_choice)
            if already_optimised_at_target(detection, resolved_profile):
                feedback.pushWarning(self._already_optimised_message())

        # _log_profile_decision() only pushes feedback (pushWarning on a
        # coercion, pushInfo otherwise) - see its own docstring.
        # requested_profile below is computed independently, not derived
        # from that call: convert() needs the RAW, uncoerced request to
        # tell "honoured" from "the file's nature required otherwise"
        # apart correctly, via its own resolve_profile_reason() - it
        # already ignores this value entirely for a forced file's actual
        # conversion (detection.forced_profile always wins there), so
        # passing the raw request through is safe either way.
        self._log_profile_decision(detection, purpose_choice, feedback)
        requested_profile = "lossy" if purpose_choice == PURPOSE_VIEWING else "lossless"

        def log_cb(phase, elapsed_seconds):
            # "translate" fires first, once Translate (plus the
            # dataset close/flush right after it) has finished. "verify"
            # fires second, right before convert() runs _verify() (the
            # COG validator) - real work with no progress percentage of
            # its own, that would otherwise sit behind stale
            # "Translating..." text for several seconds with nothing
            # telling the user why. Deliberately in this order, not the
            # reverse: a log reading "Checking the output" before
            # "Translate finished" would describe events out of
            # sequence, which is worse than the close/flush between
            # them going unlabelled (its cost is already folded into
            # elapsed_seconds below). This only changes the status
            # text, not the bar position - see convert()'s log_cb
            # docstring for why _verify() itself gets no percentage of
            # its own.
            if phase == "translate":
                feedback.pushInfo(self.tr("Translate finished in {:.1f}s").format(elapsed_seconds))
                return
            # "verify"
            feedback.setProgressText(self.tr(
                "Checking the output is a valid Cloud Optimized "
                "GeoTIFF (COG)..."
            ))

        # The bar spends its first stretch barely moving: GDAL's first
        # progress tick lands ~20ms after the gdal.Translate() call, but
        # the COG driver then works through the full-resolution image
        # before it builds the pyramids and counts that phase as almost
        # no progress (complete 0.00-0.05), so on a large Viewing
        # conversion the bar can sit near 10% for 20-30s (measured ~25s
        # of an 84s run on a 1.66 GiB source; Analysis moves more
        # evenly). Say so, so the wait reads as expected rather than
        # hung. Pushed on every run, not just the lossy path: the
        # wording itself distinguishes the two, and a Viewing request
        # coerced to Analysis would get the wrong branch anyway. An
        # earlier attempt to say this in the status text instead, swapped
        # in on the first tick, was reverted - the first tick is too
        # early for it to persist through the slow phase. See
        # docs/plugin_design_notes.md "Progress bar sits near 10%".
        feedback.pushInfo(self.tr(
            "The bar climbs slowly at first. GDAL works through the "
            "full-resolution image before it builds the pyramids, and "
            "it counts that as very little progress even though it "
            "takes a while. On a large raster written for Viewing this "
            "can be 20 to 30 seconds near 10%. Analysis moves more "
            "evenly."
        ))
        feedback.setProgressText(self.tr("Translating (tiling, compressing, pyramids)..."))
        result = convert(
            source_path, detection=detection, chosen_profile=requested_profile,
            output_path=output_path, force=overwrite,
            nodata_mode=nodata_mode,
            force_reprocess=force_reprocess,
            translate_progress_cb=make_progress_cb(10, 80),
            log_cb=log_cb,
        )

        # Previously never surfaced - convert() has always been able to
        # populate result.warnings (e.g. a cancel-cleanup failure), but
        # nothing here read it. Newly relevant now that the force-reprocess
        # override note lands there too.
        for w in result.warnings:
            feedback.pushWarning(w)

        if result.action == "already_optimised":
            feedback.pushInfo(result.message)
            return {self.OUTPUT: source_path}

        if result.action == "cancelled":
            # Not an error - the user asked for this. Return no results
            # rather than raising, which is what QGIS Processing expects
            # from a cancelled run; converter.py has already removed the
            # partial output (see its module docstring / _safe_remove).
            feedback.pushInfo(result.message)
            return {}

        if result.action in _HARD_FAILURE_ACTIONS:
            raise QgsProcessingException(result.message)

        # Only reached once convert() has genuinely finished, including
        # _verify() - see make_progress_cb's own comment above for why
        # Translate itself only ever reaches 90, not 100. Set explicitly
        # here rather than from any callback, since nothing inside
        # convert() reports a percentage for the close/flush or
        # _verify() spans - not on the already_optimised/cancelled/hard-
        # failure paths above, which never produced a freshly-verified
        # file and would misrepresent what happened if the bar jumped
        # to 100 there too.
        feedback.setProgress(100)

        feedback.pushInfo(result.message)
        if result.size_summary:
            feedback.pushInfo(result.size_summary)
        if result.size_note:
            feedback.pushInfo(result.size_note)
        if result.nodata_message:
            if result.nodata_message_severity == "warning":
                # Bucket A of the purpose-question rework (Phase 2):
                # "Reveal hidden pixels" requested on elevation is a
                # coercion, not a finding, so it gets the same
                # feedback.pushWarning() treatment as every other
                # Bucket A message (see _log_profile_decision() above) rather
                # than the bold-but-still-pushInfo emphasis below, which
                # is reserved for NoData findings specifically.
                feedback.pushWarning(result.nodata_message)
            elif result.nodata_message_severity == "emphasis":
                # The plugin's most educational finding (real content
                # found behind NoData, whether Automatic cleared it or
                # Keep as-is left it), so it gets the strongest available
                # log emphasis rather than blending into routine pushInfo
                # lines. No hardcoded colour: an earlier version used a
                # fixed blue here, which reads poorly against a dark
                # theme's log background for the same reason the help
                # panel's hardcoded colours did. Bold alone carries the
                # emphasis and inherits whatever the theme's own text
                # colour is. pushFormattedMessage(html, text) writes html
                # to htmlLog() (the GUI log panel) and text to textLog()
                # (console/qgis_process's plain-text fallback) - confirmed
                # identical on QGIS 4.2 and 3.44 LTR.
                html = (
                    '<p style="font-weight:bold; margin:4px 0;">{}</p>'
                ).format(result.nodata_message)
                text = "*** {} ***".format(result.nodata_message)
                feedback.pushFormattedMessage(html, text)
            else:
                feedback.pushInfo(result.nodata_message)

        # Phase 4 of the purpose-question rework: re-emit the
        # consequential decisions as the last thing before completion,
        # since detection's findings scroll away behind Translate's own
        # progress output otherwise. Only decisions
        # worth repeating - a plain file with nothing surprising gets
        # just the location line, not a restatement of the routine
        # "used as asked" case, which would be noise on every run.
        # feedback.pushWarning() (not pushInfo) so it carries colour,
        # matching every other Bucket A message. Same text
        # result.decisions already carries into the file's own
        # GEOKLEIN_* metadata (Phase 5) - not reworded here.
        #
        # A line is also dropped if it would immediately repeat its own
        # original: the NoData dispatch just above is always the last
        # thing pushed before this point, so the NoData line would
        # otherwise sit right under the message it's restating, with
        # nothing buried in between for the summary to resurface - that
        # defeats the point of a summary. The profile-decision line
        # doesn't have this problem: it was pushed back in
        # _log_profile_decision(), before Translate's own progress
        # output, so real content genuinely separates it from
        # here.
        last_pushed_message = result.nodata_message
        summary_lines = ["Summary"]
        dec = result.decisions
        if dec is not None:
            if dec.profile_consequential:
                summary_lines.append(dec.profile_reason)
            if (
                dec.nodata_consequential and dec.nodata_message
                and dec.nodata_message != last_pushed_message
            ):
                summary_lines.append(dec.nodata_message)
        summary_lines.append(self.tr(
            "These decisions are also recorded in the file, under "
            "Layer Properties > Information > More information."
        ))
        feedback.pushWarning("\n".join(summary_lines))

        return {self.OUTPUT: result.output_path}

    def _log_profile_decision(self, detection, purpose_choice, feedback):
        # PURPOSE always has a genuine value now (defaulted to Analysis
        # if untouched - see the PURPOSE_ANALYSIS comment), so this is
        # an unconditional mapping onto detector.py's "lossy"/"lossless"
        # identity, not a None-checked one.
        requested = "lossy" if purpose_choice == PURPOSE_VIEWING else "lossless"

        # profile_mode == "choice": a genuine judgement call, never
        # guessed. Defensive only below - every ProfileOption
        # detector.py currently produces for "choice" mode is
        # available=True (RGB_8BIT is the only remaining "choice"
        # bucket, and its one file-specific block moved to "forced" -
        # see _finish_rgb_8bit()'s nodata_only_transparency branch), so
        # this should be unreachable in practice.
        if detection.profile_mode == "choice":
            opt = next((o for o in detection.profile_options if o.profile == requested), None)
            if opt is not None and not opt.available:
                raise QgsProcessingException(opt.reason_blocked or self.tr(
                    "The {} option is not available for this file."
                ).format(requested))

        # resolve_profile_reason() (detector.py) is the single source of
        # this text for every case: honoured or not, forced or choice -
        # the same string GEOKLEIN_4_DECISION carries into the output
        # file's metadata and the end-of-run summary repeats. Nothing
        # here regenerates or rewords it; this is only place it's first
        # turned into user-visible feedback for the GUI path. Bucket A
        # of the purpose-question rework: coerce, never block, so an
        # unhonoured request is a warning, not an exception.
        #
        # consequential (not honoured) drives the severity: a request
        # can be genuinely honoured and still worth a warning - Analysis
        # requested on a JPEG source is applied exactly as asked, but
        # the file's history makes it worth flagging anyway. See
        # resolve_profile_reason()'s docstring for why honoured isn't a
        # separate returned value.
        reason, consequential = resolve_profile_reason(detection, requested)
        if consequential:
            feedback.pushWarning(reason)
        else:
            feedback.pushInfo(reason)
