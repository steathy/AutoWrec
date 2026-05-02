"""
Automatic binary downloader for the IPython sandbox PATH jail.

Ensures rg, jq, sd (all platforms) and busybox (Windows only) are available.
Checks ~/.autowrec/bin first, then system PATH, then downloads with a Rich
progress display.
"""

import os
import platform
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from . import config
from . import console as _console_mod
from .console import error, info, warn

# ── Platform detection ───────────────────────────────────────────────────────

_ARCH_MAP = {
    "AMD64": "amd64",
    "x86_64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def _detect_platform():
    os_name = {"win32": "windows", "linux": "linux", "darwin": "darwin"}.get(sys.platform)
    arch = _ARCH_MAP.get(platform.machine(), "amd64")
    return os_name, arch


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


# ── Busybox URLs (Windows only) ─────────────────────────────────────────────
# https://frippery.org/busybox/
#   busybox.exe     — 32-bit (works on 64-bit too), 632 334 bytes
#   busybox64.exe   — 64-bit, faster on x64, 717 824 bytes
#   busybox64u.exe  — 64-bit + Unicode, Win10 1903+ / Win11, 701 440 bytes
#   busybox64a.exe  — 64-bit ARM + Unicode, 663 040 bytes

_BUSYBOX_BASE = "https://frippery.org/files/busybox/"

_BUSYBOX_VARIANTS = {
    # (arch, has_unicode_support) → filename
    ("arm64", True): "busybox64a.exe",
    ("arm64", False): "busybox64a.exe",  # ARM only has the Unicode build
    ("amd64", True): "busybox64u.exe",  # Unicode — Win10 1903+ / Win11
    ("amd64", False): "busybox64.exe",  # 64-bit without Unicode
}
_BUSYBOX_FALLBACK = "busybox.exe"  # 32-bit, works everywhere


def _pick_busybox_url(arch: str) -> tuple[str, str]:
    """Return (download_url, local_filename) for the best busybox variant."""
    # Unicode builds need Win10 build 18362 (1903) or later.
    unicode_ok = False
    if sys.platform == "win32":
        ver = sys.getwindowsversion()
        unicode_ok = (ver.major, ver.minor, ver.build) >= (10, 0, 18362)

    filename = _BUSYBOX_VARIANTS.get((arch, unicode_ok), _BUSYBOX_FALLBACK)
    return f"{_BUSYBOX_BASE}{filename}", "busybox.exe"


# ── Tool download URLs ──────────────────────────────────────────────────────

RG_URLS = {
    (
        "windows",
        "amd64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-pc-windows-msvc.zip",
    (
        "linux",
        "amd64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz",
    (
        "linux",
        "arm64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-aarch64-unknown-linux-gnu.tar.gz",
    (
        "darwin",
        "amd64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-apple-darwin.tar.gz",
    (
        "darwin",
        "arm64",
    ): "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-aarch64-apple-darwin.tar.gz",
}

JQ_URLS = {
    ("windows", "amd64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-windows-amd64.exe",
    ("linux", "amd64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64",
    ("linux", "arm64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-arm64",
    ("darwin", "amd64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-macos-amd64",
    ("darwin", "arm64"): "https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-macos-arm64",
}

SD_URLS = {
    ("windows", "amd64"): "https://github.com/chmln/sd/releases/download/v1.0.0/sd-v1.0.0-x86_64-pc-windows-msvc.zip",
    (
        "linux",
        "amd64",
    ): "https://github.com/chmln/sd/releases/download/v1.0.0/sd-v1.0.0-x86_64-unknown-linux-musl.tar.gz",
    (
        "linux",
        "arm64",
    ): "https://github.com/chmln/sd/releases/download/v1.0.0/sd-v1.0.0-aarch64-unknown-linux-musl.tar.gz",
    ("darwin", "amd64"): "https://github.com/chmln/sd/releases/download/v1.0.0/sd-v1.0.0-x86_64-apple-darwin.tar.gz",
    ("darwin", "arm64"): "https://github.com/chmln/sd/releases/download/v1.0.0/sd-v1.0.0-aarch64-apple-darwin.tar.gz",
}


# ── Download helpers ─────────────────────────────────────────────────────────


def _download_file(url: str, dest: Path, label: str | None = None):
    """Download *url* to *dest* with a Rich progress bar."""
    display = label or dest.name

    # Open the connection to get Content-Length.
    req = urllib.request.Request(url, headers={"User-Agent": "AutoWrec/bin-manager"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.fields[label]}"),
            BarColumn(bar_width=30),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            transient=True,
            console=_console_mod.console,
        ) as progress:
            task = progress.add_task("download", total=total or None, label=display)

            with open(dest, "wb") as fp:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fp.write(chunk)
                    progress.advance(task, len(chunk))

    info(f"Downloaded {display} ({dest.stat().st_size:,} bytes)")


def _make_executable(path: Path):
    if sys.platform != "win32":
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _extract_binary_from_archive(archive_path: Path, binary_name: str, dest: Path):
    archive_str = str(archive_path)

    if archive_str.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.namelist():
                if member.endswith(binary_name):
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
                    _make_executable(dest)
                    return True

    elif archive_str.endswith(".tar.gz") or archive_str.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.name.endswith(binary_name):
                    f = tf.extractfile(member)
                    if f:
                        with open(dest, "wb") as dst:
                            dst.write(f.read())
                        _make_executable(dest)
                        return True

    warn(f"Could not find {binary_name} inside {archive_path}")
    return False


# ── Per-tool ensure functions ────────────────────────────────────────────────


def _ensure_busybox(bin_dir: Path, os_name: str, arch: str):
    if os_name != "windows":
        return
    dest = bin_dir / "busybox.exe"
    if dest.exists():
        return
    if shutil.which("busybox"):
        return
    url, _ = _pick_busybox_url(arch)
    _download_file(url, dest, label="busybox")


def _ensure_rg(bin_dir: Path, os_name: str, arch: str):
    dest = bin_dir / _exe("rg")
    if dest.exists():
        return
    if shutil.which("rg"):
        return
    url = RG_URLS.get((os_name, arch))
    if not url:
        warn(f"No ripgrep download available for {os_name}/{arch}")
        return
    tmp = bin_dir / os.path.basename(url)
    _download_file(url, tmp, label="ripgrep")
    _extract_binary_from_archive(tmp, _exe("rg"), dest)
    tmp.unlink(missing_ok=True)


def _ensure_jq(bin_dir: Path, os_name: str, arch: str):
    dest = bin_dir / _exe("jq")
    if dest.exists():
        return
    if shutil.which("jq"):
        return
    url = JQ_URLS.get((os_name, arch))
    if not url:
        warn(f"No jq download available for {os_name}/{arch}")
        return
    _download_file(url, dest, label="jq")
    _make_executable(dest)


def _ensure_sd(bin_dir: Path, os_name: str, arch: str):
    dest = bin_dir / _exe("sd")
    if dest.exists():
        return
    if shutil.which("sd"):
        return
    url = SD_URLS.get((os_name, arch))
    if not url:
        warn(f"No sd download available for {os_name}/{arch}")
        return
    tmp = bin_dir / os.path.basename(url)
    _download_file(url, tmp, label="sd")
    _extract_binary_from_archive(tmp, _exe("sd"), dest)
    tmp.unlink(missing_ok=True)


# ── Public API ───────────────────────────────────────────────────────────────


def ensure_binaries() -> Path:
    """Check and download all required binaries. Returns the bin directory path."""
    bin_dir = config.BIN_DIR
    bin_dir.mkdir(parents=True, exist_ok=True)

    os_name, arch = _detect_platform()

    _ensure_busybox(bin_dir, os_name, arch)
    _ensure_rg(bin_dir, os_name, arch)
    _ensure_jq(bin_dir, os_name, arch)
    _ensure_sd(bin_dir, os_name, arch)

    if os_name == "windows":
        bb = bin_dir / "busybox.exe"
        if not bb.exists() and not shutil.which("busybox"):
            error("busybox is required on Windows but was not found.")
            error("Download manually from https://frippery.org/busybox/")
            error("Place busybox.exe in: " + str(bin_dir))
            sys.exit(1)

    # Only report missing tools; stay silent when everything is fine.
    tools = ["busybox", "rg", "jq", "sd"] if os_name == "windows" else ["rg", "jq", "sd"]
    missing = [t for t in tools if not (bin_dir / _exe(t)).exists() and not shutil.which(t)]
    if missing:
        warn(f"Missing binaries: {', '.join(missing)}")
    else:
        info("Sandbox binaries ready.")

    return bin_dir
