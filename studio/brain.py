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

import atexit
import collections
import json
import os
import queue
import re
import secrets
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
- When the next calls do not need each other's answers, send them as ONE `run_many` instead of one after another:
  four independent questions are one wait, not four. A call that needs an earlier answer goes in a later batch.
  Each call in a batch keeps its own gate, state, verdict and line on the record.
- A refusal that says a person must approve is the system working. Report it plainly and say what the person would
  be approving. Never look for another way round it.
- When you do not know, say so and name the tool that would settle it."""


class NoClaude(RuntimeError):
    """Claude Code is not on this Mac. The new system has one brain; without it there is no chat, and Studio says
    so rather than falling back to a model nobody approved."""


# Where Claude Code installs itself, searched when PATH does not have it. This is not a convenience: an app opened
# from the Finder is started by LaunchServices, whose PATH is `/usr/bin:/bin:/usr/sbin:/sbin` — it does not read the
# shell's profile — and the native shell adds only Homebrew's two folders (Service.swift). The official installer
# puts `claude` in `~/.local/bin` (measured on this Mac, 2026-11-19: a symlink into
# `~/.local/share/claude/versions/`), which is on PATH in a terminal and on no PATH the app has. Without this list
# the chat is dead inside the app while `/api/ping` and the engine's health both read fine, which is the worst shape
# a failure can take.
WHERE_CLAUDE = ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude", "~/.claude/bin/claude")


def binary() -> str:
    exe = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not exe:
        for raw in WHERE_CLAUDE:
            p = Path(raw).expanduser()
            if os.access(p, os.X_OK) and p.is_file():
                return str(p)
    if not exe:
        raise NoClaude("the `claude` command is not on PATH. BioQL Studio has one brain and this is it: install "
                       "Claude Code and sign in, or run `npm i -g @anthropic-ai/claude-code && claude login`.")
    return exe


def mcp_config(path: Path | None = None, extra: dict | None = None) -> Path:
    """The private MCP config Claude is given: Studio's own server, plus whatever external sources the owner
    enabled. Written 0600 from the first byte, never written-then-chmod'ed.

    A NEW file each time, with O_EXCL, and `forget()` deletes it. It used to be one fixed path opened O_TRUNC: a
    file that already existed kept whatever mode it had, every session overwrote the same path, and NOTHING ever
    removed it — so the owner's source tokens sat in `home()` after the session ended. A warm `claude` holds its
    config for the life of the process, so the file lives as long as the session and is deleted with it.
    """
    p = path or (home() / "mcp" / f"claude-{os.getpid()}-{secrets.token_hex(6)}.json")
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    repo = str(Path(__file__).resolve().parents[1])
    servers = {"bioql": {"command": sys.executable, "args": ["-m", "studio", "mcp"], "cwd": repo,
                         "env": {"BIOQL_STUDIO_HOME": str(home())}}}
    servers.update(extra or {})
    body = json.dumps({"mcpServers": servers}, indent=1) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if path is not None else os.O_EXCL)
    fd = os.open(p, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    _MADE.add(p)
    return p


_MADE: set[Path] = set()


def forget(p: Path | None = None) -> None:
    """Delete a config this module wrote. A token-bearing file that outlives its session is a leak, not a cache."""
    for q in ([p] if p else list(_MADE)):
        _MADE.discard(q)
        try:
            q.unlink()
        except OSError:
            pass


NAMED = re.compile(r"^claude-(\d+)-[0-9a-f]+\.json$")


def sweep(folder: Path | None = None) -> int:
    """Delete the configs of processes that are gone, and say how many. Returns to the folder what `forget()` cannot.

    `forget()` runs from `atexit`, and Python does not run `atexit` on a SIGTERM — which is exactly how the native
    shell stops this service (`Service.swift::stop`). Measured on 2026-11-19, after the app's first quit: six files
    in `home()/mcp`, one per service that had ever run, none removed. They carry no secret while `bioql` is the only
    server, but `mcp_config(extra=…)` writes the owner's external sources into the same file, so a config that
    outlives its process is the leak the function above exists to prevent.

    A file is removed only when its pid is gone: a second copy of Studio must never delete the live config of the
    first, because a warm `claude` holds its config open for the life of the process.
    """
    gone = 0
    for p in (folder or (home() / "mcp")).glob("claude-*.json"):
        m = NAMED.match(p.name)
        if not m or int(m.group(1)) == os.getpid():
            continue
        try:
            os.kill(int(m.group(1)), 0)
        except ProcessLookupError:
            pass                     # nobody there: the config is an orphan
        except OSError:
            continue                 # alive and not ours to signal
        else:
            continue                 # alive
        try:
            p.unlink()
            gone += 1
        except OSError:
            pass
    return gone


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
        self.said = collections.deque(maxlen=60)   # the brain's own stderr, kept so a death can say why
        self.lock = threading.RLock()

    def _spawn(self) -> subprocess.Popen:
        # The config holds the owner's source tokens, so `stop()` deletes it — and a session stops on EVERY
        # rotation: after 40 turns, after 20 minutes idle, and on any failure. The next spawn then pointed
        # `--mcp-config` at a file that was no longer there, `claude` exited at once, and the window said "the
        # brain stopped without answering" for every turn after the first rotation. A session that is starting
        # writes itself a config.
        if not self.cfg.exists():
            self.cfg = mcp_config()
        p = subprocess.Popen(
            command(None, self.cfg, self.model, self.allowed) + ["--input-format", "stream-json"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env={**os.environ, "DISABLE_AUTOUPDATER": "1"})
        # Drain stderr: nothing reads this pipe, so a brain that writes more than the pipe holds (64 KB on macOS)
        # blocks on its own error message for ever. Kept so a death can SAY something instead of "it stopped".
        self.said.clear()
        if p.stderr is not None:
            threading.Thread(target=self._drain, args=(p,), name="brain-stderr", daemon=True).start()
        return p

    def _drain(self, p: subprocess.Popen) -> None:
        try:
            for line in p.stderr:                  # ends on its own when the process does
                self.said.append(line.rstrip()[:400])
        except (ValueError, OSError):              # the pipe was closed under us: nothing left to say
            pass

    def why(self) -> str:
        """The last thing the brain said on stderr, for a failure message. Empty when it said nothing."""
        tail = " / ".join(x for x in list(self.said)[-6:] if x)
        return (": " + tail[:600]) if tail else ""

    def _fresh(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            return False
        if self.turns >= TURNS_MAX or (self.last and time.time() - self.last > IDLE_MAX):
            self.stop()
            return False
        return True

    def stop(self) -> None:
        if self.cfg is not None:
            forget(self.cfg)
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
                yield {"type": "fail", "text": f"the brain died before the question reached it: {why}{self.why()}"}
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
                    yield {"type": "fail", "text": "the brain stopped without answering" + (self.why() or
                                   " and said nothing on its error channel")}
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
    forget()


atexit.register(shut_all)   # a config that outlives the process is the leak this exists to stop
