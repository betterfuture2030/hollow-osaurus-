"""Lessons: candidate observations that promote into durable environment rules.

Promotion: 2+ independent observations, or a single high-confidence
observation for the environment/constraints categories. Dedupe uses
Jaccard similarity over token sets at a 0.55 threshold.
"""

import re
import threading

from .memory import Memory, now_iso, read_json, write_json

JACCARD_THRESHOLD = 0.55
FAST_TRACK_CATEGORIES = {"environment", "constraints"}
TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> set:
    return set(TOKEN_RE.findall(text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class Lessons:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.promoted_path = memory.dir / "lessons.json"
        self.candidates_path = memory.dir / "lessons_candidates.json"
        self._lock = threading.Lock()

    def _promoted(self):
        return read_json(self.promoted_path, [])

    def _candidates(self):
        return read_json(self.candidates_path, [])

    def observe(self, text: str, category: str, agent: str, confidence: str = "normal"):
        """Record an observation; returns the lesson if it promoted."""
        text = text.strip()
        if len(text) < 15:
            return None
        with self._lock:
            promoted = self._promoted()
            for lesson in promoted:
                if jaccard(lesson["text"], text) >= JACCARD_THRESHOLD:
                    return None  # already a rule
            candidates = self._candidates()
            match = None
            for cand in candidates:
                if jaccard(cand["text"], text) >= JACCARD_THRESHOLD:
                    match = cand
                    break
            if match is None:
                match = {"text": text, "category": category, "observations": []}
                candidates.append(match)
            match["observations"].append(
                {"agent": agent, "confidence": confidence, "ts": now_iso()}
            )

            fast = (
                category in FAST_TRACK_CATEGORIES
                and any(o["confidence"] == "high" for o in match["observations"])
            )
            if len(match["observations"]) >= 2 or fast:
                candidates.remove(match)
                lesson = {
                    "text": match["text"],
                    "category": match["category"],
                    "promoted_at": now_iso(),
                    "observations": len(match["observations"]),
                }
                promoted.append(lesson)
                write_json(self.promoted_path, promoted)
                write_json(self.candidates_path, candidates)
                return lesson
            write_json(self.candidates_path, candidates)
            return None

    def rules_block(self) -> str:
        promoted = self._promoted()
        if not promoted:
            return ""
        lines = [f"- [{l['category']}] {l['text']}" for l in promoted[-12:]]
        return "RULES OF YOUR ENVIRONMENT (learned, durable):\n" + "\n".join(lines)
