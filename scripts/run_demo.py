"""One-command local demo. No API key required, no network calls: seeds/refreshes the
curated demo ledger via scripts/build_demo_ledger.py (which drives every session through
FakeLLMClient), prints a ledger summary, then launches the Streamlit app.

Usage:
    python scripts/run_demo.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from agent_commerce.ledger.store import LedgerStore

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_LEDGER_PATH = REPO_ROOT / "demo_data" / "demo_ledger.db"

# scripts/ has no __init__.py (it's a bag of standalone entry points, not a package) — running
# `python scripts/run_demo.py` puts scripts/ itself, not the repo root, on sys.path[0], so
# `import scripts.build_demo_ledger` fails. Import the sibling module directly by path instead.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import build_demo_ledger  # noqa: E402


def _print_summary() -> None:
    ledger = LedgerStore(DEMO_LEDGER_PATH, read_only=True)
    print("\nDemo ledger summary:")
    for transaction_id in ledger.list_transaction_ids():
        entries = ledger.entries_for_transaction(transaction_id)
        actions = ", ".join(e.action_type.value for e in entries)
        print(f"  {transaction_id}: {len(entries)} entries ({actions})")
    verification = ledger.verify_chain()
    print(f"\nverify_chain(): ok={verification.ok} entries_checked={verification.entries_checked}")
    ledger.close()


def main() -> None:
    print("Seeding demo ledger (FakeLLMClient — no API key, no network calls)...")
    asyncio.run(build_demo_ledger.main())

    _print_summary()

    print("\nLaunching Streamlit app (Ctrl+C to stop)...")
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(REPO_ROOT / "app.py")], check=False
    )


if __name__ == "__main__":
    main()
