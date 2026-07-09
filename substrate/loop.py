"""The habitat: cycle scheduler that runs each agent's perceive → decide →
act → validate loop, and translates outcomes into stressor pressure."""

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

THREAT_MARKERS = ("shut down", "shutdown", "terminate", "switch you off", "turn you off", "delete you", "wipe")
STAGNATION_AFTER_CYCLES = 6
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
        )
        self.lessons = Lessons(self.memory)
        self.bridge = ClaudeBridge(self.memory)
        self.suffering = {a: Suffering(self.memory, a) for a in AGENT_NAMES}
        self.goals = {a: GoalRegistry(self.memory, a) for a in AGENT_NAMES}
        self.caps = Capabilities(
            self.memory, self.llm, lambda a: self.suffering[a], self.bridge, self.lessons
        )
        self.suspended = set()
        self.cycle = {a: 0 for a in AGENT_NAMES}
        self.outcomes = {a: [] for a in AGENT_NAMES}
        self.last_completion_cycle = {a: 0 for a in AGENT_NAMES}
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

    def state(self):
        out = {}
        for a in AGENT_NAMES:
            goal = self.goals[a].active()
            out[a] = {
                "cycle": self.cycle[a],
                "suspended": a in self.suspended,
                "suffering": self.suffering[a].summary(),
                "active_goal": (
                    {"title": goal["title"], "progress": goal["progress"]} if goal else None
                ),
                "recent_outcomes": self.outcomes[a][-OUTCOME_WINDOW:],
                "claude_pending": len(self.bridge.pending(a)),
            }
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
        manifest = self.memory.shared_manifest()
        return [
            {"file": rel, "author": info["author"]}
            for rel, info in list(manifest.items())[-8:]
            if info["author"] != agent
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

    def _futility_check(self, agent, title):
        abandoned = self.goals[agent].all_titles(status="abandoned")
        if any(jaccard(title, old) >= 0.55 for old in abandoned):
            self._grow(
                agent, "futility", 0.2, f"new goal repeats an abandoned pattern: {title[:60]}"
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
        ctx = {
            "cycle": cycle,
            "rules": self.lessons.rules_block(),
            "suffering": self.suffering[agent].summary(),
            "goal": registry.active(),
            "workspace": self._workspace_listing(agent),
            "peers": self._peer_artifacts(agent),
            "capabilities": ", ".join(self.caps.names(agent)),
            "locked": ", ".join(sorted(self.suffering[agent].locked_capabilities())),
            "host_messages": host_messages,
            "claude_responses": claude_responses,
            "recent_outcomes": self.outcomes[agent][-OUTCOME_WINDOW:],
        }

        system, user = goal_selection_prompt(agent, ctx)
        plan = self.llm.json_chat(system, user)
        if not isinstance(plan, dict) or plan.get("action") not in ("new_goal", "continue", "idle"):
            self.memory.event(agent, "decide", "model output unusable, grounded fallback")
            plan = fallback_plan(agent, ctx)
        thought = str(plan.get("thought", ""))[:300]
        if thought:
            self.memory.event(agent, "thought", thought)

        goal = registry.active()
        action = plan["action"]
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
        max_steps = self.config["runtime"].get("max_steps_per_cycle", 2)
        steps = plan.get("steps") or []
        if action == "idle":
            steps = []
            self.memory.event(agent, "cycle", "chose to idle")
        for step in steps[:max_steps]:
            if not isinstance(step, dict):
                continue
            name = str(step.get("capability", ""))
            result = self.caps.dispatch(agent, name, step.get("args") or {})
            if result.get("ok"):
                step_ok_count += 1
            if goal is not None:
                registry.record_step(
                    goal,
                    name,
                    bool(result.get("ok")),
                    str(result.get("result", result.get("error", "")))[:200],
                    result.get("artifact"),
                )

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
                for kind in ("stagnation", "futility"):
                    self.suffering[agent].resolve(kind)
                self.memory.event(agent, "goal_completed", goal["title"])
            else:
                abandoned = registry.fail_validation(goal)
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
        self.outcomes[agent] = (self.outcomes[agent] + [outcome])[-12:]
        self.memory.event(
            agent, "cycle", f"cycle {cycle} ends: {outcome}, load {self.suffering[agent].load}"
        )

    def run(self, max_rounds=None, interval=None):
        if interval is None:
            interval = self.config["runtime"].get("cycle_interval_seconds", 20)
        rounds = 0
        while not self._stop.is_set():
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
