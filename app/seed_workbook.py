from __future__ import annotations

import argparse
from pathlib import Path

from .services import seed_workbook
from .storage import DataStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the finance tracker from an existing workbook.")
    parser.add_argument(
        "workbook", type=Path, nargs="?",
        help="workbook to import (defaults to the only .xlsx file in <data-dir>/historical/)",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    workbook = args.workbook
    if workbook is None:
        historical_dir = args.data_dir / "historical"
        candidates = sorted(historical_dir.glob("*.xlsx")) if historical_dir.is_dir() else []
        if len(candidates) != 1:
            if not candidates:
                parser.error(f"No .xlsx workbook found in {historical_dir}. Put one there or pass a workbook path.")
            names = ", ".join(candidate.name for candidate in candidates)
            parser.error(f"Found multiple .xlsx workbooks in {historical_dir}: {names}. Pass the one to import explicitly.")
        workbook = candidates[0]

    result = seed_workbook(DataStore(args.data_dir), workbook)
    print(f"Workbook: {workbook}")
    print(f"Seeded {result['imported']} historical transactions; no merchant rules were created.")


if __name__ == "__main__":
    main()

