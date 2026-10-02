import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH

# Deterministic per-address codenames for UI readability -- not an identity
# claim (see PROJECT_PLAN.md: no wallet identity inference). Same address
# always gets the same alias across re-runs, since it's derived from a hash
# of the address rather than stored random state.
ADJECTIVES = [
    "amber", "quiet", "copper", "brisk", "hollow", "ember", "faded", "grey",
    "iron", "jagged", "keen", "lunar", "mute", "north", "onyx", "pale",
    "quilted", "rusty", "slate", "tidal", "umber", "violet", "wry", "young",
    "zeal", "brass", "cedar", "drift", "echo", "flint", "glacial", "husk",
]
NOUNS = [
    "falcon", "ridge", "wolf", "harbor", "current", "anchor", "ember", "kestrel",
    "basin", "drift", "ledger", "signal", "tide", "vault", "meridian", "quarry",
    "hollow", "reef", "summit", "canyon", "delta", "spire", "thicket", "wren",
    "shoal", "gorge", "moth", "cinder", "marsh", "pike", "grove", "otter",
]


def alias_for(address):
    h = hashlib.sha256(address.encode()).hexdigest()
    adj = ADJECTIVES[int(h[:8], 16) % len(ADJECTIVES)]
    noun = NOUNS[int(h[8:16], 16) % len(NOUNS)]
    suffix = h[16:18]
    return f"{adj}-{noun}-{suffix}"


def assign_missing(conn):
    """Only the wallets without one yet -- called from
    scripts/run_watchlist_agent.py's own tick so a wallet discovered via
    live ingestion gets a real alias before it's ever shown anywhere,
    instead of needing this whole script re-run by hand (see conversation:
    77 live-discovered wallets went un-aliased until this was noticed on
    the Live Feed tab)."""
    addresses = [r["address"] for r in conn.execute("SELECT address FROM wallets WHERE alias IS NULL").fetchall()]
    if addresses:
        conn.executemany(
            "UPDATE wallets SET alias = ? WHERE address = ?",
            [(alias_for(a), a) for a in addresses],
        )
        conn.commit()
    return len(addresses)


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    addresses = [r["address"] for r in conn.execute("SELECT address FROM wallets").fetchall()]
    conn.executemany(
        "UPDATE wallets SET alias = ? WHERE address = ?",
        [(alias_for(a), a) for a in addresses],
    )
    conn.commit()
    print(f"assigned aliases to {len(addresses)} wallets")
    conn.close()


if __name__ == "__main__":
    run()
