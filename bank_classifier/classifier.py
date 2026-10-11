"""LLM-based transaction classifier using LangChain and Groq."""

import os
from datetime import datetime
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .models import (
    Account,
    Classification,
    ClassificationSource,
    DurableRule,
    Transaction,
)


# Classification confidence threshold - below this, mark as needs_review
CONFIDENCE_THRESHOLD = 0.7

# Prompt version for audit trail
PROMPT_VERSION = "v2"


class ClassificationResult(BaseModel):
    """Structured output from the LLM classifier."""
    account: str = Field(
        description="The account category. Must be one of: sales_payout, inventory, "
        "shipping, software, advertising, payroll, rent, bank_fees, tax, owner"
    )
    confidence: float = Field(
        description="Confidence score from 0.0 to 1.0. Use lower scores when uncertain."
    )
    reason: str = Field(
        description="Brief explanation of why this account was chosen."
    )


def get_llm() -> ChatGroq:
    """Get the Groq LLM instance."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY environment variable not set")
    
    return ChatGroq(
        api_key=api_key,
        model_name=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        temperature=0.1,
    )


def get_active_rules(session: Session, business_id: int) -> list[DurableRule]:
    """Get all active rules for a business."""
    return session.query(DurableRule).filter(
        DurableRule.business_id == business_id,
        DurableRule.active == True,
    ).all()


def format_rules_for_prompt(rules: list[DurableRule]) -> str:
    """Format rules as context for the LLM prompt."""
    if not rules:
        return "No known patterns yet."
    
    lines = []
    for rule in rules:
        note = f" ({rule.note})" if rule.note else ""
        lines.append(f"- {rule.counterparty_pattern} → {rule.account}{note}")
    
    return "\n".join(lines)


def get_human_corrections(session: Session, business_id: int, limit: int = 30) -> list[Classification]:
    """Most recent accountant corrections for this business, newest first."""
    return session.query(Classification).join(Transaction).filter(
        Transaction.business_id == business_id,
        Classification.source == ClassificationSource.HUMAN.value,
    ).order_by(Classification.id.desc()).limit(limit).all()


def format_corrections_for_prompt(corrections: list[Classification]) -> str:
    if not corrections:
        return "Past corrections by the accountant: none yet."
    lines = ["Past corrections by the accountant (examples, not rules; one-off exceptions apply only to that transaction):"]
    for c in corrections:
        t = c.transaction
        note = f" - note: {c.reason}" if c.reason else ""
        lines.append(f"- {t.description} ({t.amount_cents / 100:.2f}) -> {c.account}{note}")
    return "\n".join(lines)


def build_classification_prompt() -> ChatPromptTemplate:
    """Build the classification prompt template."""
    parser = PydanticOutputParser(pydantic_object=ClassificationResult)
    
    template = """You are a bookkeeper classifying bank transactions for a small business.

## Account Categories
- sales_payout: Money paid to the business by sales channels (Shopify, Stripe, etc.)
- inventory: Coffee, packaging, stock purchases (suppliers like Greenbean)
- shipping: Couriers, postage, delivery van costs (DHL, UPS, USPS, fuel)
- software: Software subscriptions (Notion, Klaviyo, etc.)
- advertising: Paid ads (Google Ads, Meta Ads)
- payroll: Wages, contractor payments (Gusto, individual names for contractors)
- rent: Rent and utilities (landlord payments)
- bank_fees: Bank charges, wire fees
- tax: Tax payments (IRS, etc.)
- owner: Money between business and owner, personal expenses on business card, transfers to/from savings

## Known Patterns for This Business
{rules}

## Transaction to Classify
Date: {date}
Description: {description}
Amount: ${amount:.2f}

## Instructions
1. Identify the counterparty from the description
2. Check if it matches any known pattern above
3. If no pattern matches, use context clues (common vendor names, transaction types)
4. For transfers to/from savings, classify as "owner"
5. If truly ambiguous, set confidence below 0.7

Be especially careful with:
- Amazon purchases: could be inventory (supplies) or owner (personal)
- Individual names: could be payroll (contractors) or owner
- Landlord payments: include utilities passed on, so larger amounts are still rent

