from __future__ import annotations

from collections import defaultdict
from copy import copy
from datetime import date
from decimal import Decimal
from pathlib import Path
import re

from .constants import ESSENTIAL_CATEGORIES, EXPENSE_CATEGORIES, INCOME_CATEGORIES
from .domain import ParsedTransaction, Transaction, normalise_text, transaction_from_parsed, validate_category
from .importers import parse_statement
from .storage import DataStore


KEYWORD_RULES = {
    "albert heijn": ("expense", "Groceries"), "jumbo": ("expense", "Groceries"),
    "vattenfall": ("expense", "Bills"), "ziggo": ("expense", "Bills"),
    "vodafone": ("expense", "Bills"), "swapfiets": ("expense", "Subscriptions"),
    "booking com": ("income", "Salary"), "revolut interest": ("income", "Other"),
}


def classify(parsed: ParsedTransaction, store: DataStore) -> tuple[str, str, str, bool]:
    haystack = normalise_text(f"{parsed.description} {parsed.original_description}")
    if any(normalise_text(rule) in haystack for rule in store.transfer_rules() if rule.strip()):
        return "transfer", "", "transfer_rule", False
    kind = "income" if parsed.signed_amount > 0 else "expense"
    rules = store.merchant_rules()
    padded_haystack = f" {haystack} "
    for merchant, match in rules.items():
        if not merchant or match.get("kind") != kind:
            continue
        if match.get("match_type", "contains") == "regex":
            try:
                matched = re.search(merchant, parsed.description, flags=re.IGNORECASE) is not None
            except re.error:
                matched = False
        else:
            matched = haystack == merchant or f" {merchant} " in padded_haystack
        if matched:
            return kind, match["category"], "merchant_rule", False
    for keyword, (rule_kind, category) in KEYWORD_RULES.items():
        if keyword in haystack and rule_kind == kind:
            return kind, category, "keyword_rule", False
    return kind, "Other", "fallback", True


def import_csv(store: DataStore, filename: str, content: bytes) -> dict[str, int]:
    store.initialise()
    _, parsed_rows = parse_statement(content)
    existing = store.existing_fingerprints()
    summary = {"imported": 0, "skipped": 0, "transfers": 0, "review": 0}
    if store.has_import_hash(content):
        summary["skipped"] = len(parsed_rows)
        store.record_import(filename, content, summary)
        return summary
    staged: list[Transaction] = []
    for parsed in parsed_rows:
        kind, category, source, needs_review = classify(parsed, store)
        transaction = transaction_from_parsed(parsed, kind=kind, category=category,
                                               classification_source=source,
                                               needs_review=needs_review, raw_source="")
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
    pending = 0
    for transaction in store.transactions():
        if transaction.kind == "transfer" or not transaction.posted_date.startswith(str(year)):
            continue
        month = int(transaction.posted_date[5:7]) - 1
        if transaction.kind == "expense" and transaction.category in expense:
            expense[transaction.category][month] += transaction.numeric_amount
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
    decimal_data = {"expense": expense, "income": income, "totals": totals, "budget": budget_data}
    chart_data = {
        "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "expense": {name: [float(value) for value in values] for name, values in expense.items()},
        "income": {name: [float(value) for value in values] for name, values in income.items()},
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
    rules = 0
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
            key = normalise_text(description)
            if key and key not in store.merchant_rules():
                store.save_merchant_rule(key, kind, category)
                rules += 1
    store.append_transactions(staged)
    store.record_import(marker, path.read_bytes(), {"imported": len(staged), "skipped": 0, "transfers": 0, "review": 0})
    return {"imported": len(staged), "rules": rules}

