# Finance Tracker

A local FastAPI app for importing ING and Revolut statement CSVs, automatically
categorising transactions, and viewing monthly spending and income.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m app.seed_workbook '/Users/mmanakhova/Downloads/Expense + Income Tracker 2026.xlsx'
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. All personal data is written under `data/`, which
is intentionally ignored by Git. The seed command is safe to repeat: it skips
the workbook if it was imported already.

## Import behavior

- Supports the ING and Revolut CSV headers supplied in `Statements/`.
- Copies each uploaded statement unchanged to `data/uploads/` and records its
  SHA-256 hash in `data/imports.json`.
- Uses a normalized CSV ledger and JSON rule/budget files instead of a database.
- Detects re-imported or overlapping rows without combining transactions from
  separate accounts.
- Marks unrecognised transactions as `Other` and sends them to the review queue.

