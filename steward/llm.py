"""Thin LLM layer. The runtime only depends on `LLM.complete`, so tests use ScriptedLLM and
production uses AnthropicLLM. Messages/blocks are plain dicts (JSON-serialisable for checkpoints)."""
from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class TransientRunError(Exception):
    """LLM/API temporarily unavailable: the worker should requeue with backoff."""


class QuotaExhausted(TransientRunError):
    """The plan/account usage limit is used up. Retrying in seconds is pointless: the worker defers the task until the reset
    time (retry_after_s) WITHOUT burning one of its attempts, and the run resumes from its checkpoint."""
    def __init__(self, msg: str, retry_after_s: float = 1800.0):
        super().__init__(msg)
        self.retry_after_s = retry_after_s


def parse_reset_seconds(text: str, now=None, default: float = 1800.0) -> float:
    """'... resets 3:20pm (Asia/Kolkata)' -> seconds until then (+60s margin); falls back to `default` when unparseable."""
    import datetime as _dt
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\(([^)]+)\))?", text or "", re.I)
    if not m:
        return default
    h, mi, ap, tzname = int(m.group(1)) % 12, int(m.group(2) or 0), m.group(3).lower(), m.group(4)
    if ap == "pm":
        h += 12
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tzname) if tzname else None
    except Exception:
        tz = None
    n = now or _dt.datetime.now(tz)
    if tz and n.tzinfo is None:
        n = n.replace(tzinfo=tz)
    t = n.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t <= n:
        t += _dt.timedelta(days=1)
    return min(max((t - n).total_seconds() + 60.0, 60.0), 6 * 3600.0)


class FatalRunError(Exception):
    """Retrying cannot help (bad/missing key, no credits, invalid request): fail fast with a clear message."""


@dataclass
class LLMResponse:
    blocks: list[dict]
    stop_reason: str
    usage: dict = field(default_factory=dict)


