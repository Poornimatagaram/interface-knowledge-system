# NOTES: a knowledge system that knows what's true *now*

## How it works

**1. Keep history, never overwrite.** `ingest()` stores every version of every page and records which
snapshots each chunk appeared in (a bitmask). A page missing from a snapshot wasn't on the site that day,
so it can't answer for that day. Identical text across snapshots is stored once.

**2. Chunk, then index.** Pages are cleaned (images dropped, link URLs kept) and split by heading. Tables
stay whole with their heading and their "Rates are effective as of ..." footnote, because a rate without its
tier row and date is meaningless. A pure-Python BM25 index covers the unique chunks (about 17k).
Ingest takes about 1-4 seconds for all five snapshots.

**3. Pick the right point in time.** If `as_of` is passed, or the question contains a date ("as of May
2026"), the system uses the **latest snapshot on or before that date**. A date before the first download
gets an honest "I don't have information from that far back." Without a date, "current" = newest snapshot.

**4. Decide who to trust.** Retrieval score = relevance x page trust, with the page type read from the URL:

| Page type | Trust | Why |
|---|---|---|
| `/rates`, `/business-rates` | highest | exists only to publish current rates |
| official product/info pages | high | maintained with the product |
| promo/landing pages, uploaded PDFs | medium-low | offers expire, files go stale |
| calculators | low | example rates are rarely updated (5.34% vs the real 5.39%) |
| news/blog | low for rates, high for history | frozen on the day they were written |

For rate questions, tables get a boost and calculators/blogs a penalty. For history questions (awards,
closures, "last year"), news is the right source, and newer posts beat older ones.

**5. Answer like a careful expert.** The top 12 chunks, each labelled with page type, publish date and
effective dates, go to Claude (`claude-haiku-5-5`) with explicit rules:
- authority beats crawl order;
- tiers are not conflicts (lead with the smallest loan-amount tier, and "savings" = Share Savings);
- use partial evidence (e.g. hours from a branch announcement), but **refuse when the specific branch or
  product asked about is never mentioned**;
- state only the numbers that answer the question, and refuse **with no numbers** when evidence is absent.

Claude returns JSON (answer, evidence IDs -> source URLs, confidence, conflict note). A rescue path
recovers the answer if the JSON is malformed.

**6. Conflicts feed the answers.** Facts settled by conflict resolution (e.g. the branch count, which
sits in press-release boilerplate that search rarely ranks) are passed to Claude as "verified facts", so
a contradiction resolved once is answered the same way every time.

**7. Conflicts.** Table rows become fact cards: (row labels + column type) -> value. Model-year ranges
are replaced by their rank in the table ("newest model-year tier"), because when the year rolls over the
site relabels 2023-26 -> 2024-27 and stale pages keep the old label *and* the old rate. Two kinds are
reported:
- **stale copy:** in the newest snapshot, different pages show different values for the same fact, and
  no single page shows both (so tier tables are never flagged). Winner: page trust, then effective date.
- **changed over time:** an official page's value changed between snapshots. Winner: the newest value.

Prose counts (number of branches) use the most recent dated statement. Result: 37 conflicts.

**8. Consistency and speed.** An answer cache keyed by (normalised question, snapshot) plus deterministic
retrieval order means asking twice gives the same answer. (The model no longer accepts a `temperature`
setting, so consistency doesn't depend on it.)

## Decisions I measured

| Experiment | Result | Decision |
|---|---|---|
| Starter -> history + trust + chunked search (no LLM) | 0.09 -> 0.33 | keep |
| + Claude answer writer | 0.33 -> 0.47 | keep |
| `max_tokens` 400 -> 1500 (replies were cut off mid-JSON) | 0.47 -> 0.66 | keep |
| Prompt: partial evidence, tier rules, recency counts | 0.66 -> 0.76, but refusals 1.00 -> 0.67 | keep, then fix |
| Narrow exception: refuse when a named branch/product is absent | refusals back to 1.00, 0.80 | keep |
| Thinking off (3x faster, p95 2.6s) | accuracy 0.82 -> 0.76, final 0.755 | **rejected** |
| Thinking with effort low | final 0.77 | **rejected** |
| Less evidence (k=8) for speed | evidence recall 40/43 -> 37/43 | **rejected** |

Accuracy multiplies the speed score, so I kept full reasoning and accepted about 3-6s answers.
Scores vary by about +/-0.02 between runs.

## My own evals

The public kit ships no conflict answer key, so I wrote one. `evals/my_eval.jsonl` has 9 hand-verified
questions (paraphrases, "as of" dates such as the 30-year mortgage at 6.125% on Jun 9 vs 7.13% on Sep 28,
and a date before the first snapshot). `evals/conflicts_gold.json` has 5 hand-checked contradictions with
the correct winner. Current result: all 5 found, 100% correct winners, final 0.91 on this set.

    python harness/run_eval.py --submission solution --eval solution/evals/my_eval.jsonl \
        --conflicts-gold solution/evals/conflicts_gold.json

## API keys

- `ANTHROPIC_API_KEY` (recommended): enables the Claude answer writer. This is a paid API: one Haiku call
  per question, about 4.5k input tokens, so a full public eval costs a few cents.
- Without a key, the system still runs: it returns the best-matching chunk (public score about 0.33).
- Optional: `KS_MODEL` overrides the model; `KS_THINKING=off` trades accuracy for speed;
  `KS_DISABLE_LLM=1` forces the no-key mode.

## Known limits / next steps

- Speed: p95 is about 5s, above the 2s target. Next, I'd try prompt caching for the fixed system prompt,
  or a fast no-thinking first pass that switches reasoning on only when sources disagree.
- Page trust is URL-based for this site; another credit union would need its URL map adjusted.
- Conflict detection covers tables and branch counts; rates stated only in prose aren't keyed yet.
