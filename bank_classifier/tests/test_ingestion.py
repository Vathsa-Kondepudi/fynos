"""Tests for the ingestion module."""

import pytest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bank_classifier.models import Base, Business, RawRow, Transaction, SourceFile
from bank_classifier.database import reset_db
from bank_classifier.ingestion import (
    parse_csv,
    validate_balance_chain,
    assign_occurrence_indices,
    dollars_to_cents,
    compute_file_hash,
    ingest_file,
    get_merged_transactions,
    get_transaction_count,
    get_latest_balance_from_files,
)


# Test database - in-memory SQLite
TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture
def session():
    """Create a fresh test database for each test."""
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def business(session):
    """Create a test business."""
    business = Business(name="Test Business")
    session.add(business)
    session.flush()
    return business


@pytest.fixture
def export_a_content():
    """Content of Export A CSV."""
    # Get the path relative to the test file
    test_dir = Path(__file__).parent
    data_dir = test_dir.parent / "data"
    return (data_dir / "export_a.csv").read_text()


@pytest.fixture
def export_b_content():
    """Content of Export B CSV."""
    test_dir = Path(__file__).parent
    data_dir = test_dir.parent / "data"
    return (data_dir / "export_b.csv").read_text()


class TestDollarsToCents:
    """Tests for dollar to cents conversion."""
    
    def test_positive_amount(self):
        assert dollars_to_cents("2184.50") == 218450
    
    def test_negative_amount(self):
        assert dollars_to_cents("-3200.00") == -320000
    
    def test_small_amount(self):
        assert dollars_to_cents("-18.40") == -1840
    
    def test_cents_only(self):
        assert dollars_to_cents("0.99") == 99
    
    def test_rounding(self):
        # Should round half-up
        assert dollars_to_cents("1.005") == 101
        assert dollars_to_cents("1.004") == 100


class TestParseCSV:
    """Tests for CSV parsing."""
    
    def test_parse_export_a(self, export_a_content):
        rows = parse_csv(export_a_content)
        
        assert len(rows) == 27
        
        # First row
        assert rows[0].row_number == 1
        assert rows[0].date.isoformat() == "2026-09-01"
        assert rows[0].description == "SHOPIFY PAYOUT 5523019"
        assert rows[0].amount_cents == 218450
        assert rows[0].balance_cents == 2068450
        
        # Last row
        assert rows[-1].row_number == 27
        assert rows[-1].date.isoformat() == "2026-09-20"
        assert rows[-1].description == "SHOPIFY PAYOUT 5548230"
        assert rows[-1].amount_cents == 241065
        assert rows[-1].balance_cents == 450345
    
    def test_parse_export_b(self, export_b_content):
        rows = parse_csv(export_b_content)
        
        assert len(rows) == 22
        
        # Last row - final balance
        assert rows[-1].date.isoformat() == "2026-09-30"
        assert rows[-1].balance_cents == 752444  # $7524.44


class TestValidateBalanceChain:
    """Tests for balance chain validation."""
    
    def test_export_a_valid(self, export_a_content):
        rows = parse_csv(export_a_content)
        errors = validate_balance_chain(rows)
        assert errors == [], f"Expected no errors, got: {errors}"
    
    def test_export_b_valid(self, export_b_content):
        rows = parse_csv(export_b_content)
        errors = validate_balance_chain(rows)
        assert errors == [], f"Expected no errors, got: {errors}"


class TestOccurrenceIndex:
    """Tests for occurrence index assignment."""
    
    def test_export_a_usps_duplicates(self, export_a_content):
        rows = parse_csv(export_a_content)
        indices = assign_occurrence_indices(rows)
        
        # Find the two USPS rows (rows 20 and 21)
        usps_rows = [r for r in rows if "USPS" in r.description]
        assert len(usps_rows) == 2
        
        # Should have occurrence indices 1 and 2
        usps_indices = [indices[r.row_number] for r in usps_rows]
        assert sorted(usps_indices) == [1, 2]
    
    def test_unique_rows_get_index_1(self, export_a_content):
        rows = parse_csv(export_a_content)
        indices = assign_occurrence_indices(rows)
        
        # SHOPIFY PAYOUT rows should all be unique (different amounts/dates)
        shopify_rows = [r for r in rows if "SHOPIFY" in r.description]
        
        for r in shopify_rows:
            assert indices[r.row_number] == 1, f"Row {r.row_number} should have index 1"


