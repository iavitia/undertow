from datetime import datetime, timezone

import requests

from config import DERIBIT_BASE_URL


def parse_instrument(instrument_name):
    """e.g. 'ETH-28AUG26-1950-C' -> strike, option_type, expiry.
    Deribit settles options at 08:00 UTC on the expiry date, per their
    documented convention."""
    parts = instrument_name.split("-")
    if len(parts) != 4:
        return None
    _, date_str, strike_str, opt_type = parts
    expiry_dt = datetime.strptime(date_str, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
    return {
        "strike": float(strike_str),
        "option_type": "call" if opt_type == "C" else "put",
        "expiry": int(expiry_dt.timestamp()),
    }


def get_recent_trades(currency, start_timestamp, end_timestamp, count=1000):
    """Only returns trades from roughly the last 24-48h regardless of
    start_timestamp -- confirmed empirically, not documented. See
    PROJECT_PLAN.md section 3 for why BTC has no historical backfill."""
    resp = requests.get(
        f"{DERIBIT_BASE_URL}/public/get_last_trades_by_currency_and_time",
        params={
            "currency": currency,
            "kind": "option",
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "count": count,
            "sorting": "asc",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]["trades"], data["result"]["has_more"]
