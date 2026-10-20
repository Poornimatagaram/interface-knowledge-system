"""
Knowledge system that knows what's true now (and what was true then).
Steps 2-6: page history, page trust, chunking, snapshot-aware search, Claude answer writer.
"""
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents
                            if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

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

# ---------------- Step 6: Claude writes the answer ----------------

SYSTEM_PROMPT = """You are Dupaco Community Credit Union's member assistant. You answer ONLY from the
evidence excerpts provided (pages from the credit union's own website, as downloaded on a given date).

Decide what is true like a careful human expert:
1. Authority: the OFFICIAL RATE SHEET and official product pages beat calculators, promo/landing pages,
   uploaded documents, blog posts and press releases. Calculators and old posts often show stale numbers.
2. Recency: prefer the most recent "effective as of" date; a dated news/blog post describes the past.
   For counts (branches, members, awards) use the most recent dated statement, and say how many times
   if the text says so (e.g. "for the fifth time"). If asked about "last year" and the newest figure on
   the site is from an earlier year, give that newest figure and name its year.
3. Tiers are not conflicts: term, balance, loan amount, model year, personal vs business, standard vs
   jumbo, intro vs standard APR, rate vs APY are DIFFERENT facts. Pick the tier the question asks for.
   If no tier is given:
   - loan-amount tiers: lead with the SMALLEST loan-amount tier (e.g. "$100,000 or less"), then the range;
   - balance tiers: name the tiers rather than picking one;
   - a generic word means the base product: "savings" = Share Savings, not money market/HSA/IRA.
   For "what are all X" / "what do you offer" questions, list every item with its value.
4. Use partial evidence: if the excerpts show the answer for some branches, products or examples
   (e.g. branch hours in a branch announcement), give it as the typical answer, say it can vary, and
   ask which branch they mean. Branch hours, phone numbers and addresses in news posts are usable.
      EXCEPTION: if the question names a SPECIFIC branch, street, product or loan type and the evidence
   never mentions that exact one, refuse; never substitute another branch's or product's details.
5. Honesty: refuse only when NOTHING in the evidence answers the question: the product, rate or branch
   is not mentioned at all, or only a DIFFERENT product's number exists. Then say
   "I don't know - that isn't listed on our website; please contact us." with NO numbers at all.
   Never guess or borrow a number from a different product.

Style: 1-3 short sentences. Include ONLY the numbers that answer the question. Never mention an old,
stale, or other-tier number, not even to say it is wrong. If asked for a link, give the product page
where members see that information (the page's own URL or its "See details" link, with #rates if shown),
copied exactly. Do not ask for more information before answering; answer first, then optionally ask
which branch/tier they mean.

Return ONLY a JSON object:
{"answer": "<member-facing text>", "evidence": [<ids of excerpts you used, best first>],
 "confident": true|false, "conflict_note": "<if sources disagreed, which you chose and why, else null>"}"""


class LLMWriter:
    @staticmethod
    def create():
        if os.getenv("KS_DISABLE_LLM") or not os.getenv("ANTHROPIC_API_KEY"):
            return None
        try:
            import anthropic
            return LLMWriter(anthropic.Anthropic(max_retries=2, timeout=25))
        except Exception:
            return None

    def __init__(self, client):
        self.client = client
        self.model = os.getenv("KS_MODEL", "claude-haiku-5-5")

    def answer(self, question, hits, snap_date, snap_id, historical):
        parts = []
        for i, (_, c) in enumerate(hits, 1):
            meta = [KIND_LABEL[c.kind]]
            if c.pdate:
                meta.append(f"published {c.pdate}")
            if c.eff:
                meta.append("rates effective " + ", ".join(c.eff))
            parts.append(f"[{i}] {c.url}  ({'; '.join(meta)})\nTitle: {c.title}\n"
                         f"Section: {c.heading}\n{c.text[:1800]}")
        when = (f"Answer as the website stood on {snap_date} (historical question)." if historical
                else f"Answer with what is current as of the newest download ({snap_date}).")
        user = f"{when}\n\nEVIDENCE:\n\n" + "\n\n---\n\n".join(parts) + f"\n\nQUESTION: {question}"
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=1500, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}])
        except Exception as e:
            print("LLM error:", e, file=sys.stderr)
            return None
        raw = "".join(getattr(b, "text", "") for b in resp.content).strip()
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except Exception:
            data = None
        if data is None:
            # rescue: pull the answer field out of broken JSON, or use the plain text
            print(f"LLM parse issue (stop_reason={resp.stop_reason}): {raw[:120]!r}", file=sys.stderr)
            a = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', raw)
            text = a.group(1) if a else re.sub(r"^```\w*|```$", "", raw).strip()
            if not text:
                return None
            ids = [int(x) for x in re.findall(r"\d+", (re.search(r'"evidence"\s*:\s*\[([^\]]*)', raw)
                                                       or re.search("()", "")).group(1))]
            refused = bool(re.search(r"don't know|isn't listed|not listed|contact us", text, re.I))
            data = {"answer": text, "evidence": ids, "confident": not refused}
        text = str(data.get("answer", "")).strip()
        if not text:
            return None
        ids = [int(x) for x in data.get("evidence", []) if str(x).isdigit() and 1 <= int(x) <= len(hits)]
        confident = bool(data.get("confident", True))
        sources = []
        if confident:
            # cite what Claude used, plus other trusted pages that state the same numbers
            nums = set(re.findall(r"\d+(?:\.\d+)?%?", text))
            extra = [i for i in range(1, len(hits) + 1) if i not in ids
                     and TRUST[hits[i - 1][1].kind] >= 0.9
                     and nums & set(re.findall(r"\d+(?:\.\d+)?%?", hits[i - 1][1].text))]
            seen = set()
            for i in list(dict.fromkeys(ids)) + extra:
                c = hits[i - 1][1]
                if c.url not in seen:
                    seen.add(c.url)
                    sources.append(Source(url=c.url, snapshot_id=snap_id, quote=c.text[:200]))
            sources = sources[:4]
        return Answer(text=text, sources=sources, confident=confident,
                      conflict_note=data.get("conflict_note") or None)

# ---------------- The knowledge system ----------------

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
        self.llm = LLMWriter.create()        # None when no API key -> fallback answers

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

    # ---------------- answer ----------------
    def answer(self, question, as_of=None):
        date = as_of or question_date(question)
        if date and date >= self.snapshots[-1][0]:
            date = None                                  # a future/today date = newest snapshot
        idx = self.snapshot_for(date)
        key = (normalize_q(question), idx)
        if key in self.cache:                            # same question -> same answer
            return self.cache[key]
        if idx is None:
            ans = Answer(text="I don't have information from that far back (our records start with "
                              "the first website download). Please contact us.", confident=False)
        else:
            hits = self.search(question, idx)
            snap_date, snap_id = self.snapshots[idx]
            ans = None
            if self.llm and hits:
                ans = self.llm.answer(question, hits, snap_date, snap_id, historical=date is not None)
            if ans is None:                              # no key / API error: simple fallback
                ans = (Answer(text=hits[0][1].text[:400],
                              sources=[Source(url=hits[0][1].url, snapshot_id=snap_id)])
                       if hits else Answer(text="I don't know - that isn't listed on our website; "
                                                "please contact us.", confident=False))
        self.cache[key] = ans
        return ans

    # ---------------- conflicts: Step 8 ----------------
    def conflicts(self):
        return []


def build():
    return TemporalKnowledge()