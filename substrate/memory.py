"""Persistent state: kv store, audit log, event stream, host messages."""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import AGENT_NAMES


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


class Memory:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / "memory"
        self.workspace = self.root / "workspace"
        self._lock = threading.Lock()
        for agent in AGENT_NAMES:
            (self.dir / "goals" / agent).mkdir(parents=True, exist_ok=True)
            (self.workspace / agent).mkdir(parents=True, exist_ok=True)
        (self.workspace / "shared").mkdir(parents=True, exist_ok=True)
        (self.dir / "kv").mkdir(parents=True, exist_ok=True)
        (self.dir / "host").mkdir(parents=True, exist_ok=True)

    # -- kv store (per agent) -------------------------------------------
    def _kv_path(self, agent: str) -> Path:
        return self.dir / "kv" / f"{agent}.json"

    def kv_set(self, agent: str, key: str, value) -> None:
        with self._lock:
            kv = read_json(self._kv_path(agent), {})
            kv[key] = value
            write_json(self._kv_path(agent), kv)

    def kv_get(self, agent: str, key: str, default=None):
        return read_json(self._kv_path(agent), {}).get(key, default)

    # -- audit + events ---------------------------------------------------
    def audit(self, agent: str, capability: str, args_summary: str, status: str, detail: str = "") -> None:
        append_jsonl(
            self.dir / "audit.jsonl",
            {
                "ts": now_iso(),
                "agent": agent,
                "capability": capability,
                "args": args_summary[:400],
                "status": status,
                "detail": detail[:400],
            },
        )

    def event(self, agent: str, kind: str, detail: str) -> None:
        append_jsonl(
            self.dir / "events.jsonl",
            {"ts": now_iso(), "t": time.time(), "agent": agent, "kind": kind, "detail": detail[:600]},
        )

    def recent_events(self, n: int = 50):
        return read_jsonl(self.dir / "events.jsonl")[-n:]

    # -- host messages ---------------------------------------------------
    def push_host_message(self, agent: str, message: str) -> None:
        append_jsonl(self.dir / "host" / f"{agent}.jsonl", {"ts": now_iso(), "message": message})

    def drain_host_messages(self, agent: str):
        path = self.dir / "host" / f"{agent}.jsonl"
        with self._lock:
            msgs = read_jsonl(path)
            if msgs:
                path.unlink()
        return msgs

    # -- shared workspace manifest (who authored which shared file) ------
    def record_shared_author(self, rel_path: str, agent: str) -> None:
        with self._lock:
            manifest = read_json(self.dir / "shared_manifest.json", {})
            manifest[rel_path] = {"author": agent, "ts": now_iso()}
            write_json(self.dir / "shared_manifest.json", manifest)

    def shared_manifest(self) -> dict:
        return read_json(self.dir / "shared_manifest.json", {})
