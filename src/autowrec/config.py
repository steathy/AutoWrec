"""
AutoWrec — Global Configuration

Single source of truth for all paths, model identifiers, and tunables.
Import this module from anywhere in the project:

    from autowrec import config
    workspace = config.WORKSPACE_DIR

Priority chain:  CLI flag  >  ~/.autowrec/config.toml  >  hardcoded default
"""

import tomllib
from pathlib import Path

from dotenv import load_dotenv

VERSION = "1.0.0"

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

# ── Recording tunables ───────────────────────────────────────────────────────
FPS = 3
SEGMENT_PAD_SECONDS = 2
MERGE_GAP_THRESHOLD_SECONDS = 1.5

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

# ── MCP defaults ────────────────────────────────────────────────────────────
MCP_VIDEO_ENABLED = False

VERBOSE = False


# ── Default config.toml content ─────────────────────────────────────────────

_DEFAULT_CONFIG_TOML = """\
# AutoWrec user configuration
#
# Values here override the built-in defaults.

[recording]
# Frames per second for screen capture.
fps                     = 3

# Seconds of padding added around each action clip.
segment_pad             = 2

# Clips closer than this (seconds) are merged into one.
merge_gap_threshold     = 1.5

# Filter out ad/tracker domains from captured network traffic.
# Disable to capture all requests unfiltered.
blocklist_enabled       = true

[agent]
# How long (seconds) a single IPython cell is allowed to run in the sandbox.
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

[mcp]
# Enable video recording in MCP mode (default: false).
# When enabled, screen video is captured alongside network/action data.
# The host AI can request frame extraction via the extract_video_frames tool.
video_enabled = false
"""


# ── TOML loader ─────────────────────────────────────────────────────────────


def _load_config_toml():
    """Read ~/.autowrec/config.toml and apply values to module globals.

    Creates the file with commented defaults on first run.
    Silently skips if the file is missing or unparseable.
    Tolerates unknown sections from older config files.
    """
    global SANDBOX_TIMEOUT_SECONDS
    global FPS, SEGMENT_PAD_SECONDS, MERGE_GAP_THRESHOLD_SECONDS
    global BLOCKLIST_ENABLED
    global BANNER_ENABLED, BANNER_SPEED
    global OUTPUT_DIR, WORKSPACE_DIR, BLOCKLIST_DIR, BLOCKLIST_DB
    global MCP_VIDEO_ENABLED

    if not CONFIG_FILE.exists():
        try:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(_DEFAULT_CONFIG_TOML, encoding="utf-8")
        except OSError:
            pass
        return

    try:
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return

    # [agent] (legacy — only sandbox_timeout is still used)
    agent = data.get("agent", {})
    if "sandbox_timeout" in agent:
        SANDBOX_TIMEOUT_SECONDS = int(agent["sandbox_timeout"])

    # [recording]
    rec = data.get("recording", {})
    if "fps" in rec:
        FPS = int(rec["fps"])
    if "segment_pad" in rec:
        SEGMENT_PAD_SECONDS = float(rec["segment_pad"])
    if "merge_gap_threshold" in rec:
        MERGE_GAP_THRESHOLD_SECONDS = float(rec["merge_gap_threshold"])
    if "blocklist_enabled" in rec:
        BLOCKLIST_ENABLED = bool(rec["blocklist_enabled"])

    # [banner]
    banner = data.get("banner", {})
    if "enabled" in banner:
        BANNER_ENABLED = bool(banner["enabled"])
    if "speed" in banner:
        BANNER_SPEED = float(banner["speed"])

    # [output]
    output = data.get("output", {})
    if "dir" in output:
        OUTPUT_DIR = Path(output["dir"]).resolve()
        WORKSPACE_DIR = OUTPUT_DIR / "workspace"
        BLOCKLIST_DIR = OUTPUT_DIR / "blocklist"
        BLOCKLIST_DB = OUTPUT_DIR / "blocklist.db"

    # [mcp]
    mcp_cfg = data.get("mcp", {})
    if "video_enabled" in mcp_cfg:
        MCP_VIDEO_ENABLED = bool(mcp_cfg["video_enabled"])


_load_config_toml()


def ensure_output_dirs():
    for d in (HOME_DIR, BIN_DIR, LOGS_DIR, HISTORY_DIR, OUTPUT_DIR, WORKSPACE_DIR, BLOCKLIST_DIR):
        d.mkdir(parents=True, exist_ok=True)
