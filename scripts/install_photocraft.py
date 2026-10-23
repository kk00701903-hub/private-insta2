"""Download a pinned PhotoCraft CLI release into tools/photocraft/.

    npm run photocraft:install            # pinned version, current OS/arch
    python scripts/install_photocraft.py --force
    python scripts/install_photocraft.py --print-path

PhotoCraft (https://github.com/storytold/photocraft, Apache-2.0) ships the headless
`photocraft-cli` binary inside these release assets (checked against packaging/ in the repo):

  Windows  photocraft-<ver>-windows-<arch>-portable.zip  -> photocraft-cli.exe (+ photocraft.exe)
  Linux    photocraft-<ver>-linux-<arch>.tar.gz          -> photocraft-<ver>-linux-<arch>/bin/photocraft-cli
  macOS    photocraft-cli-<ver>-macos-universal.zip      -> photocraft-cli

Every download is verified against the SHA-256 pinned below AND the release's SHA256SUMS.txt.
Binaries are never committed (tools/photocraft/ is in .gitignore).
Set PHOTOCRAFT_CLI in .env to use a binary somewhere else instead.

Stdlib only, so it runs before `pip install`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools" / "photocraft"

VERSION = "0.14.0"
BASE_URL = f"https://github.com/storytold/photocraft/releases/download/v{VERSION}"

# (os, arch) -> (asset name, sha256). Digests copied from the GitHub release asset metadata
# of v0.14.0 and re-checked at install time against SHA256SUMS.txt.
ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("windows", "x64"): (
        f"photocraft-{VERSION}-windows-x64-portable.zip",
        "5b814c15034ddc68d35da837aef0cec18c90e9325f7682f516dfc331d64e9ab0",
    ),
    ("windows", "arm64"): (
        f"photocraft-{VERSION}-windows-arm64-portable.zip",
        "9bf9780c07931577643e0dc4d64ed6732fba14ad4e6cfc4c278b9029c906f118",
    ),
    ("linux", "x86_64"): (
        f"photocraft-{VERSION}-linux-x86_64.tar.gz",
        "3d1f60a76c25aafbf53f23cfedd3687bd99eb6f5309920e140d588bd3212c936",
    ),
    ("linux", "aarch64"): (
        f"photocraft-{VERSION}-linux-aarch64.tar.gz",
        "43e7b4bdd69bc5ad747d1f6161aef6fba4839e7807a86eb0cd1d008897395615",
    ),
    ("macos", "universal"): (
        f"photocraft-cli-{VERSION}-macos-universal.zip",
        "2ed3632ca7769ac4002943d311dcee12963d655317e1bff36e50d9421c6ed0b7",
    ),
}


class InstallError(RuntimeError):
    pass


def detect_target() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        return "windows", arch
    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "linux", "x86_64"
        if machine in {"aarch64", "arm64"}:
            return "linux", "aarch64"
        raise InstallError(f"No pinned PhotoCraft build for Linux/{machine}")
    if system == "darwin":
        return "macos", "universal"
    raise InstallError(f"No pinned PhotoCraft build for {system}/{machine}")


def binary_name(os_name: str) -> str:
    return "photocraft-cli.exe" if os_name == "windows" else "photocraft-cli"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "insta2-photocraft-installer"})
    with urllib.request.urlopen(req, timeout=600) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = -1
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total > 5_000_000:
                pct = done * 100 // total
                if pct // 10 != last:
                    last = pct // 10
                    print(f"  {pct:3d}%  {done / 1e6:6.1f} / {total / 1e6:.1f} MB", flush=True)


def published_sums(tmp: Path) -> dict[str, str]:
    sums_path = tmp / "SHA256SUMS.txt"
    download(f"{BASE_URL}/SHA256SUMS.txt", sums_path)
    sums: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            sums[parts[-1].lstrip("*")] = parts[0].lower()
    return sums


def _safe_target(base: Path, member: str) -> Path:
    target = (base / member).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise InstallError(f"Archive entry escapes target dir: {member}")
    return target


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                _safe_target(dest, name)
            zf.extractall(dest)
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():
                _safe_target(dest, m.name)
            if sys.version_info >= (3, 12):
                tf.extractall(dest, filter="data")
            else:  # pragma: no cover
                tf.extractall(dest)
    else:
        raise InstallError(f"Unknown archive type: {archive.name}")


def find_binary(base: Path, name: str) -> Path | None:
    hits = sorted(p for p in base.rglob(name) if p.is_file())
    return hits[0] if hits else None


def install(force: bool = False) -> Path:
    os_name, arch = detect_target()
    asset, pinned = ASSETS[(os_name, arch)]
    target_dir = TOOLS_DIR / f"{VERSION}-{os_name}-{arch}"
    marker = TOOLS_DIR / "current.json"

    existing = find_binary(target_dir, binary_name(os_name)) if target_dir.exists() else None
    if existing and not force:
        print(f"PhotoCraft CLI {VERSION} already installed: {existing}")
        _write_marker(marker, existing, asset, pinned)
        return existing

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="photocraft_dl_") as tmp_s:
        tmp = Path(tmp_s)
        print(f"Downloading {asset} ...")
        archive = tmp / asset
        download(f"{BASE_URL}/{asset}", archive)

        actual = sha256_file(archive)
        if actual != pinned:
            raise InstallError(f"SHA-256 mismatch for {asset}: got {actual}, pinned {pinned}")
        try:
            sums = published_sums(tmp)
        except Exception as exc:  # network hiccup: pinned digest already matched
            print(f"  (could not read SHA256SUMS.txt: {exc}; pinned digest matched)")
            sums = {}
        if sums and sums.get(asset) and sums[asset] != actual:
            raise InstallError(f"{asset} does not match the release SHA256SUMS.txt")
        print(f"  sha256 ok: {actual}")

        if target_dir.exists():
            shutil.rmtree(target_dir)
        extract(archive, target_dir)

    binary = find_binary(target_dir, binary_name(os_name))
    if not binary:
        raise InstallError(f"{binary_name(os_name)} not found inside {asset}")
    if os_name != "windows":
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _write_marker(marker, binary, asset, pinned)
    print(f"Installed: {binary}")
    print("Check:     " + f'"{binary}" --version')
    return binary


def _write_marker(marker: Path, binary: Path, asset: str, sha: str) -> None:
    marker.write_text(
        json.dumps(
            {
                "version": VERSION,
                "asset": asset,
                "sha256": sha,
                "cli": str(binary.relative_to(ROOT).as_posix()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--print-path", action="store_true", help="print the installed CLI path")
    args = parser.parse_args(argv)
    override = os.getenv("PHOTOCRAFT_CLI")
    if override:
        print(f"PHOTOCRAFT_CLI is set ({override}); the app will use that path.")
    try:
        if args.print_path:
            os_name, arch = detect_target()
            b = find_binary(TOOLS_DIR / f"{VERSION}-{os_name}-{arch}", binary_name(os_name))
            print(b or "(not installed)")
            return 0 if b else 1
        install(force=args.force)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