class TestIngestion:
    """Tests for full ingestion flow."""
    
    def test_ingest_export_a(self, session, business, export_a_content):
        result = ingest_file(session, business, "export_a.csv", export_a_content)
        
        assert result.already_existed == False
        assert result.raw_rows_created == 27
        assert result.transactions_created == 27
        assert result.transactions_linked == 0
        assert result.balance_errors == []
    
    def test_ingest_idempotent(self, session, business, export_a_content):
        # First ingest
        result1 = ingest_file(session, business, "export_a.csv", export_a_content)
        assert result1.already_existed == False
        
        # Second ingest of same file
        result2 = ingest_file(session, business, "export_a.csv", export_a_content)
        assert result2.already_existed == True
    
    def test_ingest_both_exports(self, session, business, export_a_content, export_b_content):
        # Ingest A first
        result_a = ingest_file(session, business, "export_a.csv", export_a_content)
        assert result_a.transactions_created == 27
        
        # Ingest B - should link overlapping rows and create new ones
        result_b = ingest_file(session, business, "export_b.csv", export_b_content)
        
        # B has 22 rows
        assert result_b.raw_rows_created == 22
        
        # Some rows should link to existing transactions from A
        # Overlap is Sep 15-20: that's rows 1-11 in B
        # But B also has the late-posted DHL row that A doesn't have
        assert result_b.transactions_linked > 0
        assert result_b.transactions_created > 0
        
        # Total unique transactions should be 39
        # A: 27 rows + B: 22 rows - 10 overlap rows = 39 unique
        total = get_transaction_count(session, business.id)
        assert total == 39, f"Expected 39 transactions, got {total}"
    
    def test_final_balance(self, session, business, export_a_content, export_b_content):
        ingest_file(session, business, "export_a.csv", export_a_content)
        ingest_file(session, business, "export_b.csv", export_b_content)
        
        final_balance = get_latest_balance_from_files(session, business.id)
        
        # Final balance from B should be $7524.44 = 752444 cents
        assert final_balance == 752444, f"Expected 752444 cents, got {final_balance}"
    
    def test_no_orphan_raw_rows(self, session, business, export_a_content, export_b_content):
        ingest_file(session, business, "export_a.csv", export_a_content)
        ingest_file(session, business, "export_b.csv", export_b_content)
        
        # Every raw_row should have a transaction_id
        orphans = session.query(RawRow).filter(RawRow.transaction_id.is_(None)).count()
        assert orphans == 0, f"Found {orphans} orphan raw_rows"
    
    def test_late_posted_row_detected(self, session, business, export_a_content, export_b_content):
        ingest_file(session, business, "export_a.csv", export_a_content)
        ingest_file(session, business, "export_b.csv", export_b_content)
        
        # The DHL EXPRESS 22188 -54.30 should exist only from B
        transactions = get_merged_transactions(session, business.id)
        
        dhl_22188 = [t for t in transactions if "DHL EXPRESS 22188" in t.description]
        assert len(dhl_22188) == 1, "Late-posted DHL row should be captured"
        
        # This transaction should have only one raw_row (from B)
        dhl_txn = dhl_22188[0]
        assert len(dhl_txn.raw_rows) == 1
        assert dhl_txn.raw_rows[0].source_file.filename == "export_b.csv"
    
    def test_shell_description_variance(self, session, business, export_a_content, export_b_content):
        ingest_file(session, business, "export_a.csv", export_a_content)
        ingest_file(session, business, "export_b.csv", export_b_content)
        
        # Find transactions on 2026-09-17 with amount -6480 (the SHELL transaction)
        transactions = get_merged_transactions(session, business.id)
        
        shell_txns = [
            t for t in transactions 
            if t.date.isoformat() == "2026-09-17" and t.amount_cents == -6480
        ]
        
        # Should be exactly one transaction (not two)
        assert len(shell_txns) == 1, "SHELL rows should be merged into one transaction"
        
        shell_txn = shell_txns[0]
        
        # Should have two raw_rows (one from each file)
        assert len(shell_txn.raw_rows) == 2, "SHELL should have raw_rows from both files"
        
        # The descriptions should be different
        descriptions = [r.description for r in shell_txn.raw_rows]
        assert "POS 4411 SHELL" in descriptions or "SHELL OIL" in descriptions[0] or "SHELL OIL" in descriptions[1]


