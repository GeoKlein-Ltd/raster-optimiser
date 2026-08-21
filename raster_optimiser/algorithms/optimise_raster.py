"""QGIS Processing algorithm wrapping the Raster Optimiser engine.

Thin wrapper only. All detection and conversion logic lives in core/
(detector.py, converter.py), which have no QGIS dependency and are
untouched by this file - it declares Processing parameters, translates
them into calls into core/, and maps the results back onto QGIS
feedback/exceptions. See docs/plugin_design_notes.md for why an
already-optimised file's OUTPUT resolves to the source path rather
than a copy.

NoData handling (CLEAR_NODATA below) is a real parameter now - it used
to not be one, because clearing NoData was folded silently into the
profile choice instead (see core/converter.py's git history / the
_resolve_nodata_handling docstring). That was never something a user
chose; it was an accident of which profile happened to attempt it.
Detection's collar-vs-shadow read stays advisory, never a gate - this
parameter is what a user acts on it with, not detection itself. It's a
plain checkbox, not a three-way choice: an earlier version offered
Keep/Clear/Auto, and Auto was removed - even detect()'s most confident
read is still a collar-vs-shadow guess the plugin's design says it
can't reliably make, so it was better not offered at all than offered
as a third option that implied otherwise.

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

from ..core.converter import convert, output_exists_message, output_same_as_source_message, _same_file, _is_tiled
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
# below are only this wrapper's own dropdown-index constants.
PROFILE_LOSSLESS = 0
PROFILE_LOSSY = 1
# Benefit first, technical term in brackets - not the other way round.
# "Lossy" leading reads as a downgrade and makes people hesitate even
# when it's the right choice for their basemap; but the word can't
# disappear either, since it's the standard term across QGIS, GDAL and
# every other raster tool, and someone who only ever learns "smaller
# file" here won't recognise it elsewhere.
#
# No "looks identical"/"visually indistinguishable" on the lossy side -
# paired with the word "Lossy" it read as a contradiction to anyone who
# doesn't already know the terms ("lossy" sounds like it should look
# different). That reassurance now lives in setHelp() and the TL;DR
# instead, which a hesitant user reaches in one hover or one extra
# sentence - see this file's design note on layered effort near
# shortHelpString.
_PROFILE_LOSSLESS_NAME = "Preserve pixel values (Lossless)"
_PROFILE_LOSSY_NAME = "Considerable file size reduction (Lossy)"
PROFILE_OPTIONS = [
    _PROFILE_LOSSLESS_NAME + " [default]",
    _PROFILE_LOSSY_NAME,
]

# Not "patches holes in data" or similar - that implies filling in
# missing data, the opposite of what happens (the pixels were always
# there; NoData was hiding them). Someone with a genuine coverage gap
# would tick it, watch the collar turn black, and reasonably conclude
# the tool is broken. "Show pixels hidden by NoData" states the actual
# mechanism instead.
CLEAR_NODATA_LABEL = "Show pixels hidden by NoData"

WARN_ENABLED_LABEL = "Warn me before proceeding if something looks wrong"
FORCE_REPROCESS_LABEL = "Force reprocess"

# Below the threshold detector.py's NoData risk assessment can actually
# produce (NODATA_INTERIOR_CELL_RISK_FRACTION = 0.001, i.e. 0.1%), so
# this branch is defensive rather than reachable today - kept anyway so
# a future change to that threshold can't reintroduce a message reading
# "Up to 0.00%", which would look broken rather than reassuring.
_NODATA_PCT_DISPLAY_FLOOR = 0.005


def _nodata_extent_phrase(pct: float) -> str:
    if pct < _NODATA_PCT_DISPLAY_FLOOR:
        return "A small but detectable amount of an interior area is"
    return "Up to {:.2f}% of an interior area is".format(pct)


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
    CLEAR_NODATA = "CLEAR_NODATA"
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
        # back to the first line of shortHelpString() ("TL;DR:"), which
        # isn't useful as a tooltip on its own - this overrides it.
        return self.tr(
            "Detects slow, unoptimised orthomosaics and elevation "
            "rasters and converts them to fast-panning GeoTIFFs."
        )

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
        # Design is layered by effort, cheapest first: the dropdown
        # labels alone should be enough for most users to choose
        # correctly without reading anything here. setHelp() on each
        # parameter is one hover further. This TL;DR is a sentence more.
        # The Glossary is for a term the reader doesn't know. Full
        # explanation is the last resort. If a user has to open this
        # panel to make a safe choice, the labels already failed - so
        # each layer should stand on its own, not depend on the reader
        # having seen the one above it.
        return self.tr(
            "TL;DR:\n"
            "\n"
            "The decision:\n"
            "Both options make this file fast to pan and zoom. The only "
            "choice is whether pixel values are kept exactly as they "
            "are, or changed slightly to save space. That only matters "
            "if the values will be measured or calculated from - not if "
            "you are just looking at the file.\n"
            "\n"
            "Speed:\n"
            "Comes from tiling and pyramids, built the same way "
            "whichever option you pick.\n"
            "\n"
            "\n"
            "Glossary:\n"
            "\n"
            "Collar:\n"
            "The transparent area around the edge of the image. Drone "
            "orthos are irregular shapes stored in rectangular files, so "
            "the corners are filled with nothing.\n"
            "\n"
            "Lossless:\n"
            "Compression that keeps every pixel value exactly as it "
            "was. Nothing is discarded.\n"
            "\n"
            "Lossy:\n"
            "Compression that discards fine detail to save space. "
            "Visually identical, but the exact numbers change.\n"
            "\n"
            "NoData:\n"
            "A pixel value reserved to mean \"nothing here\", normally "
            "used to make the collar transparent. On 8-bit imagery 0 is "
            "both a real colour (pure black) and the usual NoData "
            "value, which is what causes the black gaps that Show "
            "pixels hidden by NoData fixes.\n"
            "\n"
            "Pyramids (overviews):\n"
            "Pre-built smaller copies of the image, used automatically "
            "when zoomed out, so the full-resolution data does not have "
            "to be read and shrunk every time.\n"
            "\n"
            "Tiling:\n"
            "Storing the image as a grid of small squares instead of "
            "long strips, so software can read just the part it needs "
            "rather than the whole file.\n"
            "\n"
            "\n"
            "Full explanation:\n"
            "\n"
            "Lossless:\n"
            "The default. Every pixel value survives exactly. Needed "
            "whenever those values will be measured or analysed - "
            "including from imagery, such as a vegetation index, not "
            "just from elevation. Elevation data always uses this "
            "automatically. Larger than Lossy, and occasionally larger "
            "than the source file once pyramids are added; the run log "
            "explains when that happens.\n"
            "\n"
            "Lossy:\n"
            "JPEG compression, usually 70-80% smaller. Visually "
            "identical to the original, with slight artefacting at hard "
            "edges such as the collar boundary. The exact pixel values "
            "change, so avoid it if anything will be calculated from "
            "them. Never applied to elevation, and blocked where it "
            "would be unsafe.\n"
            "\n"
            "Show pixels hidden by NoData:\n"
            "Dark content that happens to be pure black can be treated "
            "as \"nothing here\" and disappear, leaving black gaps in "
            "the image. Ticking this reveals those pixels; the collar "
            "then renders as solid black instead of transparent. It "
            "does not create missing data - it only shows pixels that "
            "were already there. Detection reports what it found, but "
            "cannot tell deep shadow from a genuine gap in coverage, so "
            "the decision is yours.\n"
            "\n"
            "What it does:\n"
            "Fixes rasters that pan and zoom sluggishly in QGIS or "
            "QField - typically large drone orthos or elevation models "
            "with no internal structure, so software has to read the "
            "whole file to draw any part of it. Works out what the file "
            "actually is and applies the right tiling, pyramid and "
            "compression settings automatically. A file that is already "
            "optimised is left untouched.\n"
            "\n"
            "Supported and refused:\n"
            "Supported: 8-bit RGB imagery (3 or 4 band), and "
            "single-band Float32 elevation (DSM, DTM, CHM). Refused "
            "with a clear reason rather than a risky guess: classified "
            "rasters, more than 4 bands, and 16-bit imagery. Each needs "
            "handling this version does not have yet.\n"
            "\n"
            "If you choose wrong:\n"
            "Your source file is never modified - only a new output is "
            "written, and any attempt to write over the source is "
            "refused, even through a renamed or aliased path. Any "
            "choice here can be redone by running again with different "
            "settings. The one thing to keep in mind: do not delete the "
            "original after a lossy conversion, because a lossless "
            "version can only ever be made from the source."
        )

    def _lossy_on_elevation_message(self):
        return self.tr(
            "This is elevation data (a DSM, DTM or CHM). Lossy "
            "compression works by discarding detail your eye won't "
            "notice - fine for photographs, but on elevation it changes "
            "the actual height values, not just how the file looks. "
            "Choose '{}' instead."
        ).format(_PROFILE_LOSSLESS_NAME)

    def _already_optimised_message(self):
        return self.tr(
            "This file is already tiled and has pyramids built, so it "
            "should already load quickly in QGIS. Converting it again "
            "won't make it faster - it would just produce a second "
            "large file. If you're deliberately reconverting anyway - "
            "for example switching from Lossless to Lossy to save disk "
            "space - tick '{}' and run again."
        ).format(FORCE_REPROCESS_LABEL)

    def _nodata_meaningful_message(self, nodata_risk):
        pct = nodata_risk.interior_max_cell_fraction * 100
        return self.tr(
            "This file has NoData=0 pixels with real content behind "
            "them, not just the transparent collar around the edge (the "
            "see-through border where the image doesn't cover a "
            "rectangular file). {extent} pure black and currently "
            "hidden - usually shadow or water sitting on the same value "
            "QGIS uses to mean \"nothing here\". Tick '{label}' to "
            "reveal it, or proceed if you've already checked and it "
            "really is just the collar."
        ).format(extent=_nodata_extent_phrase(pct), label=CLEAR_NODATA_LABEL)

    def _nodata_meaningful_formatted(self, nodata_risk):
        """(html, text) pair for feedback.pushFormattedMessage() - the
        strongest available emphasis in the Processing log, reserved for
        this one message. It's the plugin's single most consequential,
        most educational finding (real content silently hidden by
        NoData=0), easy to lose among routine progress lines if it reads
        like just another orange pushWarning() line - which is exactly
        what it was before, and was reported as too easy to miss.
        pushFormattedMessage(html, text) writes html to htmlLog() (what
        the GUI log panel renders) and text to textLog() (console/
        qgis_process's plain-text fallback) - confirmed identical on both
        QGIS 4.2 and 3.44 LTR.
        """
        body = self._nodata_meaningful_message(nodata_risk)
        html = (
            '<p style="color:#a83232; font-weight:bold; margin:4px 0;">{}</p>'
        ).format(body)
        text = "*** {} ***".format(body)
        return html, text

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
        # "choice" branch below) - the new lossy-on-elevation warning
        # further down needs it for the "forced" profile_mode case too.
        # PROFILE always has a genuine value (defaulted to lossless if
        # untouched - see the PROFILE_LOSSLESS comment), so this is safe
        # to read regardless of profile_mode.
        profile_choice = self.parameterAsEnum(parameters, self.PROFILE, context)

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

        # Hard gate, not just a log note: ticking Clear NoData on a file
        # with no NoData=0 condition at all is a pure no-op (elevation
        # always lands here too - detect_metadata_only never creates a
        # NoDataRisk for it), and nobody should walk away from a run
        # thinking they've revealed hidden pixels when nothing happened.
        # This is deliberately NOT the same check as the
        # nodata_only_transparency structural block (NoData set, but
        # clearing would reveal a border) - that stays a soft, logged
        # non-gate, unaffected by this parameter, exactly as before.
        clear_nodata = self.parameterAsBoolean(parameters, self.CLEAR_NODATA, context)
        if clear_nodata and (detection.nodata_risk is None or not detection.nodata_risk.applies):
            return False, self.tr(
                "{} has no effect on this file - it doesn't have a "
                "NoData=0 condition for this plugin to clear. Untick it, "
                "or check Raster Information if you expected one."
            ).format(CLEAR_NODATA_LABEL)

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
        #
        # NoData risk (a possible third check here) is deliberately NOT
        # included: an earlier version blocked on it too, but
        # checkParameterValues() has no "acknowledge and proceed" concept
        # in QGIS's own UI - a failure renders as a modal QMessageBox with
        # only an OK button (confirmed via processing/gui/algorithm_widget.py),
        # not a dismissible banner. Since keeping NoData (clear_nodata=False)
        # is a fully legitimate, already-supported choice - not a mistake
        # like lossy-on-elevation or wasted work like reprocessing an
        # already-optimised file - gating it here left no way to proceed
        # without changing CLEAR_NODATA, which is exactly the "warning I
        # can't dismiss is a refusal" problem. It stays exactly where it
        # already lived before WARN_ENABLED existed: an unconditional
        # pushWarning in processAlgorithm(), see the nodata_risk block
        # below - matching this file's own design principle that
        # detection's collar-vs-shadow read "stays advisory, never a
        # gate" (see this module's docstring).
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
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT, self.tr("Input raster"),
        ))
        profile_param = QgsProcessingParameterEnum(
            self.PROFILE,
            self.tr(
                "Compression profile - both options are equally fast; "
                "this is only about whether pixel values stay exact"
            ),
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
        # This is now the first place a hesitant user lands, carrying the
        # reassurance the label deliberately no longer does (see the
        # PROFILE_OPTIONS comment) - kept to four sentences on purpose,
        # this is a hover tooltip, not a document.
        profile_param.setHelp(self.tr(
            "Both options produce the same fast-panning file - the "
            "choice is only about pixel values. Lossy is visually "
            "indistinguishable from the original; what changes is the "
            "exact numeric value of each pixel, by small amounts. That's "
            "irrelevant for a basemap you're navigating or digitising "
            "over, but it matters if those values feed a calculation - "
            "vegetation indices, classification, change detection. Lossy "
            "is typically 70-80% smaller."
        ))
        self.addParameter(profile_param)

        nodata_param = QgsProcessingParameterBoolean(
            self.CLEAR_NODATA, self.tr(CLEAR_NODATA_LABEL),
            defaultValue=False,
        )
        # Unticked by default - see the CLEAR_NODATA_LABEL comment above
        # for why. Only ever actionable on RGB imagery that has NoData=0
        # with a real alpha/mask already providing positional
        # transparency (detection.nodata_risk.clear_possible) -
        # meaningless on elevation and on files without that specific
        # condition, same as PROFILE is meaningless on elevation. Always
        # visible anyway: Processing parameter widgets are declared in
        # initAlgorithm() before any input is chosen and can't react to
        # it - the same constraint already established for PROFILE.
        # checkParameterValues refuses outright if this is ticked on a
        # file where it would be a pure no-op (no NoData=0 condition at
        # all), so nobody thinks they've fixed something they haven't;
        # the run log always states what happened otherwise, including
        # "not applicable" as the defensive fallback for callers that
        # skip checkParameterValues - see
        # core/converter.py's _resolve_nodata_handling.
        nodata_param.setHelp(self.tr(
            "Dark content that happens to be pure black - deep shadow, "
            "water, wet tarmac - can be treated as \"nothing here\" and "
            "disappear, leaving black gaps in the image. Tick this to "
            "make those pixels visible again. The trade is that the "
            "collar around the edge becomes solid black instead of "
            "transparent. This does not create missing data - it only "
            "reveals pixels that were already there."
        ))
        self.addParameter(nodata_param)

        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, self.tr("Optimised output"),
        ))

        # Everything below is Advanced: Input/Profile/Clear NoData/Output
        # is the whole decision most runs need, and all three of these
        # are either a rare override (Force reprocess), routine but
        # secondary (Replace existing output file - see its own comment
        # below for why it isn't hidden further), or a safety net most
        # people should just leave on (Warn me) - none of them belong in
        # the same visual weight as the actual content decision above.

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
        # NoData risk is NOT one of the two: it's a legitimate choice
        # either way (see checkParameterValues' comment on why it was
        # removed from here), not something this checkbox gates - it
        # always logs, regardless of this setting. Both checks here are
        # metadata-only (no pixel sampling), so ticking this costs
        # nothing extra either - unlike an earlier version of this
        # parameter, it was never worth warning that it slows anything
        # down.
        warn_param.setHelp(self.tr(
            "Ticked (the default): if something looks off before "
            "running - Lossy chosen on elevation, or the file already "
            "being optimised - a message explains why and blocks Run "
            "until you address it or untick this. Unticked: the same "
            "checks still happen, but only write their findings to the "
            "log; the run proceeds regardless."
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
            "Only relevant when the file is already tiled with "
            "overviews - normally left untouched, since redoing an "
            "already-fast file wastes time and disk space for no "
            "benefit. Tick this to reprocess it anyway, for example to "
            "switch from Lossless to Lossy after the fact."
        ))
        self.addParameter(force_reprocess_param)

        # Advanced, unlike an earlier version of this file: that version
        # argued overwriting an output while iterating is routine enough
        # that the Advanced section had hidden it well enough to go
        # unnoticed in testing. It's grouped with the other overrides now
        # because leaving Input/Profile/Clear NoData/Output as the entire
        # main panel matters more, and both this and Force reprocess are
        # already right there when a rerun onto the same path needs them.
        overwrite_param = QgsProcessingParameterBoolean(
            self.OVERWRITE, self.tr("Replace existing output file"),
            defaultValue=False,
        )
        overwrite_param.setFlags(overwrite_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        # Label alone doesn't say what happens when left unticked - fixed
        # via setHelp() rather than lengthening the label itself.
        overwrite_param.setHelp(self.tr(
            "Unticked (the default): if a file already exists at the "
            "output path, the algorithm refuses to run and tells you, "
            "rather than overwriting it silently. Ticked: the existing "
            "file is replaced. Either way your source file is never "
            "touched - writing the output over the input is refused "
            "outright, even if you point both at the same file."
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
        clear_nodata = self.parameterAsBoolean(parameters, self.CLEAR_NODATA, context)
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

        if detection.nodata_risk and detection.nodata_risk.applies and detection.nodata_risk.message:
            if detection.nodata_risk.assessment == "meaningful" and not clear_nodata:
                # The actionable, teach-a-novice version, given the
                # strongest available log emphasis - see
                # _nodata_meaningful_formatted()'s docstring for why a
                # plain pushWarning() line isn't enough here.
                html, text = self._nodata_meaningful_formatted(detection.nodata_risk)
                feedback.pushFormattedMessage(html, text)
            elif detection.nodata_risk.assessment == "meaningful":
                # Clear NoData is already ticked: informational
                # confirmation of what got revealed, not a decision to
                # make - detector.py's own terser message is enough here.
                feedback.pushWarning(detection.nodata_risk.message)
            else:
                feedback.pushInfo(detection.nodata_risk.message)

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
            clear_nodata=clear_nodata,
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
