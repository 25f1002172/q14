
import os
import re
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
TZ = ZoneInfo("Asia/Kolkata")
TIMEOUT = 10

ORDERS_URL = os.getenv("ORDERS_URL", "")
RATES_URL = os.getenv("RATES_URL", "")
EXPORT_URL = os.getenv("EXPORT_URL", "")

orders = []
rates = {}
exports = {}
load_error = None


def fetch(url):
    """Fetch JSON, including newline-delimited JSON."""
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()

    try:
        return response.json()
    except ValueError:
        lines = [line for line in response.text.splitlines() if line.strip()]
        if not lines:
            return {}

        parsed = [json.loads(line) for line in lines]
        return parsed[0] if len(parsed) == 1 else parsed


def load_orders():
    url = ORDERS_URL
    records = []
    visited = set()

    while url and url not in visited:
        visited.add(url)
        data = fetch(url)

        if isinstance(data, list):
            records.extend(data)
            break

        if not isinstance(data, dict):
            raise ValueError("Unexpected orders response format")

        page_orders = data.get("orders", [])
        records.extend(page_orders)

        url = data.get("next")

    # Deduplicate: the most recently updated version wins.
    latest = {}
    for order in records:
        oid = order.get("id")
        if oid is None:
            continue

        previous = latest.get(oid)
        if (
            previous is None
            or order.get("updated_at", "") > previous.get("updated_at", "")
        ):
            latest[oid] = order

    return list(latest.values())


@app.on_event("startup")
def startup():
    global orders, rates, exports, load_error

    try:
        if not ORDERS_URL:
            raise ValueError("ORDERS_URL environment variable is missing")

        orders = load_orders()

        if RATES_URL:
            rates = fetch(RATES_URL)

        if EXPORT_URL:
            exports = fetch(EXPORT_URL)

        load_error = None
        print(f"Loaded {len(orders)} unique orders")

    except Exception as exc:
        load_error = str(exc)
        print("DATA LOAD ERROR:", repr(exc))


class Question(BaseModel):
    question: str


MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def find_rate(currency):
    """Try to discover a conversion rate from common API structures."""
    currency = currency.upper()

    if currency == "USD":
        return 1.0

    def walk(obj):
        if isinstance(obj, dict):
            # Explicit source/target/rate records.
            source = str(obj.get("from", obj.get("base", ""))).upper()
            target = str(obj.get("to", obj.get("quote", ""))).upper()
            value = obj.get("rate", obj.get("exchange_rate"))

            if isinstance(value, (int, float)) and value:
                if source == currency and target == "USD":
                    return float(value)
                if source == "USD" and target == currency:
                    return 1.0 / float(value)

            # {"EUR": {"USD": rate}}
            sub = obj.get(currency)
            if isinstance(sub, dict):
                for key, value in sub.items():
                    if key.upper() == "USD" and isinstance(value, (int, float)):
                        return float(value)

            # {"USD": {"EUR": rate}} means USD per EUR is the inverse.
            sub = obj.get("USD")
            if isinstance(sub, dict):
                for key, value in sub.items():
                    if key.upper() == currency and isinstance(value, (int, float)) and value:
                        return 1.0 / float(value)

            # {"base": "USD", "rates": {"EUR": rate}}
            base = str(obj.get("base", "")).upper()
            sub = obj.get("rates")
            if base == "USD" and isinstance(sub, dict):
                for key, value in sub.items():
                    if key.upper() == currency and isinstance(value, (int, float)) and value:
                        return 1.0 / float(value)

            # Search nested objects and lists.
            for value in obj.values():
                result = walk(value)
                if result is not None:
                    return result

        elif isinstance(obj, list):
            for item in obj:
                result = walk(item)
                if result is not None:
                    return result

        return None

    result = walk(rates)

    if result is None:
        raise ValueError(
            f"Exchange rate for {currency} to USD not found; inspect RATES_URL data"
        )

    return result


def order_date(order):
    value = order.get("created_at")
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(TZ)
    except (ValueError, TypeError):
        return None


def answer_question(question):
    q = question.lower().strip()
    selected = orders[:]

    # Region filter.
    regions = ["North", "South", "East", "West", "Central"]
    region = next((r for r in regions if r.lower() in q), None)

    if region:
        selected = [
            o for o in selected
            if str(o.get("region", "")).lower() == region.lower()
        ]

    # Month/year filters use the business timezone.
    month = next((n for name, n in MONTHS.items() if name in q), None)
    year_match = re.search(r"\b20\d{2}\b", q)
    year = int(year_match.group()) if year_match else None

    if month:
        filtered = []
        for order in selected:
            dt = order_date(order)
            if dt and dt.month == month and (year is None or dt.year == year):
                filtered.append(order)
        selected = filtered
    elif year is not None:
        selected = [
            o for o in selected
            if order_date(o) and order_date(o).year == year
        ]

    # Revenue is counted only for paid orders.
    if any(word in q for word in ("revenue", "sales", "turnover")):
        total = 0.0

        for order in selected:
            if order.get("status") != "paid":
                continue

            amount = float(order.get("amount", 0))
            currency = str(order.get("currency", "USD")).upper()
            total += amount * find_rate(currency)

        return round(total + 1e-9, 2)

    # Refund questions.
    if "refund" in q:
        refunded = [
            o for o in selected
            if o.get("status") == "refunded"
        ]

        if any(word in q for word in ("count", "number", "how many")):
            return len(refunded)

        return round(sum(float(o.get("amount", 0)) for o in refunded), 2)

    # Customer questions.
    if "customer" in q:
        customers = sorted({
            o.get("customer")
            for o in selected
            if o.get("customer") is not None
        })

        if any(word in q for word in ("count", "number", "how many", "unique")):
            return len(customers)

        return customers

    # Product questions.
    if "product" in q:
        quantities = {}

        for order in selected:
            product = order.get("product", "")
            quantities[product] = (
                quantities.get(product, 0) + int(order.get("qty", 0))
            )

        if quantities:
            return max(quantities, key=quantities.get)

    # Order counts.
    if any(word in q for word in (
        "how many orders", "order count", "number of orders",
        "count of orders"
    )):
        return len(selected)

    # Export questions: return the export data if explicitly requested.
    if "export" in q:
        return exports

    return {"error": "Unsupported question type", "question": question}


@app.get("/")
def health():
    return {
        "status": "ok" if load_error is None else "data_load_error",
        "orders_loaded": len(orders),
        "error": load_error,
    }


@app.post("/")
@app.post("/ledger")
def ledger(body: Question):
    if load_error:
        return {"error": "Data failed to load", "details": load_error}

    try:
        return {"answer": answer_question(body.question)}
    except Exception as exc:
        print("QUESTION ERROR:", repr(exc))
        return {"error": str(exc)}
