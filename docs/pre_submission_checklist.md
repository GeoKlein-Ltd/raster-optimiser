# Pre-submission checklist

What must be true before `raster_optimiser.*.zip` gets uploaded to
plugins.qgis.org. Nothing here is enforced by the build - check it by hand
before every submission.

- [ ] **github.com/GeoKlein-Ltd/raster-optimiser is public.** `metadata.txt`'s
  `repository`, `tracker` and `homepage` all point there, and plugins.qgis.org
  reviewers check that those links resolve. Three 404s from an unknown
  publisher would get the submission bounced or flagged. The repo exists as
  of 2026-08-25; whether it's public rather than private hasn't been
  confirmed here - check before submitting.
- [ ] **The `GEOKLEIN_1_TOOL` URL in `core/converter.py` has been changed
  from the GitHub repo to the real plugins.qgis.org listing URL, once one
  exists, and the placeholder note in its text removed.** It currently
  points at `https://github.com/GeoKlein-Ltd/raster-optimiser` as a stand-in,
  marked as such in the text itself, because the plugins.qgis.org listing
  doesn't exist yet. Every file produced before that change carries the
  stand-in link in its own embedded metadata - this can only be fixed going
  forward, not in files already handed to a client.
- [ ] **A changelog entry has been added to `metadata.txt` for this release.**
  Optional, but the upload form has a field for it and most published plugins
  fill it in.
- [ ] **The zip has been rebuilt (`python tools/build_zip.py`) after any of
  the above.** All three change files that ship inside it.
