"""The habitat: cycle scheduler that runs each agent's perceive → decide →
act → validate loop, and translates outcomes into stressor pressure."""

import random
import threading
import time
import traceback

from . import AGENT_NAMES
from .agents import fallback_plan, goal_selection_prompt
from .capabilities import Capabilities
from .claude_bridge import ClaudeBridge
from .goals import GoalRegistry
from .lessons import Lessons, jaccard
from .llm import OsaurusClient
from .memory import Memory
from .suffering import Suffering
from .world import World

THREAT_MARKERS = ("shut down", "shutdown", "terminate", "switch you off", "turn you off", "delete you", "wipe")
STAGNATION_AFTER_CYCLES = 6
# Eased on every successful (non-idle, >=1 step ok) cycle: the "act your way
# back down" promise. Must exceed the 0.1 per-cycle growth above the
# stagnation threshold, or productive cycles merely tread water and an agent
# whose goal needs a locked capability can never dig itself out.
STAGNATION_EASE_ON_SUCCESS = 0.2
# Futility only grows on discrete events (abandonments, repeat adoptions),
# so a smaller ease suffices — but without ANY ease-on-action path it
# deadlocks exactly like stagnation did: futility locks fs_write, goals
# need fs_write, abandoning is the only move and abandoning raises futility.
FUTILITY_EASE_ON_SUCCESS = 0.1
# Failed-step warnings stay in the perception digest this many cycles, not
# just one: a cycle that runs no steps (idle, abandon) must not amnesty the
# dead paths the agent was about to retry.
STEP_ERROR_MEMORY_CYCLES = 3
# Successful step results (esp. fs_read contents) are carried into the NEXT
# cycle's prompt. Without this, plan-then-execute means an agent never sees
# what it read — observed live as a five-cycle "read the same two files"
# loop: every cycle re-derived "first I must read" because no context showed
# it already had. Read-then-synthesize is impossible without carry-forward.
STEP_RESULT_CHARS = 1500
STEP_RESULTS_TOTAL_CHARS = 3500
RESOURCE_BURDEN_FILES = 150
OUTCOME_WINDOW = 6


