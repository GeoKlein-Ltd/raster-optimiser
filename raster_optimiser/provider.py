"""Processing provider registration.

No custom icon() override - the base class default is used deliberately,
to avoid any Qt image-loading code in a module that needs to behave
identically under both the PyQt5 (QGIS 3.x) and PyQt6 (QGIS 4.x) bindings.
"""

from qgis.core import QgsProcessingProvider

from .algorithms.optimise_raster import OptimiseRasterAlgorithm


class RasterOptimiserProvider(QgsProcessingProvider):

    def id(self):
        return "raster_optimiser"

    def name(self):
        return "Raster Optimiser"

    def loadAlgorithms(self):
        self.addAlgorithm(OptimiseRasterAlgorithm())
