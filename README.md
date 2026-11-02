# Finance Tracker

A local FastAPI app for importing ING and Revolut statement CSVs, automatically
categorising transactions, and viewing monthly spending and income.

## What it does

Finance Tracker turns monthly bank CSV exports into one searchable transaction
ledger. It recognizes ING and Revolut formats, skips duplicate rows, applies
merchant and transfer rules, and sends uncategorized transactions to a review
queue. You can correct categories, add transactions manually, and update or
delete transactions from the ledger.

The dashboard summarizes monthly income, spending, essential spending, savings,
and category budgets, with charts for category and bank totals. Transfers and
currency exchanges remain visible in the ledger but are excluded from reports.
Foreign-currency Revolut activity can be converted using exchange rates entered
by day in Settings.

The app runs locally without a database, login, or cloud sync. Its ledger,
uploaded statements, rules, budgets, and rates are kept as files in the
Git-ignored `data/` directory.

## Pages

- **Dashboard** — yearly monthly expense and income charts, category totals,
  bank spending, savings, essential spending, and budget comparisons.
- **Transactions** — a searchable year view of the full ledger with sortable
  columns, category edits, and transaction deletion.
- **Import** — upload ING or Revolut CSV files and review import history,
  including imported, skipped, transfer, and review counts.
- **Review** — categorize transactions that need attention and optionally
  save a merchant rule for future imports.
- **Budgets** — set monthly budgets by expense category and income type.
- **Settings** — manage daily foreign-currency rates, transfer descriptions,
  and learned merchant rules.

## Screenshots

The screenshots below show the app’s main pages. Settings is shown in two
screenshots so both the exchange/transfer controls and merchant rules are visible.

### Dashboard

![Dashboard with yearly totals and expense charts](docs/screenshots/Screenshot%202026-11-03%20at%2000.27.01.png)

### All transactions

![Sortable all-transactions table with category selectors](docs/screenshots/Screenshot%202026-11-03%20at%2000.32.20.png)

### Import

![CSV statement import form and import history](docs/screenshots/Screenshot%202026-11-03%20at%2000.32.28.png)

### Review

![Transaction review queue](docs/screenshots/Screenshot%202026-11-03%20at%2000.32.34.png)

### Budgets

![Monthly expense and income budget settings](docs/screenshots/Screenshot%202026-11-03%20at%2000.32.49.png)

### Settings

![Exchange rates and transfer description settings](docs/screenshots/Screenshot%202026-11-03%20at%2000.33.15.png)

![Merchant rule creation and learned rules](docs/screenshots/Screenshot%202026-11-03%20at%2000.33.23.png)

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m app.seed_workbook
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Put the historical workbook in `data/historical/`. With no workbook argument,
the seed command imports the only `.xlsx` file in that folder. If there is more
than one, pass the desired workbook path explicitly, for example:
`.venv/bin/python -m app.seed_workbook 'data/historical/history.xlsx'`.
Seeding is additive and safe to repeat: previously imported rows are skipped;
it does not replace or delete existing ledger data.

Open <http://127.0.0.1:8000>. All personal data is written under `data/`, which
is intentionally ignored by Git. The seed command is safe to repeat: it skips
the workbook if it was imported already. It imports historical transactions only;
merchant rules are learned from future review corrections.

## Import behavior

- Supports the ING and Revolut CSV headers supplied in `Statements/`.
- Copies each uploaded statement unchanged to `data/uploads/` and records its
  SHA-256 hash in `data/imports.json`.
- Uses a normalized CSV ledger and JSON rule/budget files instead of a database.
- Detects re-imported or overlapping rows without combining transactions from
  separate accounts.
- Marks unrecognised transactions as `Other` and sends them to the review queue.

