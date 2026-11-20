"""Updates: the one thing in Studio that reaches the internet, and the only code path that can replace the app.

That sentence is the reason this file is written the way it is. Everything else here listens on 127.0.0.1 and
talks to BioQL on 127.0.0.1; this module fetches from GitHub and then puts a new `BioQL Studio.app` into
/Applications. If anything in Studio is worth being paranoid about, it is this.

**Why a signature of our own, and not Apple's.** The app is signed ad-hoc. macOS will run an ad-hoc bundle that
this Mac built, and it will equally run an ad-hoc bundle that anybody built: the signature proves nothing about
who made it. Notarizing would fix that and needs an Apple Developer ID, which this project does not have. So the
trust comes from an Ed25519 key of the project's own — the same mechanism that already signs the lab notebook:

  · the PUBLIC half is pinned below, in the source, and so is covered by the app's own SOURCE-SHA256 manifest;
  · the PRIVATE half lives in HOME at 0600 on the machine that publishes, and never travels anywhere;
  · every release carries `latest.json` and a detached signature over its canonical bytes.

What that buys, precisely: somebody who takes over the GitHub account can publish a release, and the app will
REFUSE it, because they cannot sign the manifest. Somebody who can intercept the download cannot swap the .dmg,
because its sha256 is inside the signed manifest. What it does NOT buy: anything at all if the private key leaks.
It is one file; treat it as the key to every Mac running this app.

**What is verified, in order, before a single byte is installed** — each step refuses rather than continues:
  1. the manifest's signature, against the pinned public key;
  2. the version is actually newer than the running one;
  3. the downloaded .dmg's size and sha256, against the signed manifest;
  4. the image mounts, and the bundle inside it has the right identifier and the manifest's version;
  5. `codesign --verify --deep --strict` on that bundle;
  6. the bundle's own SOURCE-SHA256 manifest matches the files it ships.
Only then is it staged, and the swap happens when the app is not running.

**The three states, as everywhere else.** "GitHub says there is nothing newer" is ABSENT and is information. "The
network was not there", "the signature did not verify", "the image would not mount" are UNRESOLVABLE and are NOT
information about whether an update exists. An update that cannot be verified is never installed and never
silently skipped: it is reported.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import home, write_private
from .resolution import Resolution, absent, resolved, unresolvable

# Where releases are published. A public repository on purpose: reading a private repo's assets needs a token, and
# that token would have to travel inside the .app, where anybody holding the .dmg could read it.
OWNER = "Bionicsdemo"
REPO = "bioql-studio"
API = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"

# The public half of the release key. Its private half is in HOME at 0600 on the publishing Mac. Changing this line
# changes the app's SOURCE-SHA256 manifest, which is the point: the key a build trusts is part of what that build
# IS, and `shasum -c` over the bundle will show it.
PUBLIC_KEY = "eef7ad034efb64a9c122ae8735d2b1524ff77e4fc74d11d50acf1da02b20ee60"

KEY_NAME = "release.key"
MANIFEST = "latest.json"
SIGNATURE = "latest.json.sig"
BUNDLE_ID = "com.molevera.bioqlstudio"
TARGET = Path("/Applications/BioQL Studio.app")

MAX_MANIFEST = 64 * 1024            # a manifest is a few hundred bytes; anything near this is not one
MAX_DMG = 256 * 1024 * 1024         # the image is ~6 MB; this is a ceiling, not an expectation
TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 600.0
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")


# ---------------------------------------------------------------------------------------------- the key
def _ed():
    import importlib
    try:
        return importlib.import_module("cryptography.hazmat.primitives.asymmetric.ed25519")
    except ImportError as why:      # an unverifiable update is not installed, and the cause is said
        raise NoCrypto(f"checking an update's signature needs Ed25519 from `cryptography`: {why}") from why


class NoCrypto(RuntimeError):
    """Ed25519 is not available, so nothing can be verified. Never worked around: an unverified update is the one
    thing this module exists to refuse."""


def key_path() -> Path:
    return home() / KEY_NAME


def private_key(create: bool = False):
    """The publishing key. Only the machine that publishes has it; the app never calls this."""
    ed = _ed()
    p = key_path()
    if p.exists():
        raw = binascii.unhexlify(p.read_text().strip().split()[0])
        if len(raw) != 32:
            raise ValueError(f"{p} holds {len(raw)} bytes, not 32; it is not being replaced — move it aside by hand")
        return ed.Ed25519PrivateKey.from_private_bytes(raw)
    if not create:
        raise FileNotFoundError(f"no release key at {p}: make one with `python3 -m studio release key`")
    sk = ed.Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization
    raw = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    write_private(p, binascii.hexlify(raw).decode() + "\n")
    return sk


def public_hex(sk=None) -> str:
    from cryptography.hazmat.primitives import serialization
    sk = sk or private_key()
    return binascii.hexlify(sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def canon(manifest: dict) -> bytes:
    """What is signed: the manifest's canonical bytes. Sorted keys and no spaces, so the same facts always produce
    the same bytes to sign and to check — a signature over a formatting choice is a signature over nothing."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(manifest: dict) -> str:
    return binascii.hexlify(private_key().sign(canon(manifest))).decode()


