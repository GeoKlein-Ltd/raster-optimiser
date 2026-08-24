"""Processing provider registration.

icon() overrides the base class default so the toolbox group ("GeoKlein")
shows the cheetah logo, not QGIS's generic provider icon. See
icon_utils.plugin_icon() for how the icon is built and why that's safe
under both the PyQt5 (QGIS 3.x) and PyQt6 (QGIS 4.x) bindings.

name() (the toolbox tree heading) is "GeoKlein", not "GeoKlein Raster
Optimiser" - Phase 6 of the purpose-question rework, deliberately not
prefixed with the company name twice: the algorithm's own display name
("Optimise raster") is what someone types into the toolbox search box
before they know this plugin exists, and repeating the company name in
front of it there would bury that. id() stays "raster_optimiser" - it's
half of the permanent algorithm identifier (raster_optimiser:
optimise_raster, see OptimiseRasterAlgorithm.name()) and must never
change once anything references it (saved models, batch configs).
"""

from qgis.core import QgsProcessingProvider

from .algorithms.optimise_raster import OptimiseRasterAlgorithm
from .icon_utils import plugin_icon


class RasterOptimiserProvider(QgsProcessingProvider):

    def id(self):
        return "raster_optimiser"

    def name(self):
        return "GeoKlein"

    def icon(self):
        return plugin_icon()

    def loadAlgorithms(self):
        self.addAlgorithm(OptimiseRasterAlgorithm())
