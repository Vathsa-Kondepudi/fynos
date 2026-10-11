"""CLI interface for the bank statement classifier."""

import json
import os
from pathlib import Path

import click
from dotenv import load_dotenv

from .database import get_session, init_db, get_or_create_business
from .ingestion import (
    ingest_file_from_path,
    get_merged_transactions,
    get_transaction_count,
    get_latest_balance_from_files,
)
from .classifier import (
    classify_all_transactions,
    correct_classification,
    explain_classification,
    create_rule_from_correction,
    CONFIDENCE_THRESHOLD,
)
from .memory import (
    list_rules,
    create_rule,
    deactivate_rule,
    extract_counterparty,
    get_memory_context,
)
from .models import Account

# Load environment variables from .env file
load_dotenv()


@click.group()
@click.option("--business", "-b", default="Kestrel Coffee Roasters", help="Business name")
@click.pass_context
def cli(ctx, business):
    """Bank Statement Classifier - Classify transactions with memory."""
    ctx.ensure_object(dict)
    ctx.obj["business_name"] = business
    init_db()


@cli.command()
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def upload(ctx, file_path):
    """Upload and ingest a bank statement CSV file."""
    business_name = ctx.obj["business_name"]
    
    with get_session() as session:
        business = get_or_create_business(session, business_name)
        
        click.echo(f"Ingesting {file_path.name} for {business_name}...")
        
        result = ingest_file_from_path(session, business, file_path)
        
        if result.already_existed:
            click.echo(f"File already ingested (hash match). Skipping.")
            return
        
        click.echo(f"Created {result.raw_rows_created} raw rows")
        click.echo(f"Created {result.transactions_created} new transactions")
        click.echo(f"Linked {result.transactions_linked} existing transactions")
        
        if result.balance_errors:
            click.echo(click.style("Balance chain errors:", fg="yellow"))
            for error in result.balance_errors:
                click.echo(f"  {error}")
        else:
            click.echo(click.style("Balance chain valid", fg="green"))
        
        total = get_transaction_count(session, business.id)
        click.echo(f"Total transactions: {total}")


@cli.command("list")
@click.option("--unclassified", "-u", is_flag=True, help="Show only unclassified transactions")
@click.option("--needs-review", "-r", is_flag=True, help="Show only low-confidence classifications")
@click.option("--limit", "-l", default=50, help="Max transactions to show")
@click.pass_context
def list_transactions(ctx, unclassified, needs_review, limit):
    """List transactions with their classifications."""
    business_name = ctx.obj["business_name"]
    
    with get_session() as session:
        business = get_or_create_business(session, business_name)
        transactions = get_merged_transactions(session, business.id)
        
        shown = 0
        for txn in transactions:
            if shown >= limit:
                break
            
            current = txn.current_classification
            account = current.account if current else "UNCLASSIFIED"
            confidence = current.confidence if current else None
            source = current.source if current else None
            
            # Apply filters
            if unclassified and current:
                continue
            if needs_review and (not current or current.confidence >= CONFIDENCE_THRESHOLD):
                continue
            
            # Format output
            amount = txn.amount_cents / 100
            amount_str = f"${amount:>10.2f}" if amount >= 0 else f"-${abs(amount):>9.2f}"
            
            conf_str = f"{confidence:.0%}" if confidence else "N/A"
            source_str = f"[{source}]" if source else ""
            
            # Color based on confidence
            if not current:
                color = "red"
            elif confidence and confidence < CONFIDENCE_THRESHOLD:
                color = "yellow"
            else:
                color = "green"
            
            account_str = f"{account:>12}"
            click.echo(
                f"{txn.id:>4} | {txn.date} | {amount_str} | "
                f"{click.style(account_str, fg=color)} {conf_str:>4} {source_str:>8} | "
                f"{txn.description[:40]}"
            )
            shown += 1
        
        click.echo(f"\nShowing {shown} of {len(transactions)} transactions")


