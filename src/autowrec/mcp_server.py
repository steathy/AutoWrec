"""
AutoWrec MCP Server — Exposes browser session recording and exploration
as tools for Claude Code, Codex, and other MCP-compatible AI clients.

No API keys needed. AutoWrec makes zero LLM calls. The host AI tool
provides all intelligence.

Usage:
    autowrec mcp              # start via CLI
    autowrec-mcp              # start via entry point
    python -m autowrec.mcp_server  # direct invocation (fastest)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
from typing import Annotated, Literal


def _build_server():
    """Construct the MCP server with all tools registered.

    Returns (mcp, state, _safe_resolve) for testing.
    Heavy imports (fastmcp, recorder) happen here — the module itself
    only imports stdlib. The recorder import MUST happen at build time,
    not inside tool functions, to avoid import-lock deadlocks when
    FastMCP dispatches tools via thread pool executors.
    """
    from fastmcp import FastMCP
    from .recorder import run_recording as _run_recording

    mcp = FastMCP(
        name="autowrec",
        instructions=(
            "Record a browser session with record_session, poll check_recording, "
            "then explore via read_session_summary / read_timeline / read_transaction. "
            "Use execute_code to prototype against the live site."
        ),
    )

    _state = {"sandbox": None, "workspace": None, "recording_thread": None, "recording_error": None}
    # Guards _get_sandbox so the optional warmup thread (Fix A) and an
    # AI-triggered first execute_code can't both create the AgentSandbox
    # concurrently — that would spawn two worker subprocesses, leak one,
    # and clobber the other.
    _sandbox_lock = threading.Lock()

    def _get_workspace() -> str:
        from . import config

        # If the most recent record_session failed and never produced a
        # workspace, refuse to silently fall back to a previous one.
        err = _state.get("recording_error")
        if err and not _state.get("workspace"):
            raise ValueError(
                f"Last recording failed: {err}. Call record_session again."
            )
        thread = _state.get("recording_thread")
        if thread and thread.is_alive():
            raise ValueError(
                "A recording is in progress. Call check_recording until it finishes."
            )
        if _state["workspace"] and os.path.isdir(_state["workspace"]):
            return _state["workspace"]
        default = str(config.WORKSPACE_DIR / "session_dump")
        if os.path.isdir(default):
            _state["workspace"] = default
            return default
        raise ValueError(
            "No recorded session found. Call record_session first, "
            "or ensure a workspace exists at the default output path."
        )

    def _get_sandbox():
        from . import config

        # Double-checked locking: the fast path avoids the lock cost on every
        # execute_code call, while still serializing creation between the
        # warmup thread and the first AI-triggered call.
        if _state["sandbox"] is None:
            with _sandbox_lock:
                if _state["sandbox"] is None:
                    from .bin_manager import ensure_binaries
                    from .ipython_sandbox import AgentSandbox

                    bin_path = ensure_binaries()
                    workspace = str(config.WORKSPACE_DIR)
                    os.makedirs(workspace, exist_ok=True)
                    _state["sandbox"] = AgentSandbox(
                        working_dir=workspace,
                        timeout_seconds=config.SANDBOX_TIMEOUT_SECONDS,
                        bin_path=str(bin_path),
                    )
        return _state["sandbox"]

    def _safe_resolve(base: str, relative: str) -> str:
        base_resolved = os.path.realpath(base)
        target = os.path.realpath(os.path.join(base, relative))
        if not target.startswith(base_resolved + os.sep) and target != base_resolved:
            raise ValueError(f"Path traversal blocked: {relative!r} escapes workspace")
        return target

    # --- Tool definitions ---

    @mcp.tool()
    def record_session(
        url: Annotated[str, "The starting URL to navigate to"] = "about:blank",
        enable_video: Annotated[bool | None, "Whether to record screen video (default: from config)"] = None,
    ) -> str:
        """Launch Chrome with CDP capture and return immediately. Poll
        check_recording until the user closes the browser, then explore
        the workspace via read_session_summary."""
        from . import config

        if _state["recording_thread"] and _state["recording_thread"].is_alive():
            return "A recording is already in progress. Close the browser to finish it, or call check_recording for status."

        if enable_video is None:
            enable_video = config.MCP_VIDEO_ENABLED

        config.ensure_output_dirs()

        # Invalidate any prior session pointer so a failed recording can't
        # silently surface the previous workspace through read_* tools.
        _state["recording_error"] = None
        _state["workspace"] = None

        def _run_in_background():
            try:
                result = _run_recording(url=url, enable_video=enable_video)
                if result:
                    _state["workspace"] = result
                else:
                    _state["recording_error"] = "Recording failed or produced no output."
            except Exception as exc:
                _state["recording_error"] = str(exc)

        thread = threading.Thread(target=_run_in_background, daemon=True)
        thread.start()
        _state["recording_thread"] = thread

        return (
            f"Recording started. Chrome is launching and navigating to {url}.\n"
            "The user can now browse freely. When done, they should close the browser.\n"
            "Call check_recording to poll status, then use read_session_summary to explore the data."
        )

    @mcp.tool()
    def check_recording() -> str:
        """Report whether a background recording is running, finished, or errored."""
        thread = _state.get("recording_thread")
        if thread is None:
            return "No recording has been started. Call record_session first."
        if thread.is_alive():
            return "Recording is still in progress. The user is browsing. Wait for them to close the browser."
        err = _state.get("recording_error")
        if err:
            return f"Recording finished with an error: {err}"
        ws = _state.get("workspace")
        if ws:
            return f"Recording complete. Workspace ready at: {ws}"
        return "Recording finished but no workspace was produced."

    @mcp.tool()
    def read_session_summary(
        verbose: Annotated[bool, "Return the full SUMMARY.json (default: ~10-line digest)"] = False,
    ) -> str:
        """Digest of the recorded session (counts, top domains, auth presence).
        Pass verbose=true for the full SUMMARY.json."""
        workspace = _get_workspace()
        summary_path = os.path.join(workspace, "SUMMARY.json")
        if not os.path.exists(summary_path):
            raise FileNotFoundError(f"SUMMARY.json not found at {summary_path}")

        if verbose:
            with open(summary_path, encoding="utf-8") as f:
                return f.read()

        # Lean digest mode — extract just the high-signal fields.
        with open(summary_path, encoding="utf-8") as f:
            data = json.load(f)

        sess = data.get("session") or {}
        stats = data.get("statistics") or {}
        domains = stats.get("domains") or {}
        top_domains = sorted(domains.items(), key=lambda kv: -kv[1])[:5]

        digest = {
            "duration_s": sess.get("duration_seconds"),
            "actions": stats.get("total_actions", 0),
            "requests": {
                "total": stats.get("total_requests", 0),
                "actionable": sess.get("actionable_requests", 0),
                "redirected": sess.get("redirected_requests", 0),
                "failed": sess.get("failed_requests", 0),
            },
            "top_domains": [{"domain": d, "count": c} for d, c in top_domains],
            "auth_present": (stats.get("with_auth", 0) or 0) > 0,
            "cookies_present": (stats.get("with_cookies", 0) or 0) > 0,
            "session_flow_count": len(data.get("session_flow") or []),
            "verbose_available": True,
        }
        return json.dumps(digest, separators=(",", ":"))

    def _summarize_event(ev):
        """Compact projection of a timeline event — keeps just the high-signal
        fields the host AI uses to decide what to drill into."""
        ev_type = ev.get("event_type")
        out = {"ts": ev.get("timestamp"), "type": ev_type}
        if ev_type == "network_request":
            out["method"] = ev.get("method")
            out["url"] = ev.get("url")
            out["status"] = ev.get("status")
            folder = ev.get("folder")
            if folder:
                out["folder"] = folder
        else:
            details = ev.get("details") or {}
            out["action"] = ev.get("action")
            label = (
                ev.get("ai_macro_summary")
                or details.get("text")
                or details.get("value")
                or details.get("newUrl")
            )
            if label:
                out["label"] = str(label)[:120]
        return out

    @mcp.tool()
    def read_timeline(
        offset: Annotated[int, "Start index (0-based) for pagination"] = 0,
        limit: Annotated[int, "Maximum number of events to return"] = 100,
        summary: Annotated[
            bool,
            "Compact event projection (default). Set to false for full event payloads.",
        ] = True,
    ) -> str:
        """Paginated time-sorted events (actions + network).
        Compact summary by default; pass summary=false for full event payloads."""
        offset = max(0, offset)
        limit = max(1, min(limit, 1000))
        workspace = _get_workspace()
        timeline_path = os.path.join(workspace, "timeline.json")
        if not os.path.exists(timeline_path):
            raise FileNotFoundError(f"timeline.json not found at {timeline_path}")

        # mtime + size cache so consecutive paginated calls don't re-parse the
        # whole file. Invalidated automatically when the workspace changes.
        st = os.stat(timeline_path)
        cache_key = (timeline_path, st.st_mtime_ns, st.st_size)
        cached = _state.get("_timeline_cache")
        if cached and cached[0] == cache_key:
            events = cached[1]
        else:
            with open(timeline_path, encoding="utf-8") as f:
                events = json.load(f)
            _state["_timeline_cache"] = (cache_key, events)

        total = len(events)
        page = events[offset : offset + limit]
        if summary:
            page = [_summarize_event(e) for e in page]
        return json.dumps(
            {"events": page, "total": total, "has_more": offset + limit < total},
            separators=(",", ":"),
        )

    @mcp.tool()
    def read_transaction(
        request_folder: Annotated[str, "Folder path from timeline, e.g. 'requests/003_GET_api.example.com'"],
        level: Annotated[
            Literal["minimal", "headers", "full"],
            "minimal: method/url/status/timing/has_body. headers: + req+res headers. full: + cookies, detection.",
        ] = "minimal",
    ) -> str:
        """Inspect one HTTP transaction. Bodies are NOT inlined — call read_file on
        request_folder + '/req_payload.<ext>' (extension in content_detection)."""
        workspace = _get_workspace()
        folder_path = _safe_resolve(workspace, request_folder)

        tx_path = os.path.join(folder_path, "transaction.json")
        if not os.path.exists(tx_path):
            raise FileNotFoundError(f"transaction.json not found in {request_folder}")

        with open(tx_path, encoding="utf-8") as f:
            data = json.load(f)

        meta = data.get("metadata") or {}
        req = data.get("request") or {}
        res = data.get("response") or {}

        if level == "minimal":
            timing = meta.get("timing") or {}
            slim = {
                "method": meta.get("method"),
                "url": meta.get("url"),
                "status": meta.get("status"),
                "duration_ms": timing.get("duration_ms"),
                "has_payload": req.get("has_payload", False),
                "has_body": res.get("has_body", False),
                "security": meta.get("security"),
            }
            return json.dumps(slim, separators=(",", ":"))

        if level == "headers":
            slim = {
                "metadata": meta,
                "request": {
                    "headers": req.get("headers"),
                    "has_payload": req.get("has_payload", False),
                },
                "response": {
                    "headers": res.get("headers"),
                    "has_body": res.get("has_body", False),
                },
            }
            return json.dumps(slim, separators=(",", ":"))

        # full: return the entire transaction.json (still no body content)
        return json.dumps(data, separators=(",", ":"))

    @mcp.tool()
    def list_workspace_files(
        subdirectory: Annotated[str, "Subdirectory relative to session_dump (e.g. 'requests')"] = "",
        include_sizes: Annotated[bool, "Include file sizes in the response (off by default)"] = False,
    ) -> str:
        """List entries in the workspace. Hidden (dot-prefix) entries are skipped."""
        workspace = _get_workspace()
        target = _safe_resolve(workspace, subdirectory) if subdirectory else workspace

        if not os.path.isdir(target):
            raise FileNotFoundError(f"Directory not found: {subdirectory or 'session_dump'}")

        entries = []
        for entry in sorted(os.scandir(target), key=lambda e: e.name):
            if entry.name.startswith("."):
                continue
            info = {"name": entry.name, "type": "dir" if entry.is_dir() else "file"}
            if include_sizes and entry.is_file():
                info["size"] = entry.stat().st_size
            entries.append(info)

        return json.dumps(
            {"path": subdirectory or ".", "entries": entries}, separators=(",", ":")
        )

    @mcp.tool()
    def read_file(
        path: Annotated[str, "File path relative to session_dump directory"],
        mode: Annotated[
            Literal["stat", "head", "raw"],
            "stat: just metadata. head: first 1KB only (binary -> hex preview). raw: full read with offset/limit.",
        ] = "head",
        offset: Annotated[int, "Byte offset (raw mode only)"] = 0,
        limit: Annotated[int, "Max bytes to read in raw mode (default 50KB)"] = 50_000,
    ) -> str:
        """Read a workspace file. Default 'head' mode returns the first 1KB.
        Use 'stat' for metadata only, 'raw' with offset/limit for full content."""
        offset = max(0, offset)
        limit = max(1, min(limit, 1_000_000))
        workspace = _get_workspace()
        file_path = _safe_resolve(workspace, path)

        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"File not found: {path}")

        size = os.path.getsize(file_path)

        if mode == "stat":
            return json.dumps(
                {"path": path, "size": size, "ext": os.path.splitext(path)[1].lstrip(".")},
                separators=(",", ":"),
            )

        if mode == "head":
            head_size = min(1024, size)
            with open(file_path, "rb") as f:
                raw = f.read(head_size)
            try:
                content = raw.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                # 64-byte hex preview is much smaller than base64 in tokens.
                preview = raw[:64].hex()
                content = preview + ("..." if len(raw) > 64 else "")
                encoding = "hex-preview"
            payload = {
                "content": content,
                "encoding": encoding,
                "size": size,
                "bytes_read": len(raw),
                "has_more": len(raw) < size,
            }
            # Only nudge the AI toward raw mode when there's actually more to read.
            if payload["has_more"]:
                payload["hint"] = "Call again with mode='raw' for the full file."
            return json.dumps(payload, separators=(",", ":"))

        # raw mode
        with open(file_path, "rb") as f:
            if offset:
                f.seek(offset)
            raw = f.read(limit)

        try:
            content = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(raw).decode("ascii")
            encoding = "base64"

        return json.dumps(
            {
                "content": content,
                "encoding": encoding,
                "size": size,
                "offset": offset,
                "bytes_read": len(raw),
                "has_more": offset + len(raw) < size,
            },
            separators=(",", ":"),
        )

    @mcp.tool()
    def extract_video_frames(
        clip_path: Annotated[str, "Path to video clip relative to session_dump (e.g. 'clips/action_clip_000.mp4')"],
        num_frames: Annotated[int, "Evenly-spaced frames to extract (1-12)"] = 2,
        quality: Annotated[
            Literal["low", "med", "high"],
            "low (480px, ~10KB/frame), med (720px, ~30KB/frame), high (1280px, ~80KB/frame)",
        ] = "low",
    ) -> str:
        """Sample JPEG frames from a video clip as base64 images.
        Defaults are tuned for token efficiency — bump quality only when you need detail."""
        num_frames = max(1, min(num_frames, 12))
        workspace = _get_workspace()
        video_path = _safe_resolve(workspace, clip_path)

        if not os.path.isfile(video_path):
            raise FileNotFoundError(f"Video file not found: {clip_path}")

        import imageio_ffmpeg

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        scale, qv = {
            "low": ("scale=480:-1", "5"),
            "med": ("scale=720:-1", "4"),
            "high": ("scale=1280:-1", "2"),
        }[quality]

        probe_cmd = [
            ffmpeg_exe, "-i", video_path,
            "-f", "null", "-"
        ]
        try:
            probe_result = subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=30
            )
            duration = 0.0
            for line in probe_result.stderr.split("\n"):
                if "Duration:" in line:
                    parts = line.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = parts.split(":")
                    duration = float(h) * 3600 + float(m) * 60 + float(s)
                    break
        except Exception:
            duration = 5.0

        if duration <= 0:
            duration = 5.0

        # For very short clips, sampling N timestamps with a fixed end_pad
        # collapses everything to 0 (B2). Fall back to a single mid-clip
        # sample, and otherwise spread evenly inside a small end-pad.
        if duration < 0.5 or num_frames == 1:
            timestamps = [duration / 2]
        else:
            end_pad = min(0.05, duration * 0.02)
            span = max(duration - 2 * end_pad, 0.001)
            timestamps = [
                end_pad + span * (i + 0.5) / num_frames for i in range(num_frames)
            ]

        frames = []
        for t in timestamps:
            cmd = [
                ffmpeg_exe,
                "-ss", str(t),
                "-i", video_path,
                "-vframes", "1",
                "-vf", scale,
                "-q:v", qv,
                "-f", "image2",
                "-c:v", "mjpeg",
                "pipe:1",
            ]
            try:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
                if result.stdout and len(result.stdout) > 100:
                    frames.append(base64.b64encode(result.stdout).decode("ascii"))
            except Exception:
                continue

        return json.dumps(
            {
                "clip": clip_path,
                "duration_seconds": round(duration, 2),
                "quality": quality,
                "frames_extracted": len(frames),
                "frames": [f"data:image/jpeg;base64,{f}" for f in frames],
            },
            separators=(",", ":"),
        )

    @mcp.tool()
    def execute_code(
        code: Annotated[str, "Python/IPython code to execute in the persistent Python environment"],
        timeout: Annotated[int | None, "Override timeout in seconds (default: from config)"] = None,
    ) -> str:
        """Run Python in a persistent IPython env (cwd=workspace root, so use
        e.g. 'session_dump/SUMMARY.json'). State persists. Magics: %reset, %restore,
        %view_output Cell_N. Available: requests, curl_cffi, beautifulsoup4."""
        sandbox = _get_sandbox()
        kwargs = {}
        if timeout is not None:
            kwargs["custom_timeout"] = max(1, timeout)
        return sandbox.execute(code, **kwargs)

    # Expose the sandbox factory via _state so external callers (notably the
    # warmup thread in run_mcp_server) can trigger creation without changing
    # _build_server's return signature, which is consumed by several tests.
    _state["_get_sandbox"] = _get_sandbox

    return mcp, _state, _safe_resolve


def run_mcp_server():
    """Entry point for the MCP server.

    All heavy imports (fastmcp, rich, config) are deferred to here so that
    the module can be loaded with near-zero overhead. This is critical for
    uvx/MCP startup time — Claude Code has a ~10s connection timeout.
    """
    import io
    import sys

    from . import config
    from . import console as console_module
    from rich.console import Console

    # Redirect ALL Rich output to stderr — stdout is the MCP JSON-RPC transport.
    # Wrap stderr in a UTF-8 TextIOWrapper to prevent UnicodeEncodeError from
    # Rich's box-drawing characters on Windows legacy consoles (cp1252).
    stderr_utf8 = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    console_module.console = Console(
        theme=console_module._theme, highlight=False, file=stderr_utf8
    )

    config.ensure_output_dirs()

    mcp, _state, _ = _build_server()

    # Pre-warm the IPython sandbox in a daemon thread (Fix A). Shifts the
    # cold-start cost (binary downloads + multiprocessing spawn + IPython
    # import) into the dead time between MCP connect and the AI's first
    # execute_code call, instead of charging it to that call's 60s budget.
    # MUST be backgrounded — synchronous warmup would block the stdio
    # handshake and trip Claude Code's MCP connect timeout.
    def _prewarm_sandbox():
        try:
            _state["_get_sandbox"]()
        except Exception:
            # Warmup is best-effort — the first execute_code call will
            # retry via _get_sandbox and surface any error there.
            from .console import log_exception
            log_exception()

    threading.Thread(target=_prewarm_sandbox, daemon=True).start()

    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_mcp_server()
