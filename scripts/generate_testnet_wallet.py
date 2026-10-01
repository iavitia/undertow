"""Generates a fresh, dedicated EOA keypair for the Derive testnet paper
trading account (Phase 4). Writes DERIVE_WALLET (public address) and
DERIVE_SESSION_KEY (private key, used as the EOA signer -- see
docs.derive.xyz's Authentication reference: "the owner or a registered
session key must explicitly sign a payload") to .env.

Deliberately does NOT use the derive-py SDK's published shared example
key -- that account is used by everyone running the SDK's own examples,
so its trade/balance history would be contaminated and not a clean,
attributable track record for this project specifically.

Never prints the private key -- only the public address, which is not
sensitive. .env is already gitignored; this script refuses to run if
.env already has a non-empty DERIVE_WALLET, so it can't silently
overwrite a wallet you've already funded."""
import sys
from pathlib import Path

from eth_account import Account

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"


def run():
    if ENV_PATH.exists():
        existing = ENV_PATH.read_text()
        for line in existing.splitlines():
            if line.startswith("DERIVE_WALLET=") and line.strip() != "DERIVE_WALLET=":
                print(f"refusing to overwrite: .env already has DERIVE_WALLET set ({line.strip()})")
                sys.exit(1)
        lines = existing.splitlines()
    else:
        lines = ENV_EXAMPLE_PATH.read_text().splitlines()

    Account.enable_unaudited_hdwallet_features()
    acct = Account.create()
    # eth_account's .key.hex() omits the "0x" prefix on some versions --
    # standardize on including it, since that's what most Ethereum
    # tooling (web3.py included) expects for a private key string.
    key_hex = acct.key.hex()
    private_key_hex = key_hex if key_hex.startswith("0x") else f"0x{key_hex}"

    new_lines = []
    seen = {"DERIVE_WALLET": False, "DERIVE_SESSION_KEY": False}
    for line in lines:
        if line.startswith("DERIVE_WALLET="):
            new_lines.append(f"DERIVE_WALLET={acct.address}")
            seen["DERIVE_WALLET"] = True
        elif line.startswith("DERIVE_SESSION_KEY="):
            new_lines.append(f"DERIVE_SESSION_KEY={private_key_hex}")
            seen["DERIVE_SESSION_KEY"] = True
        else:
            new_lines.append(line)
    if not seen["DERIVE_WALLET"]:
        new_lines.append(f"DERIVE_WALLET={acct.address}")
    if not seen["DERIVE_SESSION_KEY"]:
        new_lines.append(f"DERIVE_SESSION_KEY={private_key_hex}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n")

    print(f"generated a new testnet-only wallet: {acct.address}")
    print("private key written to .env (gitignored) -- not printed here")
    print()
    print("this address has ZERO funds and isn't onboarded to Derive yet -- see next steps")


if __name__ == "__main__":
    run()
