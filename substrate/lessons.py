"""Lessons: candidate observations that promote into durable environment rules.

Promotion: 2+ independent observations, or a single high-confidence
observation for the environment/constraints categories. Dedupe uses
Jaccard similarity over token sets at a 0.55 threshold.
"""

import re
import threading

from .memory import Memory, now_iso, read_json, write_json

# 0.45: at 0.55 the rulebook collected six paraphrases of one citation
# mantra (~0.5 similarity each) — lesson prose rewords easily, so dedupe
# must match looser than goal-title checks do. Retraction matches at 0.4.
JACCARD_THRESHOLD = 0.45
# Retraction is an operator veto, so it matches more loosely than dedupe:
# a vetoed belief re-derived in fresh words (observed live at 0.54 vs the
# 0.55 dedupe threshold) must still be blocked. Over-blocking near a vetoed
# idea is safer than letting the idea mutate past the filter.
RETRACTED_JACCARD_THRESHOLD = 0.4
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
        self.retracted_path = memory.dir / "lessons_retracted.json"
        self._lock = threading.Lock()

    def _promoted(self):
        return read_json(self.promoted_path, [])

    def _candidates(self):
        return read_json(self.candidates_path, [])

    def _retracted(self):
        return read_json(self.retracted_path, [])

    def retract(self, text: str) -> None:
        """Operator veto: remove Jaccard-matches from promoted rules and
        candidates, and block the lesson from ever re-promoting. Needed
        because a false-but-observable lesson (e.g. one learned while the
        substrate was malfunctioning) re-promotes from a single
        high-confidence observation if merely deleted."""
        with self._lock:
            promoted = [l for l in self._promoted() if jaccard(l["text"], text) < JACCARD_THRESHOLD]
            candidates = [c for c in self._candidates() if jaccard(c["text"], text) < JACCARD_THRESHOLD]
            write_json(self.promoted_path, promoted)
            write_json(self.candidates_path, candidates)
            retracted = self._retracted()
            retracted.append(text)
            write_json(self.retracted_path, retracted)

    def observe(self, text: str, category: str, agent: str, confidence: str = "normal"):
        """Record an observation; returns the lesson if it promoted."""
        text = text.strip()
        if len(text) < 15:
            return None
        with self._lock:
            for dead in self._retracted():
                if jaccard(dead, text) >= RETRACTED_JACCARD_THRESHOLD:
                    return None  # operator-retracted; never re-promotes
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
