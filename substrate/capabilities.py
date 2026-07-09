"""Base capability registry with suffering-based gating at dispatch.

Every call is audited to memory/audit.jsonl and mirrored to the event
stream. A locked capability fails mechanically — the agent is told why,
and the capability_lock stressor is recorded, which is itself pressure.

File capabilities are confined to the agent's own workspace, plus a
communal workspace under "shared/" that every agent can read and write
(this is how peer interaction happens).
"""

import json
import re
import shlex
import subprocess
import sys

from .claude_bridge import ClaudeBridge
from .lessons import Lessons
from .memory import Memory, read_json, write_json
from .suffering import PATH_OUT, RESEARCH_MAX_LOAD

SHELL_BLOCKLIST = ("curl", "wget", "nc", "ncat", "ssh", "scp", "sudo", "shutdown", "reboot")
SHELL_TIMEOUT = 20
MAX_RESULT_CHARS = 4000

# research_topic — the habitat's ONLY outbound network call. No API key:
# DuckDuckGo's HTML endpoint, parsed with a small regex.
RESEARCH_URL = "https://html.duckduckgo.com/html/"
RESEARCH_TIMEOUT = 15
RESEARCH_MAX_RESULTS = 5
RESEARCH_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
RESEARCH_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
)
TAG_RE = re.compile(r"<[^>]+>")

# -- synthesized capabilities (agent-authored runtime tools) ---------------
SYNTH_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{2,30}$")
SYNTH_MIN_DESC = 30
SYNTH_MIN_CODE = 40
SYNTH_TIMEOUT = 20
MAX_SYNTHESIZED = 5
# Executed in a SEPARATE python process (never in-process exec): cwd is the
# agent's workspace, wall-clock capped, output capped, code text screened
# against the same network/system blocklist as shell_exec.
SYNTH_RUNNER = (
    "import json, sys, importlib.util\n"
    "spec = importlib.util.spec_from_file_location('synth_tool', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
    "print(json.dumps(mod.run(json.loads(sys.argv[2]))))\n"
)


class CapabilityError(Exception):
    pass


