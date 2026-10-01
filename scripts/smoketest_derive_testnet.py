"""One-time connectivity check for Phase 4: does the derive-py SDK's
signing/auth path actually work against Derive's testnet, before wiring
anything into the live paper-trading pipeline?

Uses the SDK's OWN published example credentials (.env.smoketest, copied
verbatim from https://github.com/derivexyz/derive-py's .env.template,
fetched via direct curl -- not our dedicated wallet from
scripts/generate_testnet_wallet.py, which has no funds yet and isn't
onboarded). Those example credentials are explicitly public and shared by
everyone who runs the SDK's own examples -- fine for proving the code path
works, not for an ongoing attributable track record (see conversation).

Read-only: fetches public market data, then authenticates and reads
subaccount/position state. Does NOT place an order -- no reason to touch
even a shared throwaway account's state for a pure connectivity check."""
import sys
from pathlib import Path

from derive_py import HTTPClient

ENV_FILE = Path(__file__).resolve().parent.parent / ".env.smoketest"


def run():
    if not ENV_FILE.exists():
        print(f"missing {ENV_FILE}")
        sys.exit(1)

    print("--- public data (no auth) ---")
    client = HTTPClient.from_env(env_file=ENV_FILE)
    ticker = client.markets.get_ticker(instrument_name="ETH-PERP")
    print(f"ETH-PERP ticker fetched OK: mark_price={getattr(ticker, 'mark_price', ticker)}")

    print("\n--- authenticating (signing + submitting a signed request) ---")
    client.connect()
    print("connect() succeeded -- signing path works")

    print("\n--- reading subaccount state (read-only, authenticated) ---")
    subaccounts = client.fetch_subaccounts()
    print(f"{len(subaccounts)} subaccount(s) visible to this wallet")
    for sub in subaccounts:
        print(f"  {sub}")

    client.disconnect()
    print("\nall checks passed")


if __name__ == "__main__":
    run()
