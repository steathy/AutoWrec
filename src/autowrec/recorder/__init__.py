"""
Recorder sub-package — captures a full browser session (network + video + actions)
and compiles it into a structured workspace dump for the agent.

Usage:
    from autowrec.recorder import run_recording
    run_recording("https://example.com")
"""

import asyncio
import os
import shutil
import signal
import tempfile
import urllib.request

from .. import config
from ..console import detail, error, info, log_exception, rule, warn
from .blocklist_db import BlocklistDB
from .browser_agent import BrowserAgent
from .data_compressor import compile_workspace
from .video_recorder import ActionVideoRecorder

# Module-level refs so the SIGINT handler can reach them
_browser_agent: BrowserAgent | None = None
_video_recorder: ActionVideoRecorder | None = None


def _handle_sigint(signum, frame):
    """Ctrl+C during recording = graceful stop."""
    info("Ctrl+C detected. Shutting down recorder...")
    if _browser_agent:
        _browser_agent.stop()
    if _video_recorder:
        _video_recorder.stop()


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


def run_recording(
    url: str = "about:blank",
    enable_video: bool = True,
) -> str | bool:
    """Run the full recording pipeline: browser → video → compile workspace.

    1. Launches Chrome with CDP instrumentation and (optionally) screen capture.
    2. User browses freely; Ctrl+C or closing the browser stops the session.
    3. Compiles the captured data into output/workspace/session_dump/.

    Returns:
        Path to the compiled workspace on success, False on failure.
    """
    global _browser_agent, _video_recorder

    config.ensure_output_dirs()

    try:
        prev_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _handle_sigint)
    except (OSError, ValueError) as exc:
        warn(f"Could not install SIGINT handler (running in a thread?): {exc}")
        prev_handler = signal.SIG_DFL

    temp_video_path = None
    if enable_video:
        fd, temp_video_path = tempfile.mkstemp(suffix=".mp4", prefix="autowrec_")
        os.close(fd)

    blocklist = _init_blocklist()

    if enable_video:
        _video_recorder = ActionVideoRecorder(fps=config.FPS, output_path=temp_video_path)
    _browser_agent = BrowserAgent(blocklist=blocklist)

    rule("STARTING RECORDER", style="bold cyan")
    info(f"Target URL : {url}")
    if enable_video:
        info(f"Video      : enabled ({config.FPS} FPS)")
    if blocklist:
        info(f"Blocklist  : {blocklist.total_enabled_domains()} domains loaded")
    else:
        info("Blocklist  : disabled")
    info("Press Ctrl+C or close the browser to stop recording")
    rule(style="bold cyan")

    session_data = None
    result: str | bool = False

    def _on_browser_ready(pid):
        if _video_recorder:
            _video_recorder.set_target_pid(pid)

    try:
        if _video_recorder:
            if not _video_recorder.start():
                warn("Video recording unavailable — continuing without video.")
                _video_recorder = None
                if temp_video_path and os.path.exists(temp_video_path):
                    try:
                        os.unlink(temp_video_path)
                    except OSError as exc:
                        warn(f"Could not remove temp video file {temp_video_path}: {exc}")
                    else:
                        temp_video_path = None
                else:
                    temp_video_path = None
        session_data = asyncio.run(_browser_agent.run_session(url=url, on_browser_ready=_on_browser_ready))
    except Exception as exc:
        error(f"Recording session failed: {exc}")
        log_exception()
    finally:
        video_start_unix = None
        if _video_recorder:
            try:
                video_start_unix = _video_recorder.stop()
            except Exception as exc:
                error(f"Failed to stop video recorder: {exc}")
                log_exception()

        try:
            signal.signal(signal.SIGINT, prev_handler)
        except (OSError, ValueError):
            pass

        if session_data:
            try:
                recorded_video = (
                    temp_video_path
                    if video_start_unix and temp_video_path
                    and os.path.exists(temp_video_path)
                    and os.path.getsize(temp_video_path) > 5000
                    else None
                )
            except OSError as exc:
                warn(f"Could not inspect temp video file {temp_video_path}: {exc}")
                recorded_video = None

            try:
                success = compile_workspace(
                    session_data=session_data,
                    full_video_path=recorded_video,
                    video_start_unix=video_start_unix,
                )
            except Exception as exc:
                error(f"Workspace compilation raised unexpectedly: {exc}")
                log_exception()
                success = False

            if success and recorded_video and os.path.exists(recorded_video):
                final_video_path = os.path.join(str(config.WORKSPACE_DIR), "session_dump", "full_record.mp4")
                try:
                    os.makedirs(os.path.dirname(final_video_path), exist_ok=True)
                    shutil.move(recorded_video, final_video_path)
                    info(f"Full recording saved to {final_video_path}")
                except OSError as exc:
                    error(f"Failed to move recording to workspace: {exc}")
                    log_exception()

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

        if temp_video_path and os.path.exists(temp_video_path):
            try:
                os.unlink(temp_video_path)
            except OSError as exc:
                warn(f"Could not remove temp video file {temp_video_path}: {exc}")

        _browser_agent = None
        _video_recorder = None

    return result