def verify(manifest: dict, signature: str, public: str | None = None) -> bool:
    """True only if this signature was made over THESE bytes with the pinned key. Any failure is False, and the
    caller treats False as "do not install", never as "probably fine"."""
    ed = _ed()
    try:
        pk = ed.Ed25519PublicKey.from_public_bytes(binascii.unhexlify(public or PUBLIC_KEY))
        pk.verify(binascii.unhexlify(signature.strip()), canon(manifest))
        return True
    except Exception:               # noqa: BLE001 - every failure means the same thing here: do not install
        return False


# ---------------------------------------------------------------------------------------------- versions
def newer(there: str, here: str) -> bool:
    """Whether `there` is a later version than `here`. Both must look like a version; anything else is not newer,
    because "I cannot read this version" must never become "install it"."""
    if not (VERSION_RE.match(there or "") and VERSION_RE.match(here or "")):
        return False
    a = [int(x) for x in there.split(".")]
    b = [int(x) for x in here.split(".")]
    n = max(len(a), len(b))
    return a + [0] * (n - len(a)) > b + [0] * (n - len(b))


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- publishing
def manifest_for(dmg: Path, version: str, notes: str = "") -> dict:
    """The facts a release is: which version, which bytes. Built from the file itself, never typed."""
    dmg = Path(dmg)
    return {"version": str(version), "sha256": sha256_of(dmg), "size": dmg.stat().st_size,
            "asset": dmg.name, "published": int(time.time()), "notes": str(notes)[:2000]}


# ---------------------------------------------------------------------------------------------- asking
def _get(url: str, limit: int, timeout: float) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # A generic client name. The user's name, machine or project never goes in a header to a third party.
    req = urllib.request.Request(url, headers={"User-Agent": "bioql-studio", "Accept": "application/vnd.github+json"})
    with opener.open(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url} returned more than {limit} bytes")
    return data


