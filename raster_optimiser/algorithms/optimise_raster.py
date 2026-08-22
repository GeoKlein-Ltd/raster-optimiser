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
choice (unlike lossy-on-elevation, which is never legitimate, or
reprocessing an already-optimised file, which is never useful without
Force reprocess), so it belongs where every other legitimate choice
this plugin doesn't second-guess lives: a prominent processAlgorithm()
log message, not a gate. Automatic being the default is what protects a
novice here - selecting Keep as-is at all is already the deliberate
override, not something that then also needs blocking.

QGIS renders a checkParameterValues() failure as a modal QMessageBox
with only an OK button (confirmed via processing/gui/algorithm_widget.py),
not a dismissible inline banner - framework behaviour, not something
this file can change. That rules out checkParameterValues() for
anything a user might legitimately want to proceed past, which is
exactly why only the two hard gates below remain there: lossy-on-
elevation (never legitimate) and already-optimised (never useful
without Force reprocess) both have a real, different parameter to
change, not just a different value in the same dropdown the block was
about.

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
    _same_file, _is_tiled,
)
from ..core.detector import detect, detect_metadata_only
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
# mapping left for anyone to get wrong. PROFILE_LOSSLESS/PROFILE_LOSSY
# below are only this wrapper's own dropdown-index constants. Same
# reasoning applies to NODATA_AUTO/REVEAL/KEEP further down: this file
# hardcodes the literal "auto"/"reveal"/"keep" strings at the one point
# they cross into convert(), rather than importing converter.py's
# NODATA_MODE_* constants under a second, index-shaped name here.
PROFILE_LOSSLESS = 0
PROFILE_LOSSY = 1
_PROFILE_LOSSLESS_NAME = "Preserve pixel values (lossless)"
_PROFILE_LOSSY_NAME = "Smallest file size (lossy)"
PROFILE_OPTIONS = [_PROFILE_LOSSLESS_NAME, _PROFILE_LOSSY_NAME]

# Index order matches NODATA_OPTIONS below exactly - see
# _NODATA_MODE_STRINGS for the one place this crosses into convert()'s
# plain "auto"/"reveal"/"keep" strings.
NODATA_AUTO = 0
NODATA_REVEAL = 1
NODATA_KEEP = 2
_NODATA_AUTO_NAME = "Automatic: decide per file (recommended)"
_NODATA_REVEAL_NAME = "Reveal hidden pixels"
_NODATA_KEEP_NAME = "Keep as-is"
NODATA_OPTIONS = [_NODATA_AUTO_NAME, _NODATA_REVEAL_NAME, _NODATA_KEEP_NAME]
NODATA_MODE_LABEL = "Hidden pixels (NoData)"
_NODATA_MODE_STRINGS = ("auto", "reveal", "keep")  # index -> convert()'s nodata_mode

WARN_ENABLED_LABEL = "Warn me before running if something looks wrong"
FORCE_REPROCESS_LABEL = "Reprocess even if already optimised"

# ConversionResult.action values that mean "this run did not succeed" -
# see core/converter.py's ConversionResult docstring for the full set.
# converted_unverified is deliberately in here, not treated as a soft
# warning: verification failing means "say so loudly rather than
# reporting success" (the explicit instruction this was built against),
# so it fails the Processing run rather than returning a green checkmark
# with a caveat buried in the log.
_HARD_FAILURE_ACTIONS = (
    "error", "blocked", "converted_incomplete", "converted_unverified",
    "refused_upstream",
)


