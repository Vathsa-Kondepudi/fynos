"""CSV ingestion, merge/dedupe logic, and balance chain validation."""

import csv
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from pathlib import Path
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .models import Business, RawRow, SourceFile, Transaction


@dataclass
class ParsedRow:
    """A parsed row from a bank statement CSV."""
    row_number: int  # 1-indexed
    date: date
    description: str
    amount_cents: int
    balance_cents: int


@dataclass
class IngestionResult:
    """Result of ingesting a bank statement file."""
    source_file_id: int
    raw_rows_created: int
    transactions_created: int
    transactions_linked: int  # Existing transactions linked to new raw_rows
    balance_errors: list[str]
    already_existed: bool = False


def dollars_to_cents(amount_str: str) -> int:
    """
    Convert a dollar amount string to integer cents.
    Uses Decimal for precision, rounds half-up.
    """
    d = Decimal(amount_str)
    cents = d * 100
    return int(cents.to_integral_value(rounding=ROUND_HALF_UP))


def compute_file_hash(content: str) -> str:
    """Compute SHA256 hash of file content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_csv(content: str) -> list[ParsedRow]:
    """
    Parse a bank statement CSV into ParsedRow objects.
    Expected columns: date, description, amount, balance
    """
    rows = []
    reader = csv.DictReader(StringIO(content))
    
    for row_num, row in enumerate(reader, start=1):
        parsed = ParsedRow(
            row_number=row_num,
            date=date.fromisoformat(row["date"]),
            description=row["description"].strip(),
            amount_cents=dollars_to_cents(row["amount"]),
            balance_cents=dollars_to_cents(row["balance"]),
        )
        rows.append(parsed)
    
    return rows


def validate_balance_chain(rows: list[ParsedRow]) -> list[str]:
    """
    Validate that the balance chain is internally consistent.
    Returns a list of error messages (empty if valid).
    
    For each row i > 0: balance[i] should equal balance[i-1] + amount[i]
    """
    errors = []
    
    for i in range(1, len(rows)):
        expected = rows[i - 1].balance_cents + rows[i].amount_cents
        actual = rows[i].balance_cents
        
        if expected != actual:
            diff_cents = actual - expected
            errors.append(
                f"Row {rows[i].row_number}: expected balance {expected} cents, "
                f"got {actual} cents (diff: {diff_cents} cents)"
            )
    
    return errors


def assign_occurrence_indices(rows: list[ParsedRow]) -> dict[int, int]:
    """
    Assign occurrence_index to each row based on (date, amount_cents).
    Returns a dict mapping row_number -> occurrence_index.
    
    Within each (date, amount) group, occurrence_index is assigned
    in row_number order (1, 2, 3, ...).
    """
    # Group by (date, amount_cents)
    groups: dict[tuple[date, int], list[int]] = {}
    for row in rows:
        key = (row.date, row.amount_cents)
        if key not in groups:
            groups[key] = []
        groups[key].append(row.row_number)
    
    # Assign occurrence_index within each group
    result = {}
    for key, row_numbers in groups.items():
        # Sort by row_number to maintain file order
        row_numbers.sort()
        for idx, row_num in enumerate(row_numbers, start=1):
            result[row_num] = idx
    
    return result


def find_matching_transaction(
    session: Session,
    business_id: int,
    txn_date: date,
    amount_cents: int,
    occurrence_index: int,
) -> Optional[Transaction]:
    """
    Find an existing transaction matching the given key.
    Returns None if no match found.
    """
    return session.query(Transaction).filter(
        and_(
            Transaction.business_id == business_id,
            Transaction.date == txn_date,
            Transaction.amount_cents == amount_cents,
            Transaction.occurrence_index == occurrence_index,
        )
    ).first()


def ingest_file(
    session: Session,
    business: Business,
    filename: str,
    content: str,
) -> IngestionResult:
    """
    Ingest a bank statement CSV file.
    
    1. Check file hash (idempotent - skip if already ingested)
    2. Parse CSV and write raw_rows immediately
    3. Run merge/dedupe to link raw_rows to transactions
    4. Validate balance chain
    
    Returns IngestionResult with statistics and any errors.
    """
    # Step 1: Check file hash
    file_hash = compute_file_hash(content)
    
    existing = session.query(SourceFile).filter(
        and_(
            SourceFile.business_id == business.id,
            SourceFile.file_hash == file_hash,
        )
    ).first()
    
    if existing:
        return IngestionResult(
            source_file_id=existing.id,
            raw_rows_created=0,
            transactions_created=0,
            transactions_linked=0,
            balance_errors=[],
            already_existed=True,
        )
    
    # Step 2: Parse CSV
    parsed_rows = parse_csv(content)
    
    # Validate balance chain per file
    balance_errors = validate_balance_chain(parsed_rows)
    
    # Create source_file record
    source_file = SourceFile(
        business_id=business.id,
        file_hash=file_hash,
        filename=filename,
    )
    session.add(source_file)
    session.flush()  # Get the ID
    
    # Step 3: Write raw_rows immediately (transaction_id=NULL for now)
    occurrence_indices = assign_occurrence_indices(parsed_rows)
    
    raw_rows = []
    for parsed in parsed_rows:
        raw_row = RawRow(
            source_file_id=source_file.id,
            row_number=parsed.row_number,
            date=parsed.date,
            description=parsed.description,
            amount_cents=parsed.amount_cents,
            balance_cents=parsed.balance_cents,
            transaction_id=None,  # Set during merge
        )
        session.add(raw_row)
        raw_rows.append((raw_row, parsed, occurrence_indices[parsed.row_number]))
    
    session.flush()  # Persist raw_rows to get IDs
    
    # Step 4: Merge/dedupe - link raw_rows to transactions
    transactions_created = 0
    transactions_linked = 0
    
    for raw_row, parsed, occurrence_index in raw_rows:
        # Try to find existing transaction
        existing_txn = find_matching_transaction(
            session,
            business.id,
            parsed.date,
            parsed.amount_cents,
            occurrence_index,
        )
        
        if existing_txn:
            # Link to existing transaction
            raw_row.transaction_id = existing_txn.id
            transactions_linked += 1
        else:
            # Create new transaction
            new_txn = Transaction(
                business_id=business.id,
                date=parsed.date,
                amount_cents=parsed.amount_cents,
                occurrence_index=occurrence_index,
                description=parsed.description,  # First file wins
            )
            session.add(new_txn)
            session.flush()  # Get the ID
            
            raw_row.transaction_id = new_txn.id
            transactions_created += 1
    
    # Assert no orphan raw_rows
    orphans = session.query(RawRow).filter(
        and_(
            RawRow.source_file_id == source_file.id,
            RawRow.transaction_id.is_(None),
        )
    ).count()
    
    if orphans > 0:
        raise RuntimeError(f"BUG: {orphans} raw_rows without transaction_id after merge")
    
    return IngestionResult(
        source_file_id=source_file.id,
        raw_rows_created=len(parsed_rows),
        transactions_created=transactions_created,
        transactions_linked=transactions_linked,
        balance_errors=balance_errors,
    )


def ingest_file_from_path(
    session: Session,
    business: Business,
    file_path: Path,
) -> IngestionResult:
    """Convenience function to ingest from a file path."""
    content = file_path.read_text(encoding="utf-8")
    return ingest_file(session, business, file_path.name, content)


def get_merged_transactions(
    session: Session,
    business_id: int,
) -> list[Transaction]:
    """
    Get all transactions for a business, ordered by date and occurrence_index.
    """
    return session.query(Transaction).filter(
        Transaction.business_id == business_id
    ).order_by(
        Transaction.date,
        Transaction.occurrence_index,
    ).all()


def compute_running_balance(transactions: list[Transaction], opening_balance_cents: int = 0) -> list[tuple[Transaction, int]]:
    """
    Compute the running balance for a list of transactions.
    Returns list of (transaction, running_balance_cents) tuples.
    """
    result = []
    running = opening_balance_cents
    
    for txn in transactions:
        running += txn.amount_cents
        result.append((txn, running))
    
    return result


def get_latest_balance_from_files(session: Session, business_id: int) -> Optional[int]:
    """
    Get the latest balance from the most recent file uploaded.
    Returns the balance_cents of the last row in the most recent file.
    """
    # Get the most recent source file
    latest_file = session.query(SourceFile).filter(
        SourceFile.business_id == business_id
    ).order_by(SourceFile.uploaded_at.desc()).first()
    
    if not latest_file:
        return None
    
    # Get the last row in that file
    last_row = session.query(RawRow).filter(
        RawRow.source_file_id == latest_file.id
    ).order_by(RawRow.row_number.desc()).first()
    
    if not last_row:
        return None
    
    return last_row.balance_cents


def get_transaction_count(session: Session, business_id: int) -> int:
    """Get the total number of transactions for a business."""
    return session.query(func.count(Transaction.id)).filter(
        Transaction.business_id == business_id
    ).scalar() or 0
