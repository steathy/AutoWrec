"""
AutoWrec — Global Configuration

Single source of truth for all paths, model identifiers, and tunables.
Import this module from anywhere in the project:

    from autowrec import config
    workspace = config.WORKSPACE_DIR

Priority chain:  CLI flag  >  ~/.autowrec/config.toml  >  hardcoded default
"""

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

VERSION = "1.4.0"

# ── Persistent user-level directory (~/.autowrec/) ──────────────────────────
# Stores binaries, logs, history, and user preferences across sessions.
HOME_DIR = Path.home() / ".autowrec"
BIN_DIR = HOME_DIR / "bin"
LOGS_DIR = HOME_DIR / "logs"
HISTORY_DIR = HOME_DIR / "history"
CONFIG_FILE = HOME_DIR / "config.toml"

# ── Per-project paths (CWD-relative) ────────────────────────────────────────
# .env is loaded from whichever directory the user runs `autowrec` in.
load_dotenv(Path.cwd() / ".env")

OUTPUT_DIR = Path.cwd() / "output"
WORKSPACE_DIR = OUTPUT_DIR / "workspace"

BLOCKLIST_DIR = OUTPUT_DIR / "blocklist"
BLOCKLIST_DB = OUTPUT_DIR / "blocklist.db"

# ── Redaction ───────────────────────────────────────────────────────────────
REDACT_SENSITIVE = False

# ── Blocklist ───────────────────────────────────────────────────────────────
BLOCKLIST_ENABLED = True
BLOCKLIST_SOURCES = {
    "stevenblack": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
    "adaway": "https://adaway.org/hosts.txt",
}

# ── Sandbox ─────────────────────────────────────────────────────────────────
SANDBOX_TIMEOUT_SECONDS = 60

# ── Banner ───────────────────────────────────────────────────────────────────
BANNER_ENABLED = True
BANNER_SPEED = 1.0

VERBOSE = False

# ── Proxy ──────────────────────────────────────────────────────────────────
# Format: http://host:port, http://user:pass@host:port, socks5://host:port
# Priority: --proxy CLI flag > AUTOWREC_PROXY env var > [proxy] config.toml
PROXY_URL: str | None = os.environ.get("AUTOWREC_PROXY")


# ── Default config.toml content ─────────────────────────────────────────────

_DEFAULT_CONFIG_TOML = """\
# AutoWrec user configuration
#
# Values here override the built-in defaults.

[recording]
# Filter out ad/tracker domains from captured network traffic.
# Disable to capture all requests unfiltered.
blocklist_enabled       = true

# Redact sensitive values (passwords, auth headers, cookies) in captures.
# Default: false (full capture for throwaway-account testing).
# Enable when recording with real accounts or sharing workspaces.
redact_sensitive        = false

[agent]
# How long (seconds) a single IPython cell is allowed to run.
sandbox_timeout = 60

[banner]
# Set to false to disable the animated startup banner.
enabled = true

# Animation speed multiplier (2.0 = twice as fast, 0.5 = half speed).
speed   = 1.0

[output]
# Root directory for per-project output (workspace, blocklist).
# Relative paths are resolved from the directory where you run `autowrec`.
# dir = "output"

[proxy]
# HTTP/HTTPS/SOCKS5 proxy for all browser traffic.
# Format: scheme://[user:pass@]host:port
# Examples:
#   url = "http://proxy.corp.example.com:8080"
#   url = "http://user:secret@proxy.example.com:3128"
#   url = "socks5://127.0.0.1:1080"
#
# NOTE: SOCKS5 with username:password auth is NOT supported by Chrome.
# Use IP whitelisting or a local proxy forwarder for authenticated SOCKS5.
#
# Can also be set via AUTOWREC_PROXY environment variable.
# url = ""

"""


# ── TOML loader ─────────────────────────────────────────────────────────────