class OptimiseRasterAlgorithm(QgsProcessingAlgorithm):

    INPUT = "INPUT"
    PROFILE = "PROFILE"
    NODATA_MODE = "NODATA_MODE"
    WARN_ENABLED = "WARN_ENABLED"
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
        return self.tr("Makes slow, oversized rasters load and pan quickly in QGIS.")

    def group(self):
        # Empty on purpose: with only one algorithm, a same-named group
        # ("Raster Optimiser" provider containing a "Raster Optimiser"
        # group) is a redundant tree level - Toolbox showed
        # Raster Optimiser -> Raster Optimiser -> Optimise raster.
        # Returning "" collapses straight to
        # Raster Optimiser -> Optimise raster. Doesn't touch id() /
        # name(), so this can't affect anything that references the
        # algorithm by ID (Model Designer, qgis_process, saved models).
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
        # QGIS renders this as HTML (confirmed: the Processing help
        # panel is a rich-text view, not a plain-text one), so headings
        # and lists use real <b>/<ul>/<li> tags rather than the plain
        # dashes/blank-line convention an earlier round of this file
        # used before that was confirmed.
        return self.tr(
            "<p><b>What this does</b></p>"
            "<p>Makes large rasters load and pan quickly in QGIS, and "
            "reduces their file size.</p>"

            "<p><b>Why your file is slow</b></p>"
            "<p>Most orthomosaics and elevation rasters come out of "
            "processing software missing two things GDAL needs in order "
            "to draw them quickly:</p>"
            "<ul>"
            "<li><b>Pyramids</b>, also called overviews: pre-built "
            "smaller copies of the image. Without them, QGIS has to "
            "read every pixel in the file just to draw a zoomed-out "
            "view. On a 470-megapixel ortho, that's the entire file, on "
            "every pan and every zoom.</li>"
            "<li><b>Tiling</b>: storing pixels as small squares rather "
            "than full-width rows, so software can read one part of "
            "the image without touching the rest.</li>"
            "</ul>"
            "<p>This tool adds both, and compresses the file sensibly "
            "on the way through.</p>"

            "<p><b>Choosing a compression profile</b></p>"
            "<p><i>Preserve pixel values (lossless)</i>: a smaller "
            "file with every pixel value exactly as it was. Use this "
            "whenever you'll measure something from the image: "
            "vegetation indices, classification, crown segmentation, "
            "change detection. Elevation data always uses this, "
            "whatever you select.</p>"
            "<p><i>Smallest file size (lossy)</i>: typically 15 to 30 "
            "times smaller, but pixel values shift slightly. Invisible "
            "on screen, measurable in analysis. Use it for basemaps, "
            "client copies, QField backdrops: anything you look at "
            "rather than measure.</p>"
            "<p>Both profiles produce a file that loads at the same "
            "speed. The choice only affects file size and whether "
            "pixel values survive unchanged.</p>"

            "<p><b>What it won't process</b></p>"
            "<p>Some rasters can't be optimised safely with these "
            "settings, so the tool detects them and stops rather than "
            "producing something quietly wrong:</p>"
            "<ul>"
            "<li><b>Classified rasters</b>: land cover, species class, "
            "or any map where pixel values are category codes rather "
            "than measurements. Building pyramids averages neighbouring "
            "pixels, and averaging two categories produces a third that "
            "doesn't exist.</li>"
            "<li><b>Multispectral rasters</b>, or anything with more "
            "than four bands.</li>"
            "<li><b>Files with no coordinate reference system.</b></li>"
            "</ul>"

            "<p><b>Your source file is never modified.</b> The tool "
            "always writes a new file.</p>"

            "<p><b>Glossary</b></p>"
            "<ul>"
            "<li><b>NoData</b>: a pixel value the file declares to "
            "mean \"nothing here\". Safe on elevation data, where you "
            "can pick a value no real height could ever be, such as "
            "-9999. Risky on 8-bit imagery, where every value from 0 to "
            "255 is a legitimate colour and 0 is simply black.</li>"
            "<li><b>Alpha band</b>: an extra band recording which "
            "pixels fall inside the surveyed area. It works by position "
            "rather than by value, so it never mistakes a black pixel "
            "for an empty one.</li>"
            "<li><b>Collar</b>: the transparent border around a survey "
            "area, where the image doesn't fill the rectangular "
            "file.</li>"
            "<li><b>Pyramids / overviews</b>: pre-built smaller copies "
            "of the image at successive zoom levels.</li>"
            "<li><b>Tiling</b>: storing the image as small squares "
            "instead of full-width rows.</li>"
            "</ul>"
        )

    def _lossy_on_elevation_message(self):
        return self.tr(
            "This is elevation data: a DSM, DTM or CHM.\n"
            "\n"
            "Lossy compression works by discarding detail the eye "
            "won't notice. That's fine for photographs, but elevation "
            "pixels are height measurements, not colours, so discarding "
            "detail changes the actual heights.\n"
            "\n"
            "Choose '{}' instead."
        ).format(_PROFILE_LOSSLESS_NAME)

    def _already_optimised_message(self):
        return self.tr(
            "This file is already tiled and has pyramids built, so it "
            "should already load and pan quickly in QGIS.\n"
            "\n"
            "Converting it again won't make it any faster. It would "
            "just produce a second large file.\n"
            "\n"
            "If you're reconverting deliberately, for example to switch "
            "from lossless to lossy compression, tick '{}' under "
            "Advanced parameters."
        ).format(FORCE_REPROCESS_LABEL)

    def checkParameterValues(self, parameters, context):
        # Runs before execution and can refuse instantly, with nothing run
        # yet - QGIS 4.2 and 3.44 LTR both confirmed (via local source
        # inspection, not memory/docs) to expose this as
        # checkParameterValues(self, parameters, context) -> (bool, str).
        #
        # Deliberately calls detect_metadata_only(), never detect(): the
        # scope refusals below (unsupported dtype, multispectral,
        # colour-table classified, 16-bit blocking the lossy option, no
        # CRS) are all resolvable from gdal.Open() + band metadata alone,
        # in milliseconds even on a multi-gigapixel file. Running the
        # full detect() here - which pixel-samples for the classified
        # check and the NoData black-pixel risk - is exactly the bug
        # this fixes: it cost 181s on a 1.7GB ortho before the user ever
        # saw a refusal. No detection logic is duplicated here; this
        # reads the same DetectionResult shape detect() produces, just
        # via detector.py's metadata-only code path.
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
            return False, detection.refusal_reason

        # Fetched unconditionally now (used to only be read inside the
        # "choice" branch below) - the lossy-on-elevation warning further
        # down needs it for the "forced" profile_mode case too. PROFILE
        # always has a genuine value (defaulted to lossless if untouched
        # - see the PROFILE_LOSSLESS comment), so this is safe to read
        # regardless of profile_mode.
        profile_choice = self.parameterAsEnum(parameters, self.PROFILE, context)
        nodata_choice = self.parameterAsEnum(parameters, self.NODATA_MODE, context)

        if detection.profile_mode == "choice":
            # This replaces the earlier "no default, force a choice"
            # design from an earlier round - superseded because the
            # asymmetric harm runs the other way: an untouched dropdown
            # giving a larger-than-optimal file is visible and
            # recoverable; silently altering pixel values is not.
            requested = "lossless" if profile_choice == PROFILE_LOSSLESS else "lossy"
            opt = next((o for o in detection.profile_options if o.profile == requested), None)
            if opt is not None and not opt.available:
                return False, opt.reason_blocked or self.tr(
                    "The {} option is not available for this file."
                ).format(requested)

        # Hard gate, not just a log note: choosing "Reveal hidden
        # pixels" on a file with no NoData=0 condition at all is a pure
        # no-op (elevation always lands here too - detect_metadata_only
        # never creates a NoDataRisk for it), and nobody should walk
        # away from a run thinking they've revealed hidden pixels when
        # nothing happened. This is deliberately NOT the same check as
        # the nodata_only_transparency structural block (NoData set, but
        # clearing would reveal a border) - that stays a soft, logged
        # non-gate, unaffected by this parameter, exactly as before.
        if nodata_choice == NODATA_REVEAL and (detection.nodata_risk is None or not detection.nodata_risk.applies):
            return False, self.tr(
                "'{}' has no effect on this file - it doesn't have a "
                "NoData=0 condition to reveal. Choose a different "
                "option, or check Raster Information if you expected one."
            ).format(_NODATA_REVEAL_NAME)

        # NODATA_KEEP does NOT get a hard gate here, even when detection
        # finds meaningful content behind NoData - tried twice, reverted
        # twice (see this module's docstring). Keep as-is is a
        # legitimate choice, and checkParameterValues() can only refuse,
        # never accept-with-acknowledgement, so gating a legitimate
        # choice here always reproduces the same unclosable modal loop
        # regardless of how the message is worded. That finding is
        # surfaced instead as a prominent processAlgorithm() log message
        # - see _resolve_nodata_handling()'s NODATA_MODE_KEEP branch.

        # Pre-flight risk warnings - a different class of check from
        # everything above. Those are all hard refusals: the run
        # genuinely cannot proceed as configured. These two are
        # judgement calls a user might deliberately want to override -
        # see WARN_ENABLED's setHelp() - so unlike the refusals above,
        # they're entirely skipped, not just downgraded, when WARN_ENABLED
        # is unticked. In that case the same conditions are still
        # evaluated and logged, just in processAlgorithm() instead of
        # blocking here (checkParameterValues has no feedback/log object
        # to write to - only block-or-allow).
        warn_enabled = self.parameterAsBoolean(parameters, self.WARN_ENABLED, context)
        if warn_enabled:
            force_reprocess = self.parameterAsBoolean(parameters, self.FORCE_REPROCESS, context)
            triggered = []

            msg = self._lossy_on_elevation_message() if (
                detection.profile_mode == "forced"
                and detection.forced_profile == "lossless"
                and profile_choice == PROFILE_LOSSY
            ) else None
            if msg:
                triggered.append(msg)

            # Cheap check only - block_size/raster_size/overview_count
            # all come from detect_metadata_only() above, no pixel
            # sampling triggered.
            if (
                not force_reprocess
                and _is_tiled(detection.block_size, detection.raster_size)
                and detection.overview_count > 0
            ):
                triggered.append(self._already_optimised_message())

            if triggered:
                return False, "\n\n".join(triggered)

        return True, ""

    def initAlgorithm(self, config=None):
        input_param = QgsProcessingParameterRasterLayer(
            self.INPUT, self.tr("Input layer"),
        )
        input_param.setHelp(self.tr(
            "The raster to optimise. Any format GDAL can read. The "
            "output is always a GeoTIFF."
        ))
        self.addParameter(input_param)

        profile_param = QgsProcessingParameterEnum(
            self.PROFILE, self.tr("Compression profile"),
            options=PROFILE_OPTIONS,
            defaultValue=PROFILE_LOSSLESS,
        )
        # Mandatory with a real default now (lossless, index 0) -
        # supersedes the earlier "optional, no default, force a
        # conscious choice" design from a previous round. Reasoning is
        # asymmetric harm: an untouched dropdown giving a larger-than-
        # optimal file is visible and recoverable (rerun with lossy);
        # one that silently alters pixel values is invisible and may
        # never be found. Elevation still forces lossless outright
        # regardless of this value - see _resolve_profile - so this
        # default only ever matters for imagery.
        profile_param.setHelp(self.tr(
            "Lossless keeps every pixel value exactly as it is: use "
            "it for anything you'll measure or analyse. Lossy produces "
            "a much smaller file by discarding detail the eye won't "
            "notice: use it for basemaps and anything you only look "
            "at.\n"
            "\n"
            "Both load at the same speed. Elevation data is always "
            "processed losslessly, whatever you choose here."
        ))
        self.addParameter(profile_param)

        nodata_param = QgsProcessingParameterEnum(
            self.NODATA_MODE, self.tr(NODATA_MODE_LABEL),
            options=NODATA_OPTIONS,
            defaultValue=NODATA_AUTO,
        )
        # Automatic by default - see this module's docstring for why
        # that's no longer the rejected design it once was. Meaningless
        # on elevation and on RGB files without a NoData=0 condition,
        # same as PROFILE is meaningless on elevation - always visible
        # anyway, since Processing parameter widgets are declared in
        # initAlgorithm() before any input is chosen and can't react to
        # it. checkParameterValues() refuses 'Reveal hidden pixels'
        # outright when it would be a pure no-op - that's the only hard
        # gate this parameter gets. 'Keep as-is' on a file with real
        # content behind NoData is never gated, only logged prominently
        # in processAlgorithm() - see this module's docstring for why.
        # The run log always states what happened, including "not
        # applicable" as the defensive fallback for callers that skip
        # checkParameterValues - see core/converter.py's
        # _resolve_nodata_handling.
        nodata_param.setHelp(self.tr(
            "Many orthomosaics mark transparency using a NoData value "
            "of 0. On 8-bit imagery that's unsafe, because 0 is also "
            "the value of a genuinely black pixel, so deep shadow, "
            "dark water and wet tarmac get treated as empty and "
            "punched out as holes.\n"
            "\n"
            "'{auto}' checks each file and only clears NoData when "
            "real content is hidden behind it. Files where NoData "
            "marks nothing but the transparent collar are left "
            "alone.\n"
            "\n"
            "'{reveal}' always clears NoData. Hidden content comes "
            "back, but the collar may render as a solid black border "
            "rather than transparent.\n"
            "\n"
            "'{keep}' leaves the file's NoData setting untouched."
        ).format(auto="Automatic", reveal=_NODATA_REVEAL_NAME, keep=_NODATA_KEEP_NAME))
        self.addParameter(nodata_param)

        output_param = QgsProcessingParameterRasterDestination(
            self.OUTPUT, self.tr("Optimised raster"),
        )
        output_param.setHelp(self.tr(
            "Where to save the result. Always written as a GeoTIFF, "
            "tiled with pyramids built in."
        ))
        self.addParameter(output_param)

        # Everything below is Advanced: Input/Profile/Hidden pixels
        # (NoData)/Output is the whole decision most runs need, and all
        # three of these are either a rare override (Reprocess even if
        # already optimised), routine but secondary (Replace existing
        # output file - see its own comment below for why it isn't
        # hidden further), or a safety net most people should just
        # leave on (Warn me) - none of them belong in the same visual
        # weight as the actual content decision above.

        warn_param = QgsProcessingParameterBoolean(
            self.WARN_ENABLED, self.tr(WARN_ENABLED_LABEL),
            defaultValue=True,
        )
        warn_param.setFlags(warn_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        # Ticked by default: the two things this checks (lossy chosen on
        # elevation, the file already being optimised) are genuine
        # judgement calls someone could deliberately want to proceed past
        # - see checkParameterValues' "Pre-flight risk warnings" comment
        # for why unticking skips the block entirely rather than just
        # softening it. checkParameterValues() has no feedback/log
        # object, so "log only" is implemented in processAlgorithm().
        # NoData risk is NOT one of the two: it never gates here at all
        # (see checkParameterValues' NODATA_KEEP comment for why keeping
        # NoData is never blocked), so this setting is unrelated to it
        # either way. Both checks here are metadata-only (no pixel
        # sampling), so ticking this costs nothing extra.
        warn_param.setHelp(self.tr(
            "Inspects the file before converting and stops with an "
            "explanation if you've chosen lossy compression for "
            "elevation data, or if the file is already optimised.\n"
            "\n"
            "These are quick checks that read the file's structure "
            "rather than its contents, so leaving this on costs "
            "nothing."
        ))
        self.addParameter(warn_param)

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
        force_reprocess_param.setHelp(self.tr(
            "By default, a file that's already tiled with pyramids is "
            "left alone, since converting it again wouldn't make it "
            "any faster.\n"
            "\n"
            "Tick this to convert it anyway, for example to switch an "
            "existing file from lossless to lossy compression to save "
            "disk space."
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

        # PROFILE has a real default now (lossless), so parameterAsEnum()
        # always resolves to a genuine value - no more need to read the
        # raw dict to detect "not set" (see the PROFILE_LOSSLESS comment).
        profile_choice = self.parameterAsEnum(parameters, self.PROFILE, context)
        nodata_choice = self.parameterAsEnum(parameters, self.NODATA_MODE, context)
        nodata_mode = _NODATA_MODE_STRINGS[nodata_choice]
        warn_enabled = self.parameterAsBoolean(parameters, self.WARN_ENABLED, context)
        force_reprocess = self.parameterAsBoolean(parameters, self.FORCE_REPROCESS, context)
        overwrite = self.parameterAsBoolean(parameters, self.OVERWRITE, context)
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        # One continuous 0-100 bar across all three phases: detection
        # 0-10, Translate 10-75, BuildOverviews 75-100. Each phase gets
        # its own closure so cancellation and scaling are independent -
        # see core/converter.py's _ProgressTracker for why returning
        # False here is what makes Cancel actually stop the running GDAL
        # call, not just stop future progress updates. detect()'s own
        # progress_cb (core/detector.py's _black_pixel_sample) uses the
        # same GDAL-style callback shape, so the same closure factory
        # covers all three phases.
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

        if detection.refused:
            raise QgsProcessingException(detection.refusal_reason)

        # Pre-flight warnings that WARN_ENABLED would otherwise have
        # blocked on - logged here so unticking it changes nothing except
        # whether the run stops, per WARN_ENABLED's setHelp(). Skipped
        # entirely when warn_enabled is True: in that case either nothing
        # was wrong (nothing to log), or checkParameterValues() already
        # blocked before processAlgorithm() ever started, or
        # force_reprocess bypassed the already-optimised warning
        # specifically (an informed, deliberate choice - not worth
        # re-flagging).
        if not warn_enabled and not force_reprocess:
            if _is_tiled(detection.block_size, detection.raster_size) and detection.overview_count > 0:
                feedback.pushWarning(self._already_optimised_message())

        chosen_profile = self._resolve_profile(detection, profile_choice, feedback)

        def log_cb(phase, elapsed_seconds):
            if phase == "translate":
                feedback.pushInfo(self.tr("Translate finished in {:.1f}s").format(elapsed_seconds))
                feedback.setProgressText(self.tr("Building overviews (pyramids)..."))
            elif phase == "overviews":
                feedback.pushInfo(self.tr("Build overviews finished in {:.1f}s").format(elapsed_seconds))

        feedback.setProgressText(self.tr("Translating (tiling, compressing)..."))
        result = convert(
            source_path, detection=detection, chosen_profile=chosen_profile,
            output_path=output_path, force=overwrite,
            nodata_mode=nodata_mode,
            force_reprocess=force_reprocess,
            translate_progress_cb=make_progress_cb(10, 65),
            overview_progress_cb=make_progress_cb(75, 25),
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

        feedback.pushInfo(result.message)
        if result.size_summary:
            feedback.pushInfo(result.size_summary)
        if result.size_note:
            feedback.pushInfo(result.size_note)
        if result.nodata_message:
            if result.nodata_message_emphasis:
                # Automatic just revealed real content behind NoData -
                # the plugin's most educational finding, per instruction,
                # so it gets the strongest available log emphasis rather
                # than blending into routine pushInfo lines. Blue, not
                # the red used for an actual problem: nothing is wrong
                # here, Automatic already handled it correctly.
                # pushFormattedMessage(html, text) writes html to
                # htmlLog() (the GUI log panel) and text to textLog()
                # (console/qgis_process's plain-text fallback) -
                # confirmed identical on QGIS 4.2 and 3.44 LTR.
                html = (
                    '<p style="color:#1c5f8c; font-weight:bold; margin:4px 0;">{}</p>'
                ).format(result.nodata_message)
                text = "*** {} ***".format(result.nodata_message)
                feedback.pushFormattedMessage(html, text)
            else:
                feedback.pushInfo(result.nodata_message)
        return {self.OUTPUT: result.output_path}

    def _resolve_profile(self, detection, profile_choice, feedback):
        # PROFILE always has a genuine value now (defaulted to lossless
        # if untouched - see the PROFILE_LOSSLESS comment), so this is an
        # unconditional mapping onto detector.py's "lossy"/"lossless"
        # identity, not a None-checked one. That's still correct for the
        # elevation override warning below: the default (index 0) maps
        # to "lossless", which is also what elevation always forces, so
        # an untouched dropdown never triggers it - only an explicit
        # choice of the lossy option (index 1, which nothing defaults
        # to) can produce a mismatch worth warning about.
        requested = "lossy" if profile_choice == PROFILE_LOSSY else "lossless"

        if detection.profile_mode == "forced":
            forced = detection.forced_profile
            if requested != forced:
                # Same message the WARN_ENABLED pre-flight check would
                # have shown, and the only trigger for either is this
                # exact mismatch - see _lossy_on_elevation_message().
                feedback.pushWarning(self._lossy_on_elevation_message())
            else:
                feedback.pushInfo(self.tr(
                    "Elevation data detected - using {} compression."
                ).format(forced))
            return forced

        # profile_mode == "choice": a genuine judgement call, never guessed.
        opt = next((o for o in detection.profile_options if o.profile == requested), None)
        if opt is not None and not opt.available:
            raise QgsProcessingException(opt.reason_blocked or self.tr(
                "The {} option is not available for this file."
            ).format(requested))
        return requested