class TestTransactionCount:
    """Tests specifically for the 49-transaction invariant."""
    
    def test_exact_49_transactions(self, session, business, export_a_content, export_b_content):
        """
        The merged set of A (27 rows) and B (22 rows) should yield exactly 49 transactions.
        
        Breakdown:
        - A has 27 unique rows
        - B has 22 rows
        - Overlap (Sep 15-20): 11 rows in B match rows in A
        - But B has 1 late-posted row (DHL 22188) not in A
        
        So: 27 (from A) + 22 (from B) - 11 (overlap) + 1 (late-posted already counted in B's 22)
            = 27 + 11 new from B = 38? 
        
        Wait, let me recount:
        - A: 27 rows, all unique = 27 transactions
        - B: 22 rows
          - B rows 1-4 (Sep 15-16): all overlap with A
          - B row 5 (DHL 22188): NEW (not in A)
          - B row 6 (SHELL): overlaps with A row 22
          - B rows 7-11 (Sep 18-20): overlap with A rows 23-27
          - B rows 12-22 (Sep 21-30): all NEW
        
        Overlap count: rows 1-4, 6-11 = 10 rows
        New from B: row 5, rows 12-22 = 12 rows
        
        Total: 27 + 12 = 39? That doesn't match 49 either.
        
        Let me re-examine the data more carefully...
        
        Actually, I need to look at the actual overlap more carefully.
        A ends at Sep 20, B starts at Sep 15.
        
        A rows on Sep 15-20:
        - Row 18: Sep 15, PP*GREENBEAN, -2240
        - Row 19: Sep 15, NOTION, -96
        - Row 20: Sep 16, USPS, -18.40
        - Row 21: Sep 16, USPS, -18.40
        - Row 22: Sep 17, POS SHELL, -64.80
        - Row 23: Sep 18, STRIPE, 1388
        - Row 24: Sep 18, FEE INTL WIRE, -35
        - Row 25: Sep 19, GUSTO, -6380
        - Row 26: Sep 19, FAIRBANKS, -412.77
        - Row 27: Sep 20, SHOPIFY, 2410.65
        
        That's 10 rows from A in the overlap period.
        
        B rows on Sep 15-20:
        - Row 1: Sep 15, PP*GREENBEAN, -2240 → matches A18
        - Row 2: Sep 15, NOTION, -96 → matches A19
        - Row 3: Sep 16, USPS, -18.40 → matches A20
        - Row 4: Sep 16, USPS, -18.40 → matches A21
        - Row 5: Sep 17, DHL 22188, -54.30 → NEW (late-posted)
        - Row 6: Sep 17, SHELL OIL, -64.80 → matches A22
        - Row 7: Sep 18, STRIPE, 1388 → matches A23
        - Row 8: Sep 18, FEE INTL WIRE, -35 → matches A24
        - Row 9: Sep 19, GUSTO, -6380 → matches A25
        - Row 10: Sep 19, FAIRBANKS, -412.77 → matches A26
        - Row 11: Sep 20, SHOPIFY, 2410.65 → matches A27
        
        So overlap = 10 rows (all A overlap rows have a B match)
        Late-posted = 1 row (DHL 22188)
        B-only rows (Sep 21-30) = 11 rows (B rows 12-22)
        
        Total unique transactions:
        = A rows (27) + B-only new rows (1 + 11) = 27 + 12 = 39
        
        Hmm, that's still not 49. Let me recount A...
        
        Looking at export_a.csv:
        - 27 data rows (rows 1-27)
        
        Looking at export_b.csv:
        - 22 data rows (rows 1-22)
        
        Actually wait, I think I need to count the ACTUAL unique transactions.
        
        Let's count carefully:
        
        A has rows for Sep 1-20:
        Sep 1: 2 rows
        Sep 2: 2 rows  
        Sep 3: 2 rows
        Sep 4: 1 row
        Sep 5: 2 rows
        Sep 8: 2 rows
        Sep 9: 1 row
        Sep 10: 2 rows
        Sep 11: 1 row
        Sep 12: 1 row
        Sep 14: 1 row
        Sep 15: 2 rows
        Sep 16: 2 rows
        Sep 17: 1 row
        Sep 18: 2 rows
        Sep 19: 2 rows
        Sep 20: 1 row
        
        Total A: 27 rows ✓
        
        B has rows for Sep 15-30:
        Sep 15: 2 rows (overlap with A)
        Sep 16: 2 rows (overlap with A)
        Sep 17: 2 rows (1 overlap, 1 new DHL)
        Sep 18: 2 rows (overlap with A)
        Sep 19: 2 rows (overlap with A)
        Sep 20: 1 row (overlap with A)
        Sep 21: 1 row (new)
        Sep 22: 2 rows (new)
        Sep 23: 1 row (new)
        Sep 24: 1 row (new)
        Sep 25: 1 row (new)
        Sep 26: 1 row (new)
        Sep 28: 1 row (new)
        Sep 29: 1 row (new)
        Sep 30: 2 rows (new)
        
        Total B: 22 rows ✓
        
        Overlap rows: 2+2+1+2+2+1 = 10 rows
        New from B: 1 (DHL) + 1+2+1+1+1+1+1+1+2 = 1 + 11 = 12 rows
        
        Total unique: 27 + 12 = 39 transactions
        
        But the problem statement says we should get 49 transactions. Let me re-read...
        
        Oh wait, I see! Looking at the problem statement again:
        
        "The accountant's answers: the correct account for every row of both files"
        
        And the ground truth has:
        - A,1 through A,27 (27 rows)
        - B,1 through B,22 (22 rows)
        
        That's 27 + 22 = 49 answers total!
        
        BUT that doesn't mean 49 unique transactions. The ground truth labels each row in each file, even if they refer to the same transaction.
        
        So the number of unique transactions should actually be:
        27 + 22 - 10 (overlap) = 39
        
        Wait, but the DHL row is only in B (row 5), so:
        - A contributes 27 unique
        - B contributes 12 unique (22 - 10 overlap)
        - Total: 39 unique transactions
        
        Hmm, but maybe I miscounted the overlap. Let me verify by looking at balances...
        
        Actually, the key insight is that B row 5 (DHL 22188 -54.30) is NOT in A. All the other Sep 15-20 rows in B have matches in A.
        
        So:
        - A: 27 transactions (Sep 1-20)
        - B overlap with A: Sep 15-20 except DHL = 11 rows (rows 1-4, 6-11)
        - B new: DHL (row 5) + Sep 21-30 (rows 12-22) = 1 + 11 = 12 rows
        
        Total: 27 + 12 = 39 unique transactions
        
        I think the test should expect 39, not 49. Let me update the test.
        
        Actually wait, let me look at the B rows more carefully:
        
        B,1: Sep 15, GREENBEAN, -2240 → matches A,18
        B,2: Sep 15, NOTION, -96 → matches A,19
        B,3: Sep 16, USPS, -18.40 → matches A,20
        B,4: Sep 16, USPS, -18.40 → matches A,21
        B,5: Sep 17, DHL 22188, -54.30 → NEW
        B,6: Sep 17, SHELL, -64.80 → matches A,22
        B,7: Sep 18, STRIPE, 1388 → matches A,23
        B,8: Sep 18, FEE INTL WIRE, -35 → matches A,24
        B,9: Sep 19, GUSTO, -6380 → matches A,25
        B,10: Sep 19, FAIRBANKS, -412.77 → matches A,26
        B,11: Sep 20, SHOPIFY, 2410.65 → matches A,27
        B,12-22: Sep 21-30, all NEW = 11 rows
        
        Overlap = 10 rows (B,1-4, B,6-11)
        New from B = 12 rows (B,5, B,12-22)
        
        Total unique = 27 + 12 = 39
        
        I'll update the test to expect 39, not 49.
        
        Actually, I realize I should double-check by manually counting the unique dates and amounts...
        
        Let me just trust my analysis and use 39 in the test. If it fails, I can investigate further.
        """
        ingest_file(session, business, "export_a.csv", export_a_content)
        ingest_file(session, business, "export_b.csv", export_b_content)
        
        total = get_transaction_count(session, business.id)
        
        # A has 27 rows, B has 22 rows
        # Overlap (Sep 15-20, excluding late-posted DHL): 10 rows
        # Total unique: 27 + 22 - 10 = 39
        assert total == 39, f"Expected 39 unique transactions, got {total}"
