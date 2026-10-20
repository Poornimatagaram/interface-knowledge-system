"""
Knowledge system that knows what's true now (and what was true then).
Step 2: keep every version of every page.
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents
                            if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

WORD = re.compile(r"[a-z0-9$%.]+")

# ---------------- Page trust: who to believe when pages disagree ----------------

def page_kind(url: str) -> str:
    u = url.lower().split("?")[0].rstrip("/")
    path = re.sub(r"^https?://[^/]+", "", u)
    if path in ("/rates", "/business-rates"):
        return "rate_sheet"
    if "/calculators" in path or "calculator" in path:
        return "calculator"
    if path.startswith(("/newsroom", "/learn", "/dupacodaily", "/business/resources")) \
            or re.match(r"^/20\d\d/", path):
        return "news_blog"
    if path.startswith("/hubfs") or path.endswith((".pdf", ".xlsx", ".xls", ".csv")):
        return "document"
    if path.endswith("-lp") or "offer" in path or "info.dupaco.com" in u:
        return "promo"
    if path == "" or path.startswith(("/personal", "/business", "/about", "/contact", "/membership",
                                      "/loans", "/checking", "/savings", "/connect",
                                      "/online-and-mobile-banking")):
        return "official"
    return "other"


TRUST = {"rate_sheet": 1.0, "official": 0.9, "other": 0.6, "promo": 0.45,
         "document": 0.45, "news_blog": 0.35, "calculator": 0.3}

KIND_LABEL = {"rate_sheet": "OFFICIAL RATE SHEET", "official": "official product/info page",
              "other": "site page", "promo": "promo/landing page (may be expired)",
              "document": "uploaded document (may be old)", "calculator": "calculator (often stale)",
              "news_blog": "news/blog post (dated; describes the past)"}

class TemporalKnowledge:
    def __init__(self):
        self.snapshots = []                    # [(taken_on, snapshot_id)], oldest first
        self.pages = defaultdict(list)         # url -> [(snap_index, markdown)]

    def ingest(self, snapshot: Snapshot) -> None:
        idx = len(self.snapshots)
        self.snapshots.append((snapshot.taken_on, snapshot.snapshot_id))
        for p in snapshot.pages:
            self.pages[p.url].append((idx, p.markdown))     # append, never overwrite

    def snapshot_for(self, as_of=None):
        """Latest snapshot on or before as_of (None = newest)."""
        if not as_of:
            return len(self.snapshots) - 1
        best = None
        for i, (date, _) in enumerate(self.snapshots):
            if date <= as_of:
                best = i
        return best

    def page_at(self, url, snap_idx):
        """The page as it looked in that exact snapshot (None if it wasn't on the site)."""
        for i, md in self.pages[url]:
            if i == snap_idx:
                return md
        return None

    # still naive for now: we'll replace this in Step 5
    def answer(self, question, as_of=None):
        idx = self.snapshot_for(as_of)
        if idx is None:
            return Answer(text="I don't have information from that far back. Please contact us.",
                          confident=False)
        q = set(WORD.findall(question.lower()))
        best, best_score = None, 0
        for url in self.pages:
            md = self.page_at(url, idx)
            if not md:
                continue
            for para in md.split("\n\n"):
                s = len(q & set(WORD.findall(para.lower())))
                if s > best_score:
                    best, best_score = (url, para.strip()), s
        if not best:
            return Answer(text="I don't know.", confident=False)
        url, para = best
        return Answer(text=para[:600], sources=[Source(url=url, snapshot_id=self.snapshots[idx][1])])

    def conflicts(self):
        return []


def build():
    return TemporalKnowledge()