class LLM(Protocol):
    def complete(self, *, model: str, system: str, messages: list[dict], tools: list[dict] | None = None,
                 tool_choice: dict | None = None, max_tokens: int = 4096) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, api_key: str | None = None, max_retries: int = 4, cache: bool | None = None):
        import os

        import anthropic
        self._a = anthropic
        self.cache = (os.getenv("STEWARD_PROMPT_CACHE", "1") != "0") if cache is None else cache
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries)

    def complete(self, *, model, system, messages, tools=None, tool_choice=None, max_tokens=4096) -> LLMResponse:
        # Prompt caching: the system prompt and tool list are identical on every call of a run.
        sys_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if self.cache else system
        kw: dict[str, Any] = dict(model=model, max_tokens=max_tokens, system=sys_blocks, messages=messages)
        if tools:
            tools = [dict(t) for t in tools]
            if self.cache:
                tools[-1]["cache_control"] = {"type": "ephemeral"}
            kw["tools"] = tools
        if tool_choice:
            kw["tool_choice"] = tool_choice
        try:
            r = self.client.messages.create(**kw)
        except (self._a.APIConnectionError, self._a.RateLimitError, self._a.InternalServerError) as e:
            raise TransientRunError(f"{type(e).__name__}: {e}") from e
        except (self._a.AuthenticationError, self._a.PermissionDeniedError, self._a.NotFoundError, self._a.BadRequestError) as e:
            msg = getattr(e, "message", str(e))
            raise FatalRunError(f"Anthropic API rejected the request ({type(e).__name__}): {msg}") from e
        blocks = []
        for b in r.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
        u = r.usage
        return LLMResponse(blocks, r.stop_reason, {
            "input_tokens": u.input_tokens + (getattr(u, "cache_read_input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0),
            "output_tokens": u.output_tokens, "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0})


# ------------------------------------------------------------------ test double
_ids = itertools.count(1)


def tx(text: str) -> dict:
    return {"type": "text", "text": text}


def tu(name: str, **input) -> dict:
    return {"type": "tool_use", "id": f"toolu_{next(_ids):04d}", "name": name, "input": input}


class ScriptedLLM:
    """Plays back a fixed list of turns. A turn is a list of blocks or a callable(request_kwargs) -> blocks."""

    def __init__(self, turns: list):
        self.turns, self.calls = list(turns), []

    def complete(self, **kw) -> LLMResponse:
        self.calls.append(copy.deepcopy(kw))     # snapshot: the runtime keeps mutating its message list
        if not self.turns:
            raise AssertionError("ScriptedLLM script exhausted (runtime asked for more turns than the test planned)")
        t = self.turns.pop(0)
        blocks = t(copy.deepcopy(kw)) if callable(t) else t
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return LLMResponse(blocks, stop, {"input_tokens": 0, "output_tokens": 0})


# =============================================================================================
# Extra backends. All implement LLM.complete and return Anthropic-style blocks, so the runtime
# never knows which one it is talking to.
# =============================================================================================
import json
import os
import re
import subprocess
import tempfile
import time


def compact_messages(messages: list[dict], keep_full: int = 6, limit: int = 1500) -> list[dict]:
    """Copy of the history where all but the newest `keep_full` tool results are truncated.
    Old page snapshots are stale anyway, and this keeps re-sent history (hence tokens) small."""
    msgs = copy.deepcopy(messages)
    refs = [b for m in msgs if isinstance(m.get("content"), list) for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"]
    for b in refs[:-keep_full] if keep_full else refs:
        c = b.get("content")
        if isinstance(c, str) and len(c) > limit:
            b["content"] = c[:limit] + f" ...[older result truncated, {len(c) - limit} chars]"
    return msgs


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            return None
    return None


_tool_id = itertools.count(1)


def _new_id() -> str:
    return f"toolu_{int(time.time()) % 100000}_{next(_tool_id):04d}"


class ClaudeCodeLLM:
    """Uses YOUR OWN Claude subscription through the unmodified `claude` CLI in headless mode (`claude -p`).

    * You sign in yourself (`claude` -> /login). Steward never sees or stores subscription credentials.
    * ANTHROPIC_API_KEY is removed from the child environment so the CLI uses your subscription, not API credits.
    * The CLI's built-in tools are disabled (`--tools ""`): Claude only *decides* the next action as JSON
      (enforced with --json-schema); Steward's runtime executes it, behind the policy gate.
    For personal/local development and demos only. Anthropic does not permit routing other people's requests
    through subscription credentials; a deployed product must use API-key auth (AnthropicLLM).
    """
    name = "claude-code (subscription)"
    CANDIDATE_FLAGS = ["--system-prompt", "--tools", "--json-schema", "--model", "--no-session-persistence",
                       "--disable-slash-commands", "--strict-mcp-config"]

    def __init__(self, binary: str | None = None, model: str | None = None, timeout: float = 300, min_interval: float = 0.0):
        self.binary = binary or os.getenv("STEWARD_CLAUDE_BIN", "claude")
        self.model = model or os.getenv("STEWARD_CLAUDE_MODEL") or None
        self.timeout, self.min_interval, self._last = timeout, min_interval, 0.0
        self.workdir = tempfile.mkdtemp(prefix="steward-cc-")      # empty cwd: no CLAUDE.md / project settings get loaded
        self._flags: set[str] | None = None

    def _supported(self) -> set[str]:
        if self._flags is None:
            try:
                h = subprocess.run([self.binary, "--help"], capture_output=True, text=True, timeout=60, cwd=self.workdir)
            except FileNotFoundError as e:
                raise FatalRunError("`claude` CLI not found. Install Claude Code and sign in once with `claude` (then /login), "
                                    "or set STEWARD_CLAUDE_BIN.") from e
            out = h.stdout + h.stderr
            self._flags = {f for f in self.CANDIDATE_FLAGS if f in out}
        return self._flags

    # ---------------------------------------------------------------- prompt
    @staticmethod
    def _render(messages: list[dict]) -> str:
        names: dict[str, str] = {}
        out: list[str] = []
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append(f"[{m['role']}]\n{c}")
                continue
            parts = []
            for b in c:
                if b["type"] == "text":
                    parts.append(b["text"])
                elif b["type"] == "tool_use":
                    names[b["id"]] = b["name"]
                    parts.append(f"CALLED {b['name']} {json.dumps(b['input'], ensure_ascii=False)}")
                elif b["type"] == "tool_result":
                    tag = "error from" if b.get("is_error") else "result of"
                    parts.append(f"[{tag} {names.get(b['tool_use_id'], 'tool')}]\n{b['content']}")
            out.append(f"[{m['role']}]\n" + "\n".join(parts))
        return "\n\n".join(out)

    def _prompt_and_schema(self, messages, tools, tool_choice):
        history = self._render(compact_messages(messages))
        if tool_choice and tool_choice.get("type") == "tool":
            t = next(t for t in tools if t["name"] == tool_choice["name"])
            prompt = (f"## Task\nYou must call the tool `{t['name']}` ({t['description']}).\n"
                      f"Reply with ONLY the tool's input as a JSON object matching its input schema.\n\n## Input\n{history}")
            return prompt, t["input_schema"], t["name"]
        catalog = "\n".join(f"- {t['name']}: {t['description']}\n  input_schema: {json.dumps(t['input_schema'], ensure_ascii=False)}" for t in tools or [])
        schema = {"type": "object", "properties": {
            "text": {"type": "string", "description": "One short sentence: what you are about to do and why (may be empty)."},
            "tool_calls": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string", "enum": [t["name"] for t in tools or []]}, "input": {"type": "object"}},
                "required": ["name", "input"]}}}, "required": ["text", "tool_calls"]}
        prompt = ("You are the reasoning engine of an autonomous agent. You cannot run tools yourself: you decide the next action and the agent "
                  "runtime executes it and shows you the result in the next turn.\n\n## Tools the runtime can execute for you\n" + catalog +
                  "\n\n## Conversation so far\n" + history +
                  "\n\n## Your reply\nReturn JSON {\"text\": <one short sentence on what you will do and why>, \"tool_calls\": [{\"name\": <tool>, \"input\": {...}}]}.\n"
                  "Use exactly the tool names above. Prefer ONE tool call per reply when the next step depends on its result. "
                  "Never invent tool names or arguments you were not given.")
        return prompt, schema, None

    # ---------------------------------------------------------------- call
    def complete(self, *, model, system, messages, tools=None, tool_choice=None, max_tokens=4096) -> LLMResponse:
        sup = self._supported()
        prompt, schema, forced = self._prompt_and_schema(messages, tools, tool_choice)
        cmd = [self.binary, "-p", "--output-format", "json"]
        if "--json-schema" in sup:
            cmd += ["--json-schema", json.dumps(schema)]
        if "--system-prompt" in sup:
            cmd += ["--system-prompt", system]
        else:
            prompt = system + "\n\n" + prompt
        if "--tools" in sup:
            cmd += ["--tools", ""]
        for f in ("--no-session-persistence", "--disable-slash-commands"):
            if f in sup:
                cmd.append(f)
        if self.model and "--model" in sup:
            cmd += ["--model", self.model]
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}

        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=self.workdir, env=env)
        except subprocess.TimeoutExpired as e:
            raise TransientRunError(f"claude CLI timed out after {self.timeout}s") from e
        finally:
            self._last = time.time()

        data = self._parse_envelope(p.stdout)
        text_all = ((data or {}).get("result") or "") + " " + (p.stderr or "")
        if data is None or data.get("is_error") or p.returncode != 0:
            self._raise_for(text_all, data, p)
        obj = data.get("structured_output")
        if obj is None:
            obj = _extract_json(data.get("result", ""))
        if obj is None:
            raise TransientRunError("claude CLI returned no parsable JSON: " + (data.get("result", "")[:200]))

        u = data.get("usage") or {}
        usage = {"input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                 "output_tokens": u.get("output_tokens") or 0, "cost_usd_list_price": data.get("total_cost_usd"),
                 "model_used": sorted((data.get("modelUsage") or {}).keys())}
        if forced:
            return LLMResponse([{"type": "tool_use", "id": _new_id(), "name": forced, "input": obj}], "tool_use", usage)
        blocks: list[dict] = []
        if isinstance(obj.get("text"), str) and obj["text"].strip():
            blocks.append({"type": "text", "text": obj["text"]})
        for c in obj.get("tool_calls") or []:
            inp = c.get("input", {})
            if isinstance(inp, str):
                inp = _extract_json(inp) or {}
            blocks.append({"type": "tool_use", "id": _new_id(), "name": c.get("name", ""), "input": inp})
        return LLMResponse(blocks, "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn", usage)

    @staticmethod
    def _parse_envelope(stdout: str) -> dict | None:
        try:
            d = json.loads(stdout)
        except ValueError:
            return None
        if isinstance(d, list):                                   # verbose mode returns a list of events
            d = next((x for x in reversed(d) if isinstance(x, dict) and x.get("type") == "result"), None)
        return d if isinstance(d, dict) else None

    @staticmethod
    def _raise_for(text: str, data: dict | None, p) -> None:
        low = text.lower()
        status = (data or {}).get("api_error_status")
        if any(k in low for k in ("not logged in", "please run /login", "/login", "invalid api key", "authentication", "oauth")) or status in (401, 403):
            raise FatalRunError("Claude Code is not signed in. Run `claude` once in a terminal and complete /login with your Claude plan, then retry. "
                                f"(CLI said: {text.strip()[:160]})")
        if any(k in low for k in ("session limit", "usage limit", "limit reached", "weekly limit", "hit your limit", "you've hit your", "you have hit your")):
            raise QuotaExhausted(f"Claude plan limit reached: {text.strip()[:200]}", parse_reset_seconds(text))
        if any(k in low for k in ("rate limit", "too many requests", "429", "overloaded", "529", "503", "timeout")):
            raise TransientRunError(f"Claude overload / rate limit: {text.strip()[:200]}")
        raise TransientRunError(f"claude CLI failed (exit {p.returncode}): {text.strip()[:300] or p.stderr[:300]}")


class OpenAICompatLLM:
    """Any OpenAI-compatible chat-completions endpoint with tool calling: Gemini (free tier), Groq, OpenRouter, Ollama, ..."""
    PRESETS = {
        "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
        "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
        "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        "ollama": ("http://localhost:11434/v1", None),
    }
    KEEP = {"type", "properties", "required", "items", "enum", "description", "nullable"}

    def __init__(self, base_url: str, api_key: str | None, model: str, *, min_interval: float = 0.0, client=None,
                 max_429_waits: int = 6, name: str = "openai-compatible"):
        import httpx
        self.base, self.key, self.model, self.name = base_url.rstrip("/"), api_key, model, f"{name}:{model}"
        self.min_interval, self._last, self.max_429_waits = min_interval, 0.0, max_429_waits
        self.client = client or httpx.Client(timeout=120)

    @classmethod
    def from_env(cls, preset: str | None = None) -> "OpenAICompatLLM":
        preset = preset or os.getenv("STEWARD_LLM", "gemini")
        base, keyvar = cls.PRESETS.get(preset, (None, None))
        base = os.getenv("STEWARD_OPENAI_BASE_URL", base or "")
        key = os.getenv("STEWARD_OPENAI_API_KEY") or (os.getenv(keyvar) if keyvar else None) or ("ollama" if preset == "ollama" else None)
        model = os.getenv("STEWARD_OPENAI_MODEL", "")
        if not base or not model or not key:
            raise FatalRunError(f"{preset}: set STEWARD_OPENAI_MODEL and an API key ({keyvar or 'STEWARD_OPENAI_API_KEY'}) in .env")
        return cls(base, key, model, min_interval=float(os.getenv("STEWARD_LLM_MIN_INTERVAL_S", "0")), name=preset)

    # ---------------------------------------------------------------- conversions
    @classmethod
    def _schema(cls, s: Any) -> Any:
        """Reduce JSON Schema to the portable subset (Gemini rejects unions, free-form objects, extra keywords)."""
        if isinstance(s, list):
            return [cls._schema(x) for x in s]
        if not isinstance(s, dict):
            return s
        out = {}
        for k, v in s.items():
            if k not in cls.KEEP:
                continue
            if k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                v = non_null[0] if non_null else "string"
            if k == "properties":
                v = {pk: cls._schema(pv) for pk, pv in v.items()}
            elif k == "items":
                v = cls._schema(v)
            out[k] = v
        if out.get("type") == "object" and not out.get("properties"):
            out["properties"] = {"_unused": {"type": "string", "description": "leave empty"}}
        return out

    def _messages(self, system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in compact_messages(messages):
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": m["role"], "content": c})
            elif m["role"] == "assistant":
                text = "\n".join(b["text"] for b in c if b["type"] == "text")
                calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}}
                         for b in c if b["type"] == "tool_use"]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:
                for b in c:
                    if b["type"] == "tool_result":
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                    "content": ("ERROR: " if b.get("is_error") else "") + str(b["content"])})
                txt = "\n".join(b["text"] for b in c if b["type"] == "text")
                if txt:
                    out.append({"role": "user", "content": txt})
        return out

    def complete(self, *, model, system, messages, tools=None, tool_choice=None, max_tokens=4096) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": self._messages(system, messages), "max_tokens": max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": self._schema(t["input_schema"])}} for t in tools]
            body["tool_choice"] = ({"type": "function", "function": {"name": tool_choice["name"]}}
                                   if tool_choice and tool_choice.get("type") == "tool" else "auto")
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

        for attempt in range(self.max_429_waits + 1):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.client.post(f"{self.base}/chat/completions", json=body, headers=headers)
            except Exception as e:
                raise TransientRunError(f"{type(e).__name__}: {e}") from e
            finally:
                self._last = time.time()
            if r.status_code == 429 and attempt < self.max_429_waits:
                ra = r.headers.get("retry-after")
                time.sleep(min(60.0, float(ra)) if ra and ra.replace(".", "").isdigit() else min(60.0, 4.0 * 2 ** attempt))
                continue
            break
        if r.status_code in (401, 403, 404) or (400 <= r.status_code < 500 and r.status_code != 429):
            raise FatalRunError(f"LLM endpoint rejected the request (HTTP {r.status_code}): {r.text[:300]}")
        if r.status_code >= 400:
            raise TransientRunError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        blocks: list[dict] = []
        if (msg.get("content") or "").strip():
            blocks.append({"type": "text", "text": msg["content"]})
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = _extract_json(fn.get("arguments", "")) or {}
            args = {k: v for k, v in args.items() if k != "_unused"}
            blocks.append({"type": "tool_use", "id": c.get("id") or _new_id(), "name": fn.get("name", ""), "input": args})
        u = data.get("usage") or {}
        return LLMResponse(blocks, "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
                           {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)})


def make_llm(kind: str | None = None):
    """STEWARD_LLM = anthropic (API key, default) | claude_code (your Claude plan via the claude CLI) | gemini | groq | openrouter | ollama"""
    kind = (kind or os.getenv("STEWARD_LLM", "anthropic")).strip().lower().replace("-", "_")
    if kind == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise FatalRunError("STEWARD_LLM=anthropic needs ANTHROPIC_API_KEY. Without API credits use STEWARD_LLM=claude_code (your Claude plan) "
                                "or STEWARD_LLM=gemini (free API key).")
        return AnthropicLLM()
    if kind in ("claude_code", "subscription", "claude_plan"):
        return ClaudeCodeLLM()
    if kind in OpenAICompatLLM.PRESETS or kind == "openai":
        return OpenAICompatLLM.from_env(kind)
    raise FatalRunError(f"unknown STEWARD_LLM '{kind}'")
