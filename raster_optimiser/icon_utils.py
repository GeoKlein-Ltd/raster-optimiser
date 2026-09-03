# SPDX-License-Identifier: GPL-3.0-only
"""Shared plugin icon loading.

One QIcon built from all three exported sizes via addFile(), so Qt picks
the closest match per display scaling instead of resampling a single
image. Both the Processing provider (toolbox group icon), the algorithm
itself (toolbox entry icon), and the plugin's toolbar/menu action call
plugin_icon() rather than each loading the files themselves, so there's
exactly one place that knows the file names and sizes.

Paths are resolved with os.path.dirname(__file__), not the working
directory, so this works regardless of where QGIS loaded the plugin
from (dev install via QGIS_PLUGINPATH, or a normal profile install).

QIcon.addFile() and QSize are unchanged between PyQt5 (QGIS 3.x) and
PyQt6 (QGIS 4.x) - both come from qgis.PyQt, the compatibility shim
QGIS itself provides, not a raw PyQt5/PyQt6 import.

Fails loudly on a missing or unloadable file, deliberately, instead of
globbing for "anything that looks like a 24px export": addFile() is a
void method (confirmed empirically on both bindings - it always returns
None) that no-ops silently on a missing path, so a bad filename after a
fresh Inkscape export previously produced a QIcon that was still
non-null - just missing that one size, silently falling back to a
scaled-up neighbour - with nothing anywhere to notice. A glob would
trade that failure for a worse one: if a stray export from an earlier
attempt is still sitting in icons/, a size-pattern match can silently
pick the WRONG file instead of the missing one. Exact expected
filenames plus a hard failure when they're not all present and loadable
is the only option that rules out silent-wrong as well as silent-empty.
"""

import os

from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QIcon

_ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")
_SIZES = (24, 48, 64)


class MissingIconError(RuntimeError):
    """One or more expected plugin icon files are missing or failed to load."""


def plugin_icon():
    icon = QIcon()
    missing = []
    for size in _SIZES:
        filename = "icon_{}.png".format(size)
        path = os.path.join(_ICONS_DIR, filename)
        if not os.path.isfile(path):
            missing.append("{} (not found)".format(filename))
            continue
        icon.addFile(path, QSize(size, size))

    # addFile() returns nothing to check, so the only reliable signal
    # that every size actually loaded is what QIcon ended up holding.
    loaded_sizes = {s.width() for s in icon.availableSizes()}
    for size in _SIZES:
        filename = "icon_{}.png".format(size)
        if size not in loaded_sizes and "{} (not found)".format(filename) not in missing:
            missing.append("{} (found, but failed to decode as an image)".format(filename))

    if missing:
        raise MissingIconError(
            "Raster Optimiser: {} of {} expected icon files could not be "
            "loaded from {}:\n  {}\n"
            "Re-exports from Inkscape must be saved under exactly these "
            "filenames - rename them before restarting QGIS.".format(
                len(missing), len(_SIZES), _ICONS_DIR, "\n  ".join(missing)
            )
        )
    return icon
