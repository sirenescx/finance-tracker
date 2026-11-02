from __future__ import annotations

from collections import defaultdict
from copy import copy
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from pathlib import Path
import re
from uuid import uuid4

from .constants import ESSENTIAL_CATEGORIES, EXPENSE_CATEGORIES, INCOME_CATEGORIES
from .domain import MissingExchangeRate, ParsedTransaction, Transaction, normalise_text, transaction_from_parsed, validate_category
from .importers import parse_statement
from .storage import DataStore


KEYWORD_RULES = {
    "albert heijn": ("expense", "Groceries"), "jumbo": ("expense", "Groceries"),
    "vattenfall": ("expense", "Bills"), "ziggo": ("expense", "Bills"),
    "vodafone": ("expense", "Bills"), "swapfiets": ("expense", "Subscriptions"),
    "booking com": ("income", "Salary"), "revolut interest": ("income", "Other"),
}


def classify(parsed: ParsedTransaction, store: DataStore) -> tuple[str, str, str, bool]:
    merchant_name = normalise_text(parsed.description)
    haystack = merchant_name
    if parsed.source == "Revolut" and parsed.original_description.lower().startswith("exchange |"):
        return "exchange", "", "currency_exchange", False
    if any(normalise_text(rule) in normalise_text(f"{parsed.description} {parsed.original_description}") for rule in store.transfer_rules() if rule.strip()):
        return "transfer", "", "transfer_rule", False
    kind = "income" if parsed.signed_amount > 0 else "expense"
    rules = store.merchant_rules()
    for merchant, match in rules.items():
        if not merchant or match.get("kind") != kind or match.get("category") not in (INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES):
            continue
        if match.get("match_type", "contains") == "regex":
            try:
                matched = re.search(merchant, parsed.description, flags=re.IGNORECASE) is not None
            except re.error:
                matched = False
        else:
            normalised_merchant = normalise_text(merchant)
            matched = bool(normalised_merchant) and merchant_name == normalised_merchant
        if matched:
            return kind, match["category"], "merchant_rule", False
    for keyword, (rule_kind, category) in KEYWORD_RULES.items():
        if keyword in haystack and rule_kind == kind:
            return kind, category, "keyword_rule", False
    return kind, "Other", "fallback", True


def is_currency_exchange(parsed: ParsedTransaction) -> bool:
    return parsed.source == "Revolut" and parsed.original_description.lower().startswith("exchange |")


def reclassify_review_transactions(store: DataStore) -> int:
    """Apply newly active saved rules to transactions that are awaiting review."""
    transactions = store.transactions()
    changed = 0
    for transaction in transactions:
        if not transaction.is_reviewable or transaction.kind == "transfer":
            continue
        signed_amount = transaction.numeric_amount * (1 if transaction.direction == "income" else -1)
        parsed = ParsedTransaction(
            transaction.source, transaction.account, date.fromisoformat(transaction.posted_date),
            transaction.description, transaction.original_description, signed_amount,
            int(transaction.raw_row or 0),
        )
        kind, category, source, needs_review = classify(parsed, store)
        if not needs_review:
            transaction.kind = kind
            transaction.category = category
            transaction.classification_source = source
            transaction.needs_review = "false"
            changed += 1
    if changed:
        store.replace_transactions(transactions)
    return changed


def sync_transactions_to_rules(store: DataStore) -> int:
    """Re-evaluate automatic classifications against the current saved rules."""
    transactions = store.transactions()
    changed = 0
    for transaction in transactions:
        # A manual category choice is an explicit override. Transfers marked by
        # the user are also kept until explicitly changed in the UI.
        if transaction.classification_source in {"manual", "manual_transfer"}:
            continue
        signed_amount = transaction.numeric_amount * (1 if transaction.direction == "income" else -1)
        parsed = ParsedTransaction(
            transaction.source, transaction.account, date.fromisoformat(transaction.posted_date),
            transaction.description, transaction.original_description, signed_amount,
            int(transaction.raw_row or 0),
        )
        kind, category, source, needs_review = classify(parsed, store)
        # Keep the workbook's explicitly assigned historical category when no
        # current rule matches it. A matching current rule may still update it.
        if transaction.classification_source == "legacy" and source == "fallback":
            continue
        review_value = str(needs_review).lower()
        if (transaction.kind, transaction.category, transaction.classification_source, transaction.needs_review) != (kind, category, source, review_value):
            transaction.kind = kind
            transaction.category = category
            transaction.classification_source = source
            transaction.needs_review = review_value
            changed += 1
    if changed:
        store.replace_transactions(transactions)
    return changed


