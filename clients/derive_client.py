import re
import time
from datetime import datetime, timezone

import requests

from config import DERIVE_BASE_URL, DERIVE_REQUEST_DELAY_SECONDS

# e.g. "ETH-20260828-3400-C" -> asset, expiry date, strike, C/P
INSTRUMENT_RE = re.compile(r"^[A-Z]+-(\d{8})-([\d.]+)-([CP])$")


def parse_instrument(instrument_name):
    """Derive settles options at 08:00 UTC on the expiry date encoded in the
    instrument name; this is a convention observed in sample data, not
    confirmed per-instrument against get_instruments."""
    m = INSTRUMENT_RE.match(instrument_name)
    if not m:
        return None
    date_str, strike_str, opt_type = m.groups()
    expiry_dt = datetime.strptime(date_str, "%Y%m%d").replace(hour=8, tzinfo=timezone.utc)
    return {
        "strike": float(strike_str),
        "option_type": "call" if opt_type == "C" else "put",
        "expiry": int(expiry_dt.timestamp()),
    }


def get_trade_history(currency, from_timestamp, to_timestamp, page=1, page_size=1000, max_retries=5):
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{DERIVE_BASE_URL}/public/get_trade_history",
                json={
                    "currency": currency,
                    "instrument_type": "option",
                    "from_timestamp": from_timestamp,
                    "to_timestamp": to_timestamp,
                    "page": page,
                    "page_size": page_size,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data["result"]
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ):
            if attempt == max_retries - 1:
                raise
            backoff = 2**attempt
            print(f"transient network error on page {page}, retrying in {backoff}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(backoff)


def get_ticker(instrument_name, max_retries=5):
    """Live snapshot of one instrument -- mark_price, index_price, and
    option_pricing (delta/theta/gamma/vega/iv/rho). Confirmed live earlier
    this session: this is a live-only endpoint, no historical/backfill
    equivalent exists (see scripts/backtest_stop_loss.py's docstring --
    that's why the historical backtest has to approximate with intrinsic
    value instead). Same retry/backoff shape as get_trade_history.

    V3 returns a "slim ticker" with single-letter keys (b/a=bid/ask price,
    B/A=bid/ask amount, I=index, M=mark) and an abbreviated option_pricing
    sub-object (d/t/g/v/i/r = delta/theta/gamma/vega/iv/rho, matching each
    greek's own first letter) instead of V2's full field names -- same
    issue already found and fixed in
    clients/derive_execution_client.get_ticker (that one's own docstring
    has the fuller explanation); translated back to the old names below so
    every call site written against V2's shape keeps working unmodified."""
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{DERIVE_BASE_URL}/public/get_ticker",
                json={"instrument_name": instrument_name},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            slim = data["result"]
            slim_greeks = slim.get("option_pricing") or {}
            return {
                "mark_price": slim.get("M"),
                "index_price": slim.get("I"),
                "best_bid_price": slim.get("b"),
                "best_ask_price": slim.get("a"),
                "best_bid_amount": slim.get("B"),
                "best_ask_amount": slim.get("A"),
                "option_pricing": {
                    "delta": slim_greeks.get("d"),
                    "theta": slim_greeks.get("t"),
                    "gamma": slim_greeks.get("g"),
                    "vega": slim_greeks.get("v"),
                    "iv": slim_greeks.get("i"),
                    "rho": slim_greeks.get("r"),
                },
            }
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ):
            if attempt == max_retries - 1:
                raise
            backoff = 2**attempt
            print(f"transient network error fetching ticker for {instrument_name}, retrying in {backoff}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(backoff)


def iter_trade_history(currency, from_timestamp, to_timestamp, page_size=1000):
    """Yields (trades, page, num_pages) per page so callers can commit and
    report progress incrementally. Note: the API does not honor a stable
    sort order across pages by timestamp (observed to return newest-first
    regardless of a 'sorting' param), so don't assume page order == time
    order."""
    page = 1
    while True:
        result = get_trade_history(currency, from_timestamp, to_timestamp, page=page, page_size=page_size)
        trades = result["trades"]
        pagination = result["pagination"]
        yield trades, page, pagination["num_pages"]
        if page >= pagination["num_pages"] or not trades:
            break
        page += 1
        time.sleep(DERIVE_REQUEST_DELAY_SECONDS)
