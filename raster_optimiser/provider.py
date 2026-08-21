"""Processing provider registration.

icon() overrides the base class default so the toolbox group ("Raster
Optimiser") shows the cheetah logo, not QGIS's generic provider icon.
See icon_utils.plugin_icon() for how the icon is built and why that's
safe under both the PyQt5 (QGIS 3.x) and PyQt6 (QGIS 4.x) bindings.
"""

from qgis.core import QgsProcessingProvider

from .algorithms.optimise_raster import OptimiseRasterAlgorithm
from .icon_utils import plugin_icon


class RasterOptimiserProvider(QgsProcessingProvider):

    def id(self):
        return "raster_optimiser"

    def name(self):
        return "Raster Optimiser"

    def icon(self):
        return plugin_icon()

    def loadAlgorithms(self):
        self.addAlgorithm(OptimiseRasterAlgorithm())
