# A knowledge system that knows what's true *now*

My solution to interface.ai's take-home: answer credit-union members' questions from five website
snapshots (May–Sep 2026) that contradict each other, with the **current** value, the **right tier**,
a **source**, and an honest **"I don't know"** when the site doesn't say.

**Start here: [`solution/NOTES.md`](solution/NOTES.md)** covers how it decides what's current, plus
every experiment I ran and why I kept or rejected it.

## Results (public practice set, 40 questions)

| | Score |
|---|---|
| Starter baseline | 0.09 |
| **This solution** | **~0.80** (verified from a fresh clone, 0 errors) |
| My own hand-verified eval + conflict answer key | 0.91 (5/5 conflicts found, 100% correct winners) |

Perfect scores on tier disambiguation and refusals; runs vary by about ±0.02.

## How it works (one line each)

1. **History:** every page version is kept and tagged with the snapshots it appeared in.
2. **Time travel:** "as of" questions use the latest snapshot on or before that date.
3. **Trust:** official rate pages outrank calculators, promos and old press releases.
4. **Answers:** BM25 retrieval × page trust → Claude writes one short, grounded answer.
5. **Conflicts:** table rows become fact cards, catching stale copies and values that changed over
   time; resolved facts feed back into answers.

## Run it

The website snapshots are **not** included (as requested). Put the kit's `snapshots_zip/` folder in
the repo root, then:

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # optional; without it a retrieval-only fallback runs
python harness/run_eval.py --submission solution --eval eval/public_eval.jsonl
```

My own evals:

```
python harness/run_eval.py --submission solution --eval solution/evals/my_eval.jsonl \
    --conflicts-gold solution/evals/conflicts_gold.json
```

## Layout

- `solution/`: my code (`solution.py`), `NOTES.md`, and `evals/`
- `harness/`, `eval/`, `starter/`: unchanged from the kit