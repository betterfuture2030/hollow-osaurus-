#!/usr/bin/env python3
"""Live monitor: tails memory/events.jsonl. Uses rich when available."""

import argparse
import json
import time
from pathlib import Path

from substrate import DEFAULT_ROOT

try:
    from rich.console import Console

    console = Console()

    STYLE = {
        "thought": "italic cyan",
        "goal": "bold green",
        "goal_completed": "bold green",
        "goal_abandoned": "bold red",
        "validation_failed": "red",
        "capability": "white",
        "capability_locked": "bold red",
        "lesson": "bold magenta",
        "peer_read": "yellow",
        "control": "blue",
        "error": "bold red",
        "cycle": "dim",
        "decide": "dim",
        "validation": "dim",
    }

    def emit(ev):
        style = STYLE.get(ev["kind"], "white")
        console.print(
            f"[dim]{ev['ts'][11:19]}[/dim] [bold]{ev['agent']:<8}[/bold] "
            f"[{style}]{ev['kind']:<18}[/{style}] {ev['detail']}"
        )

except ImportError:

    def emit(ev):
        print(f"{ev['ts'][11:19]} {ev['agent']:<8} {ev['kind']:<18} {ev['detail']}")


def follow(path: Path, replay: int):
    pos = 0
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines[-replay:]:
            try:
                emit(json.loads(line))
            except json.JSONDecodeError:
                pass
        pos = path.stat().st_size
    print("--- following (ctrl-c to quit) ---")
    while True:
        if path.exists() and path.stat().st_size > pos:
            with open(path, "r", encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    try:
                        emit(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                pos = f.tell()
        time.sleep(0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hollow live monitor")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--replay", type=int, default=30, help="past events to show first")
    args = parser.parse_args()
    try:
        follow(Path(args.root) / "memory" / "events.jsonl", args.replay)
    except KeyboardInterrupt:
        pass
