"""Authenticated Derive testnet execution client -- Phase 4, real (testnet)
order placement and account-state reads. Deliberately separate from
clients/derive_client.py, which stays public-only/credential-free for the
Phase 0-3 paper-trading pipeline (mainnet historical data, no auth needed).

V3 (see conversation -- Derive migrated off the old V2/"Derive Wallet" SCW
architecture on 2026-10-06): talks to https://testnet.api.derive.xyz/v3.
The old V2 environment this file originally targeted
(api-demo.lyra.finance, X-Lyra* headers) is now fully decommissioned --
confirmed live, it 530s on every request post-migration. DOMAIN_SEPARATOR
and TRADE_MODULE_ADDRESS below are V3's values (confirmed against
docs.derive.xyz/migrating/breaking-changes and live API calls this
session), not the old V2 ones. ACTION_TYPEHASH is unchanged -- it's
derived purely from the fixed EIP-712 type string, not tied to a
deployment address, and the migration docs don't list it as changed.

Reuses derive-py's own SignedAction/TradeModuleData classes for the EIP-712
encoding (both pure, environment-agnostic dataclasses that just take
DOMAIN_SEPARATOR/ACTION_TYPEHASH as constructor args) rather than
reimplementing that cryptography by hand -- only the HTTP/auth layer and
the environment-specific constants below are custom to this file.

Every function refuses to run unless DERIVE_ETH_CHAIN=SEPOLIA -- see
_assert_testnet(). This file must never be able to touch mainnet."""
import time
from decimal import Decimal

import requests
from derive_py._web3.action_signing.module_data.trade import TradeModuleData
from derive_py._web3.action_signing.signed_action import SignedAction
from eth_account import Account
from eth_account.messages import encode_defunct

from config import DERIVE_ETH_CHAIN, DERIVE_SESSION_KEY, DERIVE_WALLET

EXECUTION_BASE_URL = "https://testnet.api.derive.xyz/v3"

# V3 values -- confirmed against docs.derive.xyz/migrating/breaking-changes
# and live calls this session (e.g. public/get_margin, public/get_risk_universes
# both responded correctly using these). DOMAIN_SEPARATOR is per-chain under
# V3 (Sepolia, chainId 11155111); TRADE_MODULE is now identical across
# testnet/mainnet, unlike V2 where it was environment-specific.
TRADE_MODULE_ADDRESS = "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b"
DOMAIN_SEPARATOR = "0x24d674cd5f2b9d564691c51e9d88f649b99246a2244dd74ce27b96578d773e85"
ACTION_TYPEHASH = "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17"
INT64_MAX = (1 << 63) - 1


def _assert_testnet():
    if DERIVE_ETH_CHAIN != "SEPOLIA":
        raise RuntimeError(
            f"refusing to run: DERIVE_ETH_CHAIN={DERIVE_ETH_CHAIN!r}, expected 'SEPOLIA' -- "
            "this client must never place a real order against mainnet."
        )


def _signed_headers():
    """X-DeriveWallet/X-DeriveTimestamp/X-DeriveSignature -- V3's header
    names (X-Lyra* was V2's scheme, dead along with the old environment --
    see module docstring)."""
    _assert_testnet()
    timestamp = str(int(time.time() * 1000))
    signature = Account.sign_message(
        encode_defunct(text=timestamp), private_key=DERIVE_SESSION_KEY
    ).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    return {
        "X-DeriveWallet": DERIVE_WALLET,
        "X-DeriveTimestamp": timestamp,
        "X-DeriveSignature": signature,
    }


def _private_post(path, body, max_retries=5):
    """Same retry/backoff shape as clients/derive_client.py's public
    functions, signed with fresh headers on every attempt (a stale
    timestamp from a prior attempt would fail the signature check)."""
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{EXECUTION_BASE_URL}{path}",
                json=body,
                headers=_signed_headers(),
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
            print(f"transient network error on {path}, retrying in {backoff}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(backoff)


def get_account_state(subaccount_id):
    """Real balance/margin/collateral for one subaccount. Called again right
    after place_order() to read back what an order actually cost, and used
    as the baseline (current positions/collaterals) for simulate_margin()'s
    what-if checks before placing a new one."""
    return _private_post("/private/get_subaccount", {"subaccount_id": subaccount_id})


def simulate_margin(simulated_positions, simulated_collaterals, margin_type="SM", market=None):
    """Public, no auth needed -- the real preview/what-if endpoint
    (public/order_debug, despite its name, only validates signature
    construction, not margin; this is the actual one). Confirmed this
    session against real orders: matches real open_orders_margin almost
    exactly for perps (within normal price-drift noise), but runs ~1.75x
    the real RESTING-ORDER margin for at least one tested option strike --
    likely because this computes true HELD-POSITION margin while a resting
    order gets a lighter pre-fill reservation, which is what the open_orders_margin
    manual-probe approach (place a real order, read it back, cancel) actually
    measures. Deliberately used here instead of that approach: see
    conversation -- that approach placed real orders that unexpectedly
    filled (thin one-sided testnet books), leaving unintended short
    positions needing manual cleanup. This function never touches the real
    order book at all.

    simulated_positions: [{"instrument_name": ..., "amount": "<signed str, negative=short>"}, ...]
    simulated_collaterals: [{"asset_name": "USDC", "amount": "<str>"}, ...]
    margin_type: "SM" (Standard Margin) or "PM2" (Portfolio Margin).
    market: required for non-Prime-universe instruments -- confirmed live
    this session that "SM" against this endpoint only resolves the Prime
    universe (BTC/ETH); simulating a SOL (Alt-universe) position needs
    margin_type="PM2", market="SOL" even though the real SOL subaccount
    itself is Standard Margin, or it errors "Instrument ... is not in the
    portfolio's risk universe". Confirmed PM2 runs slightly *higher* than
    SM for an unhedged position (this session's SM-vs-PM2 comparison), so
    using it as SOL's stand-in for a margin-cap safety check is
    conservative, not optimistic.

    Returns the raw result dict (pre/post_initial_margin, pre/post_maintenance_margin,
    is_valid_trade) -- same shape as the real account-state margin fields."""
    body = {
        "margin_type": margin_type,
        "simulated_positions": simulated_positions,
        "simulated_collaterals": simulated_collaterals,
    }
    if market is not None:
        body["market"] = market
    resp = requests.post(
        f"{EXECUTION_BASE_URL}/public/get_margin",
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


def get_instrument(instrument_name):
    """Public, no auth needed -- the base_asset_address/base_asset_sub_id a
    real order's TradeModuleData needs. Confirmed live this session: the
    base_asset_address this returns for ETH matches exactly what Derive's
    own Submit Order docs hard-code for this environment.

    Does NOT carry live pricing (mark_price, best_bid/ask) -- confirmed by
    inspecting real responses, it's metadata only (strikes, fees, asset
    addresses, activation window). Use get_ticker() for pricing."""
    resp = requests.post(
        f"{EXECUTION_BASE_URL}/public/get_instrument",
        json={"instrument_name": instrument_name},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


def estimate_fee(instrument, amount, index_price):
    """Estimated taker fee for a given order size, from the instrument's
    own base_fee/taker_fee_rate fields (get_instrument's response) --
    NOT Derive's generic published rate (their help docs say "$0.5 base +
    0.04% notional"; this environment's real, observed base_fee is ~$1.5,
    confirmed against three real settled trades this session, each paying
    $1.50-1.60 on a $0.02-0.91 premium -- the flat component alone
    exceeded the entire premium at minimum order size). Using the live,
    instrument-specific field is the number actually charged here, not a
    generic figure that's already been shown to disagree with reality in
    this environment. flat + rate*notional, no attempt to replicate the
    12.5%-of-option-value cap mentioned in Derive's docs -- irrelevant at
    the trade sizes this project targets, where the flat fee dominates."""
    base_fee = float(instrument["base_fee"])
    taker_fee_rate = float(instrument["taker_fee_rate"])
    notional = float(amount) * float(index_price)
    return base_fee + taker_fee_rate * notional


def get_ticker(instrument_name):
    """Public, no auth needed -- live mark_price/best_bid/best_ask for one
    testnet instrument. Same purpose as clients/derive_client.get_ticker,
    duplicated here rather than shared because that one hits
    config.DERIVE_BASE_URL (mainnet) -- this file must never import from a
    module that could point it at the wrong environment.

    V3 returns a "slim ticker" with single-letter keys (b/a=bid/ask price,
    B/A=bid/ask amount, I=index, M=mark) instead of V2's full field names,
    and drops instrument metadata entirely (use get_instrument() for that,
    as every caller here already does). Translated back to the old
    mark_price/index_price/best_bid_price/best_ask_price names below so
    every call site in this project -- written against V2's shape --
    keeps working unmodified; this is the one place that needs to know
    about the slim format."""
    resp = requests.post(
        f"{EXECUTION_BASE_URL}/public/get_ticker",
        json={"instrument_name": instrument_name},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    slim = data["result"]
    return {
        "mark_price": slim.get("M"),
        "index_price": slim.get("I"),
        "best_bid_price": slim.get("b"),
        "best_ask_price": slim.get("a"),
        "best_bid_amount": slim.get("B"),
        "best_ask_amount": slim.get("A"),
    }


def place_order(instrument_name, direction, amount, limit_price, subaccount_id, max_fee=Decimal("1000")):
    """Places a real (testnet) limit order. direction is 'buy' or 'sell' --
    'sell' on an instrument with no existing position is a naked short
    ('Sell to Open' in the UI). Builds and signs a TradeModuleData action
    using derive-py's own EIP-712 classes, with this file's confirmed
    DOMAIN_SEPARATOR/TRADE_MODULE_ADDRESS -- not derive-py's own
    Chain.SEPOLIA defaults, which are for the other environment (see
    module docstring). Amount/limit_price are quantized to the
    instrument's own step sizes, same as derive-py's OrderOperations.create."""
    _assert_testnet()
    instrument = get_instrument(instrument_name)

    amount_step = Decimal(instrument["amount_step"])
    tick_size = Decimal(instrument["tick_size"])
    amount = Decimal(str(amount)).quantize(amount_step)
    limit_price = Decimal(str(limit_price)).quantize(tick_size)

    signer_address = Account.from_key(DERIVE_SESSION_KEY).address
    nonce = int(time.time_ns())
    signature_expiry_sec = int(time.time()) + 600  # must be >5 min out, per Derive docs

    module_data = TradeModuleData(
        asset_address=instrument["base_asset_address"],
        sub_id=int(instrument["base_asset_sub_id"]),
        limit_price=limit_price,
        amount=amount,
        max_fee=max_fee,
        recipient_id=subaccount_id,
        is_bid=(direction == "buy"),
    )
    action = SignedAction(
        subaccount_id=subaccount_id,
        owner=DERIVE_WALLET,
        signer=signer_address,
        signature_expiry_sec=signature_expiry_sec,
        nonce=nonce,
        module_address=TRADE_MODULE_ADDRESS,
        module_data=module_data,
        DOMAIN_SEPARATOR=DOMAIN_SEPARATOR,
        ACTION_TYPEHASH=ACTION_TYPEHASH,
    )
    action.sign(DERIVE_SESSION_KEY)

    order_params = {
        "instrument_name": instrument_name,
        "direction": direction,
        "amount": str(amount),
        "limit_price": str(limit_price),
        "max_fee": str(max_fee),
        # V3 flipped this from V2: now wants nonce as a decimal STRING, not
        # a raw i64 (confirmed live this session -- sending the bare int
        # gets rejected with "expected a nonce as a decimal string"). V2
        # wanted the opposite ("expected i64" when sent as a string).
        "nonce": str(nonce),
        "signature": action.signature,
        "signature_expiry_sec": signature_expiry_sec,
        "signer": signer_address,
        "subaccount_id": subaccount_id,
        "label": "",
        "mmp": False,
        "order_type": "limit",
        "reduce_only": False,
        "reject_timestamp": INT64_MAX,
        "time_in_force": "gtc",
    }
    return _private_post("/private/order", order_params)
