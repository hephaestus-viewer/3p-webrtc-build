#!/usr/bin/env python3
"""Build Hephaestus's libwebrtc: shiguredo/webrtc-build at a pinned release, plus our patches.

Reads VERSION (SHIGUREDO_TAG, HEPH_REVISION, TARGET), then:
  1. checks out shiguredo/webrtc-build at SHIGUREDO_TAG in <tools>/webrtc-build (cloning it if needed);
  2. fetches the WebRTC source with shiguredo's run.py, which applies shiguredo's own patches
     (the first fetch downloads ~20 GB and takes over an hour);
  3. puts the patches in patches/ (order: patches/series) on a fresh branch
     heph/build, starting from the last "[shiguredo-patch]" commit;
  4. builds and packages with run.py;
  5. writes <tools>/artifacts/webrtc/webrtc-<tag>-heph<rev>-<platform>.tar.xz plus a .sha256, with a
     heph-build.json inside recording the versions and patch hashes, and copies both to
     <backup>/webrtc when a backup folder is given;
  6. points the Hephaestus viewer's heph-webrtc vcpkg port (indra/vcpkg-overlays/ports/heph-webrtc in the
     viewer checkout) at the new archive, and copies the archive into vcpkg's downloads folder, so local
     builds find it before it is uploaded;
  7. prints the GitHub release the archive must be uploaded to (the port's download URL).

<platform> is Linden Lab's platform name for shiguredo's target (windows_x86_64 -> windows64), as in
secondlife/3p-webrtc-build's packages.

Usage: python heph-build-webrtc.py [--check | --port-only] [--viewer DIR] [--tools-dir DIR] [--backup-dir DIR | --no-backup]
  --check       only report whether shiguredo has a newer version tag (exit code 1 if it does)
  --port-only   only point the heph-webrtc port at the already-built archive
  --viewer      the Hephaestus viewer checkout whose port is updated (default: $HEPH_VIEWER_DIR; the port
                is left alone when neither is set)
  --tools-dir   where the build tree and artifacts live (default: D:\\HephaestusTools, or $HEPH_TOOLS)
  --backup-dir  a folder whose webrtc subfolder gets a copy of the artifacts (default: $HEPH_BACKUP_DIR;
                no copy when neither is set)

The build needs Visual Studio 2026 with its "C++ Clang Compiler for Windows" component (llvm-ar and
llvm-objcopy, since m156), and the Windows SDK named in Chromium's build/vs_toolchain.py, including
Debugging Tools for Windows.
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

REPO = Path(__file__).resolve().parent
SHIGUREDO_URL = "https://github.com/shiguredo-webrtc-build/webrtc-build.git"
TAGS_API = "https://api.github.com/repos/shiguredo-webrtc-build/webrtc-build/git/matching-refs/tags/m"
DEFAULT_TOOLS = Path(os.environ.get("HEPH_TOOLS", r"D:\HephaestusTools"))
DEFAULT_BACKUP = os.environ.get("HEPH_BACKUP_DIR")
DEFAULT_VIEWER = os.environ.get("HEPH_VIEWER_DIR")
RELEASES_URL = "https://github.com/hephaestus-viewer/3p-webrtc-build/releases"
PLATFORMS = {"windows_x86_64": "windows64"}
COMMITTER = ["-c", "user.name=Vulcan", "-c", "user.email=340877607+vulcan-hephaestus@users.noreply.github.com"]


def read_version():
    values = {}
    for line in (REPO / "VERSION").read_text().splitlines():
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
    # Since m156, run.py merges WebRTC's Rust objects into webrtc.lib with llvm-ar and llvm-objcopy from Visual
    # Studio's "C++ Clang Compiler for Windows". It looks in VC\Tools\Llvm\bin, but VS 2026 puts them in
    # VC\Tools\Llvm\x64\bin, so that goes on the end of PATH, where run.py looks next.
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.exists():
        install = subprocess.run([str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
                                 capture_output=True, text=True).stdout.strip()
        llvm_bin = Path(install) / "VC" / "Tools" / "Llvm" / "x64" / "bin"
        if install and (llvm_bin / "llvm-ar.exe").exists():
            env["PATH"] = env["PATH"] + os.pathsep + str(llvm_bin)
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


def update_port(viewer, archive, version, platform):
    # Point the viewer's heph-webrtc vcpkg port at this archive: version-string in vcpkg.json, SHA-512 and
    # platform in portfile.cmake.
    if viewer:
        port = Path(viewer) / "indra" / "vcpkg-overlays" / "ports" / "heph-webrtc"
        if not port.is_dir():
            sys.exit(f"{port} does not exist; --viewer must be a Hephaestus viewer checkout")
        manifest = json.loads((port / "vcpkg.json").read_text())
        manifest["version-string"] = version
        (port / "vcpkg.json").write_text(json.dumps(manifest, indent=2) + "\n")
        portfile = port / "portfile.cmake"
        text = portfile.read_text()
        text = re.sub(r"set\(HEPH_WEBRTC_SHA512 [^)]*\)", f"set(HEPH_WEBRTC_SHA512 {sha512(archive)})", text)
        text = re.sub(r"set\(HEPH_WEBRTC_PLATFORM [^)]*\)", f"set(HEPH_WEBRTC_PLATFORM {platform})", text)
        portfile.write_text(text)
        print(f"updated {port} to {version}")
    else:
        print("no viewer checkout given (--viewer or HEPH_VIEWER_DIR); the heph-webrtc port was not updated")

    # vcpkg takes a download whose SHA-512 matches from its downloads folder instead of fetching the URL.
    downloads = Path(os.environ.get("VCPKG_ROOT", DEFAULT_TOOLS / "vcpkg")) / "downloads"
    if downloads.is_dir():
        shutil.copy2(archive, downloads / archive.name)
        print(f"copied to {downloads}")
    else:
        print(f"warning: no vcpkg downloads folder at {downloads}; local builds will try the release URL")
    print(f"upload {archive.name} to the GitHub release {version} ({RELEASES_URL})")


def sha512(path):
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


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
    platform = PLATFORMS[target]
    if "--check" in args:
        return check_latest(tag)

    tools = Path(args[args.index("--tools-dir") + 1]) if "--tools-dir" in args else DEFAULT_TOOLS
    backup_root = args[args.index("--backup-dir") + 1] if "--backup-dir" in args else DEFAULT_BACKUP
    backup = None if "--no-backup" in args or not backup_root else Path(backup_root) / "webrtc"
    viewer = args[args.index("--viewer") + 1] if "--viewer" in args else DEFAULT_VIEWER
    build_root = tools / "webrtc-build"
    patches = [REPO / "patches" / name
               for name in (REPO / "patches" / "series").read_text().split() if name]
    started = datetime.datetime.now()
    out_dir = tools / "artifacts" / "webrtc"
    name = f"webrtc-{tag}-heph{revision}-{platform}"
    archive = out_dir / f"{name}.tar.xz"

    if "--port-only" in args:
        if not archive.exists():
            sys.exit(f"{archive} does not exist; build it first")
        update_port(viewer, archive, f"{tag}-heph{revision}", platform)
        return 0

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
        "build_repo_commit": git(["rev-parse", "HEAD"], cwd=REPO, capture=True),
        "build_repo_dirty": bool(git(["status", "--porcelain"], cwd=REPO, capture=True)),
        "patches": {p.name: sha256(p) for p in patches},
        "built": started.isoformat(timespec="seconds"),
    }
    (package / "heph-build.json").write_text(json.dumps(info, indent=2) + "\n")

    out_dir.mkdir(parents=True, exist_ok=True)
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
            print(f"warning: could not copy to the backup folder ({e})")

    update_port(viewer, archive, f"{tag}-heph{revision}", platform)

    minutes = (datetime.datetime.now() - started).total_seconds() / 60
    print(f"{archive.name}: {archive.stat().st_size / 2**20:.0f} MiB, sha256 {digest}, {minutes:.0f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
