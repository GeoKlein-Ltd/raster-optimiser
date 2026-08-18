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

# No "Auto" sentinel: an enum defaulting to a value that guarantees failure
# for RGB input (the previous PROFILE_AUTO=0 default) is worse than an
# enum with no default at all. Confirmed empirically (QGIS 4.2 and 3.44
# LTR, both from local source, not memory) that QgsProcessingParameterEnum
# has no way to represent a genuine "nothing selected yet" state once a
# run actually happens: parameterAsEnum() returns 0 identically whether
# the key is omitted, explicitly None, or the user picked index 0 - there
# is no distinguishable sentinel to detect "not chosen" from "chose A".
# What IS real: a mandatory (optional=False), no-default enum makes
# QGIS's own checkParameterValues refuse automatically - confirmed via
# direct testing - whenever a caller (Model Designer, qgis_process, the
# Python API) omits the PROFILE key entirely. That's the only enforcement
# this shape can offer; the interactive Algorithm Dialog's combo box will
# always show *some* option pre-highlighted (there's no blank combo state
# in Qt), so a user who never touches the dropdown submits whatever
# options[0] is. That's an accepted trade-off for two clean options
# instead of a three-way sentinel - confirm what the dialog actually shows
# when you test it; I can't render the interactive widget from here to
# check what it looks like at first paint.
PROFILE_A = 0
PROFILE_B = 1
PROFILE_OPTIONS = [
    "A - Display (smaller, lossy JPEG)",
    "B - Analysis (lossless, larger)",
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
            "Fixes rasters that pan and zoom sluggishly in QGIS or QField - "
            "typically large orthomosaics or elevation models exported from "
            "drone photogrammetry or LiDAR software. The default export from "
            "that software is technically correct but has no internal "
            "structure that lets software read part of it without reading "
            "all of it, which is what makes it slow.\n\n"
            "What it does: opens the file, works out what it actually is, "
            "and applies the right tiling, pyramid and compression settings "
            "automatically - no GDAL creation options to look up or "
            "remember. A file that's already tiled with pyramids is left "
            "untouched rather than reprocessed.\n\n"
            "Supported: 8-bit RGB imagery (3 or 4 band) and single-band "
            "Float32 elevation data (DSM/DTM/CHM).\n\n"
            "Refused, with a clear reason rather than a risky guess: "
            "classified/categorical rasters (e.g. land cover maps), "
            "multispectral stacks (more than 4 bands), and 16-bit imagery. "
            "Each of these needs handling this version doesn't have yet, "
            "and silently reusing the imagery recipe on them would corrupt "
            "the data rather than just fail to help.\n\n"
            "Profile is a required choice for RGB imagery, because the "
            "same file can be genuinely correct either way depending on "
            "what it's for - that's not something the pixels can answer. "
            "'A' compresses with lossy JPEG: much smaller, the right "
            "choice for a basemap or a file going onto a tablet in the "
            "field. 'B' is lossless: larger, but required the moment "
            "pixel values will be measured, analysed, or fed into "
            "another process, since JPEG discards the exact values to "
            "get its size down.\n\n"
            "Elevation data skips this choice entirely and is always "
            "lossless - a DSM or DTM is measurements, not a picture, and "
            "lossy compression would quietly corrupt the values for "
            "every use, not just some."
        )

    def checkParameterValues(self, parameters, context):
        # Runs before execution and can refuse instantly, with nothing run
        # yet - QGIS 4.2 and 3.44 LTR both confirmed (via local source
        # inspection, not memory/docs) to expose this as
        # checkParameterValues(self, parameters, context) -> (bool, str).
        #
        # Deliberately calls detect_metadata_only(), never detect(): the
        # scope refusals below (unsupported dtype, multispectral,
        # colour-table classified, 16-bit blocking Profile A, no CRS) are
        # all resolvable from gdal.Open() + band metadata alone, in
        # milliseconds even on a multi-gigapixel file. Running the full
        # detect() here - which pixel-samples for the classified check
        # and the NoData black-pixel risk - is exactly the bug this fixes:
        # it cost 181s on a 1.7GB ortho before the user ever saw "Profile
        # is a required choice". No detection logic is duplicated here;
        # this reads the same DetectionResult shape detect() produces,
        # just via detector.py's metadata-only code path.
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
            profile_choice = self.parameterAsEnum(parameters, self.PROFILE, context)
            requested = "A" if profile_choice == PROFILE_A else "B"
            opt = next((o for o in detection.profile_options if o.profile == requested), None)
            if opt is not None and not opt.available:
                return False, opt.reason_blocked or self.tr(
                    "Profile {} is not available for this file."
                ).format(requested)

        return True, ""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT, self.tr("Input raster"),
        ))
        profile_param = QgsProcessingParameterEnum(
            self.PROFILE,
            self.tr("Profile (RGB imagery only - ignored for elevation data)"),
            options=PROFILE_OPTIONS,
        )
        # No defaultValue passed above: mandatory (optional=False, the
        # QgsProcessingParameterEnum default) with no default is the
        # closest this API gets to "the user must choose" - see the
        # PROFILE_A/PROFILE_B comment above for what that does and
        # doesn't enforce.
        profile_param.setHelp(self.tr(
            "A compresses with lossy JPEG - much smaller, correct for "
            "basemaps and field use. B is lossless - use it if the pixel "
            "values will be measured or analysed."
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
            self.OVERWRITE, self.tr("Overwrite output if it already exists"),
            defaultValue=False,
        ))

    def processAlgorithm(self, parameters, context, feedback):
        input_layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if input_layer is None:
            raise QgsProcessingException(self.tr("Could not load the input raster."))
        source_path = input_layer.source()

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
        requested = "A" if profile_choice == PROFILE_A else "B"

        if detection.profile_mode == "forced":
            forced = detection.forced_profile
            if requested != forced:
                feedback.pushWarning(self.tr(
                    "Profile parameter ignored - this file's content type "
                    "forces Profile {}; Profile {} isn't applicable."
                ).format(forced, requested))
            return forced

        # profile_mode == "choice": a genuine judgement call, never guessed.
        # checkParameterValues already validated this before execution
        # reached here in the standard run flow; re-checked defensively in
        # case this algorithm is ever invoked in a way that skips it.
        opt = next((o for o in detection.profile_options if o.profile == requested), None)
        if opt is not None and not opt.available:
            raise QgsProcessingException(opt.reason_blocked or self.tr(
                "Profile {} is not available for this file."
            ).format(requested))
        return requested
