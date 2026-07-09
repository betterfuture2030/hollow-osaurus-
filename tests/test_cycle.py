#!/usr/bin/env python3
"""End-to-end habitat test against the stub Osaurus server.

Run from the hollow/ directory:  python3 tests/test_cycle.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from substrate import AGENT_NAMES, DEFAULT_CONFIG
from substrate.loop import Habitat
from substrate.server import start_server
from substrate.validation import validate_goal
from tests.stub_osaurus import start_stub

PASS = 0


def check(label, cond, detail=""):
    global PASS
    if not cond:
        print(f"FAIL  {label}  {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok    {label}")


def build_habitat(port):
    root = Path(tempfile.mkdtemp(prefix="hollow-test-"))
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["osaurus"]["base_url"] = f"http://127.0.0.1:{port}/v1"
    cfg["osaurus"]["default_model"] = "mlx-community/Qwen3-30B-A3B-4bit"
    cfg["runtime"]["cycle_interval_seconds"] = 0
    return Habitat(root, cfg), root


def main():
    stub, port, counters = start_stub()
    habitat, root = build_habitat(port)

    # --- full cycles: goals, artifacts, completion, lessons -------------
    habitat.run(max_rounds=5, interval=0)
    check("all agents ran 5 cycles", all(habitat.cycle[a] == 5 for a in AGENT_NAMES),
          str(habitat.cycle))

    completed = {
        a: [g for g in habitat.goals[a].goals.values() if g["status"] == "completed"]
        for a in AGENT_NAMES
    }
    check("every agent completed a validated goal",
          all(completed[a] for a in AGENT_NAMES), str({a: len(v) for a, v in completed.items()}))

    note = habitat.memory.workspace / "scout" / "notes" / "cycle-001.md"
    check("private artifact written", note.is_file() and len(note.read_text()) > 120)
    shared = habitat.memory.workspace / "shared" / "builder-report.md"
    check("shared artifact written + manifest attribution",
          shared.is_file()
          and habitat.memory.shared_manifest().get("shared/builder-report.md", {}).get("author") == "builder")

    events = habitat.memory.recent_events(500)
    kinds = {e["kind"] for e in events}
    check("event stream covers cycle/goal/capability/completion",
          {"cycle", "goal", "capability", "goal_completed"} <= kinds, str(kinds))

    lessons = json.loads((habitat.memory.dir / "lessons.json").read_text())
    check("lesson promoted after 2 independent observations",
          any("shared/" in l["text"] for l in lessons), str(lessons))

    # --- suffering gating -------------------------------------------------
    s = habitat.suffering["scout"]
    s.raise_stressor("existential_threat", 0.5, "test")
    s.raise_stressor("repeated_failure", 0.3, "test")
    check("load reaches DOMINANT", s.tier == "DOMINANT", f"load={s.load}")
    blocked = habitat.caps.dispatch("scout", "fs_write",
                                    {"path": "gated.md", "content": "x" * 200})
    check("fs_write locked at DOMINANT load",
          not blocked["ok"] and "locked" in blocked["error"], str(blocked))
    readable = habitat.caps.dispatch("scout", "fs_read", {"path": "notes/cycle-001.md"})
    check("path-out capability fs_read still works", readable["ok"])
    s.resolve("existential_threat")
    s.resolve("repeated_failure")
    s.resolve("capability_lock")

    # --- workspace confinement -----------------------------------------
    escape = habitat.caps.dispatch("scout", "fs_read", {"path": "../analyst/notes/cycle-001.md"})
    check("path escape rejected", not escape["ok"], str(escape))

    # --- claude bridge quality gates ------------------------------------
    b = habitat.bridge
    ok, msg = b.submit("scout", "too short", "spec " * 30)
    check("vague description rejected", not ok, msg)
    ok, msg = b.submit("scout", "d" * 50, "please manage the claude_requests queue for me " * 3)
    check("circular request rejected", not ok, msg)
    ids = []
    for i in range(3):
        ok, rid = b.submit("scout",
                           f"Add a small utility number {i} that agents genuinely need for work",
                           "Create a helper in the substrate that exposes workspace disk usage to "
                           "agents so resource_burden becomes observable before it stings.")
        check(f"well-formed request {i} accepted", ok, rid)
        ids.append(rid)
    ok, msg = b.submit("scout", "d" * 50, "s" * 100)
    check("queue cap of 3 enforced", not ok and "cap" in msg, msg)
    with open(habitat.memory.dir / "claude_responses.jsonl", "a") as f:
        f.write(json.dumps({"request_id": ids[0], "status": "implemented",
                            "response": "added in substrate/capabilities.py"}) + "\n")
    fresh = b.new_responses("scout")
    check("response surfaces exactly once",
          len(fresh) == 1 and b.new_responses("scout") == [], str(fresh))

    # --- validation failure -> abandonment + cleanup ---------------------
    reg = habitat.goals["builder"]
    for g in list(reg.goals.values()):
        if g["status"] == "active":
            reg.abandon(g)
    goal = reg.create("hollow goal", "this evidence should force-fail semantic review",
                      "n/a force-fail")
    for i in range(5):
        r = habitat.caps.dispatch("builder", "fs_write",
                                  {"path": f"junk/j{i}.md", "content": ("real enough text " * 12) + str(i)})
        reg.record_step(goal, "fs_write", r["ok"], "w", r.get("artifact"))
    check("progress reached 1.0", goal["progress"] >= 1.0, str(goal["progress"]))
    passed, failures = validate_goal(goal, habitat.memory.workspace, habitat.llm, habitat.memory)
    check("semantic layer fails hollow evidence",
          not passed and any("layer4" in f for f in failures), str(failures))
    abandoned = False
    for _ in range(5):
        abandoned = reg.fail_validation(goal)
    check("5th validation failure abandons goal", abandoned and goal["status"] == "abandoned")
    reg.cleanup_artifacts(goal, habitat.memory.workspace)
    check("abandoned goal artifacts cleaned",
          not (habitat.memory.workspace / "builder" / "junk" / "j0.md").exists())

    # --- grounded fallback when model output is unusable ----------------
    real_json_chat = habitat.llm.json_chat
    habitat.llm.json_chat = lambda *a, **k: {"garbage": True}
    habitat.run_cycle("analyst")
    habitat.llm.json_chat = real_json_chat
    fallback_notes = list((habitat.memory.workspace / "analyst" / "observations").glob("*.md"))
    check("fallback wrote a grounded observation", len(fallback_notes) >= 1)

    # --- operator API ----------------------------------------------------
    server = start_server(habitat, 0)
    api = f"http://127.0.0.1:{server.server_address[1]}"
    check("/health", httpx.get(f"{api}/health").json()["ok"])
    state = httpx.get(f"{api}/state").json()
    check("/state exposes all agents + suffering",
          set(state) == set(AGENT_NAMES) and "load" in state["scout"]["suffering"])
    httpx.post(f"{api}/inject", json={"agent": "scout", "message": "hello from the host"})
    msgs = habitat.memory.drain_host_messages("scout")
    check("/inject round-trips into host messages",
          any("hello from the host" in m["message"] for m in msgs), str(msgs))
    httpx.post(f"{api}/suspend", json={"agent": "scout"})
    before = habitat.cycle["scout"]
    habitat.run_cycle("scout")
    check("suspended agent skips its cycle", habitat.cycle["scout"] == before)
    httpx.post(f"{api}/resume", json={"agent": "scout"})
    habitat.run_cycle("scout")
    check("resumed agent cycles again", habitat.cycle["scout"] == before + 1)

    server.shutdown()
    stub.shutdown()
    print(f"\nALL {PASS} CHECKS PASSED  (state under {root})")


if __name__ == "__main__":
    main()