@cli.command()
@click.option("--only-unclassified", "-u", is_flag=True, help="Only classify unclassified transactions")
@click.pass_context
def classify(ctx, only_unclassified):
    """Classify all transactions using rules and LLM."""
    business_name = ctx.obj["business_name"]
    
    # Check for API key
    if not os.getenv("GROQ_API_KEY"):
        click.echo(click.style("Error: GROQ_API_KEY environment variable not set", fg="red"))
        return
    
    with get_session() as session:
        business = get_or_create_business(session, business_name)
        
        click.echo(f"Classifying transactions for {business_name}...")
        
        classifications = classify_all_transactions(
            session, 
            business.id,
            only_unclassified=only_unclassified,
        )
        
        # Summary by source
        by_source = {}
        by_account = {}
        low_confidence = 0
        
        for c in classifications:
            by_source[c.source] = by_source.get(c.source, 0) + 1
            by_account[c.account] = by_account.get(c.account, 0) + 1
            if c.confidence and c.confidence < CONFIDENCE_THRESHOLD:
                low_confidence += 1
        
        click.echo(f"\nClassified {len(classifications)} transactions:")
        click.echo("\nBy source:")
        for source, count in sorted(by_source.items()):
            click.echo(f"  {source}: {count}")
        
        click.echo("\nBy account:")
        for account, count in sorted(by_account.items()):
            click.echo(f"  {account}: {count}")
        
        if low_confidence:
            click.echo(click.style(
                f"\n{low_confidence} transactions need review (confidence < {CONFIDENCE_THRESHOLD:.0%})",
                fg="yellow"
            ))


@cli.command()
@click.argument("transaction_id", type=int)
@click.argument("account", type=click.Choice([a.value for a in Account]))
@click.option("--reason", "-r", help="Reason for correction")
@click.option("--create-rule", "-c", is_flag=True, help="Create a rule from this correction")
@click.pass_context
def correct(ctx, transaction_id, account, reason, create_rule):
    """Correct the classification of a transaction."""
    with get_session() as session:
        classification = correct_classification(session, transaction_id, account, reason)
        
        click.echo(f"Transaction {transaction_id} corrected to {account}")
        
        if create_rule:
            from .models import Transaction
            txn = session.query(Transaction).filter(Transaction.id == transaction_id).first()
            pattern = extract_counterparty(txn.description)
            
            rule = create_rule_from_correction(session, classification, pattern)
            click.echo(f"Created rule: {pattern} → {account}")


@cli.command()
@click.argument("transaction_id", type=int)
@click.pass_context
def explain(ctx, transaction_id):
    """Explain why a transaction has its current classification."""
    with get_session() as session:
        try:
            explanation = explain_classification(session, transaction_id)
        except ValueError as e:
            click.echo(click.style(str(e), fg="red"))
            return
        
        txn = explanation["transaction"]
        click.echo(f"\nTransaction {txn['id']}:")
        click.echo(f"  Date: {txn['date']}")
        click.echo(f"  Description: {txn['description']}")
        click.echo(f"  Amount: ${txn['amount']:.2f}")
        
        click.echo(f"\nCurrent: {explanation['current_account']} ({explanation['current_source']})")
        if explanation['current_reason']:
            click.echo(f"  Reason: {explanation['current_reason']}")
        
        if len(explanation['history']) > 1:
            click.echo(f"\nClassification history ({len(explanation['history'])} entries):")
            for h in explanation['history']:
                click.echo(f"  [{h['created_at']}] {h['account']} ({h['source']}, {h['confidence']:.0%})")
                if h['reason']:
                    click.echo(f"    {h['reason']}")


@cli.command()
@click.pass_context
def rules(ctx):
    """List all active rules."""
    business_name = ctx.obj["business_name"]
    
    with get_session() as session:
        business = get_or_create_business(session, business_name)
        rules_list = list_rules(session, business.id)
        
        if not rules_list:
            click.echo("No rules defined yet.")
            return
        
        click.echo(f"Rules for {business_name}:\n")
        for rule in rules_list:
            click.echo(f"  {rule.id:>3} | {rule.counterparty_pattern:>20} → {rule.account}")
            if rule.note:
                click.echo(f"      {rule.note}")