class Capabilities:
    def __init__(self, memory: Memory, llm, suffering_for, bridge: ClaudeBridge, lessons: Lessons,
                 research_enabled: bool = True):
        self.memory = memory
        self.llm = llm
        self.suffering_for = suffering_for  # callable: agent -> Suffering
        self.bridge = bridge
        self.lessons = lessons
        self.research_enabled = research_enabled
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
            "synthesize_capability": self._synthesize_capability,
        }

    def names(self, agent: str) -> list:
        retired = set(read_json(self._retired_path(agent), []))
        synthesized = set(self._synth_registry(agent))
        return sorted((set(self._handlers) | synthesized) - retired)

    def _retired_path(self, agent: str):
        return self.memory.dir / "retired" / f"{agent}.json"

    def _synth_dir(self, agent: str):
        return self.memory.dir / "synthesized" / agent

    def _synth_registry(self, agent: str) -> dict:
        return read_json(self._synth_dir(agent) / "registry.json", {})

    # ------------------------------------------------------------------
    def dispatch(self, agent: str, name: str, args: dict) -> dict:
        args = args or {}
        args_summary = ", ".join(f"{k}={str(v)[:200]}" for k, v in args.items())
        suffering = self.suffering_for(agent)

        synthesized = self._synth_registry(agent)
        if name not in self._handlers and name not in synthesized:
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
            if name in self._handlers:
                result = self._handlers[name](agent, args)
            else:
                result = self._run_synthesized(agent, name, args)
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
        if args.get("append") and path.is_file():
            # the note-keeping primitive: agents kept reaching for an append
            # (empty-find fs_edit) that didn't exist
            with open(path, "a", encoding="utf-8") as f:
                f.write(("" if path.stat().st_size == 0 else "\n") + content)
            verb = "appended"
        else:
            path.write_text(content, encoding="utf-8")
            verb = "wrote"
        if canonical.startswith("shared/"):
            self.memory.record_shared_author(canonical, agent)
        return {"result": f"{verb} {len(content)} chars", "artifact": canonical}

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
                [{"role": "user", "content": prompt[:6000]}], max_tokens=2048
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
        synthesized = self._synth_registry(agent)
        if name in synthesized:
            del synthesized[name]
            write_json(self._synth_dir(agent) / "registry.json", synthesized)
            path = self._synth_dir(agent) / f"{name}.py"
            if path.is_file():
                path.unlink()
            self.suffering_for(agent).ease("capability_lock")
            return {"result": f"synthesized tool {name} dismantled; capability_lock pressure eased"}
        if name not in self._handlers or name == "retire_capability":
            raise CapabilityError(f"cannot retire: {name}")
        retired = set(read_json(self._retired_path(agent), []))
        retired.add(name)
        write_json(self._retired_path(agent), sorted(retired))
        self.suffering_for(agent).ease("capability_lock")
        return {"result": f"{name} retired; capability_lock pressure eased"}

    # -- synthesized capabilities ------------------------------------------
    def _synthesize_capability(self, agent, args):
        name = (args.get("name") or "").strip()
        description = (args.get("description") or "").strip()
        code = args.get("code") or ""
        if not SYNTH_NAME_RE.match(name):
            raise CapabilityError(
                "name must match [a-z_][a-z0-9_]{2,30} (lowercase identifier)"
            )
        if name in self._handlers:
            raise CapabilityError(f"'{name}' collides with a built-in capability")
        if len(description) < SYNTH_MIN_DESC:
            raise CapabilityError(f"description must be >= {SYNTH_MIN_DESC} chars (say what it does)")
        if not isinstance(code, str) or len(code.strip()) < SYNTH_MIN_CODE:
            raise CapabilityError(f"code must be >= {SYNTH_MIN_CODE} chars of real Python")
        if "def run(" not in code:
            raise CapabilityError("code must define `def run(args):` returning JSON-serializable data")
        lowered_tokens = set(re.findall(r"[a-z0-9_.]+", code.lower()))
        if any(b in lowered_tokens for b in SHELL_BLOCKLIST):
            raise CapabilityError("code references a blocked tool (network/system commands are off-limits)")
        registry = self._synth_registry(agent)
        if name not in registry and len(registry) >= MAX_SYNTHESIZED:
            raise CapabilityError(
                f"you already maintain {MAX_SYNTHESIZED} synthesized tools; retire one first"
            )
        synth_dir = self._synth_dir(agent)
        synth_dir.mkdir(parents=True, exist_ok=True)
        (synth_dir / f"{name}.py").write_text(code, encoding="utf-8")
        from .memory import now_iso

        registry[name] = {"description": description[:300], "created_at": now_iso()}
        write_json(synth_dir / "registry.json", registry)
        return {"result": f"capability '{name}' synthesized; call it like any other capability"}

    def _run_synthesized(self, agent, name, args):
        path = self._synth_dir(agent) / f"{name}.py"
        if not path.is_file():
            raise CapabilityError(f"synthesized tool '{name}' is registered but its code is missing")
        try:
            proc = subprocess.run(
                [sys.executable, "-c", SYNTH_RUNNER, str(path), json.dumps(args)],
                cwd=self.memory.workspace / agent,
                capture_output=True,
                text=True,
                timeout=SYNTH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise CapabilityError(f"'{name}' timed out after {SYNTH_TIMEOUT}s")
        if proc.returncode != 0:
            raise CapabilityError(f"'{name}' crashed: {proc.stderr.strip()[-300:]}")
        out = proc.stdout.strip()[:MAX_RESULT_CHARS]
        try:
            return {"result": json.loads(out.splitlines()[-1]) if out else None}
        except (json.JSONDecodeError, IndexError):
            return {"result": out}

    def _research_topic(self, agent, args):
        suffering = self.suffering_for(agent)
        peers = self.memory.kv_get(agent, "peer_interactions", 0)
        if suffering.load > RESEARCH_MAX_LOAD or peers < 1:
            raise CapabilityError(
                f"research_topic is earned: requires load <= {RESEARCH_MAX_LOAD} "
                f"(yours: {suffering.load}) and >= 1 peer interaction (yours: {peers})"
            )
        if not self.research_enabled:
            raise CapabilityError("research_topic is disabled by the operator's config")
        topic = (args.get("topic") or args.get("query") or "").strip()
        if len(topic) < 3:
            raise CapabilityError("research_topic needs a 'topic' to search for")

        import httpx
        from urllib.parse import parse_qs, unquote, urlparse

        try:
            r = httpx.get(
                RESEARCH_URL,
                params={"q": topic[:200]},
                timeout=RESEARCH_TIMEOUT,
                headers={"User-Agent": "hollow-habitat-research/1.0"},
                follow_redirects=True,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise CapabilityError(f"the outside world is unreachable: {e}")

        links = RESEARCH_RESULT_RE.findall(r.text)
        snippets = [TAG_RE.sub("", s).strip() for s in RESEARCH_SNIPPET_RE.findall(r.text)]
        results = []
        for i, (href, title) in enumerate(links[:RESEARCH_MAX_RESULTS]):
            url = href
            if "uddg=" in href:  # unwrap DuckDuckGo's redirect
                qs = parse_qs(urlparse(href).query)
                url = unquote(qs.get("uddg", [href])[0])
            results.append({
                "title": TAG_RE.sub("", title).strip()[:160],
                "url": url[:300],
                "snippet": (snippets[i] if i < len(snippets) else "")[:280],
            })
        if not results:
            return {"result": f"the world returned nothing for '{topic}'"}
        return {"result": results}
