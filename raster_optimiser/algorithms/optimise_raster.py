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
migration than UI code would be. The one line flagged below
(QgsProcessingParameterDefinition.FlagAdvanced) is the single point of
real uncertainty; everything else here has been stable Processing API
since well before the qgisMinimumVersion floor this plugin declares.
"""

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

from ..core.converter import convert
from ..core.detector import detect

PROFILE_AUTO = 0
PROFILE_A = 1
PROFILE_B = 2
PROFILE_OPTIONS = [
    "Auto (recommended)",
    "A - Looking at it (JPEG, smaller, for display)",
    "B - Measuring it (lossless, for analysis)",
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
        return self.tr("Optimise Raster (Tile, Pyramid, Compress)")

    def group(self):
        return self.tr("Raster Optimiser")

    def groupId(self):
        return "raster_optimiser"

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
            "Profile only applies to RGB imagery: 'A' is JPEG, smaller, for "
            "looking at; 'B' is lossless, for measuring or further analysis. "
            "Elevation data is always lossless and this choice is skipped "
            "automatically. Left on 'Auto', a file where the choice is "
            "genuinely required (RGB imagery) will fail with a message "
            "asking you to set it explicitly - it's not something that can "
            "be guessed from the pixels."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT, self.tr("Input raster"),
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.PROFILE,
            self.tr("Profile (RGB imagery only - ignored for elevation data)"),
            options=PROFILE_OPTIONS, defaultValue=PROFILE_AUTO,
        ))
        overwrite_param = QgsProcessingParameterBoolean(
            self.OVERWRITE, self.tr("Overwrite output if it already exists"),
            defaultValue=False,
        )
        # Flagged in the module docstring as the one line here I'm not
        # fully certain is unchanged in QGIS 4.x - test the Advanced
        # section collapses/behaves the same in both installs.
        overwrite_param.setFlags(
            overwrite_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced
        )
        self.addParameter(overwrite_param)
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, self.tr("Optimised output"),
        ))

    def processAlgorithm(self, parameters, context, feedback):
        input_layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if input_layer is None:
            raise QgsProcessingException(self.tr("Could not load the input raster."))
        source_path = input_layer.source()

        profile_choice = self.parameterAsEnum(parameters, self.PROFILE, context)
        overwrite = self.parameterAsBoolean(parameters, self.OVERWRITE, context)
        output_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        feedback.pushInfo(self.tr("Detecting: {}").format(source_path))
        detection = detect(source_path)

        if detection.refused:
            raise QgsProcessingException(detection.refusal_reason)

        chosen_profile = self._resolve_profile(detection, profile_choice, feedback)

        if detection.nodata_risk and detection.nodata_risk.applies and detection.nodata_risk.message:
            if detection.nodata_risk.assessment == "meaningful":
                feedback.pushWarning(detection.nodata_risk.message)
            else:
                feedback.pushInfo(detection.nodata_risk.message)

        def progress_cb(complete, message, cb_data):
            if feedback.isCanceled():
                return False
            feedback.setProgress(complete * 100)
            return True

        result = convert(
            source_path, detection=detection, chosen_profile=chosen_profile,
            output_path=output_path, force=overwrite, progress_cb=progress_cb,
        )

        if result.action == "already_optimised":
            feedback.pushInfo(result.message)
            return {self.OUTPUT: source_path}

        if result.action in _HARD_FAILURE_ACTIONS:
            raise QgsProcessingException(result.message)

        feedback.pushInfo(result.message)
        return {self.OUTPUT: result.output_path}

    def _resolve_profile(self, detection, profile_choice, feedback):
        if detection.profile_mode == "forced":
            forced = detection.forced_profile
            if profile_choice != PROFILE_AUTO:
                requested = "A" if profile_choice == PROFILE_A else "B"
                if requested != forced:
                    feedback.pushWarning(self.tr(
                        "Profile parameter ignored - this file's content type "
                        "forces Profile {}; Profile {} isn't applicable."
                    ).format(forced, requested))
            return forced

        # profile_mode == "choice": a genuine judgement call, never guessed.
        if profile_choice == PROFILE_AUTO:
            raise QgsProcessingException(self.tr(
                "This is RGB imagery - Profile is a required choice (A or B), "
                "it can't be auto-detected from the pixels. Set the Profile "
                "parameter and re-run."
            ))
        return "A" if profile_choice == PROFILE_A else "B"
