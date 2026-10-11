"""
Evaluation: does learning from corrections improve classification?

Condition 1 (no memory): ingest A and B, classify everything with an empty memory.
Condition 2 (memory):    ingest A, classify, apply the accountant's corrections for A
                          (rules for counterparty notes), then ingest B and classify
                          only the new transactions.

Both conditions are scored on the 12 transactions that appear only in B, because the
10 overlapping B rows inherit A's human labels in condition 2 and would inflate it.

Run: python -m bank_classifier.evaluate [--repeats N]
"""

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .classifier import (
    CONFIDENCE_THRESHOLD,
    PROMPT_VERSION,
    classify_all_transactions,
    correct_classification,
    get_llm,
)
from .ingestion import ingest_file_from_path, reconcile
from .memory import create_rule
from .models import Base, Business, RawRow, SourceFile

DATA = Path(__file__).parent / "data"
EVIDENCE = Path(__file__).parent.parent / "evidence"
FILES = {"A": DATA / "export_a.csv", "B": DATA / "export_b.csv"}

# Which accountant notes describe a counterparty (become a rule) versus a one-off.
# "Coffee bags and labels." (A9, Amazon) is deliberately not a rule: B13 is a
# personal Amazon order, so an AMZN rule would be wrong.
RULE_PATTERNS = {
    ("A", 2): "FAIRBANKS",
    ("A", 3): "GREENBEAN",
    ("A", 13): "J MORALES",
    ("A", 22): "SHELL",
}


def load_labels() -> dict:
    with open(DATA / "ground_truth.csv", newline="") as f:
        return {
            (r["file"], int(r["row"])): (r["account"], r["accountant_note"] or None)
            for r in csv.DictReader(f)
        }


def new_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def row_to_transaction(session) -> dict:
    out = {}
    for rr in session.query(RawRow).join(SourceFile).all():
        letter = "A" if rr.source_file.filename == FILES["A"].name else "B"
        out[(letter, rr.row_number)] = rr.transaction
    return out


def score(session, labels, keys) -> dict:
    mapping = row_to_transaction(session)
    rows = []
    for key in keys:
        txn = mapping[key]
        c = txn.current_classification
        expected = labels[key][0]
        rows.append({
            "file": key[0], "row": key[1], "description": txn.description,
            "expected": expected,
            "predicted": c.account if c else None,
            "source": c.source if c else None,
            "confidence": c.confidence if c else None,
            "reason": c.reason if c else None,
            "correct": bool(c and c.account == expected),
            "flagged": bool(not c or (c.confidence or 0) < CONFIDENCE_THRESHOLD),
        })
    n = len(rows)
    confident = [r for r in rows if not r["flagged"]]
    return {
        "n": n,
        "accuracy": sum(r["correct"] for r in rows) / n,
        "flagged_for_review": n - len(confident),
        "accuracy_when_not_flagged": (
            sum(r["correct"] for r in confident) / len(confident) if confident else None
        ),
        "wrong_and_not_flagged": sum(1 for r in confident if not r["correct"]),
        "rows": rows,
    }


def b_only_keys(session) -> list:
    mapping = row_to_transaction(session)
    a_txn_ids = {t.id for (f, _), t in mapping.items() if f == "A"}
    return sorted(k for k, t in mapping.items() if k[0] == "B" and t.id not in a_txn_ids)


def run_no_memory(labels) -> dict:
    s = new_session()
    biz = Business(name="eval"); s.add(biz); s.flush()
    for p in FILES.values():
        ingest_file_from_path(s, biz, p)
    classify_all_transactions(s, biz.id)
    s.expire_all()
    keys_a = sorted(k for k in labels if k[0] == "A")
    return {
        "reconcile": reconcile(s, biz.id),
        "A": score(s, labels, keys_a),
        "B_only": score(s, labels, b_only_keys(s)),
    }


def run_with_memory(labels) -> dict:
    s = new_session()
    biz = Business(name="eval"); s.add(biz); s.flush()
    ingest_file_from_path(s, biz, FILES["A"])
    classify_all_transactions(s, biz.id)
    s.expire_all()
    keys_a = sorted(k for k in labels if k[0] == "A")
    a_score = score(s, labels, keys_a)

    mapping = row_to_transaction(s)
    corrections, rules = 0, []
    for key in keys_a:
        account, note = labels[key]
        txn = mapping[key]
        current = txn.current_classification
        if current and current.account == account and not note:
            continue
        c = correct_classification(s, txn.id, account, note)
        corrections += 1
        if key in RULE_PATTERNS:
            create_rule(s, biz.id, RULE_PATTERNS[key], account, note, c.id)
            rules.append(RULE_PATTERNS[key])
    s.expire_all()

    ingest_file_from_path(s, biz, FILES["B"])
    classify_all_transactions(s, biz.id, only_unclassified=True)
    s.expire_all()
    return {
        "reconcile": reconcile(s, biz.id),
        "A_before_corrections": a_score,
        "corrections_applied": corrections,
        "rules_created": rules,
        "B_only": score(s, labels, b_only_keys(s)),
    }


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()

    labels = load_labels()
    runs = []
    for i in range(args.repeats):
        print(f"Repeat {i + 1}/{args.repeats}: no memory...")
        nm = run_no_memory(labels)
        print(f"Repeat {i + 1}/{args.repeats}: with memory...")
        wm = run_with_memory(labels)
        runs.append({"no_memory": nm, "with_memory": wm})
        print(
            f"  A (no memory) accuracy: {nm['A']['accuracy']:.0%}\n"
            f"  B-only, no memory:   accuracy {nm['B_only']['accuracy']:.0%}, "
            f"flagged {nm['B_only']['flagged_for_review']}, "
            f"wrong and not flagged {nm['B_only']['wrong_and_not_flagged']}\n"
            f"  B-only, with memory: accuracy {wm['B_only']['accuracy']:.0%}, "
            f"flagged {wm['B_only']['flagged_for_review']}, "
            f"wrong and not flagged {wm['B_only']['wrong_and_not_flagged']}\n"
            f"  Reconcile ok: {nm['reconcile']['ok']} "
            f"({nm['reconcile']['computed_closing_cents']} vs {nm['reconcile']['bank_closing_cents']} cents)"
        )

    EVIDENCE.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE / f"eval_{stamp}.json"
    out.write_text(json.dumps({
        "model": get_llm().model_name,
        "prompt_version": PROMPT_VERSION,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "runs": runs,
    }, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
