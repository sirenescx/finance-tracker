from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO
from typing import Callable

from .domain import ParsedTransaction, UnsupportedStatement, parse_euro

ING_HEADERS = {"Date", "Name / Description", "Account", "Counterparty", "Debit/credit", "Amount (EUR)"}
REVOLUT_HEADERS = {"Type", "Product", "Completed Date", "Description", "Amount", "Currency"}
YONDER_HEADERS = {
    "Date/Time of transaction", "Description", "Amount (EUR)",
    "Amount (in Charged Currency)", "Currency", "Category", "Debit or Credit", "Country",
}


def _parse_ing(rows: list[dict[str, str]]) -> list[ParsedTransaction]:
    result = []
    for line, row in enumerate(rows, start=2):
        amount = parse_euro(row["Amount (EUR)"])
        if row["Debit/credit"].strip().lower() == "debit":
            amount = -amount
        description = row["Name / Description"].strip()
        notification = row.get("Notifications", "").strip()
        result.append(ParsedTransaction(
            source="ING", account=row["Account"].strip() or "ING Current",
            posted_date=datetime.strptime(row["Date"].strip(), "%Y%m%d").date(),
            description=description, original_description=" | ".join(x for x in [description, notification] if x),
            signed_amount=amount, raw_row=line,
            raw_reference="|".join([row.get("Counterparty", ""), row.get("Code", "")]),
        ))
    return result


def _parse_revolut(rows: list[dict[str, str]]) -> list[ParsedTransaction]:
    result = []
    for line, row in enumerate(rows, start=2):
        if row.get("State", "COMPLETED").strip().upper() != "COMPLETED":
            continue
        completed = row["Completed Date"].strip()
        started = row.get("Started Date", "").strip()
        transaction_date = started or completed
        parsed_date = datetime.fromisoformat(transaction_date).date()
        description = row["Description"].strip()
        signed_amount = parse_euro(row["Amount"])
        fee = parse_euro(row.get("Fee", "0") or "0")
        if signed_amount == 0 and fee != 0 and row.get("Type", "").strip().lower() == "charge":
            signed_amount = -abs(fee)
        result.append(ParsedTransaction(
            source="Revolut", account=f"Revolut {row.get('Product', 'Current').strip()}",
            posted_date=parsed_date, description=description,
            original_description=" | ".join(x for x in [row.get("Type", ""), description] if x),
            signed_amount=signed_amount, raw_row=line,
            raw_reference="|".join([row.get("Type", ""), started, completed]),
            currency=row.get("Currency", "EUR").strip().upper(),
            original_amount=f"{abs(signed_amount):.2f}",
        ))
    return result


def _parse_yonder(rows: list[dict[str, str]]) -> list[ParsedTransaction]:
    """Parse Yonder's card-statement export, which already includes EUR amounts."""
    result = []
    for line, row in enumerate(rows, start=2):
        transaction_time = row["Date/Time of transaction"].strip()
        amount = parse_euro(row["Amount (EUR)"])
        if row["Debit or Credit"].strip().lower() == "debit":
            amount = -amount
        description = row["Description"].strip()
        charged_amount = row.get("Amount (in Charged Currency)", "").strip()
        charged_currency = row.get("Currency", "EUR").strip().upper()
        result.append(ParsedTransaction(
            source="Yonder", account="Yonder Credit Card",
            posted_date=datetime.fromisoformat(transaction_time).date(),
            description=description,
            original_description=" | ".join(value for value in [
                description,
                f"{row.get('Debit or Credit', '').strip()} transaction",
                f"Yonder category: {row.get('Category', '').strip()}",
                f"{charged_currency} {charged_amount}" if charged_amount else "",
            ] if value),
            signed_amount=amount, raw_row=line,
            raw_reference="|".join([transaction_time, row.get("Country", "").strip()]),
            # The export has already converted the charge into EUR. Retaining
            # EUR here prevents the generic foreign-currency flow requesting a
            # second exchange rate.
            currency="EUR", original_amount=f"{abs(amount):.2f}",
            bank_category=row.get("Category", "").strip(),
        ))
    return result


def parse_statement(content: bytes) -> tuple[str, list[ParsedTransaction]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedStatement("The CSV must be UTF-8 encoded.") from exc
    reader = csv.DictReader(StringIO(text))
    headers = set(reader.fieldnames or [])
    rows = list(reader)
    if ING_HEADERS.issubset(headers):
        return "ING", _parse_ing(rows)
    if REVOLUT_HEADERS.issubset(headers):
        return "Revolut", _parse_revolut(rows)
    if YONDER_HEADERS.issubset(headers):
        return "Yonder", _parse_yonder(rows)
    found = ", ".join(reader.fieldnames or []) or "no headers"
    raise UnsupportedStatement(f"Unsupported CSV format. Found headers: {found}")

