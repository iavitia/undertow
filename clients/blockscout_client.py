import time

import requests

BASE_URL = "https://explorer.derive.xyz/api/v2"

# Derive Chain is its own OP-stack rollup with a Blockscout explorer
# (confirmed via search: explorer.derive.xyz / explorer.lyra.finance are
# the same instance). This is the only way to see a Derive wallet's
# activity beyond options trades -- deposits, withdrawals, staking,
# transfers -- since none of that is in Derive's own trading API.


def get_address_info(address, max_retries=5):
    for attempt in range(max_retries):
        try:
            resp = requests.get(f"{BASE_URL}/addresses/{address}", headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError):
            if attempt == max_retries - 1:
                raise
            time.sleep(2**attempt)


def iter_token_transfers(address, max_pages=50, max_retries=5):
    """Yields raw Blockscout token-transfer items, newest first."""
    params = {}
    for _ in range(max_pages):
        for attempt in range(max_retries):
            try:
                resp = requests.get(
                    f"{BASE_URL}/addresses/{address}/token-transfers",
                    params=params,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempt == max_retries - 1:
                    raise
                time.sleep(2**attempt)

        items = data.get("items", [])
        for it in items:
            yield it

        next_page = data.get("next_page_params")
        if not next_page or not items:
            break
        params = next_page
        time.sleep(0.15)
