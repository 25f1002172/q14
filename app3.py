
import os
import re
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
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def load_orders():
    url = ORDERS_URL
    records = []
    visited = set()

    while url and url not in visited:
        visited.add(url)
        data = fetch(url)
        records.extend(data.get("orders", []))
        url = data.get("next")

    latest = {}
    for order in records:
        oid = order.get("id")
        old = latest.get(oid)
        if old is None or order.get("updated_at", "") > old.get("updated_at", ""):
            latest[oid] = order

    return list(latest.values())


@app.on_event("startup")
def startup():
    global orders, rates, exports, load_error
    try:
        if not ORDERS_URL:
            raise ValueError("Missing ORDERS_URL environment variable")
        orders = load_orders()
        if RATES_URL:
            rates = fetch(RATES_URL)
        if EXPORT_URL:
            exports = fetch(EXPORT_URL)
    except Exception as exc:
        load_error = str(exc)
        print("DATA LOAD ERROR:", load_error)


class Question(BaseModel):
    question: str


def usd_rate(currency):
    currency = currency.upper()
    if currency == "USD":
        return 1.0

    # Handle common rate API formats.
    def walk(obj):
        if isinstance(obj, dict):
            for key in ("rates", "data", "exchange_rates", "conversion_rates"):
                if key in obj:
                    result = walk(obj[key])
                    if result is not None:
                        return result

            # Format: {"EUR": {"USD": 1.08}}
            for base, value in obj.items():
                if isinstance(value, dict):
                    if str(base).upper() == currency:
                        for k, v in value.items():
                            if str(k).upper() == "USD" and isinstance(v, (int, float)):
                                return float(v)
                    if str(base).upper() == "USD":
                        for k, v in value.items():
                            if str(k).upper() == currency and isinstance(v, (int, float)) and v:
                                return 1.0 / float(v)

            # Format: {"EUR": 1.08, ...}, where USD is the base
            if "USD" in obj and isinstance(obj["USD"], (int, float)):
                if currency in obj and isinstance(obj[currency], (int, float)) and obj[currency]:
                    return 1.0 / float(obj[currency])

            for value in obj.values():
                result = walk(value)
                if result is not None:
                    return result

        elif isinstance(obj, list):
            for row in obj:
                if isinstance(row, dict):
                    source = str(row.get("from", row.get("base", ""))).upper()
                    target = str(row.get("to", row.get("quote", ""))).upper()
                    value = row.get("rate", row.get("exchange_rate"))
                    if isinstance(value, (int, float)) and value:
                        if source == currency and target == "USD":
                            return float(value)
                        if source == "USD" and target == currency:
                            return 1.0 / float(value)
                result = walk(row)
                if result is not None:
                    return result
        return None

    result = walk(rates)
    if result is None:
        raise ValueError("Could not find exchange rate for " + currency)
    return result


MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12
}


def answer_question(question):
    q = question.lower()
    selected = orders[:]

    region = next(
        (r for r in ("North", "South", "East", "West", "Central")
         if r.lower() in q), None
    )
    if region:
        selected = [
            o for o in selected
            if str(o.get("region", "")).lower() == region.lower()
        ]

    month = next((n for name, n in MONTHS.items() if name in q), None)
    year_match = re.search(r"\b20\d{2}\b", q)
    year = int(year_match.group()) if year_match else None

    if month:
        filtered = []
        for order in selected:
            try:
                dt = datetime.fromisoformat(
                    order["created_at"].replace("Z", "+00:00")
                ).astimezone(TZ)
                if dt.month == month and (year is None or dt.year == year):
                    filtered.append(order)
            except (KeyError, ValueError, TypeError):
                continue
        selected = filtered

    if any(word in q for word in ("revenue", "sales", "turnover")):
        total = 0.0
        for order in selected:
            if order.get("status") == "paid":
                total += (
                    float(order.get("amount", 0))
                    * usd_rate(order.get("currency", "USD"))
                )
        return round(total + 1e-9, 2)

    if "refund" in q:
        refunded = [o for o in selected if o.get("status") == "refunded"]
        if any(word in q for word in ("count", "number", "how many")):
            return len(refunded)
        return round(sum(float(o.get("amount", 0)) for o in refunded), 2)

    if "customer" in q:
        customers = sorted(set(o.get("customer") for o in selected if o.get("customer")))
        if any(word in q for word in ("count", "number", "how many", "unique")):
            return len(customers)
        return customers

    if "product" in q:
        quantities = {}
        for o in selected:
            p = o.get("product", "")
            quantities[p] = quantities.get(p, 0) + int(o.get("qty", 0))
        if quantities:
            return max(quantities, key=quantities.get)

    if any(word in q for word in ("how many orders", "order count", "number of orders")):
        return len(selected)

    if "export" in q:
        return exports

    return {"error": "Unsupported question"}


@app.get("/")
def health():
    return {
        "status": "ok" if load_error is None else "data_load_error",
        "orders_loaded": len(orders),
        "error": load_error
    }


@app.post("/ledger")
def ledger(body: Question):
    try:
        return {"answer": answer_question(body.question)}
    except Exception as exc:
        return {"error": str(exc)}
