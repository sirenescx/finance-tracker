from __future__ import annotations

import argparse
from pathlib import Path

from .services import seed_workbook
from .storage import DataStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the finance tracker from an existing workbook.")
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    result = seed_workbook(DataStore(args.data_dir), args.workbook)
    print(f"Seeded {result['imported']} transactions and {result['rules']} merchant rules.")


if __name__ == "__main__":
    main()