def import_csv(store: DataStore, filename: str, content: bytes) -> dict[str, int]:
    store.initialise()
    _, parsed_rows = parse_statement(content)
    digest = sha256(content).hexdigest()
    prior_sources = {
        str(entry.get("stored_as", ""))
        for entry in store.imports()
        if entry.get("sha256") == digest and entry.get("stored_as")
    }
    transactions = store.transactions()
    prior_source_rows = [item for item in transactions if item.raw_source in prior_sources]
    rows_by_number: dict[str, list[Transaction]] = defaultdict(list)
    for item in prior_source_rows:
        rows_by_number[item.raw_row].append(item)
    existing = {item.fingerprint for item in transactions}
    summary = {"imported": 0, "updated": 0, "skipped": 0, "transfers": 0, "review": 0}
    missing_rates: dict[str, set[str]] = defaultdict(set)
    for parsed in parsed_rows:
        if (parsed.currency.upper() != "EUR" and not is_currency_exchange(parsed)
                and store.fx_rate(parsed.currency, parsed.posted_date.isoformat()) is None):
            missing_rates[parsed.currency.upper()].add(parsed.posted_date.isoformat())
    if missing_rates:
        details = "; ".join(
            f"{currency}: {', '.join(sorted(days))}" for currency, days in sorted(missing_rates.items())
        )
        raise MissingExchangeRate(f"Missing EUR exchange rates ({details}). Add the daily rates in Settings, then upload this CSV again.")
    staged: list[Transaction] = []
    updates: dict[str, Transaction] = {}
    removals: set[str] = set()
    for parsed in parsed_rows:
        if is_currency_exchange(parsed):
            parsed.exchange_rate = "—"
        elif parsed.currency.upper() != "EUR":
            rate_text = store.fx_rate(parsed.currency, parsed.posted_date.isoformat())
            assert rate_text is not None
            rate = Decimal(rate_text)
            if rate <= 0:
                raise MissingExchangeRate(f"The saved {parsed.currency} rate for {parsed.posted_date} must be positive.")
            parsed.exchange_rate = str(rate)
            parsed.original_amount = parsed.original_amount or f"{abs(parsed.signed_amount):.2f}"
            parsed.signed_amount = (parsed.signed_amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        kind, category, source, needs_review = classify(parsed, store)
        transaction = transaction_from_parsed(parsed, kind=kind, category=category,
                                               classification_source=source,
                                               needs_review=needs_review, raw_source="")
        candidates = rows_by_number.get(str(parsed.raw_row), [])
        previous = next((item for item in reversed(candidates) if item.id not in removals), None)
        if previous is not None:
            data_changed = (
                previous.posted_date != transaction.posted_date
                or previous.amount != transaction.amount
                or previous.currency.upper() != transaction.currency.upper()
                or previous.exchange_rate != transaction.exchange_rate
            )
            if not data_changed:
                summary["skipped"] += 1
                continue
            if transaction.fingerprint in existing and transaction.fingerprint != previous.fingerprint:
                # The corrected row already arrived through an overlapping export.
                removals.add(previous.id)
                existing.discard(previous.fingerprint)
                summary["skipped"] += 1
                continue
            # Refresh bank supplied date/amount/currency while retaining any
            # manual category or transfer decisions made after the first import.
            revised = copy(previous)
            revised.id = transaction.id
            revised.fingerprint = transaction.fingerprint
            revised.posted_date = transaction.posted_date
            revised.amount = transaction.amount
            revised.direction = transaction.direction
            revised.original_amount = transaction.original_amount
            revised.currency = transaction.currency
            revised.exchange_rate = transaction.exchange_rate
            updates[previous.id] = revised
            removals.add(previous.id)
            existing.discard(previous.fingerprint)
            existing.add(revised.fingerprint)
            summary["updated"] += 1
            continue
        if transaction.fingerprint in existing:
            summary["skipped"] += 1
            continue
        existing.add(transaction.fingerprint)
        if kind == "transfer":
            summary["transfers"] += 1
        elif needs_review:
            summary["review"] += 1
        staged.append(transaction)
        summary["imported"] += 1
    raw_source = store.record_import(filename, content, summary)
    for transaction in staged:
        transaction.raw_source = raw_source
    if removals or staged:
        revised_rows = [updates.get(item.id, item) for item in transactions if item.id not in removals or item.id in updates]
        if removals or updates:
            store.replace_transactions(revised_rows + staged)
        else:
            store.append_transactions(staged)
    return summary


def review_transaction(store: DataStore, transaction_id: str, kind: str, category: str, save_rule: bool) -> None:
    validate_category(kind, category)
    transactions = store.transactions()
    found = False
    for transaction in transactions:
        if transaction.id == transaction_id:
            transaction.kind = kind
            transaction.category = category
            transaction.direction = kind
            transaction.classification_source = "manual"
            transaction.needs_review = "false"
            if save_rule:
                store.save_merchant_rule(normalise_text(transaction.description), kind, category)
            found = True
            break
    if not found:
        raise KeyError(transaction_id)
    store.replace_transactions(transactions)


def add_manual_transaction(
    store: DataStore, posted_date: date, description: str, amount: Decimal,
    kind: str, category: str, account: str = "Manual",
) -> Transaction:
    if kind not in {"expense", "income"}:
        raise ValueError("Type must be expense or income")
    validate_category(kind, category)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")
    if not description.strip():
        raise ValueError("Description is required")
    reference = str(uuid4())
    parsed = ParsedTransaction(
        source="Manual", account=account.strip() or "Manual", posted_date=posted_date,
        description=description.strip(), original_description=description.strip(),
        signed_amount=amount if kind == "income" else -amount,
        raw_row=0, raw_reference=reference,
    )
    transaction = transaction_from_parsed(
        parsed, kind=kind, category=category, classification_source="manual",
        needs_review=False, raw_source="manual-entry",
    )
    store.append_transactions([transaction])
    return transaction


def delete_transaction(store: DataStore, transaction_id: str) -> None:
    transactions = store.transactions()
    retained = [transaction for transaction in transactions if transaction.id != transaction_id]
    if len(retained) == len(transactions):
        raise KeyError(transaction_id)
    store.replace_transactions(retained)


def mark_transfer(store: DataStore, transaction_id: str, save_rule: bool) -> None:
    transactions = store.transactions()
    found = False
    for transaction in transactions:
        if transaction.id == transaction_id:
            transaction.kind = "transfer"
            transaction.category = ""
            transaction.classification_source = "manual_transfer"
            transaction.needs_review = "false"
            if save_rule:
                rules = store.transfer_rules()
                rules.append(normalise_text(transaction.description))
                store.save_transfer_rules(sorted(set(rules)))
            found = True
            break
    if not found:
        raise KeyError(transaction_id)
    store.replace_transactions(transactions)


def dashboard(store: DataStore, year: int) -> dict[str, object]:
    expense = {category: [Decimal("0") for _ in range(12)] for category in EXPENSE_CATEGORIES}
    income = {category: [Decimal("0") for _ in range(12)] for category in INCOME_CATEGORIES}
    bank_spend = {bank: [Decimal("0") for _ in range(12)] for bank in ("ING", "Revolut")}
    pending = 0
    for transaction in store.transactions():
        if transaction.kind in {"transfer", "exchange"} or not transaction.posted_date.startswith(str(year)):
            continue
        month = int(transaction.posted_date[5:7]) - 1
        if transaction.kind == "expense" and transaction.category in expense:
            expense[transaction.category][month] += transaction.numeric_amount
            if transaction.source in bank_spend:
                bank_spend[transaction.source][month] += transaction.numeric_amount
        elif transaction.kind == "income" and transaction.category in income:
            income[transaction.category][month] += transaction.numeric_amount
        if transaction.is_reviewable:
            pending += 1
    totals = {
        "expenses": [sum(values[index] for values in expense.values()) for index in range(12)],
        "essential": [sum(expense[category][index] for category in ESSENTIAL_CATEGORIES) for index in range(12)],
        "income": [sum(values[index] for values in income.values()) for index in range(12)],
    }
    totals["savings"] = [totals["income"][index] - totals["expenses"][index] for index in range(12)]
    stored_budgets = store.budgets()
    budget_data: dict[str, dict[str, Decimal]] = {"expense": {}, "income": {}}
    for kind, categories in (("expense", EXPENSE_CATEGORIES), ("income", INCOME_CATEGORIES)):
        for category in categories:
            budget_data[kind][category] = Decimal(stored_budgets.get(kind, {}).get(category, "0"))
    decimal_data = {"expense": expense, "income": income, "bank_spend": bank_spend,
                    "totals": totals, "budget": budget_data}
    chart_data = {
        "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "expense": {name: [float(value) for value in values] for name, values in expense.items()},
        "income": {name: [float(value) for value in values] for name, values in income.items()},
        "bankSpend": {name: [float(value) for value in values] for name, values in bank_spend.items()},
    }
    chart_data["expenseTotals"] = {name: float(sum(values)) for name, values in expense.items()}
    chart_data["incomeTotals"] = {name: float(sum(values)) for name, values in income.items()}
    return {"year": year, "data": decimal_data, "chart_data": chart_data, "pending": pending}


def seed_workbook(store: DataStore, path: Path) -> dict[str, int]:
    from openpyxl import load_workbook
    if not path.exists():
        raise FileNotFoundError(path)
    marker = f"seed:{path.resolve()}"
    if any(entry.get("filename") == marker for entry in store.imports()):
        return {"imported": 0, "rules": 0}
    workbook = load_workbook(path, data_only=True, read_only=True)
    staged: list[Transaction] = []
    existing = store.existing_fingerprints()
    for sheet, kind, date_index, description_index, amount_index, category_index in [
        ("Expenses", "expense", 1, 2, 3, 4), ("Income", "income", 1, 3, 4, 2),
    ]:
        for row_number, row in enumerate(workbook[sheet].iter_rows(min_row=2, values_only=True), start=2):
            if not row or not row[date_index] or not row[amount_index] or not row[category_index]:
                continue
            category = str(row[category_index]).strip()
            try:
                validate_category(kind, category)
            except ValueError:
                continue
            description = str(row[description_index] or "Unknown").strip()
            legacy_date = row[date_index]
            if hasattr(legacy_date, "date"):
                legacy_date = legacy_date.date()
            parsed = ParsedTransaction("Legacy workbook", "Legacy", legacy_date, description,
                                      description, Decimal(str(row[amount_index])) * (1 if kind == "income" else -1),
                                      row_number, f"{sheet}:{row_number}")
            transaction = transaction_from_parsed(parsed, kind=kind, category=category,
                                                   classification_source="legacy", needs_review=False,
                                                   raw_source=marker)
            if transaction.fingerprint not in existing:
                existing.add(transaction.fingerprint)
                staged.append(transaction)
    store.append_transactions(staged)
    store.record_import(marker, path.read_bytes(), {"imported": len(staged), "skipped": 0, "transfers": 0, "review": 0})
    return {"imported": len(staged), "rules": 0}

