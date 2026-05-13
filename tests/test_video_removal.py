"""
Pre-flight & post-flight regression tests for the video removal plan
(remove-video-recording.md → v1.4.0).

Run BEFORE applying changes to get a baseline, then AFTER to confirm
no regressions. Tests are organized into two phases:

  Phase 1 — Pre-flight: Validates assumptions the plan relies on
  Phase 2 — Post-flight: Verifies the plan was executed correctly

Usage:
    python tests/test_video_removal.py          # run all applicable tests
    python tests/test_video_removal.py --pre    # pre-flight only
    python tests/test_video_removal.py --post   # post-flight only
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PASS, FAIL, SKIP = 0, 0, 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{f' — {detail}' if detail else ''}")


def skip(name, reason):
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name} — {reason}")


def section(title):
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


# ---------------------------------------------------------------------------
# Detect which phase to run
# ---------------------------------------------------------------------------

def _is_post_flight() -> bool:
    """Return True if video_recorder.py has already been deleted."""
    path = Path(__file__).resolve().parent.parent / "src" / "autowrec" / "recorder" / "video_recorder.py"
    return not path.exists()


def run_preflight():
    """Validate assumptions the plan relies on — run BEFORE making changes."""

    section("PRE-1: video_recorder.py exists and exports expected symbols")
    try:
        from autowrec.recorder.video_recorder import ActionVideoRecorder, _get_process_tree  # noqa: F401
        check("ActionVideoRecorder importable", True)
        check("_get_process_tree importable", True)
        check("split_video is a static method", hasattr(ActionVideoRecorder, "split_video"))
    except ImportError as e:
        check("video_recorder imports", False, str(e))

    section("PRE-2: config.py has all video constants that will be removed")
    from autowrec import config
    check("FPS exists", hasattr(config, "FPS"))
    check("SEGMENT_PAD_SECONDS exists", hasattr(config, "SEGMENT_PAD_SECONDS"))
    check("MERGE_GAP_THRESHOLD_SECONDS exists", hasattr(config, "MERGE_GAP_THRESHOLD_SECONDS"))
    check("MCP_VIDEO_ENABLED exists", hasattr(config, "MCP_VIDEO_ENABLED"))

    section("PRE-3: run_recording accepts enable_video param")
    from autowrec.recorder import run_recording
    sig = inspect.signature(run_recording)
    check("enable_video in run_recording signature", "enable_video" in sig.parameters)

    section("PRE-4: data_compressor imports ActionVideoRecorder")
    from autowrec.recorder import data_compressor as dc
    dc_src = inspect.getsource(dc)
    check("data_compressor imports ActionVideoRecorder", "from .video_recorder import ActionVideoRecorder" in dc_src)

    section("PRE-5: compile_workspace accepts video params")
    from autowrec.recorder.data_compressor import compile_workspace
    cw_sig = inspect.signature(compile_workspace)
    check("full_video_path in compile_workspace", "full_video_path" in cw_sig.parameters)
    check("video_start_unix in compile_workspace", "video_start_unix" in cw_sig.parameters)

    section("PRE-6: merge_and_annotate_actions is the current function name")
    check("merge_and_annotate_actions exists", hasattr(dc, "merge_and_annotate_actions"))

    section("PRE-7: extract_video_frames MCP tool exists")
    from autowrec.mcp_server import _build_server
    mcp, _, _ = _build_server()

    async def check_tool():
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        check("extract_video_frames tool present", "extract_video_frames" in tool_names)
        check("9 tools present pre-removal", len(tools) == 9, f"got {len(tools)}")
    asyncio.run(check_tool())

    section("PRE-8: record_session MCP tool has enable_video")
    mcp_src = inspect.getsource(importlib.import_module("autowrec.mcp_server"))
    check("enable_video in record_session tool", "enable_video" in mcp_src)

    section("PRE-9: console.py has video() function and theme entry")
    from autowrec import console as console_mod
    check("video() function exists", hasattr(console_mod, "video"))
    check("'video' in theme", "video" in console_mod._theme.styles)

    section("PRE-10: pyproject.toml has video deps")
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    check("mss in dependencies", "mss" in pyproject)
    check("numpy in dependencies", "numpy" in pyproject)
    check("imageio-ffmpeg in dependencies", "imageio-ffmpeg" in pyproject)

    section("PRE-11: __main__.py preloads video deps")
    main_src = inspect.getsource(importlib.import_module("autowrec.__main__"))
    check("imageio_ffmpeg preload present", "import imageio_ffmpeg" in main_src)
    check("mss preload present", "import mss" in main_src)
    check("numpy preload present", "import numpy" in main_src)

    section("PRE-12: BrowserAgent.run_session has on_browser_ready param")
    from autowrec.recorder.browser_agent import BrowserAgent
    rs_sig = inspect.signature(BrowserAgent.run_session)
    check("on_browser_ready param exists", "on_browser_ready" in rs_sig.parameters)
    check("on_browser_ready defaults to None", rs_sig.parameters["on_browser_ready"].default is None)

    section("PRE-13: _get_paths returns 4 values (including clips)")
    from autowrec.recorder.data_compressor import _get_paths
    result = _get_paths()
    check("_get_paths returns 4-tuple", len(result) == 4, f"got {len(result)}")


def run_postflight():
    """Verify the plan was executed correctly — run AFTER all changes."""

    section("POST-1: video_recorder.py deleted")
    vr_path = Path(__file__).resolve().parent.parent / "src" / "autowrec" / "recorder" / "video_recorder.py"
    check("video_recorder.py does not exist", not vr_path.exists())

    section("POST-2: config.py video constants removed")
    # Force reimport
    for mod_name in list(sys.modules.keys()):
        if "autowrec" in mod_name:
            del sys.modules[mod_name]
    from autowrec import config
    check("FPS removed", not hasattr(config, "FPS"))
    check("SEGMENT_PAD_SECONDS removed", not hasattr(config, "SEGMENT_PAD_SECONDS"))
    check("MERGE_GAP_THRESHOLD_SECONDS removed", not hasattr(config, "MERGE_GAP_THRESHOLD_SECONDS"))
    check("MCP_VIDEO_ENABLED removed", not hasattr(config, "MCP_VIDEO_ENABLED"))
    # Ensure remaining config still works
    check("SANDBOX_TIMEOUT_SECONDS still exists", hasattr(config, "SANDBOX_TIMEOUT_SECONDS"))
    check("BLOCKLIST_ENABLED still exists", hasattr(config, "BLOCKLIST_ENABLED"))
    check("REDACT_SENSITIVE still exists", hasattr(config, "REDACT_SENSITIVE"))
    check("VERSION is post-video-removal (>= 1.4.0)", config.VERSION >= "1.4.0", f"got {config.VERSION!r}")

    section("POST-3: run_recording has no enable_video param")
    from autowrec.recorder import run_recording
    sig = inspect.signature(run_recording)
    check("enable_video NOT in run_recording", "enable_video" not in sig.parameters)
    check("url param still present", "url" in sig.parameters)

    section("POST-4: compile_workspace has no video params")
    from autowrec.recorder.data_compressor import compile_workspace
    cw_sig = inspect.signature(compile_workspace)
    check("full_video_path NOT in compile_workspace", "full_video_path" not in cw_sig.parameters)
    check("video_start_unix NOT in compile_workspace", "video_start_unix" not in cw_sig.parameters)
    check("session_data param still present", "session_data" in cw_sig.parameters)

    section("POST-5: merge_and_annotate replaced with _sort_actions")
    from autowrec.recorder import data_compressor as dc
    check("merge_and_annotate_actions removed", not hasattr(dc, "merge_and_annotate_actions"))
    check("_sort_actions exists", hasattr(dc, "_sort_actions"))
    # Functional test of _sort_actions
    unsorted = [{"timestamp_unix": 3}, {"timestamp_unix": 1}, {"timestamp_unix": 2}]
    dc._sort_actions(unsorted)
    check("_sort_actions sorts correctly", [a["timestamp_unix"] for a in unsorted] == [1, 2, 3])
    # Empty list edge case
    empty = []
    dc._sort_actions(empty)
    check("_sort_actions handles empty list", empty == [])
    # Missing timestamp_unix key
    no_ts = [{"type": "click"}, {"type": "input"}]
    dc._sort_actions(no_ts)
    check("_sort_actions handles missing timestamp_unix", len(no_ts) == 2)

    section("POST-6: _get_paths returns 3 values (no clips)")
    from autowrec.recorder.data_compressor import _get_paths
    result = _get_paths()
    check("_get_paths returns 3-tuple", len(result) == 3, f"got {len(result)}")
    check("_get_paths[2] is requests dir", result[2].endswith("requests"))

    section("POST-7: MCP tools correct (8 tools, no extract_video_frames)")
    from autowrec.mcp_server import _build_server
    mcp, _state, _ = _build_server()

    async def check_tools():
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        check("8 tools registered", len(tools) == 8, f"got {len(tools)}")
        check("extract_video_frames REMOVED", "extract_video_frames" not in tool_names)
        check("record_session still present", "record_session" in tool_names)
        check("execute_code still present", "execute_code" in tool_names)
        check("read_timeline still present", "read_timeline" in tool_names)
        check("read_transaction still present", "read_transaction" in tool_names)
    asyncio.run(check_tools())

    section("POST-8: record_session tool has no enable_video param")
    mcp_src = inspect.getsource(importlib.import_module("autowrec.mcp_server"))
    # The tool function itself should not mention enable_video
    # Extract just the record_session function body
    check("enable_video NOT in mcp_server source", "enable_video" not in mcp_src)

    section("POST-9: console.py has no video() function or theme entry")
    from autowrec import console as console_mod
    check("video() function removed", not hasattr(console_mod, "video"))
    check("'video' NOT in theme", "video" not in console_mod._theme.styles)
    # Ensure other functions still work
    check("info() still exists", hasattr(console_mod, "info"))
    check("warn() still exists", hasattr(console_mod, "warn"))
    check("error() still exists", hasattr(console_mod, "error"))

    section("POST-10: pyproject.toml has no video deps")
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    check("mss NOT in dependencies", "mss" not in pyproject)
    check("numpy NOT in dependencies", "numpy" not in pyproject)
    check("imageio-ffmpeg NOT in dependencies", "imageio-ffmpeg" not in pyproject)
    # Ensure core deps are still there
    check("zendriver still in deps", "zendriver" in pyproject)
    check("fastmcp still in deps", "fastmcp" in pyproject)
    check("ipython still in deps", "ipython" in pyproject)

    section("POST-11: __main__.py has no video preloads")
    main_src = inspect.getsource(importlib.import_module("autowrec.__main__"))
    check("imageio_ffmpeg preload removed", "import imageio_ffmpeg" not in main_src)
    check("import mss preload removed", "import mss" not in main_src)
    check("import numpy preload removed", "import numpy" not in main_src)
    check("import zendriver still present", "import zendriver" in main_src)

    section("POST-12: data_compressor has no video_recorder import")
    dc_src = inspect.getsource(importlib.import_module("autowrec.recorder.data_compressor"))
    check("no ActionVideoRecorder import", "ActionVideoRecorder" not in dc_src)
    check("no video_recorder import", "video_recorder" not in dc_src)

    section("POST-13: compile_workspace works without video params")
    from autowrec import config as cfg
    saved_output = cfg.OUTPUT_DIR
    saved_ws = cfg.WORKSPACE_DIR
    test_out = Path(tempfile.mkdtemp(prefix="autowrec_vr_test_"))
    cfg.OUTPUT_DIR = test_out
    cfg.WORKSPACE_DIR = test_out / "workspace"
    cfg.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from autowrec.recorder.data_compressor import compile_workspace as cw
        ok = cw(session_data={"metadata": {}, "actions": [
            {"timestamp_unix": 2.0, "type": "click", "text": "b"},
            {"timestamp_unix": 1.0, "type": "input", "value": "a"},
        ], "requests": []})
        check("compile_workspace succeeds without video", ok is True)

        # Verify the timeline was produced and actions are sorted
        timeline_path = test_out / "workspace" / "session_dump" / "timeline.json"
        if timeline_path.exists():
            with open(timeline_path, encoding="utf-8") as f:
                timeline = json.load(f)
            actions_only = [e for e in timeline if e.get("event_type") == "user_action"]
            if actions_only:
                check(
                    "actions sorted in timeline output",
                    actions_only[0]["timestamp"] <= actions_only[-1]["timestamp"],
                    f"got timestamps {[a['timestamp'] for a in actions_only]}",
                )
                # Verify no video fields in timeline events
                check(
                    "no ai_video_file in timeline events",
                    all("ai_video_file" not in a for a in actions_only),
                )
                check(
                    "no video_start_sec in timeline events",
                    all("video_start_sec" not in a for a in actions_only),
                )
            else:
                check("actions in timeline", False, "no user_action events found")
        else:
            check("timeline.json produced", False, "file not found")

        # Verify no clips/ directory was created
        clips_path = test_out / "workspace" / "session_dump" / "clips"
        check("no clips/ directory created", not clips_path.exists())

    finally:
        cfg.OUTPUT_DIR = saved_output
        cfg.WORKSPACE_DIR = saved_ws
        shutil.rmtree(test_out, ignore_errors=True)

    section("POST-14: on_browser_ready removed from BrowserAgent.run_session")
    from autowrec.recorder.browser_agent import BrowserAgent
    rs_sig = inspect.signature(BrowserAgent.run_session)
    check(
        "on_browser_ready NOT in BrowserAgent.run_session",
        "on_browser_ready" not in rs_sig.parameters,
    )
    check("url param still in BrowserAgent.run_session", "url" in rs_sig.parameters)

    section("POST-15: No orphaned video references in source tree")
    src_root = Path(__file__).resolve().parent.parent / "src" / "autowrec"
    orphaned = []
    for py_file in src_root.rglob("*.py"):
        if py_file.name == "video_recorder.py":
            continue
        content = py_file.read_text(encoding="utf-8", errors="replace")
        if "video_recorder" in content or "ActionVideoRecorder" in content:
            orphaned.append(str(py_file.relative_to(src_root)))
    check("no orphaned video_recorder imports", len(orphaned) == 0, f"found in: {orphaned}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    is_post = _is_post_flight()

    if mode == "--pre":
        if is_post:
            print("ERROR: video_recorder.py already deleted — pre-flight tests cannot run.")
            sys.exit(1)
        run_preflight()
    elif mode == "--post":
        if not is_post:
            print("ERROR: video_recorder.py still exists — post-flight tests need the plan executed first.")
            sys.exit(1)
        run_postflight()
    else:
        # Auto-detect
        if is_post:
            print("Auto-detected: post-flight mode (video_recorder.py deleted)")
            run_postflight()
        else:
            print("Auto-detected: pre-flight mode (video_recorder.py still exists)")
            run_preflight()

    section("RESULTS")
    print(f"\n  Passed: {PASS}")
    print(f"  Failed: {FAIL}")
    print(f"  Skipped: {SKIP}")
    print(f"  Total:  {PASS + FAIL + SKIP}")
    print()
    if FAIL > 0:
        print("  SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("  ALL TESTS PASSED")


if __name__ == "__main__":
    main()
