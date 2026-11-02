from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from decimal import Decimal
from hashlib import sha256
import re
from typing import Iterable

from .constants import EXPENSE_CATEGORIES, INCOME_CATEGORIES


class UnsupportedStatement(ValueError):
    pass


class MissingExchangeRate(UnsupportedStatement):
    pass


def normalise_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def parse_euro(value: str) -> Decimal:
    cleaned = (value or "").strip().replace("€", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    return Decimal(cleaned)


@dataclass
class ParsedTransaction:
    source: str
    account: str
    posted_date: date
    description: str
    original_description: str
    signed_amount: Decimal
    raw_row: int
    raw_reference: str = ""
    currency: str = "EUR"
    exchange_rate: str = "1"
    original_amount: str = ""


@dataclass
class Transaction:
    id: str
    fingerprint: str
    source: str
    account: str
    posted_date: str
    description: str
    original_description: str
    amount: str
    direction: str
    kind: str
    category: str
    classification_source: str
    needs_review: str
    raw_source: str
    raw_row: str
    original_amount: str = ""
    currency: str = "EUR"
    exchange_rate: str = "1"

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Transaction":
        values = {field: row.get(field, "") for field in cls.__dataclass_fields__}
        values["currency"] = values["currency"] or "EUR"
        values["exchange_rate"] = values["exchange_rate"] or "1"
        return cls(**values)

    def as_row(self) -> dict[str, str]:
        return asdict(self)

    @property
    def numeric_amount(self) -> Decimal:
        return Decimal(self.amount)

    @property
    def is_reviewable(self) -> bool:
        return self.needs_review == "true"


def fingerprint_for(parsed: ParsedTransaction) -> str:
    source_key = "|".join([
        parsed.source, parsed.account, parsed.posted_date.isoformat(),
        parsed.currency.upper(), parsed.original_amount or f"{abs(parsed.signed_amount):.2f}",
        normalise_text(parsed.original_description),
        normalise_text(parsed.raw_reference),
    ])
    return sha256(source_key.encode()).hexdigest()


def transaction_from_parsed(
    parsed: ParsedTransaction,
    *, kind: str,
    category: str,
    classification_source: str,
    needs_review: bool,
    raw_source: str,
) -> Transaction:
    fingerprint = fingerprint_for(parsed)
    return Transaction(
        id=fingerprint[:24], fingerprint=fingerprint, source=parsed.source,
        account=parsed.account, posted_date=parsed.posted_date.isoformat(),
        description=parsed.description, original_description=parsed.original_description,
        amount=f"{abs(parsed.signed_amount):.2f}",
        direction="income" if parsed.signed_amount > 0 else "expense", kind=kind,
        category=category, classification_source=classification_source,
        needs_review=str(needs_review).lower(), raw_source=raw_source,
        raw_row=str(parsed.raw_row),
        original_amount=parsed.original_amount or f"{abs(parsed.signed_amount):.2f}",
        currency=parsed.currency, exchange_rate=parsed.exchange_rate,
    )


def validate_category(kind: str, category: str) -> str:
    allowed = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
    if category not in allowed:
        raise ValueError(f"{category!r} is not a valid {kind} category")
    return category


def valid_categories(kind: str) -> Iterable[str]:
    return INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES

