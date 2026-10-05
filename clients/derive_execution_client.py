"""Authenticated Derive testnet execution client -- Phase 4, real (testnet)
order placement and account-state reads. Deliberately separate from
clients/derive_client.py, which stays public-only/credential-free for the
Phase 0-3 paper-trading pipeline (mainnet historical data, no auth needed).

Talks to https://api-demo.lyra.finance -- NOT the derive-py SDK's default
Chain.SEPOLIA config (testnet.api.derive.xyz), which this session confirmed
is a different, unsynced environment: different auth header names
(X-Derive* vs the X-Lyra* scheme that actually works here, confirmed by a
live signed call returning real subaccount data), and a different
DOMAIN_SEPARATOR / TRADE_MODULE address than what Derive's own Submit
Order docs show for this specific domain. Confirmed live this session: a
public get_instrument call against api-demo.lyra.finance returns the exact
base_asset_address the docs' worked example hard-codes for ETH -- this
really is the right environment, not a guess. ACTION_TYPEHASH is the one
constant confirmed identical everywhere (derive-py's two built-in configs
and these docs), since it's derived purely from the fixed EIP-712 type
string, not tied to a deployment address.

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

EXECUTION_BASE_URL = "https://api-demo.lyra.finance"

# Confirmed live this session against EXECUTION_BASE_URL (see module
# docstring) -- NOT derive_py.config.contracts's values, which are for the
# other, unsynced environment (testnet.api.derive.xyz) and differ for
# TRADE_MODULE and DOMAIN_SEPARATOR despite looking like they should apply.
TRADE_MODULE_ADDRESS = "0x87F2863866D85E3192a35A73b388BD625D83f2be"
DOMAIN_SEPARATOR = "0x9bcf4dc06df5d8bf23af818d5716491b995020f377d3b7b64c29ed14e3dd1105"
ACTION_TYPEHASH = "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17"
INT64_MAX = (1 << 63) - 1


def _assert_testnet():
    if DERIVE_ETH_CHAIN != "SEPOLIA":
        raise RuntimeError(
            f"refusing to run: DERIVE_ETH_CHAIN={DERIVE_ETH_CHAIN!r}, expected 'SEPOLIA' -- "
            "this client must never place a real order against mainnet."
        )


def _signed_headers():
    """X-LyraWallet/X-LyraTimestamp/X-LyraSignature -- the scheme confirmed
    working against api-demo.lyra.finance this session (NOT derive-py's
    built-in X-Derive* headers, which are for the other environment)."""
    _assert_testnet()
    timestamp = str(int(time.time() * 1000))
    signature = Account.sign_message(
        encode_defunct(text=timestamp), private_key=DERIVE_SESSION_KEY
    ).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    return {
        "X-LyraWallet": DERIVE_WALLET,
        "X-LyraTimestamp": timestamp,
        "X-LyraSignature": signature,
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


def simulate_margin(simulated_positions, simulated_collaterals, margin_type="SM"):
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
    margin_type: "SM" (Standard Margin) -- confirmed this session to match
    this account's own real margin_type (get_account_state()['margin_type']).

    Returns the raw result dict (pre/post_initial_margin, pre/post_maintenance_margin,
    is_valid_trade) -- same shape as the real account-state margin fields."""
    resp = requests.post(
        f"{EXECUTION_BASE_URL}/public/get_margin",
        json={
            "margin_type": margin_type,
            "simulated_positions": simulated_positions,
            "simulated_collaterals": simulated_collaterals,
        },
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
    module that could point it at the wrong environment."""
    resp = requests.post(
        f"{EXECUTION_BASE_URL}/public/get_ticker",
        json={"instrument_name": instrument_name},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


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
        # This environment's /private/order wants a raw i64, not a string
        # (confirmed live: sending str(nonce) -- the convention derive-py's
        # own code uses, citing JS double-precision corruption on a
        # 19-digit nanosecond nonce -- gets rejected with "expected i64").
        # Safe as a plain Python int here: the `requests` json= encoder
        # serializes Python's arbitrary-precision ints as exact decimal
        # digits, not through a lossy float, so there's no corruption risk
        # on our side of this specific call.
        "nonce": nonce,
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