def latest(current: str) -> Resolution:
    """What GitHub publishes, verified. RESOLVED carries the manifest only when its signature checks out AND it is
    newer than `current`; ABSENT means there is nothing newer, which is an answer about the channel."""
    try:
        rel = json.loads(_get(API, MAX_MANIFEST * 16, TIMEOUT))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # GitHub answers 404 for "this repo publishes no release" AND for "there is no such repo, or you
            # cannot see it". They are different answers — one is information about the channel, the other is a
            # broken channel — so the repo itself is asked once, here, rather than guessing.
            try:
                _get(f"https://api.github.com/repos/{OWNER}/{REPO}", MAX_MANIFEST * 16, TIMEOUT)
            except urllib.error.HTTPError as e2:
                if e2.code == 404:
                    return unresolvable("github", f"{OWNER}/{REPO} is not there, or is not visible without "
                                                  f"credentials: there is no update channel to ask")
                return unresolvable("github", f"HTTP {e2.code} asking about {OWNER}/{REPO}")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as why:
                return unresolvable("github", f"{type(why).__name__}: {str(why)[:160]}")
            return absent("github", f"{OWNER}/{REPO} is there and has published no release yet")
        return unresolvable("github", f"HTTP {e.code} asking {API}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as why:
        return unresolvable("github", f"{type(why).__name__}: {str(why)[:160]}")

    by = {a.get("name"): a.get("browser_download_url") for a in (rel.get("assets") or [])}
    if MANIFEST not in by or SIGNATURE not in by:
        return unresolvable("github", f"the latest release carries no {MANIFEST} and {SIGNATURE}: "
                                      f"it was not published by `python3 -m studio release sign`")
    try:
        man = json.loads(_get(by[MANIFEST], MAX_MANIFEST, TIMEOUT))
        sig = _get(by[SIGNATURE], MAX_MANIFEST, TIMEOUT).decode("ascii", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as why:
        return unresolvable("github", f"{type(why).__name__}: {str(why)[:160]}")

    try:
        ok = verify(man, sig)
    except NoCrypto as why:
        return unresolvable("github", str(why))
    if not ok:
        # Said loudly and never softened: a release that does not verify is either a mistake in publishing or
        # somebody else's release. Both mean the same thing here.
        return unresolvable("github", "the published manifest is NOT signed by this build's release key. "
                                      "Nothing will be installed. Either it was published without signing, or it "
                                      "did not come from the holder of the key.")
    if man.get("asset") not in by:
        return unresolvable("github", f"the signed manifest names {man.get('asset')!r} and the release has no such asset")
    if not newer(str(man.get("version") or ""), current):
        return absent("github", f"{man.get('version')} published, {current} running: nothing newer")
    return resolved({**man, "url": by[man["asset"]]}, "github", f"{OWNER}/{REPO} {man['version']}, signature verified")


# ---------------------------------------------------------------------------------------------- taking it
def download(man: dict, into: Path) -> Resolution:
    """The .dmg, checked against the SIGNED manifest. A file whose bytes do not match is deleted, not kept."""
    into.mkdir(parents=True, exist_ok=True)
    dest = into / f"BioQL-Studio-{man['version']}.dmg"
    size = int(man.get("size") or 0)
    if size <= 0 or size > MAX_DMG:
        return unresolvable("github", f"the manifest says {size} bytes, which this will not download")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(man["url"], headers={"User-Agent": "bioql-studio"})
        with opener.open(req, timeout=DOWNLOAD_TIMEOUT) as r, open(dest, "wb") as f:
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > size:      # never write more than the signed manifest says the file is
                    raise ValueError(f"the download is longer than the signed {size} bytes")
                f.write(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as why:
        dest.unlink(missing_ok=True)
        return unresolvable("github", f"{type(why).__name__}: {str(why)[:160]}")
    if dest.stat().st_size != size:
        dest.unlink(missing_ok=True)
        return unresolvable("github", f"downloaded {dest.stat().st_size} bytes, the signed manifest says {size}")
    got = sha256_of(dest)
    if got != man.get("sha256"):
        dest.unlink(missing_ok=True)
        return unresolvable("github", f"sha256 {got[:16]}… does not match the signed {str(man.get('sha256'))[:16]}…")
    return resolved(str(dest), "github", f"{size} bytes, sha256 matches the signed manifest")


def stage(dmg: Path, version: str, into: Path | None = None) -> Resolution:
    """Mount the image, check the app inside it, and copy it out. Nothing from the image is ever executed.

    The checks are in this order on purpose: identifier, then version, then signature, then the bundle's own source
    manifest. A bundle that fails any of them is not staged, and the mount is always detached.
    """
    work = Path(into or (home() / "updates")) / str(version)
    mnt = Path(tempfile.mkdtemp(prefix="bioql-studio-update-"))
    try:
        r = subprocess.run(["hdiutil", "attach", str(dmg), "-readonly", "-nobrowse", "-noautoopen",
                            "-mountpoint", str(mnt), "-quiet"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return unresolvable("update", f"the image would not mount: {(r.stderr or r.stdout).strip()[:160]}")
    except (OSError, subprocess.SubprocessError) as why:
        return unresolvable("update", f"{type(why).__name__}: {str(why)[:160]}")
    try:
        app = mnt / "BioQL Studio.app"
        if not app.is_dir():
            return unresolvable("update", "the image holds no 'BioQL Studio.app'")
        try:
            info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        except (OSError, ValueError) as why:
            return unresolvable("update", f"the bundle has no readable Info.plist: {why}")
        if info.get("CFBundleIdentifier") != BUNDLE_ID:
            return unresolvable("update", f"the bundle identifies as {info.get('CFBundleIdentifier')!r}, not {BUNDLE_ID!r}")
        if str(info.get("CFBundleShortVersionString")) != str(version):
            return unresolvable("update", f"the bundle says version {info.get('CFBundleShortVersionString')!r} and "
                                          f"the signed manifest says {version!r}")
        r = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return unresolvable("update", f"codesign refuses the bundle in the image: {(r.stderr or '').strip()[:160]}")
        src = app / "Contents" / "Resources" / "bioql-studio"
        man = src / "SOURCE-SHA256.txt"
        if not man.is_file():
            return unresolvable("update", "the bundle carries no SOURCE-SHA256.txt")
        r = subprocess.run(["shasum", "-a", "256", "-c", str(man)], cwd=str(src),
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            bad = [ln for ln in (r.stdout or "").splitlines() if "FAILED" in ln][:3]
            return unresolvable("update", "the bundle's own source manifest does not match its files: "
                                          + "; ".join(bad or ["(no detail)"]))
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        out = work / "BioQL Studio.app"
        r = subprocess.run(["ditto", str(app), str(out)], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return unresolvable("update", f"could not copy the bundle out of the image: {(r.stderr or '').strip()[:160]}")
        return resolved(str(out), "update", f"{version} staged, identifier, version, signature and source manifest all check")
    finally:
        subprocess.run(["hdiutil", "detach", str(mnt), "-quiet"], capture_output=True, timeout=120)
        shutil.rmtree(mnt, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- the swap
# The swap runs AFTER this app has exited, from a process that is not inside the bundle being replaced. Replacing a
# running bundle in place is how you get a half-old, half-new app: the service re-reads its web files from disk on
# every request, so it would serve the new page against the old service.
#
# This script is a fixed string. The two paths are substituted by `swap()` after it has checked both of them, and
# they are quoted; nothing from a manifest, a download or a request ever reaches it.
SWAP = """#!/bin/sh
# Replace BioQL Studio once it has quit, then open it again. Written by studio/release.py; not meant to be run by
# hand. It waits for a pid, and it gives up rather than killing anything.
pid=$1
staged=$2
target=$3
n=0
while kill -0 "$pid" 2>/dev/null; do
  n=$((n+1))
  [ "$n" -gt 120 ] && exit 3      # two minutes: the app did not quit, so nothing is replaced
  sleep 1
done
sleep 1
/usr/bin/ditto "$staged" "$target.new" || exit 4
/bin/rm -rf "$target.old"
/bin/mv "$target" "$target.old" 2>/dev/null
/bin/mv "$target.new" "$target" || { /bin/mv "$target.old" "$target"; exit 5; }
/bin/rm -rf "$target.old"
/usr/bin/open "$target"
"""


def swap(staged: Path, target: Path | None = None, pid: int | None = None) -> Resolution:
    """Arrange for the staged bundle to take the place of the installed one once this process has gone.

    It returns as soon as the waiting process is detached — the replacement has NOT happened yet, and the answer
    says so rather than claiming an install that is still in the future.
    """
    staged, target = Path(staged), Path(target or TARGET)
    if not (staged / "Contents" / "MacOS").is_dir():
        return unresolvable("update", f"{staged} is not an app bundle")
    if not target.parent.is_dir():
        return unresolvable("update", f"{target.parent} is not there")
    if not os.access(target.parent, os.W_OK):
        return absent("update", f"{target.parent} is not writable by this account: install it by hand from the .dmg")
    script = home() / "updates" / "swap.sh"
    write_private(script, SWAP)
    os.chmod(script, 0o700)
    try:
        subprocess.Popen(["/bin/sh", str(script), str(pid or os.getpid()), str(staged), str(target)],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as why:
        return unresolvable("update", f"{type(why).__name__}: {str(why)[:160]}")
    return resolved({"staged": str(staged), "target": str(target), "pid": pid or os.getpid()},
                    "update", "waiting for this app to quit, then it is replaced and opened again")
