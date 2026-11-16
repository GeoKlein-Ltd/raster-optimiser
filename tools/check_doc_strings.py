# -*- coding: utf-8 -*-
"""Do the docs quote the strings the code actually writes?

Run with the QGIS interpreter, from the repository root:

    C:\\OSGeo4W\\bin\\python-qgis.bat tools/check_doc_strings.py

WHY THIS EXISTS, AND WHAT THE OLDER CHECK MISSED

The earlier sync check compared two surfaces: the rendered help panel
(shortHelpString()) against docs/raster_optimiser_ui_text.md. It was
built to stop those two drifting apart, and it did that. But it had two
gaps, and one of them let a wrong quotation sit in a shipped README
through four releases:

  1. README.md was never an input. The check only ever opened the help
     panel and ui_text.md, so a runtime string quoted in the README was
     outside everything it looked at. That is exactly what happened: the
     README quoted "Not applicable: this check is for 8-bit imagery
     whose NoData value is 0." as the GEOKLEIN_6 example for an
     elevation raster. That sentence was replaced in 1.1.0 by four
     case-specific ones, and the elevation case now reads "Not
     applicable: this check is only for 8-bit imagery." Nothing compared
     the README to the code, so nothing noticed.

  2. It checked presence, not provenance. Its passage list asked "does
     this text appear in both surfaces", never "does this text match the
     constant in the package it claims to be quoting". Two surfaces can
     agree with each other and both be out of date - which is the state
     the help panel and ui_text.md's mirror of it were found in during
     the same sweep.

So this script works the other way round: it starts from the runtime
constants, imported from the package rather than retyped, and checks the
documents against them. Every surface that quotes a string is named
explicitly, so adding a quotation somewhere new means adding it here too.

TWO CHECKS

  A. Quoted runtime strings must match the code verbatim. For each
     constant, every surface listed must contain it exactly.

  B. Retired strings must not reappear. Text that was runtime wording in
     an earlier version must not be presented as current. A Markdown
     line may still carry one if it also carries a history marker -
     ui_text.md's design notes deliberately record superseded wording,
     and should keep being able to. The help panel gets no such
     allowance: it has no lines, and every word in it is current
     user-facing text.

Exit status is 1 if anything fails, so this can gate a release.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from raster_optimiser.algorithms import optimise_raster as oa  # noqa: E402
from raster_optimiser.core import converter  # noqa: E402


def collapse(text):
    return re.sub(r"\s+", " ", text).strip()


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text)
    return text.replace("&quot;", '"').replace("&amp;", "&")


def strip_md(text):
    return text.replace("***", "").replace("**", "").replace("`", "")


# ---- the surfaces ----------------------------------------------------
def read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


README = read("README.md")
UI_TEXT = read("docs", "raster_optimiser_ui_text.md")

try:
    from qgis.core import QgsApplication
    QgsApplication.setPrefixPath(r"C:\OSGeo4W\apps\qgis", True)
    _app = QgsApplication([], False)
    _app.initQgis()
    _alg = oa.OptimiseRasterAlgorithm()
    _alg.initAlgorithm({})
    HELP = strip_html(_alg.shortHelpString())
    HELP_AVAILABLE = True
except ImportError:
    HELP = ""
    HELP_AVAILABLE = False

SURFACES = {
    "README.md": strip_md(README),
    "ui_text.md": strip_md(UI_TEXT),
    "help panel": HELP,
}

# ---- A. runtime strings the docs quote, and where --------------------
# Each entry: the live constant, and every surface that must quote it
# verbatim. A surface not listed is free not to mention it; a surface
# listed here must match the code exactly.
QUOTED = [
    ("GEOKLEIN_6, not 8-bit",
     converter._NODATA_RECORD_NOT_8_BIT,
     ["README.md", "ui_text.md"]),
    ("GEOKLEIN_6, no NoData 0",
     converter._NODATA_RECORD_NO_NODATA_ZERO,
     ["ui_text.md"]),
    ("GEOKLEIN_6, checked, nothing hidden",
     converter._NODATA_RECORD_CHECKED_NOTHING_HIDDEN,
     ["ui_text.md"]),
    ("GEOKLEIN_6, collar-only marker",
     converter._NODATA_RECORD_COLLAR_ONLY_MARKER,
     ["ui_text.md"]),
    ("Store Float32 checkbox label",
     oa.STORE_FLOAT32_LABEL,
     ["ui_text.md"]),
    # The README names the option without its parenthetical, so the name
    # alone is what it has to get right.
    ("Store Float32 option name",
     "Store Float64 elevation as Float32",
     ["README.md", "ui_text.md", "help panel"]),
]

# ---- B. wording that is no longer what the tool says -----------------
RETIRED = [
    ("pre-1.1.0 single GEOKLEIN_6 message",
     "this check is for 8-bit imagery whose NoData value is 0"),
    ("pre-1.1 act-on-it-in-other-software framing",
     "happens in whatever software wrote the file"),
    # Not the same as the live multi-band string, which says "would
    # halve the pixel data" - that one is still correct, because the
    # checkbox genuinely does not cover multi-band Float64.
    ("pre-1.1 help-panel re-export framing",
     "re-exporting the source as Float32 halves the pixel data"),
    # The box-clear advisory stopped quoting a per-file figure when the
    # exact scan was made opt-in: it now says the figure is worked out
    # only if the box is ticked. Any surface still promising one is
    # describing a version that no longer exists.
    ("pre-1.1.1 claim that the log quotes a per-file bound",
     "The log gives the worst-case rounding"),
    # Float64 has about fifteen significant figures, not fifteen decimal
    # places: a height of 117.0 m carries nowhere near fifteen digits
    # after the point. Both the help panel and ui_text.md said decimal
    # places until 1.1.2.
    ("'decimal places' where significant figures is meant",
     "fifteen decimal places"),
    # The glossary used to say the tool keeps Float64 "exactly as it
    # found it" full stop, and that the log says what Float32 would have
    # saved. True only with the box clear, and silent about the box.
    ("pre-1.1.2 glossary claim that Float64 is always kept as found",
     "keeps it exactly as it found it and notes in the log"),
    # The Float32 FAQ used to open with this flat, which answers the
    # reader who left the box clear and misleads the one who ticked it
    # and is seeing Float32 in the log for a different reason. The
    # scoped form has no comma after "wrong", so it does not match this
    # and is not caught by it.
    ("pre-1.1.2 unconditional 'nothing was changed' opening",
     "Nothing is wrong, and nothing was changed"),
]

# ---- C. passages that exist twice, and must stay identical -----------
# The help panel is the text users actually read; ui_text.md holds the
# reviewable copy of it. They are maintained as one passage in two
# places, so a change to either without the other is a defect. Add an
# entry here whenever a passage starts being mirrored - the release-time
# sync run covers a longer list, but anything recorded here is checked
# on every run of this script, which is the one that is version
# controlled.
MIRRORED = [
    ("Float32-on-Float64 FAQ",
     "Why does the log mention Float32 on my Float64 file? If you left "
     "Store Float64 elevation as Float32 clear, nothing is wrong and "
     "nothing was changed. A Float64 raster is written back out "
     "as Float64, with every value preserved exactly. The note is there "
     "because Float64 records heights far more finely than any survey can "
     "measure them, so there is a smaller way to store the same survey: "
     "the 'Store Float64 elevation as Float32' box under Advanced "
     "parameters, described above. The note points at that box rather than "
     "quoting a figure for your file. Tick the box and the run works the "
     "figure out exactly, checks it pixel by pixel, keeps Float64 anyway if "
     "the rounding would exceed a millimetre, and records what it did in the "
     "file's own metadata. Leave the box clear and nothing is rounded."),
    ("Store Float32 parameter entry",
     "Float64 elevation records heights to about fifteen significant "
     "figures, far finer than any survey measures, so most of that is "
     "storage spent on precision nobody collected."),
    ("Bit depth glossary",
     "Float64 is finer than any survey can measure. By default the tool "
     "keeps it exactly as it found it; the Store Float64 elevation as "
     "Float32 option under Advanced parameters stores it at Float32 "
     "instead."),
]

HISTORY_MARKERS = (
    "until 1.1", "an earlier version", "earlier version",
    "was going onto", "before the tool could",
    "rather than telling anyone", "if you are comparing outputs",
)

# ---- run -------------------------------------------------------------
failures = []

print("=" * 74)
print("A. DOES EACH SURFACE QUOTE THE RUNTIME STRING VERBATIM?")
print("=" * 74)
if not HELP_AVAILABLE:
    print("  NOTE: QGIS not importable, so the help panel was not checked.")
print("  %-40s%-13s%s" % ("string", "surface", "verdict"))
print("  " + "-" * 66)
for label, value, surfaces in QUOTED:
    for surface in surfaces:
        if surface == "help panel" and not HELP_AVAILABLE:
            continue
        ok = collapse(value) in collapse(SURFACES[surface])
        if not ok:
            failures.append("%s does not quote %s verbatim"
                            % (surface, label))
        print("  %-40s%-13s%s"
              % (label, surface, "ok" if ok else "MISMATCH"))

print()
print("=" * 74)
print("B. HAS ANY RETIRED WORDING SURVIVED?")
print("=" * 74)
allowed = []
for label, phrase in RETIRED:
    hits = []
    for text, name in ((README, "README.md"), (UI_TEXT, "ui_text.md")):
        for number, line in enumerate(text.splitlines(), 1):
            if phrase.lower() in strip_md(line).lower():
                low = line.lower()
                if any(m in low for m in HISTORY_MARKERS):
                    allowed.append("%s:%d (%s) - recorded as history"
                                   % (name, number, label))
                else:
                    hits.append("%s:%d" % (name, number))
    if HELP_AVAILABLE and phrase.lower() in collapse(HELP).lower():
        hits.append("help panel")
    if hits:
        failures.append("retired wording still live - %s: %s"
                        % (label, ", ".join(hits)))
    print("  %-62s%s" % (label,
                         "LIVE: " + ", ".join(hits) if hits else "gone"))

if allowed:
    print()
    print("  deliberately kept as history, not counted:")
    for line in allowed:
        print("    %s" % line)

print()
print("=" * 74)
print("C. IS MIRRORED TEXT STILL WORD FOR WORD IN BOTH PLACES?")
print("=" * 74)
if not HELP_AVAILABLE:
    print("  NOTE: QGIS not importable, so this check was skipped.")
for label, passage in MIRRORED:
    if not HELP_AVAILABLE:
        break
    wanted = collapse(passage)
    in_help = wanted in collapse(SURFACES["help panel"])
    in_doc = wanted in collapse(SURFACES["ui_text.md"])
    if not (in_help and in_doc):
        where = []
        if not in_help:
            where.append("help panel")
        if not in_doc:
            where.append("ui_text.md")
        failures.append("mirrored passage out of step - %s: missing from %s"
                        % (label, ", ".join(where)))
    print("  %-62s%s" % (label,
                         "in step" if in_help and in_doc else "OUT OF STEP"))

print()
print("=" * 74)
if failures:
    print("FAILED, %d problem(s):" % len(failures))
    for problem in failures:
        print("  - %s" % problem)
else:
    print("PASSED: every quoted runtime string matches the code, and no "
          "retired wording is presented as current.")
print("=" * 74)
sys.exit(1 if failures else 0)
