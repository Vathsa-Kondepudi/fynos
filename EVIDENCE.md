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

**One run, 11 Oct 2026, `openai/gpt-oss-20b`, prompt v2.** Raw files: `evidence/eval_20261011T130409Z.json`, `evidence/eval_console.txt`.

| Slice | Empty memory | After A's corrections |
|---|---|---|
| A (27 rows) | 93% (25/27). Only miss: Fairbanks → `owner` instead of `rent` | — (then 6 corrections, 4 rules) |
| B-only (12 rows) | 92% (11/12), 2 flagged, 1 wrong and not flagged | 83% (10/12), 1 flagged, 1 wrong and not flagged |

What memory actually changed on B:

- `J MORALES` and `GREENBEAN` were classified by **rule**, not the LLM. Morales went from a flagged 0.6 guess to a certain payroll match.
- B13 `AMZN MKTP US*7Q22` (personal order → owner) stayed **inventory** in both conditions. With memory the model cited A9 ("Amazon purchases are inventory") and raised confidence from 0.8 to 0.9. Not making an AMZN *rule* was not enough; the correction still leaked through the prompt.
- B12 `KLAVIYO` was left unclassified in the memory run because Groq returned **429 rate limit**. That one miss is why 83% looks worse than 92%. It is not a memory failure.

**Why we should believe the numbers.** Same CSVs, same labels, same model and prompt version, same 12 B-only rows. The JSON has every prediction and reason. Reconcile stayed true in both conditions (752444 cents).

**What they do not tell us.**

- 12 rows is tiny. One rate-limit miss moved accuracy by 8 points.
- This is one run, not a mean. The model varies.
- Headline accuracy hid the useful result: rules fired, and the Amazon one-off is still hard. A merchant-level AMZN rule would have made B13 and B19 the same account; we avoided the rule and still lost B13 because the prompt example was too close to a rule.
- Confidence is uncalibrated. B13 was wrong at 0.8–0.9 and not flagged.