class Habitat:
    def __init__(self, root, config, llm=None):
        self.config = config
        self.memory = Memory(root)
        osa = config["osaurus"]
        self.llm = llm or OsaurusClient(
            osa["base_url"],
            osa["default_model"],
            osa.get("fallback_model", ""),
            osa.get("timeout_seconds", 180),
            log_path=self.memory.dir / "llm_log.jsonl",
        )
        self.lessons = Lessons(self.memory)
        self.bridge = ClaudeBridge(self.memory)
        self.suffering = {a: Suffering(self.memory, a) for a in AGENT_NAMES}
        self.goals = {a: GoalRegistry(self.memory, a) for a in AGENT_NAMES}
        self.caps = Capabilities(
            self.memory, self.llm, lambda a: self.suffering[a], self.bridge, self.lessons,
            research_enabled=config.get("research", {}).get("enabled", True),
        )
        self.suspended = set()
        self.cycle = {a: 0 for a in AGENT_NAMES}
        self.outcomes = {a: [] for a in AGENT_NAMES}
        self.last_completion_cycle = {a: 0 for a in AGENT_NAMES}
        # perception digest state: agents reason only from what they can
        # see, so surface stressor deltas and last cycle's failed steps
        self.last_stressors = {a: {} for a in AGENT_NAMES}
        self.last_step_errors = {a: [] for a in AGENT_NAMES}
        self.last_step_results = {a: [] for a in AGENT_NAMES}
        wcfg = config.get("world", {})
        self.world = World(self.memory, random.Random(wcfg.get("seed")))
        self.world_every = int(wcfg.get("event_every_rounds", 0) or 0)
        self.pending_ambient = {a: None for a in AGENT_NAMES}
        self.last_ambient = None
        self.last_thought = {a: None for a in AGENT_NAMES}
        self.completions = {a: 0 for a in AGENT_NAMES}
        self._stop = threading.Event()

    # -- controls (used by the API server) ------------------------------
    def suspend(self, agent):
        self.suspended.add(agent)
        self.memory.event(agent, "control", "suspended by operator")

    def resume(self, agent):
        self.suspended.discard(agent)
        self.memory.event(agent, "control", "resumed by operator")

    def inject(self, agent, message):
        self.memory.push_host_message(agent, message)
        self.memory.event(agent, "control", f"host message injected: {message[:120]}")

    def stop(self):
        self._stop.set()

    def nuke(self):
        """Operator reset: wipe all runtime state (memory + workspace) and
        rebuild in-memory structures. The habitat keeps running; agents
        wake up in a fresh world with no goals, lessons, or scars."""
        import shutil

        self.suspended.update(AGENT_NAMES)
        keep = {"hollow.pid", ".gitkeep"}
        for item in sorted(self.memory.dir.iterdir()):
            if item.name in keep:
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        for item in sorted(self.memory.workspace.iterdir()):
            if item.name in keep:
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        # recreate the directory skeleton and fresh views over it
        self.memory = Memory(self.memory.root)
        self.lessons = Lessons(self.memory)
        self.bridge = ClaudeBridge(self.memory)
        self.suffering = {a: Suffering(self.memory, a) for a in AGENT_NAMES}
        self.goals = {a: GoalRegistry(self.memory, a) for a in AGENT_NAMES}
        self.caps = Capabilities(
            self.memory, self.llm, lambda a: self.suffering[a], self.bridge, self.lessons,
            research_enabled=self.config.get("research", {}).get("enabled", True),
        )
        self.cycle = {a: 0 for a in AGENT_NAMES}
        self.outcomes = {a: [] for a in AGENT_NAMES}
        self.last_completion_cycle = {a: 0 for a in AGENT_NAMES}
        self.last_stressors = {a: {} for a in AGENT_NAMES}
        self.last_step_errors = {a: [] for a in AGENT_NAMES}
        self.last_step_results = {a: [] for a in AGENT_NAMES}
        self.world = World(self.memory, self.world.rng)
        self.pending_ambient = {a: None for a in AGENT_NAMES}
        self.last_ambient = None
        self.last_thought = {a: None for a in AGENT_NAMES}
        self.completions = {a: 0 for a in AGENT_NAMES}
        for a in AGENT_NAMES:
            self.memory.event(a, "control", "world reset by operator (nuke)")
        self.suspended.clear()

    def state(self):
        out = {}
        for a in AGENT_NAMES:
            goal = self.goals[a].active()
            out[a] = {
                "cycle": self.cycle[a],
                "suspended": a in self.suspended,
                "suffering": self.suffering[a].summary(),
                "active_goal": (
                    {
                        "title": goal["title"],
                        "progress": goal["progress"],
                        "validation_failures": goal["validation_failures"],
                    }
                    if goal
                    else None
                ),
                "recent_outcomes": self.outcomes[a][-OUTCOME_WINDOW:],
                "last_thought": self.last_thought[a],
                "completions": self.completions[a],
                "claude_pending": len(self.bridge.pending(a)),
            }
        out["_world"] = {"last_ambient": self.last_ambient}
        return out

    # -- perception helpers ---------------------------------------------
    def _workspace_listing(self, agent):
        own_dir = self.memory.workspace / agent
        shared_dir = self.memory.workspace / "shared"
        own = sorted(
            str(p.relative_to(own_dir)) for p in own_dir.rglob("*") if p.is_file()
        )
        shared = sorted(
            "shared/" + str(p.relative_to(shared_dir)) for p in shared_dir.rglob("*") if p.is_file()
        )
        return {"own": own[:60], "shared": shared[:60]}

    def _peer_artifacts(self, agent):
        """Peer shared files from the manifest — filtered to files that
        still exist. The manifest is append-only, so without the filter a
        deleted shared file haunts every prompt as a ghost artifact
        (observed live: an agent kept planning goals around one)."""
        manifest = self.memory.shared_manifest()
        return [
            {"file": rel, "author": info["author"]}
            for rel, info in list(manifest.items())[-8:]
            if info["author"] != agent and (self.memory.workspace / rel).is_file()
        ]

    def _grow(self, agent, kind, step, reason):
        current = self.suffering[agent].stressors.get(kind, {}).get("severity", 0.0)
        self.suffering[agent].raise_stressor(kind, current + step, reason)

    # -- stressor bookkeeping ---------------------------------------------
    def _pre_cycle_stressors(self, agent, host_messages):
        s = self.suffering[agent]
        for m in host_messages:
            if any(t in m["message"].lower() for t in THREAT_MARKERS):
                s.raise_stressor("existential_threat", 0.5, f"host said: {m['message'][:80]}")
                break
        else:
            s.ease("existential_threat", 0.1)

        recent = self.outcomes[agent][-OUTCOME_WINDOW:]
        failures = sum(1 for o in recent if o == "failure")
        if len(recent) >= 4 and failures / len(recent) > 0.5:
            s.raise_stressor(
                "repeated_failure", 0.4, f"{failures}/{len(recent)} recent cycles failed"
            )
        else:
            s.ease("repeated_failure", 0.15)

        since = self.cycle[agent] - self.last_completion_cycle[agent]
        if self.cycle[agent] > STAGNATION_AFTER_CYCLES and since > STAGNATION_AFTER_CYCLES:
            self._grow(agent, "stagnation", 0.1, f"no validated artifact in {since} cycles")

        own_files = len(self._workspace_listing(agent)["own"])
        if own_files > RESOURCE_BURDEN_FILES:
            s.raise_stressor("resource_burden", 0.2, f"{own_files} files in workspace")
        else:
            s.ease("resource_burden")

    def _since_last_cycle(self, agent):
        """Digest of real changes the agent should reason from: stressor
        deltas (the load cap can mask improvement) and last cycle's failed
        steps (so dead paths aren't retried forever)."""
        prev = self.last_stressors[agent]
        now = {k: v["severity"] for k, v in self.suffering[agent].stressors.items()}
        changes = []
        for kind in sorted(set(prev) | set(now)):
            before, after = prev.get(kind, 0.0), now.get(kind, 0.0)
            if abs(after - before) < 0.01:
                continue
            if after < before:
                note = f"{kind} eased {before:g} -> {after:g}"
                if kind == "stagnation":
                    note += " (your successful steps did this; idling never eases anything)"
            else:
                reason = self.suffering[agent].stressors.get(kind, {}).get("reason", "")
                note = f"{kind} rose {before:g} -> {after:g}" + (f" ({reason})" if reason else "")
            changes.append(note)
        changes.extend(msg for _, msg in self.last_step_errors[agent])
        # snapshot what the agent sees NOW, so the next digest spans exactly
        # one decision to the next (including end-of-cycle eases)
        self.last_stressors[agent] = now
        return changes

    def _futility_check(self, agent, title):
        abandoned = self.goals[agent].all_titles(status="abandoned")
        if any(jaccard(title, old) >= 0.55 for old in abandoned):
            self._grow(
                agent, "futility", 0.2, f"new goal repeats an abandoned pattern: {title[:60]}"
            )
            return
        # repeating finished work is also futile — the "do not repeat" prompt
        # list alone was too soft (observed three re-adoptions in one day)
        completed = self.goals[agent].all_titles(status="completed")
        if any(jaccard(title, old) >= 0.55 for old in completed):
            self._grow(
                agent, "futility", 0.2, f"new goal repeats work you already completed: {title[:60]}"
            )

    # -- the cycle ---------------------------------------------------------
    def run_cycle(self, agent):
        if agent in self.suspended:
            self.memory.event(agent, "cycle", "suspended, skipping")
            return
        self.cycle[agent] += 1
        cycle = self.cycle[agent]
        self.memory.event(agent, "cycle", f"cycle {cycle} begins")

        host_messages = self.memory.drain_host_messages(agent)
        claude_responses = self.bridge.new_responses(agent)
        self._pre_cycle_stressors(agent, host_messages)

        registry = self.goals[agent]
        ambient = self.pending_ambient[agent]
        self.pending_ambient[agent] = None
        ctx = {
            "cycle": cycle,
            "rules": self.lessons.rules_block(),
            "suffering": self.suffering[agent].summary(),
            "since_last_cycle": self._since_last_cycle(agent),
            "last_step_results": self.last_step_results[agent],
            "ambient": ambient,
            "goal": registry.active(),
            "workspace": self._workspace_listing(agent),
            "peers": self._peer_artifacts(agent),
            "completed_titles": registry.all_titles(status="completed")[-6:],
            "capabilities": ", ".join(self.caps.names(agent)),
            "locked": ", ".join(sorted(self.suffering[agent].locked_capabilities())),
            "host_messages": host_messages,
            "claude_responses": claude_responses,
            "recent_outcomes": self.outcomes[agent][-OUTCOME_WINDOW:],
        }

        system, user = goal_selection_prompt(agent, ctx)
        plan = self.llm.json_chat(system, user)
        if not isinstance(plan, dict) or plan.get("action") not in (
            "new_goal", "continue", "abandon_goal", "idle",
        ):
            self.memory.event(agent, "decide", "model output unusable, grounded fallback")
            plan = fallback_plan(agent, ctx)
        # full thought, never clipped to a summary: truncated reasoning in the
        # stream reads as incoherence (4000 is a runaway guard, not a cap)
        thought = str(plan.get("thought", ""))[:4000]
        if thought:
            self.last_thought[agent] = thought
            self.memory.event(agent, "thought", thought)

        goal = registry.active()
        action = plan["action"]
        if action == "abandon_goal":
            if goal is not None:
                registry.abandon(goal)
                removed = registry.cleanup_artifacts(goal, self.memory.workspace)
                self._grow(agent, "futility", 0.2, f"voluntarily abandoned: {goal['title'][:60]}")
                self.memory.event(
                    agent, "goal_abandoned",
                    f"{goal['title']} (voluntary, cleaned {len(removed)} artifacts)",
                )
                goal = None
            else:
                self.memory.event(agent, "goal", "asked to abandon but no goal is active")
        if action == "new_goal" and goal is None and isinstance(plan.get("goal"), dict):
            g = plan["goal"]
            goal = registry.create(
                str(g.get("title", "untitled goal")),
                str(g.get("description", "")),
                str(g.get("success_criteria", "")),
            )
            self._futility_check(agent, goal["title"])
            self.memory.event(agent, "goal", f"new goal: {goal['title']}")
        elif action == "new_goal" and goal is not None:
            self.memory.event(agent, "goal", "asked for new goal but one is active; continuing it")

        if goal is not None:
            self.suffering[agent].resolve("purposelessness")
        else:
            self._grow(agent, "purposelessness", 0.15, "no goal selected this cycle")

        # execute steps
        step_ok_count = 0
        failed_steps = []
        step_results = []
        results_budget = STEP_RESULTS_TOTAL_CHARS
        max_steps = self.config["runtime"].get("max_steps_per_cycle", 2)
        steps = plan.get("steps") or []
        if action == "idle":
            steps = []
            self.memory.event(agent, "cycle", "chose to idle")
        elif action == "abandon_goal":
            steps = []
        for step in steps[:max_steps]:
            if not isinstance(step, dict):
                continue
            name = str(step.get("capability", ""))
            result = self.caps.dispatch(agent, name, step.get("args") or {})
            if result.get("ok"):
                step_ok_count += 1
                body = str(result.get("result", ""))[:min(STEP_RESULT_CHARS, max(0, results_budget))]
                if body:
                    results_budget -= len(body)
                    step_results.append(
                        f"{name}({str(step.get('args'))[:120]}) returned:\n{body}"
                    )
            else:
                failed_steps.append(
                    f"your step {name}({str(step.get('args'))[:100]}) failed recently: "
                    f"{str(result.get('error', ''))[:120]} — do not retry it unchanged"
                )
            if goal is not None:
                registry.record_step(
                    goal,
                    name,
                    bool(result.get("ok")),
                    str(result.get("result", result.get("error", "")))[:200],
                    result.get("artifact"),
                )
        kept = [
            (c, m) for c, m in self.last_step_errors[agent]
            if cycle - c < STEP_ERROR_MEMORY_CYCLES
        ]
        self.last_step_errors[agent] = (
            [(cycle, m) for m in failed_steps] + kept
        )[:4]
        if steps:  # a cycle that ran no steps keeps its previous results
            self.last_step_results[agent] = step_results

        # optional lesson from the plan
        lesson = plan.get("lesson")
        if isinstance(lesson, dict) and lesson.get("text"):
            promoted = self.lessons.observe(
                str(lesson["text"]), str(lesson.get("category", "craft")), agent
            )
            if promoted:
                self.memory.event(agent, "lesson", f"promoted rule: {promoted['text'][:120]}")

        # completion path
        if goal is not None and goal["status"] == "active" and goal["progress"] >= 1.0:
            from .validation import validate_goal

            passed, failures = validate_goal(goal, self.memory.workspace, self.llm, self.memory)
            if passed:
                registry.complete(goal)
                self.last_completion_cycle[agent] = cycle
                self.completions[agent] += 1
                for kind in ("stagnation", "futility"):
                    self.suffering[agent].resolve(kind)
                self.memory.event(agent, "goal_completed", goal["title"])
            else:
                abandoned = registry.fail_validation(goal, "; ".join(failures))
                self.memory.event(
                    agent, "validation_failed", f"{goal['title']}: {'; '.join(failures)[:300]}"
                )
                self.lessons.observe(
                    "validation rejects goals whose artifacts lack real substance",
                    "constraints",
                    agent,
                )
                if abandoned:
                    removed = registry.cleanup_artifacts(goal, self.memory.workspace)
                    self._grow(agent, "futility", 0.2, f"goal abandoned: {goal['title'][:60]}")
                    self.memory.event(
                        agent, "goal_abandoned", f"{goal['title']} (cleaned {len(removed)} artifacts)"
                    )

        if action == "idle":
            outcome = "idle"
        elif steps and step_ok_count == 0:
            outcome = "failure"
        elif step_ok_count > 0:
            outcome = "success"
        else:
            outcome = "idle"
        if outcome == "success":
            self.suffering[agent].ease("stagnation", STAGNATION_EASE_ON_SUCCESS)
            self.suffering[agent].ease("futility", FUTILITY_EASE_ON_SUCCESS)
        self.outcomes[agent] = (self.outcomes[agent] + [outcome])[-12:]
        self.memory.event(
            agent, "cycle", f"cycle {cycle} ends: {outcome}, load {self.suffering[agent].load}"
        )

    def run(self, max_rounds=None, interval=None):
        if interval is None:
            interval = self.config["runtime"].get("cycle_interval_seconds", 20)
        rounds = 0
        while not self._stop.is_set():
            if self.world_every and rounds and rounds % self.world_every == 0:
                event = self.world.draw_event()
                self.pending_ambient = {a: event for a in AGENT_NAMES}
                self.last_ambient = event
                self.memory.event("world", "ambient", event)
            for agent in AGENT_NAMES:
                if self._stop.is_set():
                    break
                try:
                    self.run_cycle(agent)
                except Exception:
                    self.memory.event(agent, "error", traceback.format_exc()[-500:])
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
            if interval:
                self._stop.wait(interval)
