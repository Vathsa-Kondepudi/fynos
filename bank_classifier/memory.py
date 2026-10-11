"""Memory module for managing durable rules and retrieving context."""

import re
from typing import Optional

from sqlalchemy.orm import Session

from .models import Classification, DurableRule, Transaction


def extract_counterparty(description: str) -> str:
    """
    Extract the likely counterparty name from a transaction description.
    
    Heuristics:
    - Remove common prefixes (PP*, POS, etc.)
    - Take the first meaningful word/phrase
    - Normalize to uppercase
    """
    # Common prefixes to strip
    prefixes = [
        r"^PP\*",           # PayPal
        r"^POS \d+",        # Point of sale
        r"^AMZN MKTP US\*", # Amazon
        r"^AMZN MKTP US",   # Amazon variant
        r"^\d+\s+",         # Leading numbers
    ]
    
    cleaned = description.strip().upper()
    
    for prefix in prefixes:
        cleaned = re.sub(prefix, "", cleaned, flags=re.IGNORECASE).strip()
    
    # Take first word or phrase (up to first number or special char)
    match = re.match(r"^([A-Z\s]+)", cleaned)
    if match:
        return match.group(1).strip()
    
    # Fallback: first 20 chars
    return cleaned[:20].strip()


def get_active_rules(session: Session, business_id: int) -> list[DurableRule]:
    """Get all active rules for a business."""
    return session.query(DurableRule).filter(
        DurableRule.business_id == business_id,
        DurableRule.active == True,
    ).order_by(DurableRule.created_at.desc()).all()


def find_matching_rule(
    session: Session,
    business_id: int,
    description: str,
) -> Optional[DurableRule]:
    """
    Find a rule that matches the given description.
    Returns the first matching rule, or None.
    """
    rules = get_active_rules(session, business_id)
    description_upper = description.upper()
    
    for rule in rules:
        if rule.counterparty_pattern.upper() in description_upper:
            return rule
    
    return None


def create_rule(
    session: Session,
    business_id: int,
    counterparty_pattern: str,
    account: str,
    note: Optional[str] = None,
    created_from_classification_id: Optional[int] = None,
) -> DurableRule:
    """
    Create a new durable rule.
    Rules are insert-only; to "delete", set active=False.
    """
    rule = DurableRule(
        business_id=business_id,
        counterparty_pattern=counterparty_pattern.upper(),
        account=account,
        note=note,
        created_from_classification_id=created_from_classification_id,
        active=True,
    )
    session.add(rule)
    session.flush()
    return rule


def deactivate_rule(session: Session, rule_id: int) -> bool:
    """
    Soft-delete a rule by setting active=False.
    Returns True if rule was found and deactivated.
    """
    rule = session.query(DurableRule).filter(
        DurableRule.id == rule_id
    ).first()
    
    if rule:
        rule.active = False
        session.flush()
        return True
    
    return False


def get_rule_by_id(session: Session, rule_id: int) -> Optional[DurableRule]:
    """Get a rule by ID."""
    return session.query(DurableRule).filter(
        DurableRule.id == rule_id
    ).first()


def list_rules(session: Session, business_id: int, include_inactive: bool = False) -> list[DurableRule]:
    """
    List all rules for a business.
    
    Args:
        session: Database session
        business_id: Business ID
        include_inactive: If True, include soft-deleted rules
    """
    query = session.query(DurableRule).filter(
        DurableRule.business_id == business_id
    )
    
    if not include_inactive:
        query = query.filter(DurableRule.active == True)
    
    return query.order_by(DurableRule.created_at.desc()).all()


def auto_create_rule_from_correction(
    session: Session,
    classification: Classification,
    note: Optional[str] = None,
) -> Optional[DurableRule]:
    """
    Automatically create a rule from a human correction.
    
    Only creates a rule if:
    - The classification is from a human
    - No existing rule matches this pattern
    - The pattern can be extracted from the description
    
    Returns the created rule, or None if no rule was created.
    """
    if classification.source != "human":
        return None
    
    transaction = classification.transaction
    pattern = extract_counterparty(transaction.description)
    
    if not pattern or len(pattern) < 3:
        return None
    
    # Check if rule already exists
    existing = find_matching_rule(session, transaction.business_id, pattern)
    if existing:
        return None
    
    # Create the rule
    return create_rule(
        session,
        business_id=transaction.business_id,
        counterparty_pattern=pattern,
        account=classification.account,
        note=note or classification.reason,
        created_from_classification_id=classification.id,
    )


def get_similar_transactions(
    session: Session,
    business_id: int,
    description: str,
    limit: int = 5,
) -> list[Transaction]:
    """
    Find transactions with similar descriptions.
    Uses simple substring matching for now.
    
    Useful for showing the accountant similar past transactions
    when they're classifying a new one.
    """
    # Extract counterparty for matching
    pattern = extract_counterparty(description)
    
    if not pattern:
        return []
    
    # Find transactions with similar descriptions
    return session.query(Transaction).filter(
        Transaction.business_id == business_id,
        Transaction.description.ilike(f"%{pattern}%"),
    ).limit(limit).all()


def get_memory_context(session: Session, business_id: int) -> dict:
    """
    Get the full memory context for a business.
    Useful for debugging or exporting.
    """
    rules = list_rules(session, business_id, include_inactive=True)
    
    return {
        "business_id": business_id,
        "active_rules": [
            {
                "id": r.id,
                "pattern": r.counterparty_pattern,
                "account": r.account,
                "note": r.note,
                "created_at": r.created_at.isoformat(),
            }
            for r in rules if r.active
        ],
        "inactive_rules": [
            {
                "id": r.id,
                "pattern": r.counterparty_pattern,
                "account": r.account,
                "note": r.note,
                "created_at": r.created_at.isoformat(),
            }
            for r in rules if not r.active
        ],
    }
