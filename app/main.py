from __future__ import annotations

import json
import os
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .constants import EXPENSE_CATEGORIES, INCOME_CATEGORIES
from .domain import UnsupportedStatement, validate_category
from .services import dashboard, import_csv, mark_transfer, review_transaction
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
    selected_year = year or date.today().year
    report = dashboard(store, selected_year)
    return templates.TemplateResponse(request, "dashboard.html", context(
        request, report=report, chart_json=json.dumps(report["chart_data"]), message=request.query_params.get("message"),
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
    except UnsupportedStatement as exc:
        return RedirectResponse(f"/import?message={str(exc).replace(' ', '+')}", status_code=303)
    message = "Imported+{imported};+skipped+{skipped};+transfers+{transfers};+review+{review}.".format(**result)
    return RedirectResponse(f"/import?message={message}", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, show_all: bool = False) -> HTMLResponse:
    transactions = sorted((item for item in store.transactions() if show_all or item.is_reviewable),
                          key=lambda item: item.posted_date, reverse=True)
    return templates.TemplateResponse(request, "review.html", context(request, transactions=transactions,
                                      show_all=show_all, message=request.query_params.get("message")))


@app.post("/review/{transaction_id}")
def review_item(transaction_id: str, kind: str = Form(...), category: str = Form(...), save_rule: bool = Form(False)) -> RedirectResponse:
    try:
        review_transaction(store, transaction_id, kind, category, save_rule)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/review?message=Transaction+updated.", status_code=303)


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
        message=request.query_params.get("message"),
    ))


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

