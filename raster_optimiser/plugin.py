"""Plugin lifecycle: register/deregister the Processing provider.

No custom dialog, no toolbar button - this is a Processing-only plugin
by design (see docs/plugin_design_notes.md), so initGui()/unload() have
exactly one job each.
"""

from qgis.core import QgsApplication

from .provider import RasterOptimiserProvider


class RasterOptimiserPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initGui(self):
        self.provider = RasterOptimiserProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
