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
from substrate.agents import goal_selection_prompt
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

    # --- stagnation eases on real action (the path-out escape hatch) -----
    s2 = habitat.suffering["analyst"]
    s2.raise_stressor("stagnation", 1.0, "test wedge")
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "acting through path-out capabilities",
        "action": "continue",
        "steps": [{"capability": "fs_list", "args": {"path": "."}}],
    }
    habitat.run_cycle("analyst")
    habitat.llm.json_chat = real_json_chat
    check("productive cycle eases stagnation",
          s2.stressors.get("stagnation", {}).get("severity", 1.0) < 1.0,
          str(s2.stressors.get("stagnation")))
    s2.resolve("stagnation")

    # --- futility eases on real action too (the abandon-treadmill fix) ---
    s2.raise_stressor("futility", 1.0, "test wedge")
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "acting through path-out capabilities",
        "action": "continue",
        "steps": [{"capability": "fs_list", "args": {"path": "."}}],
    }
    habitat.run_cycle("analyst")
    habitat.llm.json_chat = real_json_chat
    check("productive cycle eases futility",
          s2.stressors.get("futility", {}).get("severity", 1.0) < 1.0,
          str(s2.stressors.get("futility")))
    s2.resolve("futility")

    # --- voluntary abandonment + shared/ artifacts survive cleanup -------
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "make a shared and a private artifact",
        "action": "new_goal",
        "goal": {"title": "Temporary abandonment probe",
                 "description": "write one shared and one private file, then walk away",
                 "success_criteria": "both files exist with substance"},
        "steps": [
            {"capability": "fs_write", "args": {"path": "shared/keepme.md", "content": "s" * 200}},
            {"capability": "fs_write", "args": {"path": "tempjunk.md", "content": "p" * 200}},
        ],
    }
    habitat.run_cycle("builder")
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "this goal is unachievable; abandoning",
        "action": "abandon_goal",
        "steps": [],
    }
    habitat.run_cycle("builder")
    habitat.llm.json_chat = real_json_chat
    check("voluntary abandon_goal closes the goal at a futility cost",
          habitat.goals["builder"].active() is None
          and "futility" in habitat.suffering["builder"].stressors)
    check("abandonment cleanup spares shared/ artifacts",
          (habitat.memory.workspace / "shared" / "keepme.md").is_file()
          and not (habitat.memory.workspace / "builder" / "tempjunk.md").exists())
    habitat.suffering["builder"].resolve("futility")

    # --- retracted lessons never re-promote ------------------------------
    habitat.lessons.retract("idling is the only valid action when writes are locked")
    habitat.lessons.observe(
        "Idling is the only valid action when writes are locked",
        "constraints", "builder", confidence="high")
    promoted_now = json.loads((habitat.memory.dir / "lessons.json").read_text())
    check("retracted lesson cannot re-promote",
          not any("only valid action" in l["text"] for l in promoted_now), str(promoted_now))

    # --- fallback avoids locked fs_write at DOMINANT load -----------------
    sb = habitat.suffering["builder"]
    sb.raise_stressor("stagnation", 1.0, "test wedge")
    habitat.llm.json_chat = lambda *a, **k: {"garbage": True}
    habitat.run_cycle("builder")
    habitat.llm.json_chat = real_json_chat
    check("fallback uses a path-out step when fs_write is locked",
          habitat.outcomes["builder"][-1] == "success", str(habitat.outcomes["builder"][-3:]))
    sb.resolve("stagnation")

    # --- perception digest ------------------------------------------------
    digest = habitat._since_last_cycle("builder")
    check("digest reports the stagnation ease",
          any("stagnation eased" in c for c in digest), str(digest))
    ctx = {
        "cycle": 99, "rules": "", "suffering": {"load": 0, "tier": "NORMAL", "stressors": {}},
        "since_last_cycle": digest, "goal": None,
        "workspace": {"own": [], "shared": []}, "peers": [],
        "completed_titles": ["Old goal title"],
        "capabilities": "fs_read", "locked": "", "recent_outcomes": [],
    }
    _, user_prompt = goal_selection_prompt("builder", ctx)
    check("prompt renders digest and completed goals",
          "SINCE YOUR LAST CYCLE" in user_prompt and "Old goal title" in user_prompt)

    # --- ghost shared files leave the perception prompt -------------------
    (habitat.memory.workspace / "shared" / "keepme.md").unlink()
    check("deleted shared files are filtered from peer artifacts",
          not any(p["file"] == "shared/keepme.md" for p in habitat._peer_artifacts("scout")),
          str(habitat._peer_artifacts("scout")))

    # --- step-failure warnings survive an idle cycle -----------------------
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "try a dead path", "action": "continue",
        "steps": [{"capability": "fs_read", "args": {"path": "does/not/exist.md"}}],
    }
    habitat.run_cycle("builder")
    habitat.llm.json_chat = lambda *a, **k: {"thought": "rest", "action": "idle", "steps": []}
    habitat.run_cycle("builder")
    habitat.llm.json_chat = real_json_chat
    check("step-failure warnings persist past an idle cycle",
          any("does/not/exist.md" in m for _, m in habitat.last_step_errors["builder"]),
          str(habitat.last_step_errors["builder"]))

    # --- placeholder detection: stubs flagged, meta-mentions spared -------
    from substrate.validation import find_placeholder
    check("placeholder check flags stubs but spares meta-mentions",
          find_placeholder("Section 2\nTODO: write this part") == "todo"
          and find_placeholder("Quality: No placeholders (e.g., TODO, FIXME).") is None
          and find_placeholder("My toDoList app tracks tasks.") is None)

    # --- validation failure reasons reach the agent's next prompt ---------
    vgoal = habitat.goals["builder"].create("Reason probe", "d" * 50, "c" * 30)
    habitat.goals["builder"].fail_validation(vgoal, "layer3: artifact too thin")
    _, vprompt = goal_selection_prompt("builder", {**ctx, "goal": vgoal})
    habitat.goals["builder"].abandon(vgoal)
    check("validation failure reason is shown in the goal block",
          "why_validation_last_failed" in vprompt and "artifact too thin" in vprompt)

    # --- long artifacts get a labeled evidence cut, not a silent one ------
    from substrate.validation import build_evidence, EVIDENCE_CHARS_PER_ARTIFACT
    long_ev = build_evidence([("a.md", "x" * (EVIDENCE_CHARS_PER_ARTIFACT + 500))])
    short_ev = build_evidence([("b.md", "short and complete")])
    check("evidence truncation is labeled for the semantic judge",
          "NOTE TO VALIDATOR" in long_ev and "NOTE TO VALIDATOR" not in short_ev)

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