def _load_config_toml():
    """Read ~/.autowrec/config.toml and apply values to module globals.

    Creates the file with commented defaults on first run.
    Silently skips if the file is missing or unparseable.
    Tolerates unknown sections from older config files.
    """
    global SANDBOX_TIMEOUT_SECONDS, PROXY_URL
    global REDACT_SENSITIVE, BLOCKLIST_ENABLED
    global BANNER_ENABLED, BANNER_SPEED
    global OUTPUT_DIR, WORKSPACE_DIR, BLOCKLIST_DIR, BLOCKLIST_DB

    if not CONFIG_FILE.exists():
        try:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(_DEFAULT_CONFIG_TOML, encoding="utf-8")
        except OSError:
            pass
        return

    import sys

    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        print(f"[WARN] Failed to parse {CONFIG_FILE}: {exc}", file=sys.stderr)
        return

    def _safe_table(data, key):
        val = data.get(key, {})
        if isinstance(val, dict):
            return val
        print(f"[WARN] Config section [{key}] should be a table, got {type(val).__name__}. Ignoring.", file=sys.stderr)
        return {}

    def _safe_int(val, default, name=""):
        try:
            return int(val)
        except (ValueError, TypeError):
            print(f"[WARN] Invalid config value for {name}: {val!r}, using default {default}", file=sys.stderr)
            return default

    def _safe_float(val, default, name=""):
        try:
            return float(val)
        except (ValueError, TypeError):
            print(f"[WARN] Invalid config value for {name}: {val!r}, using default {default}", file=sys.stderr)
            return default

    # [agent] (legacy — only sandbox_timeout is still used)
    agent = _safe_table(data, "agent")
    if "sandbox_timeout" in agent:
        SANDBOX_TIMEOUT_SECONDS = _safe_int(agent["sandbox_timeout"], SANDBOX_TIMEOUT_SECONDS, "agent.sandbox_timeout")

    # [recording]
    rec = _safe_table(data, "recording")
    if "blocklist_enabled" in rec:
        BLOCKLIST_ENABLED = bool(rec["blocklist_enabled"])
    if "redact_sensitive" in rec:
        REDACT_SENSITIVE = bool(rec["redact_sensitive"])

    # [banner]
    banner = _safe_table(data, "banner")
    if "enabled" in banner:
        BANNER_ENABLED = bool(banner["enabled"])
    if "speed" in banner:
        BANNER_SPEED = _safe_float(banner["speed"], BANNER_SPEED, "banner.speed")

    # [output]
    output = _safe_table(data, "output")
    if "dir" in output:
        dir_val = output["dir"]
        if isinstance(dir_val, str):
            stripped = dir_val.strip()
            if stripped:
                try:
                    # Strip before passing to Path so leading/trailing whitespace
                    # doesn't get baked into the resolved path on Windows (where
                    # "   C:\\foo" is treated as a relative path under CWD).
                    OUTPUT_DIR = Path(stripped).resolve()
                    WORKSPACE_DIR = OUTPUT_DIR / "workspace"
                    BLOCKLIST_DIR = OUTPUT_DIR / "blocklist"
                    BLOCKLIST_DB = OUTPUT_DIR / "blocklist.db"
                except Exception:
                    print(f"[WARN] Invalid output.dir path: {dir_val!r}, using default", file=sys.stderr)
            else:
                # Empty / whitespace-only string would resolve to CWD, leaking
                # the user's working directory as the workspace root.
                print("[WARN] output.dir is empty, using default", file=sys.stderr)
        else:
            print(f"[WARN] output.dir must be a string, got {type(dir_val).__name__}: {dir_val!r}. Using default.", file=sys.stderr)

    # [proxy]
    proxy = _safe_table(data, "proxy")
    if "url" in proxy:
        proxy_val = proxy["url"]
        if isinstance(proxy_val, str) and proxy_val.strip():
            PROXY_URL = PROXY_URL or proxy_val.strip()
        elif not isinstance(proxy_val, str):
            print(f"[WARN] proxy.url must be a string, got {type(proxy_val).__name__}. Ignoring.", file=sys.stderr)

    # Range validation
    SANDBOX_TIMEOUT_SECONDS = max(1, SANDBOX_TIMEOUT_SECONDS)
    BANNER_SPEED = max(0.1, BANNER_SPEED)


_load_config_toml()


def ensure_output_dirs():
    for d in (HOME_DIR, BIN_DIR, LOGS_DIR, HISTORY_DIR, OUTPUT_DIR, WORKSPACE_DIR, BLOCKLIST_DIR):
        d.mkdir(parents=True, exist_ok=True)
