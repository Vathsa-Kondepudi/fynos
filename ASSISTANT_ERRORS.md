# Where the coding assistant was wrong

| What the assistant did | How it was caught | What changed |
|---|---|---|
| Architecture diagram wrote `raw_row` *after* merge/dedupe | Questioned why raw data is saved after processing | Raw rows are written first; merge reads them |
| Proposed a `raw_row_transaction` link table | Asked why not a plain FK; the relationship is many raw rows to one transaction | Link table removed; `raw_row.transaction_id` FK |
| Stored `balance` and a single `description` on `transaction` | Balances differ across files after the late-posted row, so the invariant would fail on valid data | Balance only on `raw_row`; reconciliation done separately |
| Separate `override` table plus `classification(source=human)` | Two places for human decisions | Single append-only `classification` table |
| Mutable rules referenced by old classifications | Old decisions would point at a rule that had changed | Rules insert-only with `active` flag and `created_from_classification_id` |
| Test asserted 49 unique transactions (copied from the plan) | Test failed with 39; counted by hand: 27 + 22 − 10 overlap = 39. The 49 is the number of labelled rows. | Test expects 39 |
| On LLM failure, classifier silently recorded `sales_payout` with confidence 0 | First real run labelled all 39 transactions `sales_payout`; `explain` showed the errors | Failure leaves the row unclassified; nothing is guessed |
| Tried three Groq model names from memory, all retired or not on our plan | Each run failed with 400/404 | Checked the Groq models page; model now set by `GROQ_MODEL` |
| Wrote the API key to `bank_classifier/.env` with no `.gitignore` | Review before the first commit | `.env` git-ignored at the repo root, `.env.example` committed, key to be rotated |
| SQLAlchemy relationships ambiguous (two FKs between classification and rule); `date` column shadowed the `date` type | Import errors in tests | Explicit `foreign_keys`; type imported as `date_type` |
| "Current classification" ordered by `created_at` | A correction made in the same second could tie | Ordered by `id` |
| `export` called `.value` on a string | Code read-through | Fixed |
| Added LangGraph, FastAPI and a duplicate `requirements.txt` | Nothing used them | Removed; LangChain alone does the one chain we need |
| `extract_counterparty` turns `AMZN MKTP US*2K91` into `2K91` | Read-through while designing rules | Rules use an explicit pattern given by the accountant; automatic extraction is only used by the CLI `--create-rule` and should be checked |
