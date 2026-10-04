#!/usr/bin/env python3
"""Build Hephaestus's libwebrtc: shiguredo/webrtc-build at a pinned release, plus our patches.

Reads webrtc/VERSION (SHIGUREDO_TAG, HEPH_REVISION, TARGET), then:
  1. checks out shiguredo/webrtc-build at SHIGUREDO_TAG in <tools>/webrtc-build (cloning it if needed);
  2. fetches the WebRTC source with shiguredo's run.py, which applies shiguredo's own patches
     (the first fetch downloads ~20 GB and takes over an hour);
  3. puts the patches in webrtc/patches (order: webrtc/patches/series) on a fresh branch
     heph/build, starting from the last "[shiguredo-patch]" commit;
  4. builds and packages with run.py;
  5. writes <tools>/artifacts/webrtc/webrtc-<tag>-heph<rev>-<target>.tar.xz plus a .sha256, with a
     heph-build.json inside recording the versions and patch hashes, and copies both to the backup
     share when it is reachable.

Usage: python scripts/heph-build-webrtc.py [--check] [--tools-dir DIR] [--backup-dir DIR | --no-backup]
  --check       only report whether shiguredo has a newer version tag (exit code 1 if it does)
  --tools-dir   where the build tree and artifacts live (default: D:\\HephaestusTools, or $HEPH_TOOLS)
  --backup-dir  where artifacts are copied (default: the Second Life Viewer share's artifacts\\webrtc)

The build needs Visual Studio 2026 and the Windows SDK named in Chromium's build/vs_toolchain.py
(10.0.28000.0 for m155), including Debugging Tools for Windows.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEBRTC_DIR = REPO / "webrtc"
SHIGUREDO_URL = "https://github.com/shiguredo-webrtc-build/webrtc-build.git"
TAGS_API = "https://api.github.com/repos/shiguredo-webrtc-build/webrtc-build/git/matching-refs/tags/m"
DEFAULT_TOOLS = Path(os.environ.get("HEPH_TOOLS", r"D:\HephaestusTools"))
DEFAULT_BACKUP = Path(r"\\unraid\data\CLAUDE\Second Life Viewer\artifacts\webrtc")
COMMITTER = ["-c", "user.name=Vulcan", "-c", "user.email=vulcan@noreply.invalid"]


def read_version():
    values = {}
    for line in (WEBRTC_DIR / "VERSION").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def run(args, cwd=None, env=None, capture=False):
    print("+", " ".join(str(a) for a in args), flush=True)
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True,
                            capture_output=capture, text=capture)
    return result.stdout.strip() if capture else None


def git(args, cwd, capture=False):
    return run(["git", *args], cwd=cwd, capture=capture)


def check_latest(tag):
    # shiguredo publishes versions as tags (m<milestone>.<branch>.<patch>.<build>), not GitHub releases.
    request = urllib.request.Request(TAGS_API, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as r:
        refs = json.load(r)
    tags = [ref["ref"].rsplit("/", 1)[1] for ref in refs]
    versions = [t for t in tags if re.fullmatch(r"m\d+(\.\d+){3}", t)]
    latest = max(versions, key=lambda t: tuple(int(x) for x in t[1:].split(".")))
    if latest == tag:
        print(f"webrtc is at shiguredo's newest version, {tag}")
        return 0
    print(f"shiguredo has a newer version: {latest} (we build {tag})")
    return 1


def checkout_shiguredo(build_root, tag):
    if not (build_root / ".git").exists():
        git(["clone", SHIGUREDO_URL, build_root], cwd=build_root.parent)
    if git(["status", "--porcelain", "--untracked-files=no"], cwd=build_root, capture=True):
        sys.exit(f"{build_root} has local changes; refusing to switch it to {tag}")
    current = subprocess.run(["git", "describe", "--tags", "--exact-match"], cwd=build_root,
                             capture_output=True, text=True).stdout.strip()
    if current != tag:
        git(["fetch", "--tags", "origin"], cwd=build_root)
        git(["checkout", "-q", tag], cwd=build_root)


def runpy_env(build_root, target):
    # run.py calls "python3", which on Windows is often the Microsoft Store stub. depot_tools ships a
    # real one; put it first once depot_tools exists (it is fetched by the first run.py build).
    env = dict(os.environ)
    depot_tools = build_root / "_source" / target / "depot_tools"
    reldir = depot_tools / "python3_bin_reldir.txt"
    if reldir.exists():
        env["PATH"] = str(depot_tools / reldir.read_text().strip()) + os.pathsep + env["PATH"]
    return env


def apply_patches(src, patches):
    if git(["status", "--porcelain", "--untracked-files=no"], cwd=src, capture=True):
        sys.exit(f"{src} has uncommitted changes; commit or discard them first")
    base = git(["log", "-1", "--format=%H", "--fixed-strings", "--grep=[shiguredo-patch]"], cwd=src, capture=True)
    if not base:
        sys.exit("no [shiguredo-patch] commit found in the WebRTC source; was it fetched by run.py?")
    git(["checkout", "-q", "-B", "heph/build", base], cwd=src)
    for patch in patches:
        git(["apply", "--whitespace=nowarn", patch], cwd=src)
        git([*COMMITTER, "commit", "-q", "-a", "-m", f"[heph-patch] {patch.name}"], cwd=src)


def remove_tree(path):
    # run.py's package step copies headers with Robocopy, which keeps read-only attributes (some files under
    # third_party are read-only), and its own rm_rf then fails on the next run. Clear the flag and retry.
    def clear_readonly(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onexc=clear_readonly)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    args = sys.argv[1:]
    version = read_version()
    tag, revision, target = version["SHIGUREDO_TAG"], version["HEPH_REVISION"], version["TARGET"]
    if "--check" in args:
        return check_latest(tag)

    tools = Path(args[args.index("--tools-dir") + 1]) if "--tools-dir" in args else DEFAULT_TOOLS
    backup = None if "--no-backup" in args else (
        Path(args[args.index("--backup-dir") + 1]) if "--backup-dir" in args else DEFAULT_BACKUP)
    build_root = tools / "webrtc-build"
    patches = [WEBRTC_DIR / "patches" / name
               for name in (WEBRTC_DIR / "patches" / "series").read_text().split() if name]
    started = datetime.datetime.now()

    tools.mkdir(parents=True, exist_ok=True)
    checkout_shiguredo(build_root, tag)
    runpy = [sys.executable, "run.py"]

    # Get the WebRTC source at the commit shiguredo's VERSION names, with shiguredo's patches applied:
    # a full fetch the first time, and run.py's "fetch" (checkout + gclient sync) after a tag change.
    src = build_root / "_source" / target / "webrtc" / "src"
    if not src.exists():
        run([*runpy, "build", target, "--no-history", "--webrtc-nobuild"], cwd=build_root,
            env=runpy_env(build_root, target))
    else:
        webrtc_commit = next(line.split("=", 1)[1].strip()
                             for line in (build_root / "VERSION").read_text().splitlines()
                             if line.startswith("WEBRTC_COMMIT="))
        at_version = subprocess.run(["git", "merge-base", "--is-ancestor", webrtc_commit, "HEAD"],
                                    cwd=src, capture_output=True).returncode == 0
        if not at_version:
            run([*runpy, "fetch", target], cwd=build_root, env=runpy_env(build_root, target))
    apply_patches(src, patches)

    env = runpy_env(build_root, target)
    run([*runpy, "build", target, "--no-history", "--webrtc-gen"], cwd=build_root, env=env)
    remove_tree(build_root / "_package" / target)
    run([*runpy, "package", target], cwd=build_root, env=env)

    package = build_root / "_package" / target / "webrtc"
    info = {
        "shiguredo_tag": tag,
        "heph_revision": int(revision),
        "target": target,
        "webrtc_commit": git(["rev-parse", "HEAD"], cwd=src, capture=True),
        "hephaestus_commit": git(["rev-parse", "HEAD"], cwd=REPO, capture=True),
        "hephaestus_tree_dirty": bool(git(["status", "--porcelain", "--", "webrtc", "scripts"], cwd=REPO, capture=True)),
        "patches": {p.name: sha256(p) for p in patches},
        "built": started.isoformat(timespec="seconds"),
    }
    (package / "heph-build.json").write_text(json.dumps(info, indent=2) + "\n")

    out_dir = tools / "artifacts" / "webrtc"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"webrtc-{tag}-heph{revision}-{target}"
    archive = out_dir / f"{name}.tar.xz"
    print(f"+ writing {archive}", flush=True)
    with tarfile.open(archive, "w:xz", preset=6) as tar:
        tar.add(package, arcname=name)
    digest = sha256(archive)
    (out_dir / f"{name}.tar.xz.sha256").write_text(f"{digest}  {archive.name}\n")

    if backup:
        try:
            backup.mkdir(parents=True, exist_ok=True)
            for f in (archive, out_dir / f"{name}.tar.xz.sha256"):
                shutil.copy2(f, backup / f.name)
            print(f"copied to {backup}")
        except OSError as e:
            print(f"warning: could not copy to the backup share ({e})")

    minutes = (datetime.datetime.now() - started).total_seconds() / 60
    print(f"{archive.name}: {archive.stat().st_size / 2**20:.0f} MiB, sha256 {digest}, {minutes:.0f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
