from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterable

from .constants import DEFAULT_TRANSFER_RULES, LEDGER_FIELDS
from .domain import Transaction


class DataStore:
    def __init__(self, root: Path | str = "data") -> None:
        self.root = Path(root)
        self.uploads = self.root / "uploads"
        self.ledger_path = self.root / "transactions.csv"
        self.imports_path = self.root / "imports.json"
        self.rules_path = self.root / "merchant_rules.json"
        self.budgets_path = self.root / "budgets.json"
        self.transfers_path = self.root / "transfer_rules.json"
        self.fx_rates_path = self.root / "fx_rates.json"

    def initialise(self) -> None:
        self.uploads.mkdir(parents=True, exist_ok=True)
        if not self.ledger_path.exists():
            with self.ledger_path.open("w", newline="", encoding="utf-8") as stream:
                csv.DictWriter(stream, fieldnames=LEDGER_FIELDS).writeheader()
        else:
            with self.ledger_path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != LEDGER_FIELDS:
                    rows = list(reader)
                    with self.ledger_path.open("w", newline="", encoding="utf-8") as migrated:
                        writer = csv.DictWriter(migrated, fieldnames=LEDGER_FIELDS)
                        writer.writeheader()
                        for row in rows:
                            migrated_row = {field: row.get(field, "") for field in LEDGER_FIELDS}
                            migrated_row["currency"] = migrated_row["currency"] or "EUR"
                            migrated_row["exchange_rate"] = migrated_row["exchange_rate"] or "1"
                            migrated_row["original_amount"] = migrated_row["original_amount"] or migrated_row.get("amount", "")
                            writer.writerow(migrated_row)
        self._ensure_json(self.imports_path, [])
        self._ensure_json(self.rules_path, {})
        self._ensure_json(self.budgets_path, {})
        self._ensure_json(self.transfers_path, DEFAULT_TRANSFER_RULES)
        self._ensure_json(self.fx_rates_path, {})

    def _ensure_json(self, path: Path, default: object) -> None:
        if not path.exists():
            path.write_text(json.dumps(default, indent=2) + "\n", encoding="utf-8")

    def _read_json(self, path: Path, default: object) -> object:
        self.initialise()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def _write_json(self, path: Path, value: object) -> None:
        self.initialise()
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def transactions(self) -> list[Transaction]:
        self.initialise()
        with self.ledger_path.open(newline="", encoding="utf-8") as stream:
            return [Transaction.from_row(row) for row in csv.DictReader(stream)]

    def append_transactions(self, transactions: Iterable[Transaction]) -> None:
        self.initialise()
        with self.ledger_path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=LEDGER_FIELDS)
            writer.writerows(transaction.as_row() for transaction in transactions)

    def replace_transactions(self, transactions: Iterable[Transaction]) -> None:
        self.initialise()
        with self.ledger_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=LEDGER_FIELDS)
            writer.writeheader()
            writer.writerows(transaction.as_row() for transaction in transactions)

    def existing_fingerprints(self) -> set[str]:
        return {transaction.fingerprint for transaction in self.transactions()}

    def merchant_rules(self) -> dict[str, dict[str, str]]:
        return self._read_json(self.rules_path, {})  # type: ignore[return-value]

    def save_merchant_rule(self, merchant: str, kind: str, category: str, match_type: str = "contains") -> None:
        rules = self.merchant_rules()
        rules[merchant] = {"kind": kind, "category": category, "match_type": match_type}
        self._write_json(self.rules_path, rules)

    def budgets(self) -> dict[str, dict[str, str]]:
        return self._read_json(self.budgets_path, {})  # type: ignore[return-value]

    def save_budgets(self, budgets: dict[str, dict[str, str]]) -> None:
        self._write_json(self.budgets_path, budgets)

    def transfer_rules(self) -> list[str]:
        return self._read_json(self.transfers_path, DEFAULT_TRANSFER_RULES)  # type: ignore[return-value]

    def save_transfer_rules(self, rules: list[str]) -> None:
        self._write_json(self.transfers_path, rules)

    def fx_rates(self) -> dict[str, dict[str, str]]:
        return self._read_json(self.fx_rates_path, {})  # type: ignore[return-value]

    def fx_rate(self, currency: str, day: str) -> str | None:
        if currency.upper() == "EUR":
            return "1"
        return self.fx_rates().get(day, {}).get(currency.upper())

    def save_fx_rate(self, day: str, currency: str, eur_per_unit: str) -> None:
        rates = self.fx_rates()
        rates.setdefault(day, {})[currency.upper()] = eur_per_unit
        self._write_json(self.fx_rates_path, rates)

    def delete_fx_rate(self, day: str, currency: str) -> bool:
        rates = self.fx_rates()
        day_rates = rates.get(day)
        if not day_rates or currency.upper() not in day_rates:
            return False
        del day_rates[currency.upper()]
        if not day_rates:
            del rates[day]
        self._write_json(self.fx_rates_path, rates)
        return True

    def imports(self) -> list[dict[str, object]]:
        return self._read_json(self.imports_path, [])  # type: ignore[return-value]

    def record_import(self, filename: str, content: bytes, summary: dict[str, int]) -> str:
        self.initialise()
        digest = sha256(content).hexdigest()
        extension = Path(filename).suffix.lower() or ".csv"
        raw_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}{extension}"
        destination = self.uploads / raw_name
        if not destination.exists():
            destination.write_bytes(content)
        imports = self.imports()
        imports.append({"filename": filename, "stored_as": raw_name, "sha256": digest,
                        "imported_at": datetime.now(timezone.utc).isoformat(), **summary})
        self._write_json(self.imports_path, imports)
        return raw_name

    def has_import_hash(self, content: bytes) -> bool:
        digest = sha256(content).hexdigest()
        return any(entry.get("sha256") == digest for entry in self.imports())

