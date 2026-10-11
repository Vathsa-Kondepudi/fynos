# Bank Statement Classifier with Memory

First-pass classifier for business bank statements. Ingests overlapping CSV exports without double counting, reconciles against the bank's balances, classifies each transaction with an LLM (Groq), says when it is unsure, and learns from the accountant's corrections.

## Setup

Requires Python 3.10+.

```
pip install -e ".[dev]"
cp .env.example .env        # then put your Groq key in .env
```

`.env` is git-ignored. `GROQ_MODEL` defaults to `openai/gpt-oss-20b`.

## Use

Every command takes `--business/-b` (default `Kestrel Coffee Roasters`), so one database serves many businesses.

```
python -m bank_classifier upload bank_classifier/data/export_a.csv
python -m bank_classifier upload bank_classifier/data/export_b.csv
python -m bank_classifier reconcile          # exit code 1 if totals disagree with the bank
python -m bank_classifier classify
python -m bank_classifier list               # -r shows only rows needing review
python -m bank_classifier explain 26         # full classification history for a transaction
python -m bank_classifier correct 26 rent -r "Fairbanks is our landlord" --create-rule
python -m bank_classifier rules
```

## Tests and evaluation

```
python -m pytest                              # ingestion and dedupe, no LLM calls
python -m bank_classifier.evaluate            # calls the LLM, writes evidence/eval_*.json
```

- [DESIGN.md](DESIGN.md): data model, invariants, failure modes, trade-offs
- [EVIDENCE.md](EVIDENCE.md): results and what they do and do not show
- [ASSISTANT_ERRORS.md](ASSISTANT_ERRORS.md): where the coding assistant was wrong
