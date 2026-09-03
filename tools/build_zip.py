# SPDX-License-Identifier: GPL-3.0-only
"""Build the plugins.qgis.org submission zip.

Reuses deploy.py's file allowlist (_iter_source_files) rather than
maintaining a second list of what ships - the zip's contents can never
drift from what a dev-profile deploy actually installs, since both
read from the exact same generator.

The zip's internal layout is what plugins.qgis.org and QGIS's own
"Install from ZIP" require: a single top-level folder named exactly
like the plugin package (raster_optimiser/), with metadata.txt and
__init__.py directly inside it - not nested one level deeper, and
without any of the repo's own dev files (testdata/, docs/, tools/,
.git/, .qgis_dev/) leaking in around it.

Usage:
    python tools/build_zip.py [--out <path>]

Defaults to <repo root>/raster_optimiser-<version>.zip, version read
from metadata.txt - the naming convention plugins.qgis.org expects.
"""

import argparse
import configparser
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy import (  # noqa: E402
    MAX_PAYLOAD_BYTES, PACKAGE_ROOT, PLUGIN_NAME, REPO_ROOT,
    _check_pyqt_imports, _iter_source_files,
)


def _read_version() -> str:
    parser = configparser.ConfigParser()
    parser.read(os.path.join(PACKAGE_ROOT, "metadata.txt"), encoding="utf-8")
    return parser.get("general", "version", fallback="unknown")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", default=None, help="Output zip path (default: repo root, versioned)")
    args = parser.parse_args(argv)

    files = list(_iter_source_files())

    total_size = sum(os.path.getsize(src) for src, _ in files)
    if total_size > MAX_PAYLOAD_BYTES:
        print(
            f"REFUSING: payload is {total_size / 1024 / 1024:.2f} MB, over "
            f"the {MAX_PAYLOAD_BYTES / 1024 / 1024:.0f} MB cap. Nothing was "
            "written.",
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

    version = _read_version()
    out_path = args.out or os.path.join(REPO_ROOT, f"{PLUGIN_NAME}-{version}.zip")

    if os.path.exists(out_path):
        os.remove(out_path)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, rel in sorted(files, key=lambda pair: pair[1]):
            arcname = "/".join((PLUGIN_NAME, rel.replace(os.sep, "/")))
            zf.write(src, arcname)

    print(f"Built {out_path} ({os.path.getsize(out_path) / 1024:.1f} KB, {len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
