import calendar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastmcp import FastMCP


def _load_dotenv() -> None:
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv()

mcp = FastMCP("expense-tracker")

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_MONTHLY_INCOME = 19800.0
_session_mcp_key: str = os.environ.get("MCP_API_KEY", "").strip()


def _supabase_config() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_ANON_KEY environment variables must be set.")
    return url, key


def now_ist() -> tuple[str, str]:
    dt = datetime.now(IST)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")


def normalize_time(t: Optional[str]) -> str:
    if not t or not str(t).strip():
        return now_ist()[1]
    s = str(t).strip()
    if re.fullmatch(r"\d{2}:\d{2}", s):
        return f"{s}:00"
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", s):
        return s
    raise ValueError("exact_time must be in HH:MM or HH:MM:SS format")


def validate_expense(data: dict[str, Any], is_update: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    ist_date, ist_time = now_ist()

    if "expense_date" in data or not is_update:
        d = str(data.get("expense_date") or ist_date).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise ValueError("expense_date must be YYYY-MM-DD")
        out["expense_date"] = d

    if "exact_time" in data or not is_update:
        out["exact_time"] = normalize_time(data.get("exact_time") or ist_time)

    if "category" in data or not is_update:
        cat = str(data.get("category") or "").strip()
        words = [w for w in re.split(r"\s+", cat) if w]
        if not words or len(words) > 2:
            raise ValueError("category is required and must be 1 or 2 words (e.g. Dinner, Investments, Lunch, Party)")
        out["category"] = " ".join(w.capitalize() for w in words)

    if "flow_type" in data or not is_update:
        ft = str(data.get("flow_type") or "spent").strip().lower()
        if ft not in ("spent", "received", "nil"):
            raise ValueError("flow_type must be 'spent', 'received', or 'nil'")
        out["flow_type"] = ft

    if "amount" in data or not is_update:
        ft = out.get("flow_type", "spent")
        amt = 0.0 if ft == "nil" else float(data.get("amount") or 0.0)
        if amt < 0:
            raise ValueError("amount must be non-negative")
        out["amount"] = 0.0 if ft == "nil" else round(amt, 2)

    if "description" in data or not is_update:
        desc = str(data.get("description") or "").strip()
        if not is_update and not desc:
            raise ValueError("description is required to justify the category")
        out["description"] = desc

    if "spent_where" in data or not is_update:
        out["spent_where"] = str(data.get("spent_where") or "").strip()

    if "received_from" in data or not is_update:
        out["received_from"] = str(data.get("received_from") or "").strip()

    if is_update:
        out["updated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def _http_json(url: str, method: str = "GET", headers: Optional[dict[str, str]] = None, body: Any = None) -> Any:
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=raw, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        err_text = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_text)
            msg = err_json.get("msg") or err_json.get("error_description") or err_json.get("message") or err_text
        except Exception:
            msg = err_text
        raise RuntimeError(f"Supabase error ({e.code}): {msg}") from e


def _auth_headers(token: Optional[str] = None) -> dict[str, str]:
    _, anon_key = _supabase_config()
    tok = (token or _session_mcp_key or os.environ.get("MCP_API_KEY", "")).strip()
    if not tok:
        raise ValueError("Authentication required. Call signin_user / signup_user first, pass api_key, or set MCP_API_KEY.")
    headers = {
        "apikey": anon_key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if tok.startswith("mcp_"):
        headers["Authorization"] = f"Bearer {anon_key}"
        headers["x-mcp-api-key"] = tok
    else:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _sb_rest(endpoint: str, method: str = "GET", body: Any = None, token: Optional[str] = None) -> Any:
    base_url, _ = _supabase_config()
    return _http_json(f"{base_url}/rest/v1/{endpoint}", method=method, headers=_auth_headers(token), body=body)


def _get_profile(token: Optional[str] = None) -> dict[str, Any]:
    rows = _sb_rest("users?select=*", method="GET", token=token)
    if not rows:
        raise ValueError("Invalid or expired authentication token / MCP API key.")
    return rows[0]


@mcp.tool()
def signup_user(
    email: str,
    password: str,
    full_name: str,
    monthly_income: float = DEFAULT_MONTHLY_INCOME,
) -> dict[str, Any]:
    """Create a new user account with email, password (min 8 chars), full_name, and monthly_income (default 19800)."""
    global _session_mcp_key
    clean_email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", clean_email):
        raise ValueError("Valid email address is required")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if monthly_income < 0:
        raise ValueError("monthly_income must be non-negative")

    base_url, anon_key = _supabase_config()
    data = _http_json(
        f"{base_url}/auth/v1/signup",
        method="POST",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        body={
            "email": clean_email,
            "password": password,
            "data": {"full_name": full_name.strip() or clean_email.split("@")[0], "monthly_income": round(monthly_income, 2)},
        },
    )
    profile = _get_profile(data.get("access_token"))
    _session_mcp_key = profile["mcp_api_key"]
    return {"user": profile, "mcp_api_key": profile["mcp_api_key"]}


@mcp.tool()
def signin_user(email: str, password: str) -> dict[str, Any]:
    """Sign in with email and password to activate your session and retrieve your personal mcp_api_key."""
    global _session_mcp_key
    clean_email = email.strip().lower()
    base_url, anon_key = _supabase_config()
    data = _http_json(
        f"{base_url}/auth/v1/token?grant_type=password",
        method="POST",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        body={"email": clean_email, "password": password},
    )
    profile = _get_profile(data.get("access_token"))
    _session_mcp_key = profile["mcp_api_key"]
    return {"user": profile, "mcp_api_key": profile["mcp_api_key"]}


@mcp.tool()
def add_expense(
    category: str,
    description: str,
    flow_type: str = "spent",
    amount: float = 0.0,
    spent_where: str = "",
    received_from: str = "",
    expense_date: str = "",
    exact_time: str = "",
    api_key: str = "",
) -> dict[str, Any]:
    """Add a day-to-day expense, received money, or nil entry (category must be 1-2 words like Dinner, Lunch, Investments, Party)."""
    _get_profile(api_key or None)
    row = validate_expense(
        {
            "category": category,
            "description": description,
            "flow_type": flow_type,
            "amount": amount,
            "spent_where": spent_where,
            "received_from": received_from,
            "expense_date": expense_date or None,
            "exact_time": exact_time or None,
        },
        is_update=False,
    )
    created = _sb_rest("expenses", method="POST", body=row, token=api_key or None)
    return created[0]


@mcp.tool()
def list_expenses(
    date: str = "",
    month: str = "",
    category: str = "",
    flow_type: str = "",
    spent_where: str = "",
    received_from: str = "",
    limit: int = 200,
    api_key: str = "",
) -> dict[str, Any]:
    """List expenses for the authenticated user with exact date, time, category, description, spent_where, and received_from."""
    _get_profile(api_key or None)
    params: list[tuple[str, str]] = [
        ("select", "*"),
        ("order", "expense_date.desc,exact_time.desc"),
        ("limit", str(min(max(1, limit), 1000))),
    ]
    if date.strip():
        params.append(("expense_date", f"eq.{date.strip()}"))
    if month.strip():
        y, m = map(int, month.strip().split("-"))
        next_m = f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01"
        params.append(("expense_date", f"gte.{month.strip()}-01"))
        params.append(("expense_date", f"lt.{next_m}"))
    if category.strip():
        params.append(("category", f"ilike.{category.strip()}"))
    if flow_type.strip():
        params.append(("flow_type", f"eq.{flow_type.strip().lower()}"))
    if spent_where.strip():
        params.append(("spent_where", f"ilike.*{spent_where.strip()}*"))
    if received_from.strip():
        params.append(("received_from", f"ilike.*{received_from.strip()}*"))

    qs = urllib.parse.urlencode(params)
    rows = _sb_rest(f"expenses?{qs}", method="GET", token=api_key or None)
    return {"count": len(rows), "expenses": rows}


@mcp.tool()
def update_expense(
    id: str,
    category: Optional[str] = None,
    description: Optional[str] = None,
    flow_type: Optional[str] = None,
    amount: Optional[float] = None,
    spent_where: Optional[str] = None,
    received_from: Optional[str] = None,
    expense_date: Optional[str] = None,
    exact_time: Optional[str] = None,
    api_key: str = "",
) -> dict[str, Any]:
    """Update an existing expense entry by UUID for the authenticated user."""
    if not id.strip():
        raise ValueError("id is required")
    _get_profile(api_key or None)
    raw = {
        k: v
        for k, v in {
            "category": category,
            "description": description,
            "flow_type": flow_type,
            "amount": amount,
            "spent_where": spent_where,
            "received_from": received_from,
            "expense_date": expense_date,
            "exact_time": exact_time,
        }.items()
        if v is not None and v != ""
    }
    patch = validate_expense(raw, is_update=True)
    rows = _sb_rest(f"expenses?id=eq.{urllib.parse.quote(id.strip())}", method="PATCH", body=patch, token=api_key or None)
    if not rows:
        raise ValueError(f"Expense not found or unauthorized: {id}")
    return rows[0]


@mcp.tool()
def delete_expense(id: str, api_key: str = "") -> dict[str, Any]:
    """Delete an expense entry by UUID for the authenticated user."""
    if not id.strip():
        raise ValueError("id is required")
    _get_profile(api_key or None)
    rows = _sb_rest(f"expenses?id=eq.{urllib.parse.quote(id.strip())}", method="DELETE", token=api_key or None)
    if not rows:
        raise ValueError(f"Expense not found or unauthorized: {id}")
    return rows[0]


@mcp.tool()
def get_expense_analytics(month: str = "", date: str = "", api_key: str = "") -> dict[str, Any]:
    """Compute salary & spending analytics against the authenticated user's monthly income (default 19,800)."""
    profile = _get_profile(api_key or None)
    income = float(profile.get("monthly_income") or DEFAULT_MONTHLY_INCOME)
    ist_date, _ = now_ist()
    target_month = month.strip() or (date.strip()[:7] if date.strip() else ist_date[:7])
    yr, mo = map(int, target_month.split("-"))
    days_in_month = calendar.monthrange(yr, mo)[1]
    current_day = int(ist_date[8:10]) if target_month == ist_date[:7] else days_in_month
    remaining_days = max(1, days_in_month - current_day + 1)

    rows = list_expenses(date=date, month=target_month, limit=1000, api_key=api_key)["expenses"]

    total_spent = 0.0
    total_received = 0.0
    nil_count = 0
    by_category: dict[str, dict[str, Any]] = {}
    by_where: dict[str, dict[str, Any]] = {}
    by_from: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}

    for r in rows:
        amt = float(r.get("amount") or 0.0)
        d = r["expense_date"]
        if d not in by_day:
            by_day[d] = {"date": d, "spent": 0.0, "received": 0.0, "nil_count": 0, "items": []}
        by_day[d]["items"].append(r)

        ft = r.get("flow_type")
        if ft == "spent":
            total_spent += amt
            by_day[d]["spent"] = round(by_day[d]["spent"] + amt, 2)
            cat = r.get("category") or "Other"
            if cat not in by_category:
                by_category[cat] = {"category": cat, "spent": 0.0, "count": 0, "descriptions": []}
            by_category[cat]["spent"] = round(by_category[cat]["spent"] + amt, 2)
            by_category[cat]["count"] += 1
            if r.get("description") and len(by_category[cat]["descriptions"]) < 5:
                by_category[cat]["descriptions"].append(r["description"])

            place = r.get("spent_where") or "Unspecified Place"
            if place not in by_where:
                by_where[place] = {"spent_where": place, "spent": 0.0, "count": 0}
            by_where[place]["spent"] = round(by_where[place]["spent"] + amt, 2)
            by_where[place]["count"] += 1
        elif ft == "received":
            total_received += amt
            by_day[d]["received"] = round(by_day[d]["received"] + amt, 2)
            src = r.get("received_from") or "Unspecified Source"
            if src not in by_from:
                by_from[src] = {"received_from": src, "received": 0.0, "count": 0, "descriptions": []}
            by_from[src]["received"] = round(by_from[src]["received"] + amt, 2)
            by_from[src]["count"] += 1
            if r.get("description") and len(by_from[src]["descriptions"]) < 5:
                by_from[src]["descriptions"].append(r["description"])
        else:
            nil_count += 1
            by_day[d]["nil_count"] += 1

    total_spent = round(total_spent, 2)
    total_received = round(total_received, 2)
    salary_remaining = round(income - total_spent, 2)
    total_balance_remaining = round(income + total_received - total_spent, 2)
    daily_avg = round(total_spent / max(1, current_day), 2)

    return {
        "user": {"id": profile["id"], "email": profile["email"], "full_name": profile["full_name"]},
        "month": target_month,
        "monthly_income": income,
        "total_spent": total_spent,
        "total_received": total_received,
        "nil_entries_count": nil_count,
        "salary_remaining": salary_remaining,
        "total_balance_remaining": total_balance_remaining,
        "salary_spent_pct": round((total_spent / income) * 100, 1) if income > 0 else 0.0,
        "daily_average_spend": daily_avg,
        "projected_month_spend": round(daily_avg * days_in_month, 2),
        "safe_daily_allowance_remaining": round(max(0.0, salary_remaining) / remaining_days, 2),
        "by_category": sorted(
            [
                {
                    **c,
                    "pct_of_spend": round((c["spent"] / total_spent) * 100, 1) if total_spent > 0 else 0.0,
                    "pct_of_salary": round((c["spent"] / income) * 100, 1) if income > 0 else 0.0,
                }
                for c in by_category.values()
            ],
            key=lambda x: x["spent"],
            reverse=True,
        ),
        "by_spent_where": sorted(by_where.values(), key=lambda x: x["spent"], reverse=True),
        "by_received_from": sorted(by_from.values(), key=lambda x: x["received"], reverse=True),
        "daily_breakdown": sorted(by_day.values(), key=lambda x: x["date"], reverse=True),
    }


if __name__ == "__main__":
    port = os.environ.get("PORT")
    if port or os.environ.get("MCP_TRANSPORT") == "http":
        mcp.run(transport="http", host="0.0.0.0", port=int(port or 8000))
    else:
        mcp.run()

