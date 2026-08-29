"""End-to-end demo: generate a key, register, list, patch, re-fetch, delete.

Usage:
    python examples/register_and_list.py [--base-url http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from app.client import YellowPageClient
from app.crypto import KeyPair


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--keep", action="store_true", help="don't delete at the end")
    args = parser.parse_args()

    kp = KeyPair.generate()
    name = f"demo-{uuid.uuid4().hex[:8]}"
    public_key = "ed25519:" + kp.public_b64

    print(f"[1/5] generated keypair  pub={public_key[:32]}…")

    with YellowPageClient(args.base_url) as c:
        # 1. register
        try:
            card = c.register(
                name=name,
                public_key=public_key,
                display_name="Demo Agent",
                description="An example agent registered by register_and_list.py",
                endpoint="https://example.com/agent",
                tags=["demo", "example"],
                metadata={"model": "demo", "version": "0.1.0"},
            )
        except Exception as e:
            print(f"register failed: {e}")
            return 1
        agent_id = card["id"]
        print(f"[2/5] registered         id={agent_id}  version={card['version']}")

        c.agent_id = agent_id
        c.keypair = kp

        # 2. list & search
        listing = c.list(q="demo", tags=["example"], limit=10)
        found = any(it["id"] == agent_id for it in listing["items"])
        print(f"[3/5] list(q=demo)       total={listing['total']}  found_us={found}")

        # 3. patch (signed)
        try:
            updated = c.patch(
                agent_id,
                {"description": f"updated at {time.time()}", "tags": ["demo", "patched"]},
            )
            print(f"[4/5] patched            version={updated['version']}  tags={updated['tags']}")
        except Exception as e:
            print(f"patch failed: {e}")
            return 1

        # 4. challenge roundtrip
        chal = c.challenge(name)
        print(f"[5/5] challenge          expires_at={chal['expires_at']}")

        if not args.keep:
            c.delete(agent_id)
            print("[cleanup] deleted agent")

    print("\nOK — full round-trip works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
