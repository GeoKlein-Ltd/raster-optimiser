"""QGIS Processing algorithm wrapping the Raster Optimiser engine.

Thin wrapper only. All detection and conversion logic lives in core/
(detector.py, converter.py), which have no QGIS dependency and are
untouched by this file - it declares Processing parameters, translates
them into calls into core/, and maps the results back onto QGIS
feedback/exceptions. See docs/plugin_design_notes.md for why the
NoData question isn't a parameter here (the sampling is advisory, not
a gate - core/converter.py already clears NoData whenever it's
structurally safe to, regardless of the risk assessment) and why an
already-optimised file's OUTPUT resolves to the source path rather
than a copy.

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
    QgsProcessingParameterEnum,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from ..core.converter import convert, output_exists_message
from ..core.detector import detect, detect_metadata_only

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
PROFILE_OPTIONS = [
    "Speed + lossless (pixel values preserved exactly)",
    "Speed + lossy (visually identical, pixel values changed)",
]

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

    def name(self):
        return "optimise_raster"

    def displayName(self):
        # Sentence case (title case reads off-convention next to QGIS's
        # own algorithms), and no parenthetical - it truncated in Model
        # Designer. What it does belongs in shortHelpString(), not here.
        return self.tr("Optimise raster")

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
            "optimiser", "optimizer", "slow", "large", "large file",
            "pyramids", "overviews", "compress", "compression", "tiling",
            "tiled", "geotiff", "cog", "cloud optimised geotiff",
            "cloud optimized geotiff", "jpeg", "zstd", "performance",
            "speed", "drone", "photogrammetry", "lidar", "dsm", "dtm",
            "chm", "elevation", "qfield",
        ]

    def shortHelpString(self):
        return self.tr(
            "Lossless (the default)\n"
            "Every pixel value survives exactly. Required the moment "
            "those values will be measured, analysed, or fed into "
            "another process - not just for elevation. RGB imagery needs "
            "it too whenever it's the input to something like a "
            "vegetation index: lossy compression discards the exact "
            "values those indices are derived from, so a file that's "
            "perfectly fine as a basemap can be the wrong file for that "
            "calculation. The choice is about what the file is for, not "
            "what it contains - the same source imagery can genuinely "
            "need either option depending on the use. Elevation data "
            "(DSM/DTM/CHM) always uses this option automatically, "
            "whatever is selected - a DSM is measurements, not a "
            "picture, and lossy compression would quietly corrupt the "
            "values for every use, not just some.\n\n"
            "Lossy\n"
            "JPEG compression, typically 70-80% smaller than an "
            "uncompressed export (measured so far on one file - a range, "
            "not a guarantee, until more are tested). At quality 90 the "
            "result is visually indistinguishable from the original - "
            "what changes is the exact numeric value of each pixel, by "
            "small amounts. That's irrelevant for a basemap you're "
            "navigating or digitising over, and it's the right choice "
            "for anything going onto a tablet in the field. It only "
            "matters if those values feed a calculation - vegetation "
            "indices, classification, change detection - which is what "
            "the lossless option is for. Only offered for RGB imagery, "
            "and only when it's safe (see Refused, below); never offered "
            "for elevation.\n\n"
            "What this algorithm is for: rasters that pan and zoom "
            "sluggishly in QGIS or QField - typically large orthomosaics "
            "or elevation models exported from drone photogrammetry or "
            "LiDAR software. The default export from that software is "
            "technically correct but has no internal structure that lets "
            "software read part of it without reading all of it, which "
            "is what makes it slow.\n\n"
            "What it does: opens the file, works out what it actually is, "
            "and applies the right tiling, pyramid and compression "
            "settings automatically - no GDAL creation options to look "
            "up or remember. A file that's already tiled with pyramids "
            "is left untouched rather than reprocessed.\n\n"
            "Supported: 8-bit RGB imagery (3 or 4 band) and single-band "
            "Float32 elevation data (DSM/DTM/CHM).\n\n"
            "Refused, with a clear reason rather than a risky guess: "
            "classified/categorical rasters (e.g. land cover maps), "
            "multispectral stacks (more than 4 bands), and 16-bit "
            "imagery. Each of these needs handling this version doesn't "
            "have yet, and silently reusing the imagery recipe on them "
            "would corrupt the data rather than just fail to help."
        )

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

        # Cheapest possible check first, before anything raster-related:
        # an os.path.exists() call, not even a gdal.Open(). This used to
        # only surface as a "blocked" failure from convert() - after a
        # full detection run had already cost over a minute on a large
        # file. Same message convert() itself would use (see
        # core/converter.py's output_exists_message docstring for why
        # it's a shared function, not copied text).
        overwrite = self.parameterAsBoolean(parameters, self.OVERWRITE, context)
        if not overwrite:
            output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
            if output_path and os.path.exists(output_path):
                return False, output_exists_message(output_path)

        input_layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if input_layer is None:
            # No/invalid input yet - let the base parameter validation or
            # processAlgorithm produce that error, don't duplicate it here.
            return True, ""
        source_path = input_layer.source()

        try:
            detection = detect_metadata_only(source_path)
        except Exception:  # noqa: BLE001 - never block the dialog on a quick-check crash
            return True, ""

        if detection.refused:
            return False, detection.refusal_reason

        if detection.profile_mode == "choice":
            # PROFILE now has a real default (lossless, index 0 - see
            # the PROFILE_LOSSLESS comment), so there's no "not set" case
            # left to detect here: parameterAsEnum() always resolves to
            # a genuine value, defaulted or chosen. This replaces the
            # earlier "no default, force a choice" design from an
            # earlier round - superseded because the asymmetric harm
            # runs the other way: an untouched dropdown giving a
            # larger-than-optimal file is visible and recoverable;
            # silently altering pixel values is not.
            profile_choice = self.parameterAsEnum(parameters, self.PROFILE, context)
            requested = "lossless" if profile_choice == PROFILE_LOSSLESS else "lossy"
            opt = next((o for o in detection.profile_options if o.profile == requested), None)
            if opt is not None and not opt.available:
                return False, opt.reason_blocked or self.tr(
                    "The {} option is not available for this file."
                ).format(requested)

        return True, ""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT, self.tr("Input raster"),
        ))
        profile_param = QgsProcessingParameterEnum(
            self.PROFILE,
            self.tr("Compression profile - choose based on intended use"),
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
            "Lossless keeps every pixel value exact - the default, and "
            "required if those values will be measured or analysed. "
            "Lossy (JPEG) is usually 70-80% smaller and visually "
            "indistinguishable at quality 90, right for a basemap or "
            "field use - but it changes the exact pixel values, so skip "
            "it if you'll compute anything from them."
        ))
        self.addParameter(profile_param)
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, self.tr("Optimised output"),
        ))
        # Not Advanced: overwriting an output while iterating on a file is
        # routine, not an edge case, and the collapsed Advanced section
        # hid it well enough that it went unnoticed in testing. Declared
        # after OUTPUT so it reads as "...and here's what to do if that
        # path already exists."
        self.addParameter(QgsProcessingParameterBoolean(
            self.OVERWRITE, self.tr("Replace existing output file"),
            defaultValue=False,
        ))

    def processAlgorithm(self, parameters, context, feedback):
        input_layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if input_layer is None:
            raise QgsProcessingException(self.tr("Could not load the input raster."))
        source_path = input_layer.source()

        # PROFILE has a real default now (lossless), so parameterAsEnum()
        # always resolves to a genuine value - no more need to read the
        # raw dict to detect "not set" (see the PROFILE_LOSSLESS comment).
        profile_choice = self.parameterAsEnum(parameters, self.PROFILE, context)
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

        chosen_profile = self._resolve_profile(detection, profile_choice, feedback)

        if detection.nodata_risk and detection.nodata_risk.applies and detection.nodata_risk.message:
            if detection.nodata_risk.assessment == "meaningful":
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
            translate_progress_cb=make_progress_cb(10, 65),
            overview_progress_cb=make_progress_cb(75, 25),
            log_cb=log_cb,
        )

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
                feedback.pushWarning(self.tr(
                    "Elevation data detected - using {} compression. Lossy "
                    "compression alters elevation values, so it is never "
                    "applied to elevation."
                ).format(forced))
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
