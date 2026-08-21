"""Plugin lifecycle: register/deregister the Processing provider, and add
a toolbar button + Raster-menu entry as one-click entry points into the
same algorithm dialog the Processing Toolbox already opens.

Still no custom dialog (see docs/plugin_design_notes.md): the QAction's
only job is to call processing.execAlgorithmDialog() with this plugin's
algorithm ID, so there's exactly one parameter UI to maintain - the
Toolbox entry and this action just open it two different ways.

QAction cross-version note: PyQt6 moved QAction from QtWidgets to QtGui
upstream, a common Qt5->Qt6 migration trap. qgis.PyQt.QtWidgets.QAction
still works under both bindings - QGIS's compatibility shim re-exports
it there - so import it from qgis.PyQt.QtWidgets, not qgis.PyQt.QtGui,
for consistency with older QGIS code. Confirmed directly: on QGIS 4.2
(PyQt6) the constructed object's actual class is PyQt6.QtGui.QAction;
on QGIS 3.44 LTR (PyQt5) it's PyQt5.QtWidgets.QAction. Same import line,
same behaviour either way. iface.addToolBarIcon() / addPluginToRasterMenu()
and their removal counterparts are plain QgisInterface methods, not raw
Qt - stable across the Qt5->Qt6 migration.
"""

from qgis.core import QgsApplication
from qgis.PyQt.QtWidgets import QAction

from .icon_utils import plugin_icon
from .provider import RasterOptimiserProvider

ALGORITHM_ID = "raster_optimiser:optimise_raster"

# Raster menu, not Plugins menu: this is a raster-processing tool (see
# metadata.txt's category=Raster) and QGIS ships addPluginToRasterMenu()
# specifically for that case. Core raster tools (Conversion, Analysis,
# ...) already live under Raster, so that's where someone looking for a
# raster tool checks first - the Plugins menu is an undifferentiated
# grab-bag of every installed plugin regardless of what it does.
RASTER_MENU_NAME = "Raster Optimiser"


class RasterOptimiserPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None

    def initGui(self):
        self.provider = RasterOptimiserProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        # One QAction, added to both the toolbar and the menu - Qt
        # supports an action living in multiple places at once, so
        # there's a single icon/text/enabled state to keep in sync
        # rather than two.
        self.action = QAction(plugin_icon(), "Optimise raster...", self.iface.mainWindow())
        self.action.triggered.connect(self._run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu(RASTER_MENU_NAME, self.action)

    def _run(self):
        # Deferred import: processing's own package import is heavier
        # than this plugin needs at load time, and the callback only
        # runs long after QGIS's Python environment is fully up anyway.
        import processing
        processing.execAlgorithmDialog(ALGORITHM_ID)

    def unload(self):
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginRasterMenu(RASTER_MENU_NAME, self.action)
            self.action = None

        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
