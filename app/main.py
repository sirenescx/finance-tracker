from __future__ import annotations

import json
import os
import re
from urllib.parse import quote_plus
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .constants import EXPENSE_CATEGORIES, INCOME_CATEGORIES
from .domain import MissingExchangeRate, UnsupportedStatement, validate_category
from .services import add_manual_transaction, dashboard, delete_transaction, import_csv, mark_transfer, review_transaction, sync_transactions_to_rules
from .storage import DataStore

BASE_DIR = Path(__file__).resolve().parent
store = DataStore(os.environ.get("FINANCE_TRACKER_DATA_DIR", "data"))
app = FastAPI(title="Finance Tracker", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def money(value: Decimal | float | int | str) -> str:
    return f"€{Decimal(str(value)):,.2f}"


templates.env.filters["money"] = money
templates.env.filters["category_class"] = lambda value: "category-" + re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def context(request: Request, **kwargs: object) -> dict[str, object]:
    return {"request": request, "expense_categories": EXPENSE_CATEGORIES,
            "income_categories": INCOME_CATEGORIES, **kwargs}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, year: int | None = None) -> HTMLResponse:
    sync_transactions_to_rules(store)
    selected_year = year or date.today().year
    years = sorted(
        {int(item.posted_date[:4]) for item in store.transactions() if item.posted_date[:4].isdigit()}
        | {date.today().year, selected_year},
        reverse=True,
    )
    report = dashboard(store, selected_year)
    return templates.TemplateResponse(request, "dashboard.html", context(
        request, report=report, years=years, chart_json=json.dumps(report["chart_data"]), message=request.query_params.get("message"),
    ))


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "import.html", context(request, imports=store.imports(), message=request.query_params.get("message")))


@app.post("/import")
async def import_statement(file: UploadFile = File(...)) -> RedirectResponse:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return RedirectResponse("/import?message=Please+choose+a+CSV+file.", status_code=303)
    content = await file.read()
    if not content or len(content) > 10 * 1024 * 1024:
        return RedirectResponse("/import?message=The+file+must+be+between+1+byte+and+10+MB.", status_code=303)
    try:
        result = import_csv(store, file.filename, content)
    except MissingExchangeRate as exc:
        return RedirectResponse(f"/settings?message={quote_plus(str(exc))}", status_code=303)
    except UnsupportedStatement as exc:
        return RedirectResponse(f"/import?message={quote_plus(str(exc))}", status_code=303)
    message = "Imported+{imported};+updated+{updated};+skipped+{skipped};+transfers+{transfers};+review+{review}.".format(**result)
    return RedirectResponse(f"/import?message={message}", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, show_all: bool = False) -> HTMLResponse:
    sync_transactions_to_rules(store)
    transactions = sorted((item for item in store.transactions() if show_all or item.is_reviewable),
                          key=lambda item: item.posted_date, reverse=True)
    return templates.TemplateResponse(request, "review.html", context(request, transactions=transactions,
                                      show_all=show_all, message=request.query_params.get("message")))


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request, year: int | None = None) -> HTMLResponse:
    sync_transactions_to_rules(store)
    all_transactions = store.transactions()
    years = sorted({int(item.posted_date[:4]) for item in all_transactions if item.posted_date[:4].isdigit()}, reverse=True)
    selected_year = year or (years[0] if years else date.today().year)
    transactions = sorted((item for item in all_transactions if item.posted_date.startswith(str(selected_year))),
                          key=lambda item: (item.posted_date, item.description.lower()), reverse=True)
    return templates.TemplateResponse(request, "transactions.html", context(
        request, transactions=transactions, years=years, selected_year=selected_year,
        message=request.query_params.get("message"),
    ))


@app.get("/transactions/new", response_class=HTMLResponse)
def new_transaction_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "transaction_new.html", context(
        request, today=date.today().isoformat(), message=request.query_params.get("message"),
    ))


@app.post("/transactions/new")
def create_manual_transaction(
    posted_date: date = Form(...), description: str = Form(...), amount: str = Form(...),
    kind: str = Form(...), category: str = Form(...), account: str = Form("Manual"),
) -> RedirectResponse:
    try:
        transaction = add_manual_transaction(store, posted_date, description, Decimal(amount.replace(",", ".")), kind, category, account)
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/transactions?year={transaction.posted_date[:4]}&message=Transaction+added.", status_code=303)


@app.post("/review/{transaction_id}")
def review_item(transaction_id: str, kind: str = Form(...), category: str = Form(...), save_rule: bool = Form(False)) -> RedirectResponse:
    try:
        review_transaction(store, transaction_id, kind, category, save_rule)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/review?message=Transaction+updated.", status_code=303)


