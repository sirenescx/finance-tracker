from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO
from typing import Callable

from .domain import ParsedTransaction, UnsupportedStatement, parse_euro

ING_HEADERS = {"Date", "Name / Description", "Account", "Counterparty", "Debit/credit", "Amount (EUR)"}
REVOLUT_HEADERS = {"Type", "Product", "Completed Date", "Description", "Amount", "Currency"}


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
        parsed_date = datetime.fromisoformat(completed).date()
        description = row["Description"].strip()
        result.append(ParsedTransaction(
            source="Revolut", account=f"Revolut {row.get('Product', 'Current').strip()}",
            posted_date=parsed_date, description=description,
            original_description=" | ".join(x for x in [row.get("Type", ""), description] if x),
            signed_amount=parse_euro(row["Amount"]), raw_row=line,
            raw_reference="|".join([row.get("Type", ""), row.get("Started Date", ""), completed]),
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
    found = ", ".join(reader.fieldnames or []) or "no headers"
    raise UnsupportedStatement(f"Unsupported CSV format. Found headers: {found}")