{format_instructions}
"""
    
    return ChatPromptTemplate.from_template(template).partial(
        format_instructions=parser.get_format_instructions()
    )


def classify_by_rule(
    session: Session,
    transaction: Transaction,
    business_id: int,
) -> Optional[Classification]:
    """
    Try to classify using durable rules.
    Returns Classification if a rule matches, None otherwise.
    """
    rules = get_active_rules(session, business_id)
    
    description_upper = transaction.description.upper()
    
    for rule in rules:
        pattern_upper = rule.counterparty_pattern.upper()
        if pattern_upper in description_upper:
            # Rule matches!
            classification = Classification(
                transaction_id=transaction.id,
                account=rule.account,
                source=ClassificationSource.RULE.value,
                confidence=1.0,
                reason=f"Matched rule: {rule.counterparty_pattern} → {rule.account}",
                rule_id=rule.id,
            )
            session.add(classification)
            session.flush()
            return classification
    
    return None


def classify_by_llm(
    session: Session,
    transaction: Transaction,
    business_id: int,
    llm: Optional[ChatGroq] = None,
) -> Classification:
    """
    Classify a transaction using the LLM.
    Always creates a Classification record.
    """
    if llm is None:
        llm = get_llm()
    
    rules = get_active_rules(session, business_id)
    rules_text = format_rules_for_prompt(rules) + "\n\n" + format_corrections_for_prompt(
        get_human_corrections(session, business_id)
    )
    
    # Build prompt
    prompt = build_classification_prompt()
    parser = PydanticOutputParser(pydantic_object=ClassificationResult)
    
    # Create the chain
    chain = prompt | llm | parser
    
    # Run classification
    try:
        result: ClassificationResult = chain.invoke({
            "rules": rules_text,
            "date": transaction.date.isoformat(),
            "description": transaction.description,
            "amount": transaction.amount_cents / 100,
        })
        
    except Exception as e:
        print(f"  LLM error on transaction {transaction.id}: {e}")
        return None

    # An unknown account is left unclassified rather than mapped to a guess.
    if result.account not in [a.value for a in Account]:
        print(f"  Invalid account '{result.account}' on transaction {transaction.id}")
        return None

    # Create classification record
    classification = Classification(
        transaction_id=transaction.id,
        account=result.account,
        source=ClassificationSource.LLM.value,
        confidence=result.confidence,
        reason=result.reason,
        model_version=llm.model_name if llm else "unknown",
        prompt_version=PROMPT_VERSION,
    )
    session.add(classification)
    session.flush()
    
    return classification


def classify_transaction(
    session: Session,
    transaction: Transaction,
    business_id: int,
    llm: Optional[ChatGroq] = None,
) -> Classification:
    """
    Classify a transaction using rules first, then LLM if no rule matches.
    Returns the Classification record.
    """
    # Try rule-based classification first
    classification = classify_by_rule(session, transaction, business_id)
    if classification:
        return classification
    
    # Fall back to LLM
    return classify_by_llm(session, transaction, business_id, llm)


def classify_all_transactions(
    session: Session,
    business_id: int,
    only_unclassified: bool = True,
) -> list[Classification]:
    """
    Classify all transactions for a business.
    
    Args:
        session: Database session
        business_id: Business to classify for
        only_unclassified: If True, skip transactions that already have classifications
    
    Returns:
        List of new Classification records created
    """
    from .ingestion import get_merged_transactions
    
    llm = get_llm()
    transactions = get_merged_transactions(session, business_id)
    
    new_classifications = []
    
    for txn in transactions:
        # Skip if already classified and only_unclassified is True
        if only_unclassified and txn.classifications:
            continue
        
        classification = classify_transaction(session, txn, business_id, llm)
        if classification is not None:
            new_classifications.append(classification)
    
    return new_classifications


def correct_classification(
    session: Session,
    transaction_id: int,
    new_account: str,
    reason: Optional[str] = None,
) -> Classification:
    """
    Record a human correction for a transaction.
    Returns the new Classification record.
    """
    # Validate account
    valid_accounts = [a.value for a in Account]
    if new_account not in valid_accounts:
        raise ValueError(f"Invalid account: {new_account}. Must be one of {valid_accounts}")
    
    classification = Classification(
        transaction_id=transaction_id,
        account=new_account,
        source=ClassificationSource.HUMAN.value,
        confidence=1.0,
        reason=reason,
    )
    session.add(classification)
    session.flush()
    
    return classification


def create_rule_from_correction(
    session: Session,
    classification: Classification,
    counterparty_pattern: str,
) -> DurableRule:
    """
    Create a durable rule from a human correction.
    """
    transaction = classification.transaction
    
    rule = DurableRule(
        business_id=transaction.business_id,
        counterparty_pattern=counterparty_pattern,
        account=classification.account,
        note=classification.reason,
        created_from_classification_id=classification.id,
    )
    session.add(rule)
    session.flush()
    
    return rule


def get_classification_history(
    session: Session,
    transaction_id: int,
) -> list[Classification]:
    """Get the full classification history for a transaction."""
    return session.query(Classification).filter(
        Classification.transaction_id == transaction_id
    ).order_by(Classification.created_at).all()


def explain_classification(
    session: Session,
    transaction_id: int,
) -> dict:
    """
    Explain why a transaction has its current classification.
    Returns a dict with transaction details and classification history.
    """
    transaction = session.query(Transaction).filter(
        Transaction.id == transaction_id
    ).first()
    
    if not transaction:
        raise ValueError(f"Transaction {transaction_id} not found")
    
    history = get_classification_history(session, transaction_id)
    
    current = history[-1] if history else None
    
    return {
        "transaction": {
            "id": transaction.id,
            "date": transaction.date.isoformat(),
            "description": transaction.description,
            "amount": transaction.amount_cents / 100,
        },
        "current_account": current.account if current else None,
        "current_source": current.source if current else None,
        "current_reason": current.reason if current else None,
        "history": [
            {
                "account": c.account,
                "source": c.source,
                "confidence": c.confidence,
                "reason": c.reason,
                "model_version": c.model_version,
                "rule_id": c.rule_id,
                "created_at": c.created_at.isoformat(),
            }
            for c in history
        ],
    }
