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
from typing import Annotated


def _build_server():
    """Construct the MCP server with all tools registered.

    Returns (mcp, state, _safe_resolve) for testing.
    Heavy imports (fastmcp, rich) happen here — the module itself
    only imports stdlib.
    """
    from fastmcp import FastMCP

    mcp = FastMCP(
        name="autowrec",
        instructions=(
            "AutoWrec records browser sessions and lets you explore the captured "
            "data (network requests, user actions, video). Use record_session to "
            "capture a session, then explore the workspace with read_* tools. "
            "Use execute_code to run Python in a persistent sandbox for building "
            "automation scripts. No API keys are needed — you are the AI."
        ),
    )

    _state = {"sandbox": None, "workspace": None}

    def _get_workspace() -> str:
        from . import config

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
        enable_video: Annotated[bool, "Whether to record screen video"] = False,
    ) -> str:
        """Record a browser session. Launches Chrome with CDP instrumentation.
        The user browses freely and closes the browser or presses Ctrl+C to stop.
        Network requests, user actions (clicks, typing, navigation), and optionally
        screen video are captured and compiled into a structured workspace.

        AI analysis is NOT performed — you (the host AI) analyze the data yourself.

        Returns the absolute path to the compiled session_dump directory.
        """
        from . import config
        from .recorder import run_recording

        config.ensure_output_dirs()

        result = run_recording(
            url=url,
            enable_video=enable_video,
        )

        if not result:
            raise RuntimeError("Recording failed or produced no output.")

        _state["workspace"] = result
        return f"Recording complete. Workspace: {result}"

    @mcp.tool()
    def read_session_summary() -> str:
        """Read the SUMMARY.json from the recorded session.
        Contains: session metadata, session_flow (chronological summaries of user
        actions with timestamps), and statistics (request counts, domain breakdown,
        status codes, auth/cookie counts).

        Recommended first tool to call after recording a session.
        """
        workspace = _get_workspace()
        summary_path = os.path.join(workspace, "SUMMARY.json")
        if not os.path.exists(summary_path):
            raise FileNotFoundError(f"SUMMARY.json not found at {summary_path}")
        with open(summary_path, encoding="utf-8") as f:
            return f.read()

    @mcp.tool()
    def read_timeline(
        offset: Annotated[int, "Start index (0-based) for pagination"] = 0,
        limit: Annotated[int, "Maximum number of events to return"] = 100,
    ) -> str:
        """Read the timeline.json from the recorded session.
        Contains time-sorted interleaved events: user_action (clicks, keypresses,
        page navigations) and network_request (method, url, status, folder ref).
        Use offset/limit for large timelines.
        """
        workspace = _get_workspace()
        timeline_path = os.path.join(workspace, "timeline.json")
        if not os.path.exists(timeline_path):
            raise FileNotFoundError(f"timeline.json not found at {timeline_path}")
        with open(timeline_path, encoding="utf-8") as f:
            events = json.load(f)
        total = len(events)
        sliced = events[offset : offset + limit]
        return json.dumps({"events": sliced, "total": total, "has_more": offset + limit < total}, indent=2)

    @mcp.tool()
    def read_transaction(
        request_folder: Annotated[str, "Folder path from timeline, e.g. 'requests/003_GET_api.example.com'"],
        include_request_body: Annotated[bool, "Include the request payload content"] = False,
        include_response_body: Annotated[bool, "Include the response body content"] = False,
    ) -> str:
        """Read a specific HTTP transaction from the session dump.
        Returns transaction.json (metadata, headers, cookies, timing, security flags).
        Optionally includes request payload and response body content.
        Bodies capped at 100KB — use read_file for full content with pagination.
        """
        workspace = _get_workspace()
        folder_path = _safe_resolve(workspace, request_folder)

        tx_path = os.path.join(folder_path, "transaction.json")
        if not os.path.exists(tx_path):
            raise FileNotFoundError(f"transaction.json not found in {request_folder}")

        with open(tx_path, encoding="utf-8") as f:
            result = json.load(f)

        max_body_size = 100_000

        if include_request_body:
            req_files = [f for f in os.listdir(folder_path) if f.startswith("req_payload")]
            if req_files:
                req_path = os.path.join(folder_path, req_files[0])
                size = os.path.getsize(req_path)
                try:
                    with open(req_path, encoding="utf-8", errors="replace") as f:
                        content = f.read(max_body_size)
                    result["request_body"] = content
                    result["request_body_truncated"] = size > max_body_size
                except Exception:
                    with open(req_path, "rb") as f:
                        raw = f.read(max_body_size)
                    result["request_body"] = base64.b64encode(raw).decode("ascii")
                    result["request_body_encoding"] = "base64"
                    result["request_body_truncated"] = size > max_body_size

        if include_response_body:
            res_files = [f for f in os.listdir(folder_path) if f.startswith("res_body")]
            if res_files:
                res_path = os.path.join(folder_path, res_files[0])
                size = os.path.getsize(res_path)
                try:
                    with open(res_path, encoding="utf-8", errors="replace") as f:
                        content = f.read(max_body_size)
                    result["response_body"] = content
                    result["response_body_truncated"] = size > max_body_size
                except Exception:
                    with open(res_path, "rb") as f:
                        raw = f.read(max_body_size)
                    result["response_body"] = base64.b64encode(raw).decode("ascii")
                    result["response_body_encoding"] = "base64"
                    result["response_body_truncated"] = size > max_body_size

        return json.dumps(result, indent=2)

    @mcp.tool()
    def list_workspace_files(
        subdirectory: Annotated[str, "Subdirectory relative to session_dump (e.g. 'requests')"] = "",
    ) -> str:
        """List files and directories in the session workspace.
        With no argument, lists top-level session_dump contents.
        """
        workspace = _get_workspace()
        target = _safe_resolve(workspace, subdirectory) if subdirectory else workspace

        if not os.path.isdir(target):
            raise FileNotFoundError(f"Directory not found: {subdirectory or 'session_dump'}")

        entries = []
        for entry in sorted(os.scandir(target), key=lambda e: e.name):
            info = {"name": entry.name, "type": "dir" if entry.is_dir() else "file"}
            if entry.is_file():
                info["size"] = entry.stat().st_size
            entries.append(info)

        return json.dumps({"path": subdirectory or ".", "entries": entries}, indent=2)

    @mcp.tool()
    def read_file(
        path: Annotated[str, "File path relative to session_dump directory"],
        offset: Annotated[int, "Byte offset to start reading from"] = 0,
        limit: Annotated[int, "Maximum bytes to read (default 50KB)"] = 50_000,
    ) -> str:
        """Read any file from the session workspace by relative path.
        Text files returned as UTF-8, binary as base64. Use offset/limit for large files.
        """
        workspace = _get_workspace()
        file_path = _safe_resolve(workspace, path)

        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"File not found: {path}")

        size = os.path.getsize(file_path)

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
            indent=2,
        )

    @mcp.tool()
    def extract_video_frames(
        clip_path: Annotated[str, "Path to video clip relative to session_dump (e.g. 'clips/action_clip_000.mp4')"],
        num_frames: Annotated[int, "Number of evenly-spaced frames to extract"] = 4,
    ) -> str:
        """Extract JPEG frames from a video clip as base64-encoded images.
        Use this to visually inspect what happened during a recorded action.
        The frames are evenly spaced across the clip duration.
        """
        workspace = _get_workspace()
        video_path = _safe_resolve(workspace, clip_path)

        if not os.path.isfile(video_path):
            raise FileNotFoundError(f"Video file not found: {clip_path}")

        import imageio_ffmpeg

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

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

        step = duration / num_frames
        timestamps = [max(0, min(duration - 0.1, step * i + step / 2)) for i in range(num_frames)]

        frames = []
        for t in timestamps:
            cmd = [
                ffmpeg_exe,
                "-ss", str(t),
                "-i", video_path,
                "-vframes", "1",
                "-vf", "scale=1280:-1",
                "-q:v", "2",
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
                "frames_extracted": len(frames),
                "frames": [f"data:image/jpeg;base64,{f}" for f in frames],
            },
            indent=2,
        )

    @mcp.tool()
    def execute_code(
        code: Annotated[str, "Python/IPython code to execute in the persistent sandbox"],
        timeout: Annotated[int | None, "Override timeout in seconds (default: from config)"] = None,
    ) -> str:
        """Execute Python code in a persistent IPython sandbox.
        State persists across calls (variables, imports, session history).
        Supports: magic commands, !shell commands (rg, jq, grep, ls, cat, etc.),
        %reset (wipe state), %restore (replay history), %view_output Cell_N.

        Available libraries: requests, curl_cffi, beautifulsoup4, json, re, os, etc.
        Working directory is the workspace root — use relative paths like
        'session_dump/SUMMARY.json' to access recorded data.
        """
        sandbox = _get_sandbox()
        kwargs = {}
        if timeout is not None:
            kwargs["custom_timeout"] = timeout
        return sandbox.execute(code, **kwargs)

    return mcp, _state, _safe_resolve


def run_mcp_server():
    """Entry point for the MCP server.

    All heavy imports (fastmcp, rich, config) are deferred to here so that
    the module can be loaded with near-zero overhead. This is critical for
    uvx/MCP startup time — Claude Code has a ~10s connection timeout.
    """
    import sys

    from . import config
    from . import console as console_module
    from rich.console import Console

    # Redirect ALL Rich output to stderr — stdout is the MCP JSON-RPC transport
    console_module.console = Console(
        theme=console_module._theme, highlight=False, file=sys.stderr
    )

    config.ensure_output_dirs()

    mcp, _, _ = _build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_mcp_server()
