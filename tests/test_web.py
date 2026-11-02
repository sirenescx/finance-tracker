from __future__ import annotations

from fastapi.testclient import TestClient

from app import main
from app.storage import DataStore


def test_web_pages_and_upload(tmp_path) -> None:
    main.store = DataStore(tmp_path / "data")
    client = TestClient(main.app)
    for path in ["/", "/transactions", "/import", "/review", "/budgets", "/settings"]:
        response = client.get(path)
        assert response.status_code == 200
    content = b'Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\nCard Payment,Current,2026-10-01 10:00:00,2026-10-01 11:00:00,Coffee,-3.50,0,EUR,COMPLETED,10\n'
    response = client.post("/import", files={"file": ("statement.csv", content, "text/csv")}, follow_redirects=True)
    assert response.status_code == 200
    assert "Imported 1" in response.text
    assert "Coffee" in client.get("/review").text
    assert "All transactions" in client.get("/review?show_all=true").text
    transactions_page = client.get("/transactions")
    assert "Category for Coffee" in transactions_page.text
    assert "refreshes every 60 seconds" not in transactions_page.text
    response = client.post("/budgets", data={"budget_expense_Food & Drink": "50", "budget_income_Salary": "100"}, follow_redirects=True)
    assert "Budgets saved" in response.text
    response = client.post("/settings/rules", data={"pattern": r"^Payment from .*$", "match_type": "regex", "kind": "income", "category": "Other"}, follow_redirects=True)
    assert "Merchant rule saved" in response.text
