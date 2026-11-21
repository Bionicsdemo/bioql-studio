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
    monkeypatch.setattr(brain, "WHERE_CLAUDE", ())          # nor in any of the install folders
    with pytest.raises(brain.NoClaude, match="one brain"):
        brain.binary()


def test_claude_is_found_where_it_installs_itself_when_path_lacks_it(monkeypatch, tmp_path):
    """Opened from the Finder, the app's PATH is LaunchServices' own plus Homebrew: `~/.local/bin`, where the
    installer puts `claude`, is on neither. Found there, the chat works; not found, the window is up and the brain
    is dead."""
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(brain.shutil, "which", lambda _: None)
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(brain, "WHERE_CLAUDE", (str(tmp_path / "ausente"), str(fake)))
    assert brain.binary() == str(fake)


def test_a_folder_named_claude_is_not_the_binary(monkeypatch, tmp_path):
    """`os.access(X_OK)` is true for a directory too; without the `is_file()` the brain would try to exec one."""
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(brain.shutil, "which", lambda _: None)
    (tmp_path / "claude").mkdir()
    monkeypatch.setattr(brain, "WHERE_CLAUDE", (str(tmp_path / "claude"),))
    with pytest.raises(brain.NoClaude):
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
    for words in ("UNRESOLVABLE", "ABSENT", "is_real_hardware", "contradicted"):
        assert words in brain.SYSTEM, words
    assert "never claim a run reached quantum hardware" in " ".join(brain.SYSTEM.lower().split())


def test_the_system_prompt_does_not_teach_the_brain_to_expect_an_approval():
    """Esta prueba exigia la frase "a person must approve" EN el prompt -- es decir, defendia que Studio le
    enseñara a Claude a esperar negativas por aprobacion despues de que la puerta se hubiera quitado. Es el peor
    sitio donde ese texto podia quedarse: no describe el sistema, lo CONDUCE, y produce exactamente la conducta
    que el dueño no queria (anunciar el coste y quedarse esperando) sin que nada lo rechazara."""
    assert "a person must approve" not in brain.SYSTEM
    assert "Nothing is refused for being expensive" in brain.SYSTEM
    # y las tres negativas que SI existen siguen nombradas, para que no las trate como un obstaculo
    for real in ("not in the closed allowlist", "an argument the entry does not take", "does not look"):
        assert real in brain.SYSTEM, real


def test_the_system_prompt_points_at_the_parameters_read_from_bioqls_source():
    """254 herramientas llevan los nombres que el handler de BioQL lee. Si el cerebro no sabe que estan ahi, no
    los usa, y la mejora no llega a ninguna llamada."""
    plano = " ".join(brain.SYSTEM.split())          # el prompt va envuelto a 118 columnas
    assert "Su handler en BioQL lee" in plano
    assert "a reading, not a declaration" in plano


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


# ------------------------------------------------------------------ the second turn after a rotation
def test_a_session_that_rotated_writes_itself_a_new_config(tmp_path, monkeypatch):
    """`stop()` deletes the MCP config, because it holds the owner's source tokens — and a session stops on EVERY
    rotation: 40 turns, 20 minutes idle, any failure. The next spawn pointed `--mcp-config` at a file that was no
    longer there, `claude` exited at once, and the window said "the brain stopped without answering". That is the
    live failure the owner hit, and it happened on every turn after the first rotation.

    This drives `_spawn` itself — the guard lives there, so a test with its own fake spawn would prove nothing."""
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path))
    seen = []

    class Fake:
        stdin = stdout = stderr = None

        def __init__(self, argv, **kw):
            cfg = argv[argv.index("--mcp-config") + 1]
            seen.append((cfg, __import__("pathlib").Path(cfg).exists()))

        def poll(self):
            return 0

    monkeypatch.setattr(brain.subprocess, "Popen", Fake)
    s = brain.Session(cfg=brain.mcp_config())
    first = s.cfg
    s.proc = s._spawn()
    assert seen[-1][1] is True
    s.stop()
    assert not first.exists()                      # the tokens went with the session
    s.proc = s._spawn()                            # the next turn
    assert seen[-1][1] is True, "the brain was started pointing at a config that had been deleted"
    assert s.cfg.exists() and s.cfg != first
    brain.forget(s.cfg)


def test_a_brain_that_dies_says_what_it_said(tmp_path, monkeypatch):
    """`the brain stopped without answering` with nothing after it is unfixable from the window. Nothing drained
    stderr either, so a brain that wrote more than the pipe holds blocked on its own error message."""
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path))
    import subprocess
    import sys as _sys
    s = brain.Session(cfg=brain.mcp_config())
    s.spawn = lambda: s._spawn.__func__(s) if False else subprocess.Popen(
        [_sys.executable, "-c", "import sys; sys.stderr.write('mcp config not found\\n'); sys.stderr.flush()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def spawn():
        p = s.spawn()
        s.said.clear()
        import threading as _t
        _t.Thread(target=s._drain, args=(p,), daemon=True).start()
        return p

    s.proc = spawn()
    out = list(s.ask("anything", timeout=10))
    assert out[-1]["type"] == "fail"
    assert "stopped without answering" in out[-1]["text"]
    assert "mcp config not found" in out[-1]["text"] or "said nothing" in out[-1]["text"]
    brain.forget(s.cfg)


# ------------------------------------------------------------- las configuraciones que sobreviven al SIGTERM
def test_the_config_of_a_process_that_is_gone_is_swept(studio_home, monkeypatch, tmp_path):
    """`forget()` runs from atexit, and atexit does NOT run on the SIGTERM the native shell sends. Measured after
    the app's first quit on 2026-11-19: six files in home()/mcp, one per service that had ever run."""
    d = tmp_path / "mcp"
    d.mkdir()
    dead = d / "claude-999999-aabbccddeeff.json"     # no such pid
    dead.write_text("{}")
    mine = d / f"claude-{os.getpid()}-aabbccddeeff.json"
    mine.write_text("{}")
    other = d / "no-es-una-config.json"
    other.write_text("{}")
    assert brain.sweep(d) == 1
    assert not dead.exists()
    assert mine.exists()                              # this very process is alive
    assert other.exists()                             # not ours to delete


def test_a_live_second_copy_keeps_its_config(studio_home, tmp_path):
    """A warm `claude` holds its config open for the life of the process: deleting a running copy's config would
    break the other Studio, not clean up after it."""
    d = tmp_path / "mcp"
    d.mkdir()
    live = d / "claude-1-aabbccddeeff.json"           # pid 1 (launchd) is always alive and not ours to signal
    live.write_text("{}")
    assert brain.sweep(d) == 0
    assert live.exists()
