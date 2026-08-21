"""Release proof, not the install method.

Development install is tools/qgis-ltr-dev.bat and tools/qgis-4-dev.bat,
which point QGIS at the repo in place via QGIS_PLUGINPATH - zero copying,
edits take effect on next launch. This script is a filtered, allowlisted
copy of exactly what would ship in a real plugin package, so the deployed
file set can be verified as complete and self-sufficient (no dependency
on docs/, testdata/, or anything outside the plugin package) before this
ever goes near QGIS's plugin repository or a real user's machine.

IMPORTANT: QGIS_PLUGINPATH must be UNSET when launching QGIS to test a
deployed copy made by this script. If it's still set (e.g. from a shell
that ran one of the tools/qgis-*-dev.bat launchers earlier), QGIS sees
BOTH the repo, via QGIS_PLUGINPATH, and the deployed copy, via the
profile's python/plugins - you could be exercising the repo while
believing you're testing the deployed copy. Launch QGIS normally (its
regular installed shortcut) to test a deployed copy, not via the dev
launchers.

Usage:
    python tools/deploy.py --target <profile dir>

<profile dir> is the QGIS profile root, not the plugins folder directly -
get it by running this in the QGIS Python console:

    from qgis.core import QgsApplication
    QgsApplication.qgisSettingsDirPath()

The plugin is deployed to <profile dir>/python/plugins/raster_optimiser.
"""

import argparse
import os
import re
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.join(REPO_ROOT, "raster_optimiser")
PLUGIN_NAME = "raster_optimiser"

MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # hard cap - see module docstring

# Allowlist, not denylist: anything not named here is skipped, full stop.
# This is what stops testdata/, docs/, or repo-dev files landing in a
# profile if the package layout ever goes wrong.
ALLOWLIST_FILES = ["metadata.txt", "__init__.py", "plugin.py", "provider.py", "icon_utils.py"]
ALLOWLIST_DIRS = ["core", "algorithms", "icons"]

# LICENSE lives at the repo root (GitHub convention), one level above
# PACKAGE_ROOT, but the QGIS plugin repository requires a LICENSE file
# inside the uploaded plugin itself - so it's deployed to the package
# root alongside metadata.txt even though it isn't sourced from there.
ROOT_FILES = ["LICENSE"]

PYQT_DIRECT_IMPORT_RE = re.compile(r"^\s*(from|import)\s+PyQt[56]\b", re.MULTILINE)


def _iter_source_files():
    """Yield (absolute_src_path, path_relative_to_package_root) for everything the allowlist selects."""
    for name in ROOT_FILES:
        src = os.path.join(REPO_ROOT, name)
        if os.path.isfile(src):
            yield src, name

    for name in ALLOWLIST_FILES:
        src = os.path.join(PACKAGE_ROOT, name)
        if os.path.isfile(src):
            yield src, name

    for dirname in ALLOWLIST_DIRS:
        base = os.path.join(PACKAGE_ROOT, dirname)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                src = os.path.join(root, f)
                yield src, os.path.relpath(src, PACKAGE_ROOT)


def _check_pyqt_imports(files):
    offenders = []
    for src, rel in files:
        if not src.endswith(".py"):
            continue
        try:
            text = open(src, "r", encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        if PYQT_DIRECT_IMPORT_RE.search(text):
            offenders.append(rel)
    return offenders


def _remove_existing(dest):
    if not os.path.exists(dest):
        return
    try:
        # Removes a junction/symlink itself without touching what it
        # points at. Only fails (non-empty dir) for a real directory
        # tree, which is the signal to fall back to a real recursive
        # delete.
        os.rmdir(dest)
    except OSError:
        shutil.rmtree(dest)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target", required=True,
        help="QGIS profile directory, from QgsApplication.qgisSettingsDirPath()",
    )
    args = parser.parse_args(argv)

    files = list(_iter_source_files())

    total_size = sum(os.path.getsize(src) for src, _ in files)
    if total_size > MAX_PAYLOAD_BYTES:
        print(
            f"REFUSING: payload is {total_size / 1024 / 1024:.2f} MB, over "
            f"the {MAX_PAYLOAD_BYTES / 1024 / 1024:.0f} MB cap. Nothing was "
            "written. Check that no large or test files ended up inside "
            "the package (core/, algorithms/).",
            file=sys.stderr,
        )
        return 1

    pyqt_warnings = _check_pyqt_imports(files)
    if pyqt_warnings:
        print(
            "WARNING: direct PyQt5/PyQt6 imports found - these should "
            "import from qgis.PyQt for 3.x/4.x compatibility:"
        )
        for rel in pyqt_warnings:
            print(f"  {rel}")

    plugins_dir = os.path.join(args.target, "python", "plugins")
    dest = os.path.join(plugins_dir, PLUGIN_NAME)

    os.makedirs(plugins_dir, exist_ok=True)
    _remove_existing(dest)
    os.makedirs(dest)

    for src, rel in files:
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    metadata_path = os.path.join(dest, "metadata.txt")
    if not os.path.isfile(metadata_path):
        print(f"FAILED: metadata.txt missing at {metadata_path} after deploy.", file=sys.stderr)
        return 1

    print(f"Deployed {len(files)} files, {total_size / 1024:.1f} KB, to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
