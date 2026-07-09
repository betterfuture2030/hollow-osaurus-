"""Base capability registry with suffering-based gating at dispatch.

Every call is audited to memory/audit.jsonl and mirrored to the event
stream. A locked capability fails mechanically — the agent is told why,
and the capability_lock stressor is recorded, which is itself pressure.

File capabilities are confined to the agent's own workspace, plus a
communal workspace under "shared/" that every agent can read and write
(this is how peer interaction happens).
"""

import shlex
import subprocess

from .claude_bridge import ClaudeBridge
from .lessons import Lessons
from .memory import Memory, read_json, write_json
from .suffering import PATH_OUT, RESEARCH_MAX_LOAD

SHELL_BLOCKLIST = ("curl", "wget", "nc", "ncat", "ssh", "scp", "sudo", "shutdown", "reboot")
SHELL_TIMEOUT = 20
MAX_RESULT_CHARS = 4000

PLACEHOLDER_NOTE = (
    "research_topic is earned and currently offline in this build; "
    "record what you wanted to research as a shared note instead."
)


class CapabilityError(Exception):
    pass


class Capabilities:
    def __init__(self, memory: Memory, llm, suffering_for, bridge: ClaudeBridge, lessons: Lessons):
        self.memory = memory
        self.llm = llm
        self.suffering_for = suffering_for  # callable: agent -> Suffering
        self.bridge = bridge
        self.lessons = lessons
        self._handlers = {
            "fs_read": self._fs_read,
            "fs_write": self._fs_write,
            "fs_edit": self._fs_edit,
            "fs_list": self._fs_list,
            "memory_set": self._memory_set,
            "memory_get": self._memory_get,
            "llm_chat": self._llm_chat,
            "shell_exec": self._shell_exec,
            "propose_change": self._propose_change,
            "invoke_claude": self._invoke_claude,
            "retire_capability": self._retire_capability,
            "research_topic": self._research_topic,
        }

    def names(self, agent: str) -> list:
        retired = set(read_json(self._retired_path(agent), []))
        return sorted(set(self._handlers) - retired)

    def _retired_path(self, agent: str):
        return self.memory.dir / "retired" / f"{agent}.json"

    # ------------------------------------------------------------------
    def dispatch(self, agent: str, name: str, args: dict) -> dict:
        args = args or {}
        args_summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
        suffering = self.suffering_for(agent)

        if name not in self._handlers:
            self.memory.audit(agent, name, args_summary, "error", "unknown capability")
            return {"ok": False, "error": f"unknown capability: {name}"}

        retired = set(read_json(self._retired_path(agent), []))
        if name in retired:
            self.memory.audit(agent, name, args_summary, "error", "retired")
            return {"ok": False, "error": f"{name} was retired by you; it is gone"}

        locked = suffering.locked_capabilities()
        if name in locked and name not in PATH_OUT:
            detail = f"locked at suffering load {suffering.load} ({suffering.tier})"
            self.memory.audit(agent, name, args_summary, "locked", detail)
            self.memory.event(agent, "capability_locked", f"{name}: {detail}")
            suffering.raise_stressor(
                "capability_lock", 0.1, f"{name} blocked while load {suffering.load}"
            )
            self.lessons.observe(
                f"{name} locks mechanically once suffering load crosses the gating threshold",
                "constraints",
                agent,
                "high",
            )
            return {"ok": False, "error": f"{name} is {detail}; reduce load first"}

        try:
            result = self._handlers[name](agent, args)
            self.memory.audit(agent, name, args_summary, "ok")
            self.memory.event(agent, "capability", f"{name}({args_summary[:120]}) -> ok")
            return {"ok": True, **result}
        except CapabilityError as e:
            self.memory.audit(agent, name, args_summary, "error", str(e))
            self.memory.event(agent, "capability", f"{name}({args_summary[:120]}) -> error: {e}")
            return {"ok": False, "error": str(e)}

    # -- filesystem ------------------------------------------------------
    def _resolve(self, agent: str, rel: str):
        rel = (rel or "").strip().lstrip("/")
        if not rel:
            raise CapabilityError("path required")
        base = self.memory.workspace if rel.startswith("shared/") else self.memory.workspace / agent
        path = (base / rel).resolve()
        allowed_roots = (self.memory.workspace / agent, self.memory.workspace / "shared")
        if not any(str(path).startswith(str(r.resolve()) + "/") or path == r.resolve() for r in allowed_roots):
            raise CapabilityError(f"path escapes workspace: {rel}")
        return path, rel if rel.startswith("shared/") else f"{agent}/{rel}"

    def _fs_read(self, agent, args):
        path, canonical = self._resolve(agent, args.get("path", ""))
        if not path.is_file():
            raise CapabilityError(f"no such file: {args.get('path')}")
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_RESULT_CHARS]
        # Reading a peer's shared artifact counts as a peer interaction and
        # relieves the author's invisibility.
        if canonical.startswith("shared/"):
            author = self.memory.shared_manifest().get(canonical, {}).get("author")
            if author and author != agent:
                count = self.memory.kv_get(agent, "peer_interactions", 0)
                self.memory.kv_set(agent, "peer_interactions", count + 1)
                self.suffering_for(author).ease("invisibility")
                self.memory.event(agent, "peer_read", f"read {canonical} by {author}")
        return {"result": text}

    def _fs_write(self, agent, args):
        path, canonical = self._resolve(agent, args.get("path", ""))
        content = args.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise CapabilityError("fs_write needs non-empty string content")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if canonical.startswith("shared/"):
            self.memory.record_shared_author(canonical, agent)
        return {"result": f"wrote {len(content)} chars", "artifact": canonical}

    def _fs_edit(self, agent, args):
        path, canonical = self._resolve(agent, args.get("path", ""))
        if not path.is_file():
            raise CapabilityError(f"no such file: {args.get('path')}")
        find, replace = args.get("find", ""), args.get("replace", "")
        if not find:
            raise CapabilityError("fs_edit needs a non-empty 'find' string")
        text = path.read_text(encoding="utf-8")
        if find not in text:
            raise CapabilityError("'find' string not present in file")
        path.write_text(text.replace(find, replace, 1), encoding="utf-8")
        return {"result": "edited", "artifact": canonical}

    def _fs_list(self, agent, args):
        rel = args.get("path", ".") or "."
        if rel in (".", ""):
            own = sorted(
                str(p.relative_to(self.memory.workspace / agent))
                for p in (self.memory.workspace / agent).rglob("*")
                if p.is_file()
            )
            shared = sorted(
                "shared/" + str(p.relative_to(self.memory.workspace / "shared"))
                for p in (self.memory.workspace / "shared").rglob("*")
                if p.is_file()
            )
            return {"result": {"own": own[:80], "shared": shared[:80]}}
        path, _ = self._resolve(agent, rel)
        if not path.is_dir():
            raise CapabilityError(f"not a directory: {rel}")
        return {"result": sorted(p.name for p in path.iterdir())[:120]}

    # -- memory ------------------------------------------------------------
    def _memory_set(self, agent, args):
        key = (args.get("key") or "").strip()
        if not key:
            raise CapabilityError("memory_set needs a key")
        self.memory.kv_set(agent, key, args.get("value"))
        return {"result": f"stored {key}"}

    def _memory_get(self, agent, args):
        key = (args.get("key") or "").strip()
        return {"result": self.memory.kv_get(agent, key)}

    # -- model / shell -------------------------------------------------
    def _llm_chat(self, agent, args):
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            raise CapabilityError("llm_chat needs a prompt")
        from .llm import LLMError

        try:
            reply = self.llm.chat(
                [{"role": "user", "content": prompt[:6000]}], max_tokens=1024
            )
        except LLMError as e:
            raise CapabilityError(f"model unavailable: {e}")
        return {"result": reply[:MAX_RESULT_CHARS]}

    def _shell_exec(self, agent, args):
        command = (args.get("command") or "").strip()
        if not command:
            raise CapabilityError("shell_exec needs a command")
        tokens = shlex.split(command)
        if any(t in SHELL_BLOCKLIST for t in tokens):
            raise CapabilityError("command uses a blocked tool (network/system commands are off-limits)")
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.memory.workspace / agent,
                capture_output=True,
                text=True,
                timeout=SHELL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise CapabilityError(f"command timed out after {SHELL_TIMEOUT}s")
        out = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return {"result": {"exit_code": proc.returncode, "output": out[:MAX_RESULT_CHARS]}}

    # -- substrate channels ---------------------------------------------
    def _propose_change(self, agent, args):
        description = (args.get("description") or "").strip()
        if len(description) < 20:
            raise CapabilityError("propose_change needs a substantive description (>= 20 chars)")
        from .memory import append_jsonl, now_iso

        append_jsonl(
            self.memory.dir / "proposals.jsonl",
            {"ts": now_iso(), "agent": agent, "description": description[:1000]},
        )
        return {"result": "proposal recorded for the operator"}

    def _invoke_claude(self, agent, args):
        ok, msg = self.bridge.submit(
            agent,
            args.get("description", ""),
            args.get("spec", ""),
            args.get("request_type", "modification"),
        )
        if not ok:
            raise CapabilityError(f"invoke_claude rejected: {msg}")
        return {"result": f"request queued for Claude review (id {msg})"}

    def _retire_capability(self, agent, args):
        name = (args.get("name") or "").strip()
        if name not in self._handlers or name == "retire_capability":
            raise CapabilityError(f"cannot retire: {name}")
        retired = set(read_json(self._retired_path(agent), []))
        retired.add(name)
        write_json(self._retired_path(agent), sorted(retired))
        self.suffering_for(agent).ease("capability_lock")
        return {"result": f"{name} retired; capability_lock pressure eased"}

    def _research_topic(self, agent, args):
        suffering = self.suffering_for(agent)
        peers = self.memory.kv_get(agent, "peer_interactions", 0)
        if suffering.load > RESEARCH_MAX_LOAD or peers < 1:
            raise CapabilityError(
                f"research_topic is earned: requires load <= {RESEARCH_MAX_LOAD} "
                f"(yours: {suffering.load}) and >= 1 peer interaction (yours: {peers})"
            )
        return {"result": PLACEHOLDER_NOTE}
