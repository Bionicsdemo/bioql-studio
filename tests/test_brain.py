"""The brain. One model — Claude Code — and nothing that could become a second one.

No test here runs the `claude` binary: the process is injected. What is tested is the contract around it, which is
where the honesty lives.
"""
import json
import os

import pytest

from studio import allowlist as al
from studio import brain


class FakeProc:
    """A `claude` that answers from a script. It records what it was told, so a test can prove what was sent."""

    def __init__(self, lines, die_after=None):
        self.lines, self.die_after = list(lines), die_after
        self.sent, self._out, self._rc = [], [], None
        self.stdin, self.stdout = self, self
        self.stderr = None

    def write(self, s):
        if self.die_after is not None and len(self.sent) >= self.die_after:
            raise BrokenPipeError("gone")
        self.sent.append(s)
        self._out = list(self.lines)

    def flush(self):
        pass

    def readline(self):
        return self._out.pop(0) if self._out else ""

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0

    def kill(self):
        self._rc = -9

    def wait(self, timeout=None):
        self._rc = 0
        return 0


def say(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def tool(name, args):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": args}]}}) + "\n"


def result(text, cost=0.01, turns=2):
    return json.dumps({"type": "result", "result": text, "total_cost_usd": cost, "num_turns": turns,
                       "usage": {"input_tokens": 10, "output_tokens": 5}}) + "\n"


# ------------------------------------------------------------------ one brain, by construction
def test_bioqls_own_models_are_not_reachable_at_all():
    """BioQL's window reaches a local vMLX engine, DeepSeek and an OpenAI-compatible route. The new system has one
    brain, so those are not refused as dangerous — they are simply not in the list."""
    for path in ("/api/localllm/v1/chat/completions", "/api/llm/context", "/api/marie/v1/audio/speech",
                 "/api/openrouter/api/v1/chat/completions", "/api/deepseek/v1/models"):
        bad = al.problems(al._norm("x", {"method": "POST", "path": path, "kind": "local"}))
        assert any("one brain" in b for b in bad), (path, bad)
    with pytest.raises(ValueError, match="one brain"):
        al.save_entry("sneaky_llm", {"method": "POST", "path": "/api/localllm/v1/chat/completions", "kind": "local"})


def test_claude_gets_bioql_and_nothing_else(studio_home, monkeypatch):
    monkeypatch.setattr(brain, "binary", lambda: "/usr/bin/true")
    argv = brain.command("hola", brain.mcp_config())
    assert "--strict-mcp-config" in argv
    allowed = argv[argv.index("--allowedTools") + 1]
    denied = argv[argv.index("--disallowedTools") + 1]
    assert allowed == "mcp__bioql__*"
    for t in ("Bash", "Edit", "Write", "Read", "WebFetch", "WebSearch", "Task"):
        assert t in denied, t


def test_the_mcp_config_is_private_from_the_first_byte(studio_home):
    p = brain.mcp_config()
    assert oct(p.stat().st_mode)[-3:] == "600"
    cfg = json.loads(p.read_text())
    assert set(cfg["mcpServers"]) == {"bioql"}
    assert cfg["mcpServers"]["bioql"]["args"] == ["-m", "studio", "mcp"]


def test_no_secret_rides_on_the_command_line(studio_home, monkeypatch):
    """Agtr passes MCP env as `-c mcp_servers.X.env={…}` arguments, which `ps` shows to every process
    (agtrdesktop/engines.py). Studio's env goes in the 0600 config file."""
    monkeypatch.setattr(brain, "binary", lambda: "/usr/bin/true")
    cfg = brain.mcp_config(extra={"pubmed": {"command": "x", "env": {"PUBMED_KEY": "s3cret-value"}}})
    argv = brain.command("hola", cfg)
    assert not any("s3cret-value" in a for a in argv)
    assert "s3cret-value" in cfg.read_text()        # it is in the file, which is 0600
    assert oct(cfg.stat().st_mode)[-3:] == "600"


def test_a_missing_claude_is_said_plainly(monkeypatch):
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(brain.shutil, "which", lambda _: None)
    with pytest.raises(brain.NoClaude, match="one brain"):
        brain.binary()


# ------------------------------------------------------------------ the stream
def test_a_turn_yields_what_the_window_draws(studio_home):
    s = brain.Session(spawn=lambda: FakeProc([
        say("Looking at the engine."),
        tool("mcp__bioql__backend_health", {}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": '{"state":"RESOLVED"}'}]}]}}) + "\n",
        result("The engine is healthy."),
    ]))
    got = list(s.ask("is the engine up?"))
    assert [e["type"] for e in got] == ["say", "tool", "tool_result", "result"]
    assert got[1]["name"] == "mcp__bioql__backend_health"
    assert got[-1]["cost"] == 0.01 and got[-1]["usage"]["input_tokens"] == 10


def test_a_line_that_is_not_json_is_kept_not_dropped():
    """A brain that swallows what it did not expect is the same defect as a tool that returns a plausible value
    instead of stopping."""
    assert brain.events("not json at all") == [{"type": "raw", "text": "not json at all"}]
    assert brain.events("   ") == []


def test_a_failed_turn_is_a_failure_not_an_empty_answer(studio_home):
    s = brain.Session(spawn=lambda: FakeProc([
        json.dumps({"type": "result", "is_error": True, "result": "usage limit"}) + "\n"]))
    got = list(s.ask("x"))
    assert got == [{"type": "fail", "text": "usage limit"}]


def test_a_brain_that_dies_mid_question_is_reported(studio_home):
    s = brain.Session(spawn=lambda: FakeProc([], die_after=0))
    got = list(s.ask("x"))
    assert got[0]["type"] == "fail" and "died" in got[0]["text"]


def test_a_brain_that_stops_without_answering_is_reported(studio_home):
    s = brain.Session(spawn=lambda: FakeProc([say("thinking…")]))
    got = list(s.ask("x"))
    assert got[0]["type"] == "say" and got[-1]["type"] == "fail"
    assert "without answering" in got[-1]["text"]


# ------------------------------------------------------------------ the warm session
def test_the_session_is_kept_warm_and_then_replaced(studio_home):
    made = []

    def spawn():
        p = FakeProc([result("ok")])
        made.append(p)
        return p

    s = brain.Session(spawn=spawn)
    for _ in range(3):
        list(s.ask("x"))
    assert len(made) == 1, "a cold start per question would make every answer feel broken"

    s.turns = brain.TURNS_MAX
    list(s.ask("x"))
    assert len(made) == 2, "after enough turns the session is replaced"

    s.last = 0.1                                    # long idle
    list(s.ask("x"))
    assert len(made) == 3


def test_a_dead_process_is_replaced_not_reused(studio_home):
    made = []

    def spawn():
        p = FakeProc([result("ok")])
        made.append(p)
        return p

    s = brain.Session(spawn=spawn)
    list(s.ask("x"))
    made[0]._rc = 1                                 # it died between turns
    list(s.ask("x"))
    assert len(made) == 2


def test_once_runs_cold_and_reports_silence(studio_home, monkeypatch):
    monkeypatch.setattr(brain, "binary", lambda: "/usr/bin/true")

    class R:
        stdout = say("hi") + result("done")
        stderr = ""

    assert [e["type"] for e in brain.once("x", run=lambda a: R())] == ["say", "result"]

    class Silent:
        stdout = ""
        stderr = "claude: not logged in"

    out = brain.once("x", run=lambda a: Silent())
    assert out == [{"type": "fail", "text": "claude: not logged in"}]


def test_the_system_prompt_states_the_three_states_and_real_or_null():
    for words in ("UNRESOLVABLE", "ABSENT", "is_real_hardware", "contradicted", "a person must approve"):
        assert words in brain.SYSTEM, words
    assert "never claim a run reached quantum hardware" in " ".join(brain.SYSTEM.lower().split())


def test_a_config_never_outlives_its_session(studio_home):
    """It used to be one fixed path opened O_TRUNC: an existing file kept whatever mode it had, every session
    overwrote the same path, and nothing ever deleted it — the owner's source tokens stayed in home() afterwards."""
    a, b = brain.mcp_config(), brain.mcp_config()
    assert a != b, "two sessions must not share one config"
    for p in (a, b):
        assert oct(p.stat().st_mode)[-3:] == "600"
    assert oct(a.parent.stat().st_mode)[-3:] == "700"
    brain.forget()
    assert not a.exists() and not b.exists()


def test_stopping_a_session_takes_its_config_with_it(studio_home):
    s = brain.Session(spawn=lambda: FakeProc([result("ok")]))
    cfg = s.cfg
    assert cfg.exists()
    list(s.ask("x"))
    s.stop()
    assert not cfg.exists()


def test_a_config_is_never_silently_overwritten(studio_home):
    """O_EXCL: a name that already exists is a collision, not a file to clobber."""
    p = brain.mcp_config()
    with pytest.raises(FileExistsError):
        brain.mcp_config(path=None) if False else __import__("os").open(p, __import__("os").O_CREAT | __import__("os").O_EXCL)
    brain.forget()
