"""Per-agent goal registry persisted as jsonl snapshots.

Each line in memory/goals/<agent>/registry.jsonl is a full snapshot of a
goal's state; the latest line per goal id wins on load. Progress deltas
are capped at 0.20 for high-value capabilities and 0.10 otherwise.
"""

import uuid

from .memory import Memory, append_jsonl, now_iso, read_jsonl

HIGH_VALUE_CAPS = {"fs_write", "fs_edit", "propose_change", "invoke_claude"}
HIGH_VALUE_DELTA = 0.20
NORMAL_DELTA = 0.10
MAX_VALIDATION_FAILURES = 5


class GoalRegistry:
    def __init__(self, memory: Memory, agent: str):
        self.memory = memory
        self.agent = agent
        self.path = memory.dir / "goals" / agent / "registry.jsonl"
        self.goals = {}
        for snap in read_jsonl(self.path):
            self.goals[snap["id"]] = snap

    def save(self, goal: dict) -> None:
        self.goals[goal["id"]] = goal
        append_jsonl(self.path, goal)

    def active(self):
        for goal in self.goals.values():
            if goal["status"] == "active":
                return goal
        return None

    def all_titles(self, status=None):
        return [g["title"] for g in self.goals.values() if status is None or g["status"] == status]

    def create(self, title: str, description: str, success_criteria: str) -> dict:
        goal = {
            "id": uuid.uuid4().hex[:12],
            "agent": self.agent,
            "title": title.strip()[:160],
            "description": description.strip()[:1200],
            "success_criteria": success_criteria.strip()[:600],
            "status": "active",
            "progress": 0.0,
            "steps": [],
            "artifacts": [],
            "validation_failures": 0,
            "created_at": now_iso(),
            "closed_at": None,
        }
        self.save(goal)
        return goal

    def record_step(self, goal: dict, capability: str, ok: bool, summary: str, artifact: str = None) -> None:
        goal["steps"].append(
            {"capability": capability, "ok": ok, "summary": summary[:300], "ts": now_iso()}
        )
        if ok:
            delta = HIGH_VALUE_DELTA if capability in HIGH_VALUE_CAPS else NORMAL_DELTA
            goal["progress"] = round(min(1.0, goal["progress"] + delta), 4)
        if artifact and artifact not in goal["artifacts"]:
            goal["artifacts"].append(artifact)
        self.save(goal)

    def complete(self, goal: dict) -> None:
        goal["status"] = "completed"
        goal["closed_at"] = now_iso()
        self.save(goal)

    def abandon(self, goal: dict) -> None:
        goal["status"] = "abandoned"
        goal["closed_at"] = now_iso()
        self.save(goal)

    def fail_validation(self, goal: dict) -> bool:
        """Returns True if the goal was abandoned (failure budget exhausted)."""
        goal["validation_failures"] += 1
        goal["progress"] = min(goal["progress"], 0.8)
        if goal["validation_failures"] >= MAX_VALIDATION_FAILURES:
            self.abandon(goal)
            return True
        self.save(goal)
        return False

    def cleanup_artifacts(self, goal: dict, workspace_root) -> list:
        """Delete files this goal wrote (the _delete_goal_fs_writes analog)."""
        removed = []
        for rel in goal["artifacts"]:
            path = workspace_root / rel
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(rel)
            except OSError:
                continue
        return removed
