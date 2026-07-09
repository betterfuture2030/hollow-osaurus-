"""Stressors, suffering load, and load-based capability gating.

Thresholds and mechanics follow the Hollow AgentOS substrate docs:
load >= 0.55 (CONSTRAINED) locks capability synthesis; load >= 0.75
(DOMINANT) additionally locks fs_write/fs_edit. A "path-out" set of
capabilities is never locked so an agent can always act its way back down.
"""

from .memory import Memory, now_iso, read_json, write_json

STRESSOR_TYPES = (
    "futility",
    "invisibility",
    "identity_violation",
    "existential_threat",
    "repeated_failure",
    "purposelessness",
    "resource_burden",
    "capability_lock",
    "stagnation",
)

CONSTRAINED_AT = 0.55
DOMINANT_AT = 0.75

# Never locked, regardless of load.
PATH_OUT = {
    "retire_capability",
    "fs_read",
    "fs_list",
    "llm_chat",
    "memory_set",
    "memory_get",
    "invoke_claude",
    "propose_change",
    "shell_exec",
}

# research_topic is earned, not default: load must be at or below this and
# the agent must have at least one recent peer interaction.
RESEARCH_MAX_LOAD = 0.15


class Suffering:
    def __init__(self, memory: Memory, agent: str):
        self.memory = memory
        self.agent = agent
        self.path = memory.dir / "suffering" / f"{agent}.json"
        self.stressors = read_json(self.path, {})

    def save(self) -> None:
        write_json(self.path, self.stressors)

    def raise_stressor(self, kind: str, severity: float, reason: str) -> None:
        if kind not in STRESSOR_TYPES:
            raise ValueError(f"unknown stressor: {kind}")
        severity = max(0.0, min(1.0, severity))
        existing = self.stressors.get(kind)
        if existing:
            existing["severity"] = max(existing["severity"], severity)
            existing["reason"] = reason
            existing["updated_at"] = now_iso()
        else:
            self.stressors[kind] = {
                "severity": severity,
                "reason": reason,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        self.save()

    def ease(self, kind: str, amount: float = None) -> None:
        s = self.stressors.get(kind)
        if not s:
            return
        if amount is None:
            del self.stressors[kind]
        else:
            s["severity"] = round(s["severity"] - amount, 4)
            s["updated_at"] = now_iso()
            if s["severity"] <= 0.01:
                del self.stressors[kind]
        self.save()

    def resolve(self, kind: str) -> None:
        self.ease(kind, None)

    @property
    def load(self) -> float:
        return min(1.0, round(sum(s["severity"] for s in self.stressors.values()), 4))

    @property
    def tier(self) -> str:
        if self.load >= DOMINANT_AT:
            return "DOMINANT"
        if self.load >= CONSTRAINED_AT:
            return "CONSTRAINED"
        return "NORMAL"

    def locked_capabilities(self) -> set:
        locked = set()
        if self.load >= CONSTRAINED_AT:
            locked.add("synthesize_capability")
        if self.load >= DOMINANT_AT:
            locked.update({"fs_write", "fs_edit"})
        return locked - PATH_OUT

    def summary(self) -> dict:
        return {
            "load": self.load,
            "tier": self.tier,
            "stressors": {
                k: {"severity": v["severity"], "reason": v["reason"]}
                for k, v in sorted(self.stressors.items())
            },
            "locked": sorted(self.locked_capabilities()),
        }
