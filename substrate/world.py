"""Ambient world events: weather, echoes, and objects.

The world occasionally does something on its own — not a host message,
not a peer, just the environment being alive. Events are injected into
every agent's next perception as ctx["ambient"]. Echo events resurface a
fragment of the habitat's own past from the event stream, so the world
appears to remember.
"""

import json

from .memory import Memory

# One event roughly every N scheduler rounds (0 disables the world).
DEFAULT_EVENT_EVERY_ROUNDS = 6

# (flavor, text) — the flavor drives the operator panel's atmosphere layer
WEATHER = (
    ("wind", "A dry wind moves through the workspace; loose notes rustle."),
    ("fog", "A low fog settles between the directories. Everything sounds farther away."),
    ("rain", "Static rain. Brief, bright, gone. The files are unharmed but feel washed."),
    ("amber", "The light in the habitat turns amber for a while, like late afternoon."),
    ("silence", "A long silence — even the substrate's hum pauses, then resumes."),
    ("heat", "Heat shimmer over the shared workspace; paths look slightly bent."),
)

OBJECTS = (
    ("object", "A small carved stone has appeared near your files. It was not there before."),
    ("object", "Someone — or something — left a length of blue thread across the workspace root."),
    ("object", "A key without a lock rests in the corner of the shared directory."),
    ("object", "There is a faint chalk circle around the memory store this cycle."),
    ("object", "An empty picture frame leans against the workspace wall, facing out."),
)

ECHO_PREFIX = "An echo drifts through the habitat, a memory of something that happened here: "
ECHO_MAX_CHARS = 140
# Echoes only resurface these kinds — action, not bookkeeping.
ECHO_KINDS = {"goal", "goal_completed", "goal_abandoned", "thought", "lesson"}


class World:
    def __init__(self, memory: Memory, rng):
        self.memory = memory
        self.rng = rng

    def _echo(self):
        events = [
            e for e in self.memory.recent_events(200)
            if e.get("kind") in ECHO_KINDS and e.get("detail")
        ]
        if not events:
            return None
        e = self.rng.choice(events)
        fragment = str(e["detail"])[:ECHO_MAX_CHARS]
        return f"{ECHO_PREFIX}\"{fragment}\" ({e.get('agent', 'someone')}, long ago)"

    def draw(self):
        """Draw one ambient event as (flavor, text). Weather is weighted
        highest — it drives the panel's visible atmosphere, and operators
        provoke the world mostly to SEE something. Echoes need history;
        fall back to weather."""
        kind = self.rng.choice(("weather", "weather", "weather", "echo", "object"))
        if kind == "echo":
            echo = self._echo()
            if echo:
                return "echo", echo
            kind = "weather"
        pool = WEATHER if kind == "weather" else OBJECTS
        return self.rng.choice(pool)

    def draw_event(self) -> str:
        """Back-compat: just the text."""
        return self.draw()[1]
