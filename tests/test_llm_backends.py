"""Backends other than the Anthropic API: the `claude` CLI (subscription) and OpenAI-compatible endpoints.
No network and no real CLI: a fake `claude` executable and httpx.MockTransport."""
import json
import os
import stat
import sys

import httpx
import pytest

from steward.llm import (ClaudeCodeLLM, FatalRunError, OpenAICompatLLM, TransientRunError, compact_messages,
                         make_llm)

TOOLS = [{"name": "files__list", "description": "list files", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
         {"name": "finish", "description": "done", "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}]
MSGS = [{"role": "user", "content": "do it"}]

FAKE = r'''#!{py}
import json, os, sys
if "--help" in sys.argv:
    print("Usage: claude [options]  --system-prompt --tools --json-schema --model --no-session-persistence --disable-slash-commands")
    sys.exit(0)
rec = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
       "has_key": "ANTHROPIC_API_KEY" in os.environ, "has_tok": "ANTHROPIC_AUTH_TOKEN" in os.environ}}
open(os.environ["FAKE_REC"], "w").write(json.dumps(rec))
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "ok":
    print(json.dumps({{"type": "result", "is_error": False, "structured_output": json.loads(os.environ["FAKE_OUT"]),
                      "usage": {{"input_tokens": 10, "output_tokens": 5}}, "total_cost_usd": 0.01}}))
elif mode == "result_text":
    print(json.dumps({{"type": "result", "is_error": False, "result": "```json\n" + os.environ["FAKE_OUT"] + "\n```"}}))
elif mode == "login":
    print(json.dumps({{"type": "result", "is_error": True, "result": "Not logged in - Please run /login"}})); sys.exit(1)
elif mode == "limit":
    print(json.dumps({{"type": "result", "is_error": True, "result": "You've hit your session limit · resets 3:20pm (Asia/Kolkata)"}})); sys.exit(1)
elif mode == "garbage":
    print("not json at all")
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    p = tmp_path / "claude"
    p.write_text(FAKE.format(py=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    rec = tmp_path / "rec.json"
    monkeypatch.setenv("FAKE_REC", str(rec))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-not-leak")

    def run(mode="ok", out=None, **kw):
        monkeypatch.setenv("FAKE_MODE", mode)
        monkeypatch.setenv("FAKE_OUT", json.dumps(out or {}))
        llm = ClaudeCodeLLM(binary=str(p), **kw)
        return llm, (lambda: json.loads(rec.read_text()))
    return run


def test_claude_code_normal_turn_and_isolation(fake_claude):
    llm, rec = fake_claude(out={"text": "listing", "tool_calls": [{"name": "files__list", "input": {"path": "."}}]}, model="sonnet")
    r = llm.complete(model="x", system="SYS PROMPT", messages=MSGS, tools=TOOLS)
    assert r.stop_reason == "tool_use"
    assert r.blocks[0] == {"type": "text", "text": "listing"}
    tu = r.blocks[1]
    assert tu["type"] == "tool_use" and tu["name"] == "files__list" and tu["input"] == {"path": "."} and tu["id"].startswith("toolu_")
    assert r.usage["output_tokens"] == 5
    d = rec()
    a = d["argv"]
    assert a[0] == "-p" and "--json-schema" in a and a[a.index("--system-prompt") + 1] == "SYS PROMPT"
    assert a[a.index("--tools") + 1] == ""                       # CLI built-in tools disabled
    assert "--no-session-persistence" in a and a[a.index("--model") + 1] == "sonnet"
    assert not d["has_key"] and not d["has_tok"]                 # API credentials never reach the subscription path
    assert d["cwd"] == llm.workdir and os.listdir(llm.workdir) == []
    assert "files__list" in d["stdin"] and "do it" in d["stdin"]


def test_claude_code_forced_tool(fake_claude):
    llm, rec = fake_claude(out={"summary": "all good"})
    r = llm.complete(model="x", system="s", messages=MSGS, tools=TOOLS, tool_choice={"type": "tool", "name": "finish"})
    assert r.blocks == [{"type": "tool_use", "id": r.blocks[0]["id"], "name": "finish", "input": {"summary": "all good"}}]
    schema = json.loads(rec()["argv"][rec()["argv"].index("--json-schema") + 1])
    assert schema["required"] == ["summary"]


def test_claude_code_parses_fenced_json_in_result(fake_claude):
    llm, _ = fake_claude(mode="result_text", out={"text": "", "tool_calls": []})
    r = llm.complete(model="x", system="s", messages=MSGS, tools=TOOLS)
    assert r.stop_reason == "end_turn" and r.blocks == []


def test_claude_code_error_classification(fake_claude):
    llm, _ = fake_claude(mode="login")
    with pytest.raises(FatalRunError, match="not signed in"):
        llm.complete(model="x", system="s", messages=MSGS, tools=TOOLS)
    llm, _ = fake_claude(mode="limit")
    from steward.llm import QuotaExhausted
    with pytest.raises(QuotaExhausted, match="limit") as ei:
        llm.complete(model="x", system="s", messages=MSGS, tools=TOOLS)
    assert 60 <= ei.value.retry_after_s <= 6 * 3600
    llm, _ = fake_claude(mode="garbage")
    with pytest.raises(TransientRunError):
        llm.complete(model="x", system="s", messages=MSGS, tools=TOOLS)


def test_claude_code_missing_binary_is_fatal(tmp_path):
    with pytest.raises(FatalRunError, match="not found"):
        ClaudeCodeLLM(binary=str(tmp_path / "nope")).complete(model="x", system="s", messages=MSGS, tools=TOOLS)


def test_compact_messages_truncates_old_tool_results_only():
    big = "x" * 2000
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": big}]} for i in range(6)]
    out = compact_messages(msgs, keep_full=2, limit=100)
    lens = [len(m["content"][0]["content"]) for m in out]
    assert lens[-2:] == [2000, 2000] and all(n < 200 for n in lens[:-2])
    assert len(msgs[0]["content"][0]["content"]) == 2000          # input untouched


# ----------------------------------------------------------------- OpenAI-compatible
def _llm(handler, **kw):
    return OpenAICompatLLM("http://x/v1", "k", "m1", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_openai_compat_request_and_response_shape():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "files__list", "arguments": "{\"path\": \"/\", \"_unused\": \"\"}"}}]}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {"a": {"type": ["string", "null"], "title": "A", "additionalProperties": False}}}},
             {"name": "empty", "description": "d", "input_schema": {"type": "object"}}]
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "text", "text": "hm"}, {"type": "tool_use", "id": "c0", "name": "t", "input": {"a": "1"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c0", "content": "boom", "is_error": True}]}]
    r = _llm(lambda q: handler(q)).complete(model="ignored", system="SYS", messages=msgs, tools=tools,
                                            tool_choice={"type": "tool", "name": "t"})
    b = seen["body"]
    assert seen["auth"] == "Bearer k" and b["model"] == "m1"
    assert b["messages"][0] == {"role": "system", "content": "SYS"}
    assert b["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"a": "1"}'
    assert b["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": "ERROR: boom"}
    params = b["tools"][0]["function"]["parameters"]
    assert params["properties"]["a"] == {"type": "string"}          # union collapsed, junk keywords dropped
    assert "_unused" in b["tools"][1]["function"]["parameters"]["properties"]
    assert b["tool_choice"] == {"type": "function", "function": {"name": "t"}}
    assert r.stop_reason == "tool_use" and r.blocks[1] == {"type": "tool_use", "id": "c1", "name": "files__list", "input": {"path": "/"}}
    assert r.usage == {"input_tokens": 7, "output_tokens": 3}


def test_openai_compat_retries_429_then_succeeds(monkeypatch):
    import steward.llm as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "1"}, text="slow down")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    r = _llm(handler).complete(model="x", system="s", messages=MSGS)
    assert len(calls) == 3 and r.stop_reason == "end_turn"


def test_openai_compat_error_classes(monkeypatch):
    import steward.llm as m
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    with pytest.raises(FatalRunError):
        _llm(lambda q: httpx.Response(401, text="bad key")).complete(model="x", system="s", messages=MSGS)
    with pytest.raises(TransientRunError):
        _llm(lambda q: httpx.Response(503, text="down")).complete(model="x", system="s", messages=MSGS)
    with pytest.raises(TransientRunError):
        _llm(lambda q: httpx.Response(429, text="slow"), max_429_waits=1).complete(model="x", system="s", messages=MSGS)


def test_make_llm_selection(monkeypatch):
    for k in ("STEWARD_LLM", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "STEWARD_OPENAI_MODEL", "STEWARD_OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(FatalRunError, match="claude_code"):
        make_llm()                                                  # default needs an API key, error explains alternatives
    assert isinstance(make_llm("claude_code"), ClaudeCodeLLM)
    assert isinstance(make_llm("claude-code"), ClaudeCodeLLM)
    with pytest.raises(FatalRunError, match="STEWARD_OPENAI_MODEL"):
        make_llm("gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("STEWARD_OPENAI_MODEL", "gemini-2.5-flash")
    g = make_llm("gemini")
    assert isinstance(g, OpenAICompatLLM) and "generativelanguage" in g.base
    with pytest.raises(FatalRunError, match="unknown"):
        make_llm("bogus")
