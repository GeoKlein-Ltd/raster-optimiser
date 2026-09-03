# SPDX-License-Identifier: GPL-3.0-only
"""QGIS plugin entry point.

Deferred import inside classFactory is deliberate, standard QGIS plugin
convention: QGIS scans this file for classFactory() before the QGIS
Python environment is necessarily ready for the rest of the plugin's
imports (Qt bindings, processing registry, etc.), so nothing beyond the
stdlib runs at module import time.
"""


def classFactory(iface):
    from .plugin import RasterOptimiserPlugin
    return RasterOptimiserPlugin(iface)
