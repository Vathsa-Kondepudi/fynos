# Design

## Data model

```
business        id, name
source_file     id, business_id, file_hash, filename, uploaded_at     UNIQUE(business_id, file_hash)
raw_row         id, source_file_id, row_number, date, description,
                amount_cents, balance_cents, transaction_id (FK)
transaction     id, business_id, date, amount_cents, occurrence_index,
                description                                            UNIQUE(business_id, date, amount_cents, occurrence_index)
classification  id, transaction_id, account, source (rule|llm|human),
                confidence, reason, model_version, prompt_version, rule_id, created_at
durable_rule    id, business_id, counterparty_pattern, account, note,
                created_from_classification_id, active, created_at
```

- `raw_row` is what each file said, verbatim and never edited. `transaction` is one real bank event. Many raw rows point to one transaction (`raw_row.transaction_id`), so there is no link table.
- Balances live only on `raw_row`. They legitimately differ between files for the same event (see the late-posted row below), so a single balance on `transaction` would be wrong.
- `transaction.description` is taken from the first file that showed the event. All other descriptions remain on `raw_row`.
- `classification` is append-only. The current account is the row with the highest `id`. Human corrections are just rows with `source = human`, so history is never overwritten and `explain` can show every decision.
- `durable_rule` is never edited, only deactivated, so old classifications that cite a `rule_id` still point at what the rule said at the time.
- Money is integer cents everywhere.

## How ingestion works

1. Hash the file. If this business already has that hash, stop (re-upload is a no-op).
2. Parse and validate. Each file's own balance chain must hold: `balance[i] = balance[i-1] + amount[i]`.
3. Write every row to `raw_row`.
4. Match each row to a transaction on `(business, date, amount_cents, occurrence_index)`, where `occurrence_index` is the nth row with that date and amount within the file. Link it if found, create a new transaction if not.
5. Assert no raw row is left without a transaction.

Why this key: date and amount are the only fields stable across exports. Description changes (`POS 4411 SHELL` vs `SHELL OIL 57442 HOUSTON TX`), and balance changes after a late-posted row. `occurrence_index` keeps genuine same-day duplicates apart (two `USPS PO 0441 -18.40` on 9/16).

## Invariants and how they are guaranteed

| Invariant | Guarantee |
|---|---|
| Same file twice does nothing | `UNIQUE(business_id, file_hash)` plus check before insert |
| One transaction per real event | `UNIQUE(business_id, date, amount_cents, occurrence_index)` |
| Every raw row belongs to exactly one transaction | FK column, asserted after merge, tested |
| Each file is internally consistent | Balance chain checked on parse, errors reported on upload |
| Totals agree with the bank | `reconcile`: opening balance + sum of all transactions = latest closing balance (18500.00 + ... = 7524.44); exits non-zero otherwise |
| Discrepancies are shown, not hidden | `reconcile` lists overlapping rows whose balances differ across files and rows seen in only one file inside the overlap window |
| Only valid accounts | `CHECK` constraint on `classification.account` and `durable_rule.account`, plus a code check on LLM output |
| No cross-tenant data | Every query filters by `business_id`; rules and corrections are per business |
| A correction stays corrected | Re-classification only touches unclassified transactions; a human row is always the latest |

## Classification and memory

Order: active rule match (exact substring of the counterparty), then LLM.

The LLM prompt includes the business's active rules and its 30 most recent human corrections with the accountant's notes. The model returns account, confidence, and reason as structured output. Confidence under 0.7 is shown as needing review. If the call fails or returns an unknown account, nothing is written and the transaction stays unclassified. It is never filled in with a default.

There are two kinds of memory:
- **Rules** come from a correction whose note describes the counterparty ("Fairbanks is our landlord"). They short-circuit the LLM.
- **One-off corrections** ("this Amazon order was personal") stay as examples in the prompt only. Turning them into a rule would mislabel every Amazon order.

The accountant decides which kind a correction is (`correct --create-rule`). The system does not guess that.

## What can go wrong in real use

| Problem | What happens |
|---|---|
| Overlapping exports | Matched by key, no double count (tested: 27 + 22 rows give 39 transactions) |
| Row posted late into one export | Becomes its own transaction; `reconcile` shows the 54.30 balance gap on six overlap rows and names the row only B had |
| Two different merchants, same date and amount, in different order across files | **Not handled.** They match by position and the stored description can be the wrong one. Amounts and totals are still right. The fix is description similarity as a tie-breaker inside such groups. |
| A row deleted or reversed by the bank between exports | Old transaction stays and `reconcile` fails. That is the right outcome, since it needs a person. |
| Edited file re-uploaded | Different hash, so it is treated as a new file and merged; changed rows show up in `reconcile` |
| LLM down, rate-limited, or invalid output | Transaction left unclassified and the error printed; re-run `classify` later |
| Model retired | Happened during development; the model is set by `GROQ_MODEL` |
| Rule too broad | Deactivate it (`delete-rule`); history keeps it |
| Wrong correction | Correct again; the newest human row wins and both stay in history |

## Trade-offs

- **SQLite and a CLI**, no API server. The brief needs it drivable from a script; a CLI is the smallest thing that does that.
- **Substring rules, no embeddings.** They are predictable and explainable; similarity search is not needed at this size.
- **One LLM call per transaction.** Simple and easy to explain per row, but slow (about 5 s each with gpt-oss-20b). Batching would be the first optimisation.
- **First-file description wins.** Simple, and the other descriptions are kept on `raw_row`.
- **The confidence score is the model's own number.** It is not calibrated, so the threshold is a guess. The evidence shows how often it is wrong while still claiming high confidence.
