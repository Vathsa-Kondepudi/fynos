# Evidence

Two claims: the ledger is complete and not double-counted, and corrections make later classification better. Raw machine output lives in `evidence/`.

## 1. The ledger is correct

**What we ran.** `python -m pytest` (no LLM). After ingesting both exports, `python -m bank_classifier reconcile`.

**What we measured.**

| Check | Result |
|---|---|
| Unique transactions | 39 (27 in A + 22 in B − 10 overlapping rows) |
| Every raw row has a transaction | 0 orphans |
| Same file uploaded twice | No new rows |
| Two `USPS PO 0441 -18.40` on 9/16 | Two transactions (`occurrence_index` 1 and 2) |
| `POS 4411 SHELL` (A) and `SHELL OIL 57442 HOUSTON TX` (B) | One transaction, two raw rows |
| `DHL EXPRESS 22188 -54.30` | Present only from B |
| Opening + all amounts = bank closing | 18500.00 + sum = 7524.44 |
| Overlap rows whose balances differ | Six rows, each off by 54.30, explained by the late-posted DHL |

The 49 figure in the brief is the number of labelled *rows* (27 + 22), not unique events.

**Why this is believable.** The numbers are arithmetic on the committed CSVs. The tests fail if a row is dropped, duplicated, or if the SHELL descriptions are treated as two events.

**What it does not show.** It does not prove the matcher works if two different merchants share a date and amount and appear in swapped order. That case is not in the data.

## 2. Learning from corrections

**Protocol.** `python -m bank_classifier.evaluate`

- Condition 1 (empty memory): ingest A and B, classify everything, score A and the 12 B-only transactions.
- Condition 2 (memory): ingest A, classify, apply the accountant's A labels. Notes that name a counterparty become rules (`FAIRBANKS`, `GREENBEAN`, `J MORALES`, `SHELL`). The Amazon "coffee bags" note is *not* made into a rule, because B13 is a personal Amazon order. Then ingest B and classify only new transactions.

We score only the 12 B-only rows in the comparison. Scoring all of B after A's corrections would count the 10 overlapping rows that already have human labels.

**Raw output.**

- `evidence/reconcile.json` — ledger check after ingesting A and B
- `evidence/eval_*.json` — every prediction, source, confidence, and reason from `python -m bank_classifier.evaluate` (run this after setting `GROQ_API_KEY`; it is not committed until that run finishes)

**Why we should believe the numbers.** Same ground-truth file, same model, same prompt version, same 12 rows. The only change between conditions is memory.

**What they do not tell us.**

- 12 rows is tiny. One flip moves accuracy by 8 points.
- The model's output varies between runs. One eval is a snapshot, not a mean.
- The B score can still be inflated if the A notes in the prompt describe the same counterparties that appear on B (Fairbanks, Greenbean, Morales). That is the intended effect of memory, but it is not the same as generalising to unseen merchants.
- Confidence is the model's own number and is not calibrated. High-confidence wrong answers are more useful to look at than the headline accuracy.
- The Amazon personal order (B13) is the test that a merchant-level rule would fail. If condition 2 still gets it right, that is because we refused to turn A9 into an AMZN rule.
