#!/usr/bin/env python3
"""Hollow-on-Osaurus entry point.

First run walks a setup wizard (probes Osaurus, picks a model from what
you actually have installed). After that:

    python3 hollow.py            # start the habitat + operator API
    python3 hollow.py stop       # stop a running habitat
    python3 hollow.py status     # ping the operator API
"""

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from substrate import DEFAULT_CONFIG, DEFAULT_ROOT, load_config, save_config
from substrate.llm import OsaurusClient
from substrate.loop import Habitat
from substrate.server import start_server

OSAURUS_HELP = """\
Osaurus doesn't seem to be running at {base_url}.

  1. Open the Osaurus app (https://github.com/dinoki-ai/osaurus — or:
     brew install --cask osaurus)
  2. Start the server from the menu bar (note the port; default 1337)
  3. In Osaurus, download at least one model. On a 24 GB machine the
     sweet spot is a Qwen3-30B-A3B 4-bit MLX build; Qwen3-4B-4bit is a
     solid lightweight fallback.
  4. Re-run: python3 hollow.py
"""


def rank_model(model_id: str) -> int:
    """Crude preference order for the wizard's recommendation."""
    m = model_id.lower()
    if "30b-a3b" in m or "a3b" in m:
        return 0
    for marker, rank in (("14b", 1), ("8b", 2), ("7b", 3), ("4b", 4), ("3b", 5)):
        if marker in m:
            return rank
    return 6


def wizard(config_path: Path, base_url: str, assume_yes: bool) -> dict:
    print("Hollow-on-Osaurus setup")
    print(f"  probing Osaurus at {base_url} ...")
    client = OsaurusClient(base_url, "")
    if not client.health():
        print(OSAURUS_HELP.format(base_url=base_url))
        sys.exit(1)
    models = client.list_models()
    if not models:
        print("  Osaurus is up but has no models installed.")
        print("  Download one in the Osaurus app first (see step 3 above).")
        sys.exit(1)

    ranked = sorted(models, key=rank_model)
    print("  installed models:")
    for i, m in enumerate(ranked):
        marker = "  <- recommended" if i == 0 else ""
        print(f"    [{i}] {m}{marker}")
    choice = 0
    if not assume_yes and sys.stdin.isatty():
        raw = input(f"  pick a model [0-{len(ranked) - 1}] (default 0): ").strip()
        if raw.isdigit() and int(raw) < len(ranked):
            choice = int(raw)
    default_model = ranked[choice]
    fallback = ranked[-1] if len(ranked) > 1 else ""

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["osaurus"]["base_url"] = base_url
    cfg["osaurus"]["default_model"] = default_model
    cfg["osaurus"]["fallback_model"] = fallback
    save_config(config_path, cfg)
    print(f"  wrote {config_path} (model: {default_model})")
    return cfg


def cmd_run(args):
    root = Path(args.root)
    config_path = Path(args.config) if args.config else root / "config.json"
    if config_path.exists():
        cfg = load_config(config_path)
    else:
        cfg = wizard(config_path, args.base_url, args.yes)

    pidfile = root / "memory" / "hollow.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text().strip()), 0)
            print("hollow already running (memory/hollow.pid). Use: python3 hollow.py stop")
            sys.exit(1)
        except (OSError, ValueError):
            pidfile.unlink()

    habitat = Habitat(root, cfg)
    port = cfg["runtime"]["api_port"]
    server = start_server(habitat, port)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))

    def shutdown(signum, frame):
        print("\nstopping habitat (state persists) ...")
        habitat.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"habitat up: 3 agents, operator API on http://127.0.0.1:{port}")
    print("watch live:   python3 thoughts.py")
    print("inject tasks: python3 submit_task.py <agent> \"<message>\"")
    try:
        habitat.run(max_rounds=args.rounds, interval=args.interval)
    finally:
        server.shutdown()
        if pidfile.exists():
            pidfile.unlink()
    print("habitat stopped.")


def cmd_stop(args):
    pidfile = Path(args.root) / "memory" / "hollow.pid"
    if not pidfile.exists():
        print("no pidfile; habitat not running?")
        return
    pid = int(pidfile.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {pid}")
    except OSError as e:
        print(f"could not signal {pid}: {e}; removing stale pidfile")
        pidfile.unlink()


def cmd_status(args):
    import httpx

    cfg_path = Path(args.config) if args.config else Path(args.root) / "config.json"
    port = DEFAULT_CONFIG["runtime"]["api_port"]
    if cfg_path.exists():
        port = load_config(cfg_path)["runtime"]["api_port"]
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/state", timeout=5)
        print(json.dumps(r.json(), indent=2))
    except httpx.HTTPError:
        print(f"operator API not reachable on port {port}; habitat not running?")


def main():
    parser = argparse.ArgumentParser(description="Hollow-on-Osaurus habitat")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "stop", "status"])
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="data root (default: this folder)")
    parser.add_argument("--config", default=None, help="explicit config.json path")
    parser.add_argument("--base-url", default=DEFAULT_CONFIG["osaurus"]["base_url"])
    parser.add_argument("--rounds", type=int, default=None, help="run N rounds then exit (testing)")
    parser.add_argument("--interval", type=float, default=None, help="seconds between rounds")
    parser.add_argument("--yes", action="store_true", help="non-interactive wizard: accept defaults")
    args = parser.parse_args()
    {"run": cmd_run, "stop": cmd_stop, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    main()
