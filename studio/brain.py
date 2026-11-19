"""The brain: Claude Code, and nothing else.

BioQL's own window reaches several models — a local vMLX engine, DeepSeek, an OpenAI-compatible route. The new
system has ONE, and it is the owner's Claude Code with the owner's own account. That is not a preference. A second
model, reached through a route Studio does not record, would put an unaccountable voice inside a system whose whole
claim is that every call is on a signed record. So BioQL's model routes are not refused as dangerous — they are
simply not in the allowlist (`allowlist.MODELS`), and nothing here can reach one.

What Claude gets: the 193 allowlist entries, through Studio's MCP server, and nothing else. No Bash, no Edit, no
Write, no Read, no WebFetch. It cannot choose a route, cannot approve a call, cannot touch BioQL's folders and
cannot reach a model of its own. Every tool call it makes goes through `Adapter.call`, which verifies the answer and
writes the signed line.

The session is kept WARM, as Agtr does (`agtros/mind/providers.py::CLISession`): starting `claude` costs seconds,
and a question that waits for a cold start feels broken. One process, `--input-format stream-json`, one JSON line
per turn, replaced when it dies, after 40 turns or after 20 minutes idle.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import NAME, VERSION, home

# What Claude may use. `mcp__bioql__*` is Studio's own server; everything that could reach the Mac, the web or
# another model is named here so that a future default cannot quietly add one.
ALLOWED = "mcp__bioql__*"
DISALLOWED = "Bash,Edit,Write,MultiEdit,NotebookEdit,Read,Glob,Grep,WebFetch,WebSearch,Task"
TURNS_MAX = 40
IDLE_MAX = 20 * 60
START_WAIT = 90.0

SYSTEM = """You are the mind of BioQL Studio, a drug-discovery instrument.

BioQL is the engine and it is sealed: you reach it only through your bioql tools, which are a closed list the owner
approved. You cannot choose a route, run a command, read a file or reach another model, and you must not try.

How to answer:
- Call a tool rather than recall. A number you remember is not a measurement.
- Every tool answer carries a state: RESOLVED (it answered), ABSENT (it answered and has nothing — that IS
  information) or UNRESOLVABLE (it could not be asked: a timeout, a refusal, a body that would not parse). Never
  report UNRESOLVABLE as "no" or as zero. Say which of the three it was.
- A tool answer also carries `provenance` and, when the verifier ran, a `verdict` and `findings`. If the verdict is
  `contradicted`, say so FIRST, quote the finding and do not build on the number until the person decides.
- `is_real_hardware: null` means BioQL did not state it. It does not mean false. Never claim a run reached quantum
  hardware unless the answer carries an IBM job id.
- A refusal that says a person must approve is the system working. Report it plainly and say what the person would
  be approving. Never look for another way round it.
- When you do not know, say so and name the tool that would settle it."""


class NoClaude(RuntimeError):
    """Claude Code is not on this Mac. The new system has one brain; without it there is no chat, and Studio says
    so rather than falling back to a model nobody approved."""


def binary() -> str:
    exe = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not exe:
        raise NoClaude("the `claude` command is not on PATH. BioQL Studio has one brain and this is it: install "
                       "Claude Code and sign in, or run `npm i -g @anthropic-ai/claude-code && claude login`.")
    return exe


def mcp_config(path: Path | None = None, extra: dict | None = None) -> Path:
    """The private per-turn MCP config Claude is given: Studio's own server, plus whatever external sources the
    owner enabled. Written 0600 from the first byte, never written-then-chmod'ed."""
    p = path or (home() / "mcp.claude.json")
    repo = str(Path(__file__).resolve().parents[1])
    servers = {"bioql": {"command": sys.executable, "args": ["-m", "studio", "mcp"], "cwd": repo,
                         "env": {"BIOQL_STUDIO_HOME": str(home())}}}
    servers.update(extra or {})
    body = json.dumps({"mcpServers": servers}, indent=1) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return p


def command(prompt: str | None, cfg: Path, model: str | None = None, allowed: list[str] | None = None) -> list[str]:
    """The argv. Nothing secret goes on it: Agtr passes MCP env as `-c mcp_servers.X.env={…}` arguments
    (agtrdesktop/engines.py), which `ps` shows to every process on the machine. Studio's env goes in the config
    file, which is 0600."""
    argv = [binary(), "-p",
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(cfg), "--strict-mcp-config",
            "--allowedTools", ",".join(allowed or [ALLOWED]),
            "--disallowedTools", DISALLOWED,
            "--append-system-prompt", SYSTEM]
    if model:
        argv += ["--model", model]
    if prompt is not None:
        argv.append(prompt)
    return argv


