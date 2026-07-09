"""Hollow-on-Osaurus substrate.

A Mac-native rebuild of the Hollow AgentOS substrate that talks to an
OpenAI-compatible local model server (Osaurus) instead of Ollama.
"""

import json
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

AGENT_NAMES = ("scout", "analyst", "builder")

DEFAULT_CONFIG = {
    "osaurus": {
        "base_url": "http://127.0.0.1:1337/v1",
        "default_model": "",
        "fallback_model": "",
        "timeout_seconds": 180,
    },
    "runtime": {
        "cycle_interval_seconds": 20,
        "api_port": 7777,
        "max_steps_per_cycle": 2,
    },
}


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for section, values in cfg.items():
        merged.setdefault(section, {})
        if isinstance(values, dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
