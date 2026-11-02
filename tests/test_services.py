from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.domain import ParsedTransaction, UnsupportedStatement, parse_euro
from app.importers import parse_statement
from app.services import classify, dashboard, import_csv, review_transaction, seed_workbook
from app.storage import DataStore


ING = '"Date","Name / Description","Account","Counterparty","Code","Debit/credit","Amount (EUR)","Transaction type","Notifications"\n"20261001","Albert Heijn","NL01","","BA","Debit","12,50","Card",""\n"20261002","Booking.com","NL01","","OV","Credit","100,00","Transfer","Wage/Salary"\n'
REVOLUT = 'Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\nCard Payment,Current,2026-10-01 10:00:00,2026-10-01 11:00:00,Coffee,-3.50,0,EUR,COMPLETED,10\n'


def test_parsers_handle_amount_direction_and_headers() -> None:
    bank, rows = parse_statement(ING.encode())
    assert bank == "ING"
    assert rows[0].signed_amount == Decimal("-12.50")
    assert rows[1].signed_amount == Decimal("100.00")
    bank, rows = parse_statement(REVOLUT.encode())
    assert bank == "Revolut" and rows[0].posted_date == date(2026, 10, 1)
    with pytest.raises(UnsupportedStatement):
        parse_statement(b"a,b\n1,2\n")


def test_import_dedupes_and_keeps_accounts_separate(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "data")
    result = import_csv(store, "ing.csv", ING.encode())
    assert result == {"imported": 2, "skipped": 0, "transfers": 0, "review": 0}
    assert import_csv(store, "again.csv", ING.encode())["skipped"] == 2
    assert len(store.transactions()) == 2
    report = dashboard(store, 2026)
    assert report["data"]["expense"]["Groceries"][9] == Decimal("12.50")
    assert report["data"]["income"]["Salary"][9] == Decimal("100.00")


def test_revolut_keeps_identical_same_day_purchases_with_distinct_start_times(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "data")
    rows = REVOLUT + 'Card Payment,Current,2026-10-01 12:00:00,2026-10-01 13:00:00,Coffee,-3.50,0,EUR,COMPLETED,6.5\n'
    assert import_csv(store, "revolut.csv", rows.encode())["imported"] == 2


def test_fallback_review_and_learned_rule(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "data")
    parsed = ParsedTransaction("ING", "A", date(2026, 1, 1), "New Shop", "New Shop", Decimal("-5"), 2)
    assert classify(parsed, store) == ("expense", "Other", "fallback", True)
    import_csv(store, "revolut.csv", REVOLUT.encode())
    transaction = store.transactions()[0]
    review_transaction(store, transaction.id, "expense", "Food & Drink", True)
    assert classify(ParsedTransaction("ING", "A", date(2026, 1, 2), "Coffee", "Coffee", Decimal("-4"), 2), store)[2] == "merchant_rule"


def test_regex_merchant_rule_matches_case_insensitively(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "data")
    store.save_merchant_rule(r"^Payment from .*$", "income", "Other", "regex")
    parsed = ParsedTransaction("Revolut", "Current", date(2026, 1, 2), "Payment from Alex", "Payment from Alex", Decimal("12"), 2)
    assert classify(parsed, store) == ("income", "Other", "merchant_rule", False)


def test_transfer_excluded_and_seed_workbook(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "data")
    transfer = ING.replace("Albert Heijn", "Mariia Manakhova")
    result = import_csv(store, "transfer.csv", transfer.encode())
    assert result["transfers"] == 1
    assert dashboard(store, 2026)["data"]["totals"]["expenses"][9] == Decimal("0")
    workbook = Workbook()
    expenses = workbook.active; expenses.title = "Expenses"; expenses.append(["Timestamp", "Purchase Date", "Item", "Amount", "Category"]); expenses.append([None, date(2026, 1, 3), "Legacy Shop", 8, "Shopping"])
    income = workbook.create_sheet("Income"); income.append(["Timestamp", "Date", "Income Source", "Description/Invoice No.", "Income Amount"]); income.append([None, date(2026, 1, 4), "Salary", "Legacy Employer", 1000])
    path = tmp_path / "seed.xlsx"; workbook.save(path)
    seeded = seed_workbook(store, path)
    assert seeded["imported"] == 2 and seeded["rules"] == 0
    assert store.merchant_rules() == {}

