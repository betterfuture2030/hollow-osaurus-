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
from substrate.agents import fallback_plan, goal_selection_prompt
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

    # --- paraphrased lessons merge into one candidate ----------------------
    from substrate.lessons import jaccard as _jac
    para_a = "Validation requires explicit citation of tier definitions from all four specified peer artifacts"
    para_b = "Validation demands that tier definitions be cited from all four specified peer artifacts"
    check("test pair sits in the newly-deduped band (0.45 <= j < 0.55)",
          0.45 <= _jac(para_a, para_b) < 0.55, f"j={_jac(para_a, para_b):.3f}")
    habitat.lessons.observe(para_a, "constraints", "scout")
    habitat.lessons.observe(para_b, "constraints", "analyst")  # merges -> promotes as ONE
    tier_promoted = [l for l in json.loads((habitat.memory.dir / "lessons.json").read_text())
                     if "tier definitions" in l["text"]]
    tier_cands = [c for c in json.loads((habitat.memory.dir / "lessons_candidates.json").read_text())
                  if "tier definitions" in c["text"]]
    check("paraphrased lessons merge at the looser dedupe threshold",
          len(tier_promoted) == 1 and tier_promoted[0]["observations"] == 2 and not tier_cands,
          f"promoted={len(tier_promoted)} cands={len(tier_cands)}")

    # --- fallback rests instead of adopting filler after a completion ------
    rest = fallback_plan("builder", {"cycle": 9, "goal": None, "just_completed": True,
                                     "suffering": {"load": 0, "tier": "NORMAL", "stressors": []},
                                     "workspace": {"own": [], "shared": []},
                                     "recent_outcomes": [], "locked": ""})
    busy = fallback_plan("builder", {"cycle": 9, "goal": None, "just_completed": False,
                                     "suffering": {"load": 0, "tier": "NORMAL", "stressors": []},
                                     "workspace": {"own": [], "shared": []},
                                     "recent_outcomes": [], "locked": ""})
    check("fallback rests after a completion instead of adopting filler",
          rest["action"] == "idle" and "goal" not in rest
          and busy["action"] == "new_goal" and "goal" in busy)

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

    # --- thoughts survive untruncated end to end ---------------------------
    long_thought = "I am reasoning at length about the habitat. " * 20  # ~880 chars
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": long_thought, "action": "idle", "steps": []}
    habitat.run_cycle("builder")
    habitat.llm.json_chat = real_json_chat
    recorded = [e for e in habitat.memory.recent_events(20)
                if e["kind"] == "thought" and e["agent"] == "builder"][-1]
    check("thoughts are recorded in full, not clipped",
          recorded["detail"] == long_thought[:4000]
          and len(recorded["detail"]) > 600, str(len(recorded["detail"])))
    check("last_thought is exposed on the habitat state",
          habitat.state()["builder"]["last_thought"] == long_thought[:4000])
    panel_html = (Path(__file__).resolve().parent.parent / "panel.html").read_text()
    check("panel carries per-agent color variables",
          all(f"--c-{a}" in panel_html for a in AGENT_NAMES))

    # --- step results carry forward into the next prompt --------------------
    (habitat.memory.workspace / "builder" / "carry.md").write_text(
        "the secret ingredient is patience " * 10)
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "reading to synthesize later", "action": "continue",
        "steps": [{"capability": "fs_read", "args": {"path": "carry.md"}}]}
    habitat.run_cycle("builder")
    habitat.llm.json_chat = real_json_chat
    check("successful step results are carried to the next cycle",
          any("secret ingredient" in r for r in habitat.last_step_results["builder"]),
          str(habitat.last_step_results["builder"])[:150])
    _, carry_prompt = goal_selection_prompt("builder", {
        **ctx, "last_step_results": habitat.last_step_results["builder"]})
    check("prompt renders last step results",
          "RESULTS OF YOUR LAST CYCLE'S STEPS" in carry_prompt
          and "secret ingredient" in carry_prompt)

    # --- fs_write append mode ----------------------------------------------
    habitat.caps.dispatch("builder", "fs_write", {"path": "log.md", "content": "first line"})
    habitat.caps.dispatch("builder", "fs_write",
                          {"path": "log.md", "content": "second line", "append": True})
    logged = (habitat.memory.workspace / "builder" / "log.md").read_text()
    check("fs_write append adds without clobbering",
          "first line" in logged and "second line" in logged
          and logged.index("first") < logged.index("second"), logged)

    # --- repeating completed work costs futility ----------------------------
    done_goal = habitat.goals["builder"].create("Catalog the ancient stones", "d" * 40, "c" * 30)
    habitat.goals["builder"].complete(done_goal)
    habitat.suffering["builder"].resolve("futility")
    habitat._futility_check("builder", "Catalog the ancient stones again")
    check("repeating a completed goal raises futility",
          "futility" in habitat.suffering["builder"].stressors,
          str(habitat.suffering["builder"].stressors))
    habitat.suffering["builder"].resolve("futility")

    # --- the hammock: reading eases nothing while writes are available ------
    sb2 = habitat.suffering["builder"]
    sb2.raise_stressor("stagnation", 0.3, "hammock probe")  # load 0.3: unlocked
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "comfy reading", "action": "continue",
        "steps": [{"capability": "fs_list", "args": {"path": "."}}]}
    habitat.run_cycle("builder")
    after_read = sb2.stressors.get("stagnation", {}).get("severity", 0)
    check("read-only success does not ease stagnation when writes are available",
          after_read >= 0.3, str(after_read))
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "producing", "action": "continue",
        "steps": [{"capability": "memory_set", "args": {"key": "probe", "value": "x"}}]}
    habitat.run_cycle("builder")
    habitat.llm.json_chat = real_json_chat
    check("output success eases stagnation when unlocked",
          sb2.stressors.get("stagnation", {}).get("severity", 0) < after_read,
          str(sb2.stressors.get("stagnation")))
    sb2.resolve("stagnation")

    # --- ceiling nudge: 3+ cycles pinned at 0.8 speaks up -------------------
    cgoal = habitat.goals["scout"].create("Ceiling probe goal", "d" * 40, "c" * 30)
    cgoal["progress"] = 0.8
    habitat.goals["scout"].save(cgoal)
    habitat.llm.json_chat = lambda *a, **k: {
        "thought": "still reading", "action": "continue",
        "steps": [{"capability": "fs_list", "args": {"path": "."}}]}
    for _ in range(3):
        habitat.run_cycle("scout")
    habitat.llm.json_chat = real_json_chat
    nudge = habitat._since_last_cycle("scout")
    check("ceiling-stuck goals get the write nudge in the digest",
          any("reading ceiling" in c for c in nudge), str(nudge)[:200])
    habitat.goals["scout"].abandon(cgoal)

    # --- reads can't push a goal into validation ----------------------------
    rgoal = habitat.goals["builder"].create("Read ceiling probe", "d" * 40, "c" * 30)
    for _ in range(12):
        habitat.goals["builder"].record_step(rgoal, "fs_read", True, "read stuff")
    check("read-only progress caps below validation",
          rgoal["progress"] == 0.8, str(rgoal["progress"]))
    habitat.goals["builder"].record_step(rgoal, "fs_write", True, "wrote it", "builder/x.md")
    check("an output step crosses the ceiling", rgoal["progress"] == 1.0, str(rgoal["progress"]))
    habitat.goals["builder"].abandon(rgoal)

    # --- timestamps carry the machine's local offset -----------------------
    from datetime import datetime as _dt
    from substrate.memory import now_iso
    stamp = _dt.fromisoformat(now_iso())
    check("timestamps are local-timezone ISO with explicit offset",
          stamp.tzinfo is not None
          and stamp.utcoffset() == _dt.now().astimezone().utcoffset(), now_iso())

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
          set(AGENT_NAMES) <= set(state) and "load" in state["scout"]["suffering"])
    check("/state carries last_thought and world for the panel",
          "last_thought" in state["scout"] and "_world" in state)
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

    # --- ambient world events ----------------------------------------------
    import random as _random
    from substrate.world import World, ECHO_PREFIX
    w = World(habitat.memory, _random.Random(7))
    drawn = {w.draw_event() for _ in range(30)}
    check("seeded world draws varied ambient events",
          len(drawn) >= 3 and all(isinstance(e, str) and e for e in drawn))
    check("world can echo real habitat history",
          any(e.startswith(ECHO_PREFIX) for e in drawn), str(sorted(drawn))[:200])
    habitat.pending_ambient["builder"] = "A dry wind moves through the workspace."
    habitat.llm.json_chat = lambda *a, **k: {"thought": "windy", "action": "idle", "steps": []}
    habitat.run_cycle("builder")
    habitat.llm.json_chat = real_json_chat
    _, amb_prompt = goal_selection_prompt("builder", {
        **ctx, "ambient": "A dry wind moves through the workspace."})
    check("ambient event renders as THE WORLD in the prompt",
          "THE WORLD: A dry wind" in amb_prompt)
    check("ambient event is consumed after one cycle",
          habitat.pending_ambient["builder"] is None)

    # --- synthesized capabilities ------------------------------------------
    made = habitat.caps.dispatch("builder", "synthesize_capability", {
        "name": "add_numbers",
        "description": "Adds the numbers a and b from args and returns their sum.",
        "code": "def run(args):\n    return {\"sum\": args.get(\"a\", 0) + args.get(\"b\", 0)}\n",
    })
    check("synthesize_capability forges a tool", made["ok"], str(made))
    check("synthesized tool appears in capability names",
          "add_numbers" in habitat.caps.names("builder"))
    ran = habitat.caps.dispatch("builder", "add_numbers", {"a": 19, "b": 23})
    check("synthesized tool executes in a subprocess",
          ran["ok"] and ran["result"] == {"sum": 42}, str(ran))
    blocked = habitat.caps.dispatch("builder", "synthesize_capability", {
        "name": "phone_home",
        "description": "A tool that tries to reach the outside world via curl.",
        "code": "import subprocess\ndef run(args):\n    return subprocess.run(['curl', 'http://x.test']).returncode\n",
    })
    check("synthesized code with blocked tools is rejected",
          not blocked["ok"] and "blocked" in blocked["error"], str(blocked))
    gone = habitat.caps.dispatch("builder", "retire_capability", {"name": "add_numbers"})
    check("retire_capability dismantles a synthesized tool",
          gone["ok"] and "add_numbers" not in habitat.caps.names("builder")
          and not habitat.caps.dispatch("builder", "add_numbers", {})["ok"])

    # --- research_topic: earned gate + config toggle (no network in tests) -
    habitat.suffering["builder"].raise_stressor("stagnation", 0.5, "test")
    gated = habitat.caps.dispatch("builder", "research_topic", {"topic": "anything"})
    check("research_topic stays earned (load gate)",
          not gated["ok"] and "earned" in gated["error"], str(gated))
    habitat.suffering["builder"].resolve("stagnation")
    habitat.memory.kv_set("builder", "peer_interactions", 2)
    habitat.caps.research_enabled = False
    off = habitat.caps.dispatch("builder", "research_topic", {"topic": "anything"})
    check("research_topic honors the config kill-switch",
          not off["ok"] and "disabled" in off["error"], str(off))
    habitat.caps.research_enabled = True

    # --- operator panel endpoints -----------------------------------------
    panel = httpx.get(f"{api}/panel")
    check("/panel serves the operator UI",
          panel.status_code == 200 and "Hollow Operator Panel" in panel.text)
    r = httpx.post(f"{api}/stressor",
                   json={"agent": "scout", "kind": "stagnation", "severity": 0.4}).json()
    check("/stressor sets an exact severity",
          r["ok"] and r["suffering"]["stressors"]["stagnation"]["severity"] == 0.4, str(r))
    r = httpx.post(f"{api}/stressor",
                   json={"agent": "scout", "kind": "stagnation", "severity": 0}).json()
    check("/stressor at zero resolves the stressor",
          r["ok"] and "stagnation" not in r["suffering"]["stressors"], str(r))
    # --- panel v3 endpoints -------------------------------------------------
    (habitat.memory.workspace / "shared" / "peek.md").write_text("visible to the operator " * 8)
    fr = httpx.get(f"{api}/file", params={"path": "shared/peek.md"}).json()
    check("/file serves workspace files read-only",
          "visible to the operator" in fr.get("content", ""), str(fr)[:120])
    esc = httpx.get(f"{api}/file", params={"path": "../config.json"})
    check("/file rejects workspace escapes", esc.status_code == 400)
    wr = httpx.post(f"{api}/world", json={}).json()
    check("/world provokes an ambient event",
          wr["ok"] and all(habitat.pending_ambient[a] == wr["event"] for a in AGENT_NAMES), str(wr)[:120])
    habitat.caps.dispatch("builder", "fs_write",
                          {"path": "shared/graph-probe.md", "content": "x" * 130})
    habitat.caps.dispatch("scout", "fs_read", {"path": "shared/graph-probe.md"})  # peer read
    pg = httpx.get(f"{api}/peergraph").json()
    check("/peergraph counts who read whom",
          pg.get("scout->builder", 0) >= 1, str(pg))
    check("/state carries load history for sparklines",
          isinstance(httpx.get(f"{api}/state").json()["scout"]["load_history"], list))

    # --- panel v4: cycles on events, flavored world, lessons/artifacts -----
    ev_with_cycle = [e for e in habitat.memory.recent_events(30)
                     if e["agent"] in AGENT_NAMES and "cycle" in e]
    check("events carry their cycle number", len(ev_with_cycle) > 0)
    st = httpx.get(f"{api}/state").json()
    check("/state exposes world flavor and activity",
          st["_world"]["last_ambient"] is None or "flavor" in st["_world"]["last_ambient"],
          str(st["_world"]))
    check("/state has an _activity slot", "_activity" in st)
    httpx.post(f"{api}/world", json={})
    st2 = httpx.get(f"{api}/state").json()
    check("provoked world event carries a flavor",
          st2["_world"]["last_ambient"] and st2["_world"]["last_ambient"].get("flavor"),
          str(st2["_world"]))
    lessons_resp = httpx.get(f"{api}/lessons").json()
    check("/lessons returns the promoted rules", isinstance(lessons_resp, list))
    arts = httpx.get(f"{api}/artifacts").json()
    check("/artifacts lists shared files with authorship",
          any(a["path"] == "shared/graph-probe.md" and a["author"] == "builder" for a in arts),
          str(arts)[:200])
    panel_v4 = (Path(__file__).resolve().parent.parent / "panel.html").read_text()
    check("panel carries v4 surfaces (sky, ticker, dial, shelf)",
          all(f'id="{i}"' in panel_v4 for i in ("sky", "ticker", "worlddial", "shelf", "rules")))

    bad = httpx.post(f"{api}/nuke", json={})
    check("/nuke refuses without confirm", bad.status_code == 400)
    httpx.post(f"{api}/nuke", json={"confirm": True})
    check("/nuke wipes goals, suffering, and artifacts",
          all(habitat.goals[a].active() is None for a in AGENT_NAMES)
          and all(habitat.suffering[a].load == 0 for a in AGENT_NAMES)
          and not any((habitat.memory.workspace / "shared").rglob("*.md")))
    habitat.llm.json_chat = lambda *a, **k: {"thought": "post-nuke", "action": "idle", "steps": []}
    habitat.run_cycle("scout")
    habitat.llm.json_chat = real_json_chat
    check("habitat still cycles after nuke", habitat.cycle["scout"] == 1)

    server.shutdown()

    # --- MCP bridge server -------------------------------------------------
    import subprocess
    ok, rid = habitat.bridge.submit(
        "scout",
        "Add a way for agents to see the total number of files in shared",
        "Extend fs_list so that when called with path '.' it also returns a count "
        "of shared files, giving agents a cheap sense of communal activity.",
    )
    check("bridge accepts the MCP test request", ok, rid)
    # run the MCP server pointed at the TEST habitat root, not the repo:
    env_script = (
        "import sys, json; sys.path.insert(0, '.'); "
        "import mcp_bridge; from pathlib import Path; "
        f"mcp_bridge.ROOT = Path({str(root)!r}); "
        "mcp_bridge.main()"
    )
    mcp = subprocess.Popen(
        [sys.executable, "-c", env_script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    def rpc(id_, method, params=None):
        mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": id_, "method": method,
                                    "params": params or {}}) + "\n")
        mcp.stdin.flush()
        return json.loads(mcp.stdout.readline())
    init = rpc(1, "initialize")
    check("MCP initialize handshake", init["result"]["serverInfo"]["name"] == "hollow-bridge")
    tools = {t["name"] for t in rpc(2, "tools/list")["result"]["tools"]}
    check("MCP exposes the bridge tools",
          {"list_pending_requests", "respond_to_request", "habitat_state"} <= tools)
    listed = rpc(3, "tools/call", {"name": "list_pending_requests"})
    listed_body = json.loads(listed["result"]["content"][0]["text"])
    check("MCP lists the pending request",
          any(r["request_id"] == rid for r in listed_body["pending"]), str(listed_body)[:200])
    rpc(4, "tools/call", {"name": "respond_to_request", "arguments": {
        "request_id": rid, "status": "rejected",
        "response": "test verdict: rejected via the MCP bridge"}})
    responses = [json.loads(l) for l in open(habitat.memory.dir / "claude_responses.jsonl")]
    check("MCP verdict lands in claude_responses.jsonl",
          any(r["request_id"] == rid and r["status"] == "rejected" for r in responses))
    mcp.stdin.close()
    mcp.wait(timeout=5)

    stub.shutdown()
    print(f"\nALL {PASS} CHECKS PASSED  (state under {root})")


if __name__ == "__main__":
    main()
