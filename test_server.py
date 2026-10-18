import time
import server


def call_tool(t, **kwargs):
    fn = getattr(t, "fn", t)
    return fn(**kwargs)


def test_validation():
    try:
        server.validate_expense({"category": "Too Many Words", "description": "x", "flow_type": "spent", "amount": 10})
        assert False, "Expected ValueError for >2 word category"
    except ValueError as e:
        assert "1 or 2 words" in str(e)

    nil_row = server.validate_expense({"category": "window shopping", "description": "Looked around", "flow_type": "nil", "amount": 999})
    assert nil_row["category"] == "Window Shopping"
    assert nil_row["amount"] == 0.0


def test_user_isolation_and_crud():
    ts = int(time.time() * 1000)
    email_a = f"fastmcp_a_{ts}@test.local"
    email_b = f"fastmcp_b_{ts}@test.local"

    res_a = call_tool(server.signup_user, email=email_a, password="Password123!", full_name="Alice", monthly_income=19800)
    res_b = call_tool(server.signup_user, email=email_b, password="Password123!", full_name="Bob", monthly_income=25000)
    key_a = res_a["mcp_api_key"]
    key_b = res_b["mcp_api_key"]
    assert key_a.startswith("mcp_") and key_b.startswith("mcp_") and key_a != key_b

    exp = call_tool(
        server.add_expense,
        category="Dinner",
        description="Paneer tikka & roti",
        flow_type="spent",
        amount=220,
        spent_where="Zomato",
        expense_date="2026-10-18",
        exact_time="21:30:00",
        api_key=key_a,
    )
    assert exp["user_id"] == res_a["user"]["id"]

    bob_list = call_tool(server.list_expenses, api_key=key_b)
    assert bob_list["count"] == 0

    stats = call_tool(server.get_expense_analytics, month="2026-10", api_key=key_a)
    assert stats["monthly_income"] == 19800.0
    assert stats["total_spent"] == 220.0
    assert stats["salary_remaining"] == 19580.0

    deleted = call_tool(server.delete_expense, id=exp["id"], api_key=key_a)
    assert deleted["id"] == exp["id"]


if __name__ == "__main__":
    test_validation()
    print("[OK] FastMCP validation passed")
    test_user_isolation_and_crud()
    print("[OK] FastMCP user signup, RLS isolation, analytics, and CRUD passed")
