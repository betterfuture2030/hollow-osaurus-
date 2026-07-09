#!/usr/bin/env python3
"""Inject a host message/task into a named agent's next cycle."""

import argparse
import sys

import httpx

from substrate import AGENT_NAMES, DEFAULT_CONFIG

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Submit a task to an agent")
    parser.add_argument("agent", choices=AGENT_NAMES)
    parser.add_argument("message", help="the task / host message text")
    parser.add_argument("--port", type=int, default=DEFAULT_CONFIG["runtime"]["api_port"])
    args = parser.parse_args()
    try:
        r = httpx.post(
            f"http://127.0.0.1:{args.port}/inject",
            json={"agent": args.agent, "message": args.message},
            timeout=5,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"failed: {e} — is the habitat running? (python3 hollow.py)")
        sys.exit(1)
    print(f"queued for {args.agent}; it lands at the start of their next cycle")