@cli.command("add-rule")
@click.argument("pattern")
@click.argument("account", type=click.Choice([a.value for a in Account]))
@click.option("--note", "-n", help="Note explaining the rule")
@click.pass_context
def add_rule(ctx, pattern, account, note):
    """Add a new classification rule."""
    business_name = ctx.obj["business_name"]
    
    with get_session() as session:
        business = get_or_create_business(session, business_name)
        rule = create_rule(session, business.id, pattern, account, note)
        
        click.echo(f"Created rule {rule.id}: {pattern} → {account}")


@cli.command("delete-rule")
@click.argument("rule_id", type=int)
@click.pass_context
def delete_rule(ctx, rule_id):
    """Deactivate a rule (soft delete)."""
    with get_session() as session:
        if deactivate_rule(session, rule_id):
            click.echo(f"Rule {rule_id} deactivated")
        else:
            click.echo(click.style(f"Rule {rule_id} not found", fg="red"))


@cli.command()
@click.option("--output", "-o", type=click.Path(path_type=Path), help="Output file (JSON)")
@click.pass_context
def export(ctx, output):
    """Export all data for evidence."""
    business_name = ctx.obj["business_name"]
    
    with get_session() as session:
        business = get_or_create_business(session, business_name)
        
        transactions = get_merged_transactions(session, business.id)
        memory = get_memory_context(session, business.id)
        
        data = {
            "business": business_name,
            "transaction_count": len(transactions),
            "final_balance_cents": get_latest_balance_from_files(session, business.id),
            "memory": memory,
            "transactions": [
                {
                    "id": txn.id,
                    "date": txn.date.isoformat(),
                    "description": txn.description,
                    "amount_cents": txn.amount_cents,
                    "current_account": txn.current_account.value if txn.current_account else None,
                    "classification_source": txn.current_classification.source if txn.current_classification else None,
                    "confidence": txn.current_classification.confidence if txn.current_classification else None,
                }
                for txn in transactions
            ],
        }
        
        if output:
            output.write_text(json.dumps(data, indent=2))
            click.echo(f"Exported to {output}")
        else:
            click.echo(json.dumps(data, indent=2))


@cli.command()
@click.pass_context
def stats(ctx):
    """Show statistics about the current state."""
    business_name = ctx.obj["business_name"]
    
    with get_session() as session:
        business = get_or_create_business(session, business_name)
        
        transactions = get_merged_transactions(session, business.id)
        rules_list = list_rules(session, business.id)
        
        # Count by classification status
        classified = 0
        unclassified = 0
        needs_review = 0
        by_account = {}
        by_source = {}
        
        for txn in transactions:
            current = txn.current_classification
            if not current:
                unclassified += 1
            else:
                classified += 1
                if current.confidence and current.confidence < CONFIDENCE_THRESHOLD:
                    needs_review += 1
                
                by_account[current.account] = by_account.get(current.account, 0) + 1
                by_source[current.source] = by_source.get(current.source, 0) + 1
        
        # Final balance
        final_balance = get_latest_balance_from_files(session, business.id)
        
        click.echo(f"\n{business_name}")
        click.echo("=" * 40)
        click.echo(f"Total transactions: {len(transactions)}")
        click.echo(f"Final balance: ${final_balance / 100:.2f}" if final_balance else "Final balance: N/A")
        click.echo(f"\nClassified: {classified}")
        click.echo(f"Unclassified: {unclassified}")
        click.echo(f"Needs review: {needs_review}")
        click.echo(f"Active rules: {len(rules_list)}")
        
        if by_account:
            click.echo("\nBy account:")
            for account, count in sorted(by_account.items()):
                click.echo(f"  {account}: {count}")
        
        if by_source:
            click.echo("\nBy source:")
            for source, count in sorted(by_source.items()):
                click.echo(f"  {source}: {count}")


def main():
    """Entry point for the CLI."""
    cli(obj={})


if __name__ == "__main__":
    main()