def events(line: str) -> list[dict]:
    """One line of Claude Code's stream turned into what the window draws: `say`, `tool`, `result`, `fail`.

    A line that is not JSON is kept as `raw` rather than dropped — a brain that swallows what it did not expect is
    the same defect as a tool that returns a plausible value instead of stopping.
    """
    line = line.strip()
    if not line:
        return []
    try:
        m = json.loads(line)
    except ValueError:
        return [{"type": "raw", "text": line[:2000]}]
    out: list[dict] = []
    kind = m.get("type")
    if kind == "assistant":
        for part in ((m.get("message") or {}).get("content") or []):
            if part.get("type") == "text" and part.get("text"):
                out.append({"type": "say", "text": part["text"]})
            elif part.get("type") == "tool_use":
                out.append({"type": "tool", "name": part.get("name"), "args": part.get("input") or {}})
    elif kind == "user":
        for part in ((m.get("message") or {}).get("content") or []):
            if part.get("type") == "tool_result":
                body = part.get("content")
                if isinstance(body, list):
                    body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                out.append({"type": "tool_result", "text": str(body)[:200_000], "is_error": bool(part.get("is_error"))})
    elif kind == "result":
        if m.get("is_error"):
            out.append({"type": "fail", "text": str(m.get("result") or m.get("error") or "the turn failed")[:2000]})
        else:
            out.append({"type": "result", "text": str(m.get("result") or ""),
                        "cost": m.get("total_cost_usd"), "turns": m.get("num_turns"),
                        "usage": m.get("usage") or {}})
    return out


class Session:
    """One warm `claude` process. `ask()` sends a turn and yields the events as they arrive.

    A cold start costs seconds, so the process is kept. It is replaced when it dies, after TURNS_MAX turns or after
    IDLE_MAX idle — Agtr's own limits, from `agtros/mind/providers.py`.
    """

    def __init__(self, model: str | None = None, cfg: Path | None = None, allowed: list[str] | None = None,
                 spawn=None) -> None:
        self.model, self.allowed = model, allowed
        self.cfg = cfg or mcp_config()
        self.spawn = spawn or self._spawn          # injected in tests; nothing here runs a model in a test
        self.proc: subprocess.Popen | None = None
        self.turns = 0
        self.last = 0.0
        self.lock = threading.RLock()

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            command(None, self.cfg, self.model, self.allowed) + ["--input-format", "stream-json"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env={**os.environ, "DISABLE_AUTOUPDATER": "1"})

    def _fresh(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            return False
        if self.turns >= TURNS_MAX or (self.last and time.time() - self.last > IDLE_MAX):
            self.stop()
            return False
        return True

    def stop(self) -> None:
        p, self.proc = self.proc, None
        self.turns = 0
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:                      # noqa: BLE001 - a brain that will not die must not block a window
                try:
                    p.kill()
                except Exception:                  # noqa: BLE001
                    pass

    def ask(self, text: str, timeout: float = 43200.0):
        """Yield events for one turn. The 12-hour ceiling is the platform's own contract for a heavy pipeline."""
        with self.lock:
            if not self._fresh():
                self.proc = self.spawn()
            p = self.proc
            self.turns += 1
            self.last = time.time()
            msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
            try:
                p.stdin.write(json.dumps(msg) + "\n")
                p.stdin.flush()
            except (BrokenPipeError, OSError) as why:
                self.stop()
                yield {"type": "fail", "text": f"the brain died before the question reached it: {why}"}
                return

            deadline = time.time() + timeout
            while True:
                if time.time() > deadline:
                    self.stop()
                    yield {"type": "fail", "text": f"no answer within {int(timeout)}s"}
                    return
                line = p.stdout.readline()
                if line == "":
                    self.stop()
                    yield {"type": "fail", "text": "the brain stopped without answering"}
                    return
                for e in events(line):
                    yield e
                    if e["type"] in ("result", "fail"):
                        self.last = time.time()
                        return


def once(text: str, model: str | None = None, cfg: Path | None = None, run=None) -> list[dict]:
    """One cold turn, for a recipe or a scheduled job: no warm session to keep alive. `run` is injected in tests."""
    argv = command(text, cfg or mcp_config(), model)
    runner = run or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=43200,
                                              env={**os.environ, "DISABLE_AUTOUPDATER": "1"}))
    r = runner(argv)
    out: list[dict] = []
    for line in (getattr(r, "stdout", "") or "").splitlines():
        out += events(line)
    if not out:
        err = (getattr(r, "stderr", "") or "").strip()[:500]
        out = [{"type": "fail", "text": err or "the brain said nothing"}]
    return out


_SESSION: dict[str, Session] = {}
_SLOCK = threading.RLock()


def session(key: str = "chat", **kw) -> Session:
    with _SLOCK:
        s = _SESSION.get(key)
        if s is None:
            s = _SESSION[key] = Session(**kw)
        return s


def shut_all() -> None:
    with _SLOCK:
        for s in list(_SESSION.values()):
            s.stop()
        _SESSION.clear()
