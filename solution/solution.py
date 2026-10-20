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

# ---------------- Search helpers ----------------

STOP = set("""a an the of to in on for and or is are was were be been do does did i you your we our
my me it its this that what which who whom how can could would should will with at by from as any
there their them please tell about get give know want like have has had dupaco dupaco's s
current currently now today right still""".split())
SYN = {"cd": "certificate", "cds": "certificate", "certificates": "certificate", "mortgages": "mortgage",
       "locator": "location", "locations": "location", "hours": "hour", "branches": "branch",
       "cards": "card", "rates": "rate", "loans": "loan", "car": "auto", "vehicle": "auto",
       "savings": "saving"}
TOK = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def stem(w):
    w = SYN.get(w, w)
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("es") and not w.endswith("ses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text):
    return [stem(t) for t in TOK.findall(text.lower()) if t not in STOP]


def url_words(url):
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"^https?://[^/]+", "", url.lower().split("?")[0]))


def question_date(q):
    """'as of May 2026' -> '2026-05-31'; 'June 9, 2026' -> '2026-06-09'; ISO dates too."""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m:
        return m.group(0)
    m = MDY.search(q)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(" + "|".join(MONTHS) + r")\.?\s+(?:of\s+)?(20\d\d)\b", q, re.I)
    if m:
        mon, yr = MONTHS[m.group(1).lower()], int(m.group(2))
        last = [31, 29 if yr % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mon - 1]
        return f"{yr}-{mon:02d}-{last:02d}"
    return None


def normalize_q(q):
    return " ".join(sorted(set(tokens(q))))

import hashlib
import math


class Chunk:
    __slots__ = ("cid", "url", "title", "heading", "text", "snaps", "kind", "is_table", "pdate", "eff")

    def __init__(self, cid, url, title, heading, text, kind, pdate, eff):
        self.cid, self.url, self.title, self.heading, self.text = cid, url, title, heading, text
        self.snaps = 0                      # bitmask: which snapshots this chunk appeared in
        self.kind, self.is_table = kind, text.startswith("|")
        self.pdate, self.eff = pdate, eff   # publish date (news), effective dates (rate tables)


class TemporalKnowledge:
    def __init__(self):
        self.snapshots = []                  # [(taken_on, snapshot_id)], oldest first
        self.chunks = []
        self.chunk_by_hash = {}
        self.page_chunks = {}                # (url, page_hash) -> [chunk ids]
        self.postings = defaultdict(list)    # word -> [(chunk id, count)]
        self.doc_len = []
        self.cache = {}

    # ---------------- ingest ----------------
    def ingest(self, snapshot: Snapshot) -> None:
        idx = len(self.snapshots)
        self.snapshots.append((snapshot.taken_on, snapshot.snapshot_id))
        bit = 1 << idx
        for p in snapshot.pages:
            key = (p.url, hashlib.md5(p.markdown.encode()).hexdigest())
            if key not in self.page_chunks:          # a page version we haven't seen: chunk it
                md = clean_markdown(p.markdown)
                kind = page_kind(p.url)
                pdate = page_date(md) if kind == "news_blog" else None
                ids = []
                for heading, body in chunk_page(md):
                    text = body.strip()
                    if len(text) < 25:
                        continue
                    h = hashlib.md5((p.url + "\x00" + text).encode()).hexdigest()
                    cid = self.chunk_by_hash.get(h)
                    if cid is None:                  # same text seen before? store it once
                        cid = len(self.chunks)
                        self.chunk_by_hash[h] = cid
                        self.chunks.append(Chunk(cid, p.url, p.title, heading, text, kind, pdate,
                                                 effective_dates(text)))
                        self._index(cid, f"{p.title} {heading} {text} {url_words(p.url)}")
                    ids.append(cid)
                self.page_chunks[key] = ids
            for cid in self.page_chunks[key]:
                self.chunks[cid].snaps |= bit        # mark: present in this snapshot
        self.cache.clear()

    def _index(self, cid, text):
        tf = defaultdict(int)
        toks = tokens(text)
        for t in toks:
            tf[t] += 1
        for t, n in tf.items():
            self.postings[t].append((cid, n))
        self.doc_len.append(len(toks))

    # ---------------- time travel ----------------
    def snapshot_for(self, as_of=None):
        if not as_of:
            return len(self.snapshots) - 1
        best = None
        for i, (date, _) in enumerate(self.snapshots):
            if date <= as_of:
                best = i
        return best

    # ---------------- search: relevance x trust ----------------
    def search(self, question, snap_idx, k=12):
        bit = 1 << snap_idx
        N = len(self.chunks)
        avg = sum(self.doc_len) / N if N else 1.0
        scores = defaultdict(float)
        for t in dict.fromkeys(tokens(question)):
            plist = self.postings.get(t)
            if not plist:
                continue
            idf = math.log(1 + (N - len(plist) + 0.5) / (len(plist) + 0.5))
            for cid, tf in plist:
                if not self.chunks[cid].snaps & bit:     # not on the site that day
                    continue
                dl = self.doc_len[cid]
                scores[cid] += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * dl / avg))

        ql = question.lower()
        rate_q = bool(re.search(r"\b(rate|apr|apy|dividend|interest|yield|%)", ql))
        history_q = bool(re.search(r"\b(last year|history|when did|founded|award|recogni|forbes|press|"
                                   r"announce|close|clos|open|employees|volunteer|year)\b", ql))
        snap_year = int(self.snapshots[snap_idx][0][:4])
        ranked = []
        for cid, s in scores.items():
            c = self.chunks[cid]
            trust = TRUST[c.kind]
            if c.kind == "news_blog" and history_q:
                trust = 0.9                              # news is the right source for history
            if c.pdate:                                  # newer posts beat older ones
                trust *= max(0.55, 1 - 0.07 * max(0, snap_year - int(c.pdate[:4])))
            if rate_q and c.is_table:
                s *= 1.25
            if rate_q and c.kind in ("news_blog", "calculator"):
                trust *= 0.6
            ranked.append((s * (0.4 + 0.6 * trust), c))
        ranked.sort(key=lambda x: (-x[0], x[1].cid))     # cid tie-break = same order every time

        out, per_url, seen = [], defaultdict(int), set()
        for s, c in ranked:                              # max 3 chunks per page, no duplicates
            if per_url[c.url] >= 3 or c.text[:200] in seen:
                continue
            per_url[c.url] += 1
            seen.add(c.text[:200])
            out.append((s, c))
            if len(out) >= k:
                break
        return out

    # ---------------- answer: placeholder until Step 6 ----------------
    def answer(self, question, as_of=None):
        date = as_of or question_date(question)
        if date and date >= self.snapshots[-1][0]:
            date = None                                  # a future/today date = newest snapshot
        idx = self.snapshot_for(date)
        if idx is None:
            return Answer(text="I don't have information from that far back (our records start with "
                               "the first website download). Please contact us.", confident=False)
        hits = self.search(question, idx)
        if not hits:
            return Answer(text="I don't know - that isn't listed on our website; please contact us.",
                          confident=False)
        c = hits[0][1]
        return Answer(text=c.text[:400], sources=[Source(url=c.url, snapshot_id=self.snapshots[idx][1])])

    def conflicts(self):
        return []

def build():
    return TemporalKnowledge()