"""
Recorder sub-package — captures a full browser session (network + actions)
and compiles it into a structured workspace dump for the agent.

Usage:
    from autowrec.recorder import run_recording
    run_recording("https://example.com")
"""

import asyncio
import os
import signal
import urllib.request

from .. import config
from ..console import detail, error, info, log_exception, rule, warn
from .blocklist_db import BlocklistDB
from .browser_agent import BrowserAgent
from .data_compressor import compile_workspace

# Module-level ref so the SIGINT handler can reach it
_browser_agent: BrowserAgent | None = None


def _handle_sigint(signum, frame):
    """Ctrl+C during recording — fast path only. Heavy cleanup in run_recording's finally block."""
    info("Ctrl+C detected. Shutting down recorder...")
    if _browser_agent:
        _browser_agent.stop()


def _init_blocklist() -> BlocklistDB | None:
    """Create (or open) the persistent blocklist DB and ensure all configured
    sources are downloaded and loaded. Returns None if blocklist is disabled."""
    if not config.BLOCKLIST_ENABLED:
        return None

    db = BlocklistDB(db_path=str(config.BLOCKLIST_DB))

    for name, url in config.BLOCKLIST_SOURCES.items():
        hosts_file = config.BLOCKLIST_DIR / f"{name}.txt"

        if not hosts_file.exists():
            info(f"Downloading blocklist '{name}' ...")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AutoWrec"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    hosts_file.write_bytes(resp.read())
                info(f"Saved {hosts_file.name}")
            except Exception as exc:
                warn(f"Failed to download blocklist '{name}': {exc}")
                continue

        count = db.load_file(str(hosts_file), source_name=name, source_url=url)
        detail(f"{name}: {count:,} domains")

    return db


def run_recording(url: str = "about:blank") -> str | bool:
    """Run the full recording pipeline: browser → compile workspace.

    1. Launches Chrome with CDP instrumentation.
    2. User browses freely; Ctrl+C or closing the browser stops the session.
    3. Compiles the captured data into output/workspace/session_dump/.

    Returns:
        Path to the compiled workspace on success, False on failure.
    """
    global _browser_agent

    config.ensure_output_dirs()

    try:
        prev_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _handle_sigint)
    except (OSError, ValueError) as exc:
        warn(f"Could not install SIGINT handler (running in a thread?): {exc}")
        prev_handler = signal.SIG_DFL

    blocklist = _init_blocklist()

    _browser_agent = BrowserAgent(blocklist=blocklist)

    rule("STARTING RECORDER", style="bold cyan")
    info(f"Target URL : {url}")
    if blocklist:
        info(f"Blocklist  : {blocklist.total_enabled_domains()} domains loaded")
    else:
        info("Blocklist  : disabled")
    info("Press Ctrl+C or close the browser to stop recording")
    rule(style="bold cyan")

    session_data = None
    result: str | bool = False

    try:
        session_data = asyncio.run(_browser_agent.run_session(url=url))
    except Exception as exc:
        error(f"Recording session failed: {exc}")
        log_exception()
    finally:
        try:
            signal.signal(signal.SIGINT, prev_handler)
        except (OSError, ValueError):
            pass

        if session_data:
            try:
                success = compile_workspace(session_data=session_data)
            except Exception as exc:
                error(f"Workspace compilation raised unexpectedly: {exc}")
                log_exception()
                success = False

            if success:
                result = os.path.join(str(config.WORKSPACE_DIR), "session_dump")
        else:
            warn("No session data captured. Skipping compilation.")

        blocked = _browser_agent.stats["blocked_by_blocklist"] if _browser_agent else 0
        if blocked:
            info(f"Blocklist filtered {blocked} ad/tracker request(s)")

        if blocklist:
            try:
                blocklist.close()
            except Exception as exc:
                warn(f"Failed to close blocklist DB: {exc}")

        _browser_agent = None

    return result
