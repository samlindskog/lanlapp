"""Adapter: USACE Talent Acquisition job board on Yello (usace.yellogov.com).

Board cards come from a JSON endpoint; each posting page carries the USAJOBS
announcement number and an Apply link straight to the USAJOBS control-number
URL, where the real duties/qualifications live.

Adapter contract (used by jobscan.py and jobboard/server.py):
    DISPLAY_NAME, BOARD_URL
    PRIMARY_KIND   document kind that holds the real description (shown first)
    KEY_SECTION    section name that decides eligibility (previewed, judged)
    list_jobs(fetch, since_days, max_pages) -> [card dict]
    fetch_job(fetch, card)                  -> [document dict]
    parse_fields(card, docs)                -> dict of common job fields
    sections(text, kind)                    -> ordered {name: text} for a document
"""
import html
import json
import re
import sys

sys.path.insert(0, __file__.rsplit("/sources/", 1)[0])
from jobscan import html_to_text  # noqa: E402

BOARD_ID = "rHrorPQ_B8HbgbWJDca8Ng"
BASE = "https://usace.yellogov.com"
BOARD_URL = f"{BASE}/job_boards/{BOARD_ID}"
DISPLAY_NAME = "USACE Talent Acquisition (Yello)"
PRIMARY_KIND = "usajobs_announcement"
KEY_SECTION = "Qualifications"

# Top-level headings on a USAJOBS announcement page, in page order. The text
# extractor emits each as a line on its own.
USAJOBS_HEADINGS = ["Summary", "Duties", "Requirements", "Conditions of Employment",
                    "Qualifications", "Education", "Additional information",
                    "How you will be evaluated", "Benefits", "Required documents",
                    "How to apply", "Next steps", "Fair and Transparent"]
YELLOGOV_HEADINGS = ["Announcement Type", "Position Type", "USACE Organization",
                     "Locations (City, State)", "Position Overview/Duties",
                     "Other Conditions and Requirements"]


def sections(text, kind="usajobs_announcement"):
    """Split a stored document into an ordered {heading: text} dict. USAJOBS
    repeats heading names in its side navigation, so for each heading the
    occurrence with the longest following body is taken as the real section."""
    heads = USAJOBS_HEADINGS if kind == "usajobs_announcement" else YELLOGOV_HEADINGS
    lines = (text or "").split("\n")
    occ = [(i, ln.strip()) for i, ln in enumerate(lines) if ln.strip() in heads]
    best = {}
    for n, (i, name) in enumerate(occ):
        j = occ[n + 1][0] if n + 1 < len(occ) else len(lines)
        body = "\n".join(lines[i + 1:j]).strip()
        if body and len(body) > len(best.get(name, (0, ""))[1]):
            best[name] = (i, body)
    return {name: body for name, (i, body) in sorted(best.items(), key=lambda kv: kv[1][0])}
