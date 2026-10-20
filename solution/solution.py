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

# ---------------- Cleaning, dates, chunking ----------------

MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
MDY = re.compile(r"\b(" + "|".join(MONTHS) + r")\.?\s+(\d{1,2}),?\s+(20\d\d)\b", re.I)
EFFECTIVE = re.compile(r"effective as of (\d{1,2})/(\d{1,2})/(\d{4})", re.I)


def clean_markdown(md: str) -> str:
    md = md.replace("\\\\r\\\\n", " ").replace("\\r\\n", " ")
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)                        # drop images
    md = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", md)  # keep link text + url
    md = re.sub(r"[ \t]+", " ", md)
    return re.sub(r"\n{3,}", "\n\n", md)


def page_date(md: str):
    """Publish date near the top of a news/blog post, e.g. 'June 07, 2022' -> '2022-06-07'."""
    m = MDY.search(md[:600])
    return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}" if m else None


def effective_dates(text: str):
    return sorted({f"{y}-{int(m):02d}-{int(d):02d}" for m, d, y in EFFECTIVE.findall(text)})


MAX_CHARS = 900


def chunk_page(md: str):
    """Split a page into [(heading, body)]. Tables stay whole with their heading."""
    blocks = [b.strip() for b in md.split("\n\n") if b.strip()]
    out, heading, buf, recent_short = [], "", [], []

    def flush():
        if buf:
            out.append((heading, "\n\n".join(buf)))
            buf.clear()

    for b in blocks:
        if b.startswith("#"):                              # a heading starts a new section
            flush()
            heading = b.lstrip("#").strip()[:120]
            recent_short = []
            continue
        if b.startswith("|"):                              # a table: keep it whole
            flush()
            label = " / ".join(recent_short[-2:])          # short lines above = table name
            rows = b.split("\n")
            head, body = rows[:2], rows[2:]
            for i in range(0, max(1, len(body)), 30):      # split only very long tables
                out.append((f"{heading} | {label}".strip(" |"), "\n".join(head + body[i:i + 30])))
            continue
        if len(b) < 70 and "\n" not in b and not b.endswith("."):
            recent_short.append(b)
        if sum(len(x) for x in buf) + len(b) > MAX_CHARS:
            flush()
        buf.append(b)
        # the footnote right under a table ("Rates are effective as of...") belongs to that table
        if out and out[-1][1].startswith("|") and len(buf) == 1 and \
                ("effective" in b.lower() or b[:1].isdigit()):
            h, t = out[-1]
            out[-1] = (h, t + "\n\n" + b[:400])
    flush()
    return out

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