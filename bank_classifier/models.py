"""SQLAlchemy models for the bank statement classifier."""

from datetime import date as date_type, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Account(str, Enum):
    """Valid account categories for classification."""
    SALES_PAYOUT = "sales_payout"
    INVENTORY = "inventory"
    SHIPPING = "shipping"
    SOFTWARE = "software"
    ADVERTISING = "advertising"
    PAYROLL = "payroll"
    RENT = "rent"
    BANK_FEES = "bank_fees"
    TAX = "tax"
    OWNER = "owner"


class ClassificationSource(str, Enum):
    """Source of a classification decision."""
    RULE = "rule"
    LLM = "llm"
    HUMAN = "human"


class Base(DeclarativeBase):
    pass


class Business(Base):
    """A business/tenant in the system."""
    __tablename__ = "business"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    # Relationships
    source_files: Mapped[list["SourceFile"]] = relationship(back_populates="business")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="business")
    rules: Mapped[list["DurableRule"]] = relationship(back_populates="business")


class SourceFile(Base):
    """A bank statement file uploaded by the accountant."""
    __tablename__ = "source_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("business.id"), index=True)
    file_hash: Mapped[str] = mapped_column()  # SHA256 for idempotency
    filename: Mapped[str] = mapped_column()
    uploaded_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    # Relationships
    business: Mapped["Business"] = relationship(back_populates="source_files")
    raw_rows: Mapped[list["RawRow"]] = relationship(back_populates="source_file")

    __table_args__ = (
        UniqueConstraint("business_id", "file_hash", name="uq_business_file_hash"),
    )


class RawRow(Base):
    """A verbatim row from a bank statement CSV. Immutable audit trail."""
    __tablename__ = "raw_row"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_file.id"), index=True)
    row_number: Mapped[int] = mapped_column()  # 1-indexed row in original file

    # Original fields from CSV (immutable)
    date: Mapped[date_type] = mapped_column()
    description: Mapped[str] = mapped_column()
    amount_cents: Mapped[int] = mapped_column()  # Stored as cents to avoid float errors
    balance_cents: Mapped[int] = mapped_column()

    # FK to transaction, set during merge/dedupe (nullable until then)
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transaction.id"), nullable=True, index=True
    )

    # Relationships
    source_file: Mapped["SourceFile"] = relationship(back_populates="raw_rows")
    transaction: Mapped[Optional["Transaction"]] = relationship(back_populates="raw_rows")

    __table_args__ = (
        Index("ix_raw_row_date_amount", "date", "amount_cents"),
    )


class Transaction(Base):
    """A canonical, deduplicated bank event. One per real money movement."""
    __tablename__ = "transaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("business.id"), index=True)

    # Matching key (date, amount_cents, occurrence_index)
    date: Mapped[date_type] = mapped_column()
    amount_cents: Mapped[int] = mapped_column()
    occurrence_index: Mapped[int] = mapped_column()  # nth occurrence of (date, amount) that day

    # Description from first file that mentioned this transaction
    description: Mapped[str] = mapped_column()

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    # Relationships
    business: Mapped["Business"] = relationship(back_populates="transactions")
    raw_rows: Mapped[list["RawRow"]] = relationship(back_populates="transaction")
    classifications: Mapped[list["Classification"]] = relationship(
        back_populates="transaction", order_by="Classification.created_at"
    )

    __table_args__ = (
        UniqueConstraint(
            "business_id", "date", "amount_cents", "occurrence_index",
            name="uq_transaction_identity"
        ),
        Index("ix_transaction_date", "date"),
    )

    @property
    def current_account(self) -> Optional["Account"]:
        """The most recent classification's account, or None if unclassified."""
        if self.classifications:
            return self.classifications[-1].account
        return None

    @property
    def current_classification(self) -> Optional["Classification"]:
        """The most recent classification, or None."""
        if self.classifications:
            return self.classifications[-1]
        return None


class Classification(Base):
    """
    An append-only record of a classification decision.
    Current account = latest row for a transaction.
    """
    __tablename__ = "classification"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transaction.id"), index=True)

    account: Mapped[str] = mapped_column()  # Account enum value
    source: Mapped[str] = mapped_column()  # ClassificationSource enum value
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)  # 0.0 to 1.0
    reason: Mapped[Optional[str]] = mapped_column(nullable=True)

    # LLM-specific fields (nullable for non-LLM sources)
    model_version: Mapped[Optional[str]] = mapped_column(nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(nullable=True)

    # If this classification came from a rule
    rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("durable_rule.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    # Relationships
    transaction: Mapped["Transaction"] = relationship(back_populates="classifications")
    rule: Mapped[Optional["DurableRule"]] = relationship(
        back_populates="classifications",
        foreign_keys=[rule_id]
    )

    __table_args__ = (
        CheckConstraint(
            "account IN ('sales_payout', 'inventory', 'shipping', 'software', "
            "'advertising', 'payroll', 'rent', 'bank_fees', 'tax', 'owner')",
            name="ck_valid_account"
        ),
        CheckConstraint(
            "source IN ('rule', 'llm', 'human')",
            name="ck_valid_source"
        ),
    )


class DurableRule(Base):
    """
    A business-scoped rule learned from corrections.
    Insert-only: never edit, use active=False to soft-delete.
    """
    __tablename__ = "durable_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("business.id"), index=True)

    counterparty_pattern: Mapped[str] = mapped_column()  # Pattern to match in description
    account: Mapped[str] = mapped_column()  # Account enum value
    note: Mapped[Optional[str]] = mapped_column(nullable=True)  # Accountant's explanation

    # Which correction created this rule (for audit trail)
    created_from_classification_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("classification.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    active: Mapped[bool] = mapped_column(default=True)  # Soft delete

    # Relationships
    business: Mapped["Business"] = relationship(back_populates="rules")
    classifications: Mapped[list["Classification"]] = relationship(
        back_populates="rule",
        foreign_keys="[Classification.rule_id]"
    )

    __table_args__ = (
        CheckConstraint(
            "account IN ('sales_payout', 'inventory', 'shipping', 'software', "
            "'advertising', 'payroll', 'rent', 'bank_fees', 'tax', 'owner')",
            name="ck_rule_valid_account"
        ),
        Index("ix_rule_business_active", "business_id", "active"),
    )