@app.post("/transactions/{transaction_id}/category")
def change_transaction_category(transaction_id: str, kind: str = Form(...), category: str = Form(...), year: int = Form(...)) -> RedirectResponse:
    try:
        review_transaction(store, transaction_id, kind, category, save_rule=False)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/transactions?year={year}&message=Category+updated.", status_code=303)


@app.post("/transactions/{transaction_id}/delete")
def remove_transaction(transaction_id: str, year: int = Form(...)) -> RedirectResponse:
    try:
        delete_transaction(store, transaction_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Transaction not found") from exc
    return RedirectResponse(f"/transactions?year={year}&message=Transaction+deleted.", status_code=303)


@app.post("/review/{transaction_id}/transfer")
def transfer_item(transaction_id: str, save_rule: bool = Form(False)) -> RedirectResponse:
    try:
        mark_transfer(store, transaction_id, save_rule)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Transaction not found") from exc
    return RedirectResponse("/review?message=Transaction+marked+as+a+transfer.", status_code=303)


@app.get("/budgets", response_class=HTMLResponse)
def budgets_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "budgets.html", context(request, budgets=store.budgets(),
                                      message=request.query_params.get("message")))


@app.post("/budgets")
async def save_budgets(request: Request) -> RedirectResponse:
    form = await request.form()
    budgets: dict[str, dict[str, str]] = {"expense": {}, "income": {}}
    for kind, categories in (("expense", EXPENSE_CATEGORIES), ("income", INCOME_CATEGORIES)):
        for category in categories:
            value = str(form.get(f"budget_{kind}_{category}", "")).strip().replace(",", ".")
            if value:
                try:
                    amount = Decimal(value)
                except Exception:
                    return RedirectResponse("/budgets?message=Budgets+must+be+numbers.", status_code=303)
                if amount < 0:
                    return RedirectResponse("/budgets?message=Budgets+cannot+be+negative.", status_code=303)
                budgets[kind][category] = f"{amount:.2f}"
    store.save_budgets(budgets)
    return RedirectResponse("/budgets?message=Budgets+saved.", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", context(request,
        transfer_rules="\n".join(store.transfer_rules()), merchant_rules=store.merchant_rules(),
        fx_rates=store.fx_rates(),
        message=request.query_params.get("message"),
    ))


@app.post("/settings/fx-rates")
def save_exchange_rate(day: str = Form(...), currency: str = Form(...), eur_per_unit: str = Form(...)) -> RedirectResponse:
    try:
        date.fromisoformat(day)
        currency = currency.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency) or currency == "EUR":
            raise ValueError("Currency must be a three-letter code other than EUR.")
        rate = Decimal(eur_per_unit.replace(",", "."))
        if not rate.is_finite() or rate <= 0:
            raise ValueError("Rate must be a positive number.")
    except (ValueError, ArithmeticError) as exc:
        return RedirectResponse(f"/settings?message={quote_plus(str(exc))}", status_code=303)
    store.save_fx_rate(day, currency, f"{rate:.8f}".rstrip("0").rstrip("."))
    return RedirectResponse("/settings?message=Exchange+rate+saved.", status_code=303)


@app.post("/settings/fx-rates/delete")
def delete_exchange_rate(day: str = Form(...), currency: str = Form(...)) -> RedirectResponse:
    try:
        date.fromisoformat(day)
        currency = currency.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency) or currency == "EUR":
            raise ValueError("Currency must be a three-letter code other than EUR.")
    except ValueError as exc:
        return RedirectResponse(f"/settings?message={quote_plus(str(exc))}", status_code=303)
    if not store.delete_fx_rate(day, currency):
        return RedirectResponse("/settings?message=Exchange+rate+not+found.", status_code=303)
    return RedirectResponse("/settings?message=Exchange+rate+removed.", status_code=303)


@app.post("/settings/transfers")
def save_transfer_settings(transfer_rules: str = Form("")) -> RedirectResponse:
    rules = [line.strip().lower() for line in transfer_rules.splitlines() if line.strip()]
    store.save_transfer_rules(rules)
    return RedirectResponse("/settings?message=Transfer+rules+saved.", status_code=303)


@app.post("/settings/rules")
def save_merchant_rule(
    pattern: str = Form(...), match_type: str = Form("contains"), kind: str = Form(...), category: str = Form(...),
) -> RedirectResponse:
    pattern = pattern.strip()
    if not pattern or match_type not in {"contains", "regex"}:
        raise HTTPException(status_code=400, detail="Provide a rule pattern and a valid match type.")
    try:
        validate_category(kind, category)
        if match_type == "regex":
            re.compile(pattern, flags=re.IGNORECASE)
    except (ValueError, re.error) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid merchant rule: {exc}") from exc
    store.save_merchant_rule(pattern.lower() if match_type == "contains" else pattern, kind, category, match_type)
    return RedirectResponse("/settings?message=Merchant+rule+saved.", status_code=303)

