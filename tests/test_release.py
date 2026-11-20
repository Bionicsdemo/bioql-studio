"""The update channel: the one thing that reaches the internet and the only path that can replace the app.

Nothing here touches GitHub. The channel is pointed at a local HTTP server that serves whatever each test wants it
to serve, including the hostile cases — an unsigned manifest, one signed with somebody else's key, one whose bytes
were edited after signing, a .dmg whose contents do not match the hash that was signed. Every one of them must end
with nothing installed and a stated cause.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from studio import release as rel


# ------------------------------------------------------------------ harness
class Serves(ThreadingHTTPServer):
    files: dict = {}


def server(files: dict):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):                                     # noqa: N802
            body = self.server.files.get(self.path)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    s = Serves(("127.0.0.1", 0), H)
    s.files = files
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


@pytest.fixture
def channel(monkeypatch, tmp_path):
    """A release channel on localhost, and a key pair that is NOT the one pinned in the source."""
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path / "home"))
    sk = rel.private_key(create=True)
    pub = rel.public_hex(sk)
    monkeypatch.setattr(rel, "PUBLIC_KEY", pub)               # this build trusts the test's key, and only it
    made = {}

    def start(version="9.9.9", dmg_bytes=b"not really an image", sign_it=True, tamper=None, with_assets=True,
              signer=None):
        dmg = f"BioQL-Studio-{version}.dmg"
        path = tmp_path / dmg
        path.write_bytes(dmg_bytes)
        man = rel.manifest_for(path, version)
        sig = (signer or rel.sign)(man) if sign_it else "00" * 64
        if tamper:
            man = {**man, **tamper}                           # edited AFTER signing
        files = {}
        s = server(files)
        base = f"http://127.0.0.1:{s.server_address[1]}"
        if with_assets:
            files["/" + rel.MANIFEST] = json.dumps(man).encode()
            files["/" + rel.SIGNATURE] = sig.encode()
        files["/" + dmg] = dmg_bytes
        assets = [{"name": n.lstrip("/"), "browser_download_url": base + n} for n in files]
        files["/releases/latest"] = json.dumps({"tag_name": version, "assets": assets}).encode()
        monkeypatch.setattr(rel, "API", base + "/releases/latest")
        made["server"] = s
        made["man"] = man
        made["dmg"] = path
        return made

    yield start
    if "server" in made:
        made["server"].shutdown()
        made["server"].server_close()


# ------------------------------------------------------------------ the signature
def test_a_signature_covers_the_bytes_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path))
    sk = rel.private_key(create=True)
    man = {"version": "1.2.3", "sha256": "ab" * 32, "size": 10, "asset": "x.dmg", "published": 1, "notes": ""}
    sig = rel.sign(man)
    pub = rel.public_hex(sk)
    assert rel.verify(man, sig, pub)
    # the same facts in a different order are the same bytes: the signature is over the canonical form
    assert rel.verify(dict(reversed(list(man.items()))), sig, pub)
    # one byte of the manifest changed, and it is no longer signed
    assert not rel.verify({**man, "sha256": "cd" * 32}, sig, pub)
    assert not rel.verify({**man, "version": "1.2.4"}, sig, pub)
    assert not rel.verify(man, "00" * 64, pub)
    # and another key's signature over the very same bytes does not pass
    other = tmp_path / "other"
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(other))
    sk2 = rel.private_key(create=True)
    assert not rel.verify(man, rel.sign(man), pub)
    assert rel.public_hex(sk2) != pub


def test_the_private_key_is_0600_and_never_regenerated_over_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path))
    sk = rel.private_key(create=True)
    assert oct(rel.key_path().stat().st_mode & 0o777) == "0o600"
    assert rel.public_hex(rel.private_key()) == rel.public_hex(sk)      # same key on the second read
    rel.key_path().write_text("not hex at all\n")
    with pytest.raises(Exception):
        rel.private_key()                                                # never silently replaced


# ------------------------------------------------------------------ versions
@pytest.mark.parametrize("there,here,want", [
    ("0.1.1", "0.1.0", True), ("0.2", "0.1.9", True), ("1.0", "0.9.9", True), ("0.1.0", "0.1.0", False),
    ("0.0.9", "0.1.0", False), ("", "0.1.0", False), ("latest", "0.1.0", False), ("0.1.0", "", False),
    ("9.9.9.9", "9.9.9.8", True), ("0.1.0-beta", "0.1.0", False),
])
def test_only_a_version_that_reads_as_newer_is_newer(there, here, want):
    """A version this cannot parse is NOT newer. "I cannot read this" must never become "install it"."""
    assert rel.newer(there, here) is want


# ------------------------------------------------------------------ asking the channel
def test_a_signed_newer_release_is_offered(channel):
    channel(version="9.9.9")
    res = rel.latest("0.1.0")
    assert res.resolved and res.value["version"] == "9.9.9" and res.value["url"].endswith(".dmg")


def test_an_older_or_equal_release_is_absent_not_an_error(channel):
    channel(version="0.0.1")
    res = rel.latest("0.1.0")
    assert res.absent and "nothing newer" in res.detail


def test_an_unsigned_release_is_refused(channel):
    channel(version="9.9.9", sign_it=False)
    res = rel.latest("0.1.0")
    assert not res.resolved and not res.absent
    assert "NOT signed" in res.detail


def test_a_manifest_edited_after_signing_is_refused(channel):
    """The attack this exists for: the .dmg is swapped and the manifest is pointed at it."""
    channel(version="9.9.9", tamper={"sha256": "ff" * 32})
    res = rel.latest("0.1.0")
    assert not res.resolved and "NOT signed" in res.detail


def test_a_release_signed_by_another_key_is_refused(channel, tmp_path, monkeypatch):
    def stranger(man):
        here = os.environ["BIOQL_STUDIO_HOME"]
        monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path / "stranger"))
        rel.private_key(create=True)                  # the stranger has a key of their own, not this build's
        sig = rel.sign(man)
        monkeypatch.setenv("BIOQL_STUDIO_HOME", here)
        return sig

    channel(version="9.9.9", signer=stranger)
    res = rel.latest("0.1.0")
    assert not res.resolved and "NOT signed" in res.detail


def test_a_release_without_the_manifest_assets_is_refused(channel):
    channel(version="9.9.9", with_assets=False)
    res = rel.latest("0.1.0")
    assert not res.resolved and "release sign" in res.detail


def test_a_channel_that_cannot_be_reached_is_never_read_as_up_to_date(monkeypatch):
    monkeypatch.setattr(rel, "API", "http://127.0.0.1:1/releases/latest")   # nothing listens there
    res = rel.latest("0.1.0")
    assert not res.resolved and not res.absent      # UNRESOLVABLE: it is not "there is nothing newer"


# ------------------------------------------------------------------ taking it
def test_the_download_is_checked_against_the_signed_hash_and_deleted_when_it_does_not_match(channel, tmp_path):
    made = channel(version="9.9.9", dmg_bytes=b"the real bytes")
    res = rel.latest("0.1.0")
    assert res.resolved
    man = dict(res.value)
    into = tmp_path / "into"

    good = rel.download(man, into)
    assert good.resolved and Path(good.value).exists()
    Path(good.value).unlink()

    # the manifest now claims a different hash for the same bytes: nothing is kept
    bad = rel.download({**man, "sha256": "ab" * 32}, into)
    assert not bad.resolved and "does not match the signed" in bad.detail
    assert not list(into.glob("*.dmg")), "a file that failed its hash must not be left on disk"

    # and a claimed size that does not match is refused too
    short = rel.download({**man, "size": 3}, into)
    assert not short.resolved
    assert not list(into.glob("*.dmg"))


def test_an_absurd_size_is_refused_before_a_byte_is_fetched(channel, tmp_path):
    made = channel(version="9.9.9")
    res = rel.latest("0.1.0")
    out = rel.download({**res.value, "size": rel.MAX_DMG + 1}, tmp_path / "into")
    assert not out.resolved and "will not download" in out.detail


# ------------------------------------------------------------------ the swap
def test_the_swap_refuses_anything_that_is_not_an_app_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path / "home"))
    not_an_app = tmp_path / "Thing.app"
    not_an_app.mkdir()
    out = rel.swap(not_an_app, target=tmp_path / "Applications" / "BioQL Studio.app")
    assert not out.resolved and "not an app bundle" in out.detail


def test_the_swap_script_is_fixed_text_and_takes_its_paths_as_arguments():
    """Nothing from a manifest, a download or a request is ever interpolated into the script: a release that could
    put characters in there would be a release that could run a command."""
    # No f-string hole and no .format placeholder. `{` does appear in the script — as a shell group in the
    # fallback that puts the old bundle back — so what is checked is that no `{...}` holds a NAME.
    import re as _re
    assert not _re.search(r"\{\s*[A-Za-z_][A-Za-z_0-9.\[\]'\"]*\s*\}", rel.SWAP)
    assert "$1" in rel.SWAP and "$2" in rel.SWAP and "$3" in rel.SWAP
    assert 'ditto "$staged" "$target.new"' in rel.SWAP
    assert "kill -0" in rel.SWAP and "kill -9" not in rel.SWAP     # it waits; it never kills anything
    src = (Path(__file__).resolve().parents[1] / "studio" / "release.py").read_text()
    body = src.split("def swap(")[1]
    assert "shell=True" not in body


def test_an_unwritable_target_is_absent_with_what_to_do_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOQL_STUDIO_HOME", str(tmp_path / "home"))
    app = tmp_path / "staged" / "BioQL Studio.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    apps = tmp_path / "ro"
    apps.mkdir()
    os.chmod(apps, 0o500)
    try:
        out = rel.swap(app.parent.parent, target=apps / "BioQL Studio.app")
        assert out.absent and "by hand" in out.detail
    finally:
        os.chmod(apps, 0o700)


# ------------------------------------------------------------------ what the channel is
def test_the_channel_is_a_public_repo_and_carries_no_credential():
    """A private repo's assets need a token, and that token would travel inside the .app — anybody holding the
    .dmg would hold read access to the repo."""
    # CODE only. This module's own prose explains at length WHY it carries no token, and a guard that reads its
    # own explanation is a guard that fires on itself — the project has caught that three times in one day before.
    src = _code_only(Path(__file__).resolve().parents[1] / "studio" / "release.py")
    for bad in ("Authorization", "token ", "Bearer ", "GH_TOKEN", "GITHUB_TOKEN"):
        assert bad not in src, f"release.py mentions {bad!r} in CODE"
    assert rel.API.startswith("https://api.github.com/repos/")
    assert len(rel.PUBLIC_KEY) == 64 and int(rel.PUBLIC_KEY, 16) >= 0      # 32 bytes of hex, pinned


def test_the_user_agent_names_no_person_or_project():
    """A request to a third party carries nothing about who is running this."""
    src = (Path(__file__).resolve().parents[1] / "studio" / "release.py").read_text()
    assert '"User-Agent": "bioql-studio"' in src
    for bad in ("heinz", "jgheinz", "@gmail", "Spectrix_BioQL"):
        assert bad.lower() not in src.lower()


def _code_only(path: Path) -> str:
    """The file's code with docstrings and comments removed. Three guards written on 2026-09-18 fired on their own
    explanations; this is how the project stopped writing that bug."""
    import ast
    import io
    import tokenize
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
    out = ast.unparse(tree)
    # `ast.unparse` already drops comments; tokenize is not needed, but the import keeps the intent obvious if
    # somebody later swaps the approach.
    del io, tokenize
    return out
