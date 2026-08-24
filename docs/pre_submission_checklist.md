# Pre-submission checklist

What must be true before `raster_optimiser.*.zip` gets uploaded to
plugins.qgis.org. Nothing here is enforced by the build - check it by hand
before every submission.

- [ ] **github.com/GeoKlein/raster-optimiser is public.** `metadata.txt`'s
  `repository`, `tracker` and `homepage` all point there, and plugins.qgis.org
  reviewers check that those links resolve. Three 404s from an unknown
  publisher would get the submission bounced or flagged.
- [ ] **The `GEOKLEIN_1_TOOL` placeholder URL in `core/converter.py` has been
  replaced with the real plugins.qgis.org listing URL, once one exists.**
  Every file produced before that change carries a dead link in its own
  embedded metadata - this can only be fixed going forward, not in files
  already handed to a client.
- [ ] **A changelog entry has been added to `metadata.txt` for this release.**
  Optional, but the upload form has a field for it and most published plugins
  fill it in.
- [ ] **The zip has been rebuilt (`python tools/build_zip.py`) after any of
  the above.** All three change files that ship inside it.
