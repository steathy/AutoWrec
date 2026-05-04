"""
Standalone test script for AutoWrec library and MCP server.
Tests that the refactored codebase works without any LLM dependencies.

Usage:
    python test_standalone.py
"""

import asyncio
import json
import multiprocessing
import os
import shutil
import sys
import tempfile

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{f' — {detail}' if detail else ''}")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def run_tests():
    # ─────────────────────────────────────────────────────────────────────────
    section("1. Core Imports (no LLM deps)")
    # ─────────────────────────────────────────────────────────────────────────

    try:
        import autowrec.config
        check("autowrec.config imports", True)
    except Exception as e:
        check("autowrec.config imports", False, str(e))

    try:
        import autowrec.console
        check("autowrec.console imports", True)
    except Exception as e:
        check("autowrec.console imports", False, str(e))

    try:
        import autowrec.mcp_server
        check("autowrec.mcp_server imports", True)
    except Exception as e:
        check("autowrec.mcp_server imports", False, str(e))

    try:
        import autowrec.recorder
        check("autowrec.recorder imports", True)
    except Exception as e:
        check("autowrec.recorder imports", False, str(e))

    try:
        import autowrec.recorder.data_compressor
        check("autowrec.recorder.data_compressor imports", True)
    except Exception as e:
        check("autowrec.recorder.data_compressor imports", False, str(e))

    try:
        import autowrec.ipython_sandbox
        check("autowrec.ipython_sandbox imports", True)
    except Exception as e:
        check("autowrec.ipython_sandbox imports", False, str(e))

    try:
        import autowrec.bin_manager
        check("autowrec.bin_manager imports", True)
    except Exception as e:
        check("autowrec.bin_manager imports", False, str(e))

    check("litellm NOT imported", "litellm" not in sys.modules)
    check("instructor NOT imported", "instructor" not in sys.modules)

    # ─────────────────────────────────────────────────────────────────────────
    section("2. Config System")
    # ─────────────────────────────────────────────────────────────────────────

    from autowrec import config

    check("VERSION is set", config.VERSION == "1.3.2")
    check("FPS default", config.FPS == 3)
    check("SEGMENT_PAD_SECONDS default", config.SEGMENT_PAD_SECONDS == 2)
    check("SANDBOX_TIMEOUT_SECONDS default", config.SANDBOX_TIMEOUT_SECONDS == 60)
    check("MCP_VIDEO_ENABLED default", config.MCP_VIDEO_ENABLED is False)
    check("AGENT_MODEL removed", not hasattr(config, "AGENT_MODEL"))
    check("RECORDER_AI_MODEL removed", not hasattr(config, "RECORDER_AI_MODEL"))
    check("API_BASE removed", not hasattr(config, "API_BASE"))
    check("MAX_AGENT_STEPS removed", not hasattr(config, "MAX_AGENT_STEPS"))

    # ─────────────────────────────────────────────────────────────────────────
    section("3. MCP Server — Tool Registration")
    # ─────────────────────────────────────────────────────────────────────────

    from autowrec.mcp_server import _build_server

    mcp, _mcp_state, _safe_resolve = _build_server()

    async def test_tools():
        tools = await mcp.list_tools()
        tool_names = {t.name for t in tools}
        check("9 tools registered", len(tools) == 9, f"got {len(tools)}")
        check("record_session tool", "record_session" in tool_names)
        check("check_recording tool", "check_recording" in tool_names)
        check("read_session_summary tool", "read_session_summary" in tool_names)
        check("read_timeline tool", "read_timeline" in tool_names)
        check("read_transaction tool", "read_transaction" in tool_names)
        check("list_workspace_files tool", "list_workspace_files" in tool_names)
        check("read_file tool", "read_file" in tool_names)
        check("extract_video_frames tool", "extract_video_frames" in tool_names)
        check("execute_code tool", "execute_code" in tool_names)
        for t in tools:
            check(f"  {t.name} has description", bool(t.description))

    asyncio.run(test_tools())

    # ─────────────────────────────────────────────────────────────────────────
    section("4. Path Traversal Protection")
    # ─────────────────────────────────────────────────────────────────────────

    test_workspace = tempfile.mkdtemp(prefix="autowrec_test_")
    os.makedirs(os.path.join(test_workspace, "requests"), exist_ok=True)

    try:
        result = _safe_resolve(test_workspace, "requests")
        check("valid subdir resolves", os.path.basename(result) == "requests")
    except Exception as e:
        check("valid subdir resolves", False, str(e))

    try:
        _safe_resolve(test_workspace, "../../etc/passwd")
        check("traversal ../../etc/passwd blocked", False, "should have raised")
    except ValueError:
        check("traversal ../../etc/passwd blocked", True)

    try:
        _safe_resolve(test_workspace, "requests/../../../etc")
        check("traversal via nested ../ blocked", False, "should have raised")
    except ValueError:
        check("traversal via nested ../ blocked", True)

    shutil.rmtree(test_workspace, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────────
    section("5. Workspace Tools (mock data)")
    # ─────────────────────────────────────────────────────────────────────────

    fake_workspace = tempfile.mkdtemp(prefix="autowrec_ws_")
    fake_session = os.path.join(fake_workspace, "session_dump")
    os.makedirs(os.path.join(fake_session, "requests", "000_GET_example.com"), exist_ok=True)
    os.makedirs(os.path.join(fake_session, "clips"), exist_ok=True)

    summary_data = {
        "session": {"duration_seconds": 30.0},
        "session_flow": [{"timestamp_iso": "2026-01-01T00:00:00Z", "summary": "User clicked login"}],
        "statistics": {"total_requests": 5, "total_actions": 2},
    }
    with open(os.path.join(fake_session, "SUMMARY.json"), "w") as f:
        json.dump(summary_data, f)

    timeline_data = [
        {"timestamp": 1.0, "event_type": "user_action", "action": "click"},
        {"timestamp": 2.0, "event_type": "network_request", "method": "GET", "url": "https://example.com"},
        {"timestamp": 3.0, "event_type": "user_action", "action": "input"},
    ]
    with open(os.path.join(fake_session, "timeline.json"), "w") as f:
        json.dump(timeline_data, f)

    tx_dir = os.path.join(fake_session, "requests", "000_GET_example.com")
    tx_data = {"metadata": {"method": "GET", "url": "https://example.com", "status": 200}}
    with open(os.path.join(tx_dir, "transaction.json"), "w") as f:
        json.dump(tx_data, f)
    with open(os.path.join(tx_dir, "res_body.html"), "w") as f:
        f.write("<html><body>Hello World</body></html>")

    _mcp_state["workspace"] = fake_session

    def _tool_text(tool_result):
        """Extract text from a FastMCP ToolResult."""
        return tool_result.content[0].text

    async def test_workspace_tools():
        # M1: lean digest by default, full SUMMARY.json with verbose=true
        try:
            digest = json.loads(_tool_text(await mcp.call_tool("read_session_summary", {})))
            check(
                "read_session_summary lean digest",
                "duration_s" in digest and "requests" in digest and "verbose_available" in digest,
            )
            full = json.loads(_tool_text(await mcp.call_tool("read_session_summary", {"verbose": True})))
            check(
                "read_session_summary verbose=true",
                "session" in full and "statistics" in full,
            )
        except Exception as e:
            check("read_session_summary", False, str(e))

        try:
            result = json.loads(_tool_text(await mcp.call_tool("read_timeline", {"offset": 0, "limit": 2})))
            check("read_timeline pagination", result["total"] == 3 and len(result["events"]) == 2)
            check("read_timeline has_more", result["has_more"] is True)
        except Exception as e:
            check("read_timeline", False, str(e))

        # M3 + M4: read_transaction returns metadata only (no body inlining).
        try:
            minimal = json.loads(_tool_text(await mcp.call_tool("read_transaction", {
                "request_folder": "requests/000_GET_example.com",
            })))
            check(
                "read_transaction default level=minimal",
                "method" in minimal and "url" in minimal and "request_body" not in minimal,
            )
            full = json.loads(_tool_text(await mcp.call_tool("read_transaction", {
                "request_folder": "requests/000_GET_example.com",
                "level": "full",
            })))
            check(
                "read_transaction level=full keeps metadata, no inline body",
                "metadata" in full and "request_body" not in full,
            )
        except Exception as e:
            check("read_transaction", False, str(e))

        # M8: list_workspace_files no longer includes sizes by default.
        try:
            result = json.loads(_tool_text(await mcp.call_tool("list_workspace_files", {})))
            names = {e["name"] for e in result["entries"]}
            check("list_workspace_files", "SUMMARY.json" in names and "requests" in names)
            check(
                "list_workspace_files default omits size field",
                all("size" not in e for e in result["entries"]),
            )
            with_sizes = json.loads(_tool_text(await mcp.call_tool(
                "list_workspace_files", {"include_sizes": True}
            )))
            check(
                "list_workspace_files include_sizes=true adds sizes",
                any("size" in e for e in with_sizes["entries"]),
            )
        except Exception as e:
            check("list_workspace_files", False, str(e))

        # M5: read_file default mode is 'head'; raw mode for full read.
        try:
            head = json.loads(_tool_text(await mcp.call_tool("read_file", {"path": "SUMMARY.json"})))
            check("read_file default mode=head", head["encoding"] == "utf-8" and head["size"] > 0)
            stat = json.loads(_tool_text(await mcp.call_tool(
                "read_file", {"path": "SUMMARY.json", "mode": "stat"}
            )))
            check("read_file mode=stat returns metadata only", "size" in stat and "content" not in stat)
            raw = json.loads(_tool_text(await mcp.call_tool(
                "read_file", {"path": "SUMMARY.json", "mode": "raw", "limit": 9999}
            )))
            check("read_file mode=raw honors offset/limit", raw["bytes_read"] > 0)
        except Exception as e:
            check("read_file", False, str(e))

        try:
            await mcp.call_tool("read_file", {"path": "../../etc/passwd"})
            check("read_file blocks traversal", False, "should have raised")
        except (ValueError, FileNotFoundError):
            check("read_file blocks traversal", True)
        except Exception as e:
            check("read_file blocks traversal", "traversal" in str(e).lower() or "not found" in str(e).lower(), str(e))

    asyncio.run(test_workspace_tools())

    shutil.rmtree(fake_workspace, ignore_errors=True)
    _mcp_state["workspace"] = None

    # ─────────────────────────────────────────────────────────────────────────
    section("6. IPython Sandbox")
    # ─────────────────────────────────────────────────────────────────────────

    from autowrec.ipython_sandbox import AgentSandbox

    sandbox_dir = tempfile.mkdtemp(prefix="autowrec_sandbox_")
    sandbox = AgentSandbox(working_dir=sandbox_dir, timeout_seconds=15)

    try:
        result = sandbox.execute("print('hello from sandbox')")
        check("sandbox basic execution", "hello from sandbox" in result)
    except Exception as e:
        check("sandbox basic execution", False, str(e))

    try:
        sandbox.execute("x = 42")
        result = sandbox.execute("print(x * 2)")
        check("sandbox state persists", "84" in result)
    except Exception as e:
        check("sandbox state persists", False, str(e))

    try:
        result = sandbox.execute("import json; print(json.dumps({'a': 1}))")
        check("sandbox imports work", '{"a": 1}' in result)
    except Exception as e:
        check("sandbox imports work", False, str(e))

    try:
        result = sandbox.execute("1/0")
        check("sandbox reports errors", "ZeroDivisionError" in result)
    except Exception as e:
        check("sandbox error handling", False, str(e))

    try:
        result = sandbox.execute("%reset")
        check("sandbox %reset", "RESET" in result)
    except Exception as e:
        check("sandbox %reset", False, str(e))

    try:
        sandbox.execute("y = 99")
        result = sandbox.execute("%restore")
        check("sandbox %restore no deadlock", "RESTORED" in result or "No history" in result)
    except Exception as e:
        check("sandbox %restore no deadlock", False, str(e))

    sandbox.close()
    shutil.rmtree(sandbox_dir, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────────
    section("7. Console Redirect (MCP safety)")
    # ─────────────────────────────────────────────────────────────────────────

    import io
    from autowrec import console as console_mod
    from rich.console import Console

    original_console = console_mod.console
    stderr_buf = io.StringIO()
    console_mod.console = Console(theme=console_mod._theme, highlight=False, file=stderr_buf)

    console_mod.info("test redirect message")
    output = stderr_buf.getvalue()
    check("info() respects redirect", "test redirect message" in output)

    from autowrec import bin_manager as bm
    check("bin_manager sees redirected console", bm._console_mod.console is console_mod.console)

    console_mod.console = original_console

    # ─────────────────────────────────────────────────────────────────────────
    section("8. Data Compressor (no AI deps)")
    # ─────────────────────────────────────────────────────────────────────────

    from autowrec.recorder.data_compressor import merge_and_annotate_actions

    actions = [
        {"timestamp_unix": 100.0, "type": "click", "text": "button"},
        {"timestamp_unix": 101.0, "type": "input", "value": "hello"},
        {"timestamp_unix": 105.0, "type": "click", "text": "submit"},
    ]

    result = merge_and_annotate_actions(actions, None, None)
    check("merge_and_annotate no video", len(result) == 3)

    result = merge_and_annotate_actions(actions, "/nonexistent.mp4", 99.0)
    check("merge_and_annotate missing video", len(result) == 3)

    # ─────────────────────────────────────────────────────────────────────────
    section("9. Config Validation")
    # ─────────────────────────────────────────────────────────────────────────

    from autowrec import config as cfg

    # Test with real bad config file
    bad_config_dir = tempfile.mkdtemp(prefix="autowrec_badcfg_")
    bad_config_file = os.path.join(bad_config_dir, "config.toml")
    with open(bad_config_file, "w") as f:
        f.write('[recording]\nfps = "fast"\nsegment_pad = "broken"\n\n[banner]\nspeed = "slow"\n')

    saved_config_file = cfg.CONFIG_FILE
    saved_fps = cfg.FPS
    saved_pad = cfg.SEGMENT_PAD_SECONDS
    saved_speed = cfg.BANNER_SPEED
    try:
        from pathlib import Path
        cfg.CONFIG_FILE = Path(bad_config_file)
        cfg.FPS = 3
        cfg.SEGMENT_PAD_SECONDS = 2.0
        cfg.BANNER_SPEED = 1.0
        cfg._load_config_toml()
        check("bad config survives import", True)
        check("bad fps keeps default", cfg.FPS == 3, f"got {cfg.FPS}")
        check("bad segment_pad keeps default", cfg.SEGMENT_PAD_SECONDS == 2.0, f"got {cfg.SEGMENT_PAD_SECONDS}")
        check("bad speed keeps default", cfg.BANNER_SPEED == 1.0, f"got {cfg.BANNER_SPEED}")
    except Exception as e:
        check("bad config survives import", False, str(e))
    finally:
        cfg.CONFIG_FILE = saved_config_file
        cfg.FPS = saved_fps
        cfg.SEGMENT_PAD_SECONDS = saved_pad
        cfg.BANNER_SPEED = saved_speed
    shutil.rmtree(bad_config_dir, ignore_errors=True)

    # Test non-table sections and non-string output.dir
    bad2_dir = tempfile.mkdtemp(prefix="autowrec_badcfg2_")
    bad2_file = os.path.join(bad2_dir, "config.toml")
    with open(bad2_file, "w") as f:
        f.write('recording = 5\nbanner = "not a table"\n\n[output]\ndir = 42\n')

    saved_output_dir = cfg.OUTPUT_DIR
    saved_workspace_dir = cfg.WORKSPACE_DIR
    saved_blocklist_dir = cfg.BLOCKLIST_DIR
    saved_blocklist_db = cfg.BLOCKLIST_DB
    try:
        cfg.CONFIG_FILE = Path(bad2_file)
        cfg.FPS = 3
        cfg.OUTPUT_DIR = saved_output_dir
        cfg._load_config_toml()
        check("non-table section survives", True)
        check("FPS unchanged by scalar section", cfg.FPS == 3, f"got {cfg.FPS}")
        check("output.dir rejects int", cfg.OUTPUT_DIR == saved_output_dir, f"got {cfg.OUTPUT_DIR}")
    except Exception as e:
        check("non-table section survives", False, str(e))
    finally:
        cfg.CONFIG_FILE = saved_config_file
        cfg.FPS = saved_fps
        cfg.OUTPUT_DIR = saved_output_dir
        cfg.WORKSPACE_DIR = saved_workspace_dir
        cfg.BLOCKLIST_DIR = saved_blocklist_dir
        cfg.BLOCKLIST_DB = saved_blocklist_db
    shutil.rmtree(bad2_dir, ignore_errors=True)

    # Test CLI timeout clamping
    from types import SimpleNamespace
    from autowrec.__main__ import _apply_config_overrides
    saved_timeout = cfg.SANDBOX_TIMEOUT_SECONDS
    fake_args = SimpleNamespace(output_dir=None, sandbox_timeout=0, no_banner=False, no_blocklist=False, redact=False, verbose=False)
    _apply_config_overrides(fake_args)
    check("CLI timeout=0 clamped to 1", cfg.SANDBOX_TIMEOUT_SECONDS == 1, f"got {cfg.SANDBOX_TIMEOUT_SECONDS}")
    fake_args.sandbox_timeout = -10
    _apply_config_overrides(fake_args)
    check("CLI timeout=-10 clamped to 1", cfg.SANDBOX_TIMEOUT_SECONDS == 1, f"got {cfg.SANDBOX_TIMEOUT_SECONDS}")
    cfg.SANDBOX_TIMEOUT_SECONDS = saved_timeout

    check("FPS range validation", cfg.FPS >= 1, f"got {cfg.FPS}")
    check("SEGMENT_PAD >= 0", cfg.SEGMENT_PAD_SECONDS >= 0)
    check("SANDBOX_TIMEOUT >= 1", cfg.SANDBOX_TIMEOUT_SECONDS >= 1)
    check("BANNER_SPEED > 0", cfg.BANNER_SPEED > 0)
    check("REDACT_SENSITIVE exists", hasattr(cfg, "REDACT_SENSITIVE"))

    # ─────────────────────────────────────────────────────────────────────────
    section("10. Redaction & Header Safety")
    # ─────────────────────────────────────────────────────────────────────────

    from autowrec.recorder.data_compressor import _redact_headers

    # With redaction off (default)
    original_redact = cfg.REDACT_SENSITIVE
    cfg.REDACT_SENSITIVE = False
    headers = {"Authorization": "Bearer token123", "Content-Type": "application/json"}
    result_h = _redact_headers(headers)
    check("redact off: headers unchanged", result_h["Authorization"] == "Bearer token123")

    # With redaction on
    cfg.REDACT_SENSITIVE = True
    result_h = _redact_headers(headers)
    check("redact on: auth redacted", "REDACTED" in result_h["Authorization"])
    check("redact on: preserves prefix", result_h["Authorization"].startswith("Bearer"))
    check("redact on: content-type untouched", result_h["Content-Type"] == "application/json")

    # Cookie redaction
    cookie_headers = {"Cookie": "session_id=abc123; user=bob", "X-Custom": "safe"}
    result_h = _redact_headers(cookie_headers)
    check("redact on: cookie redacted", "REDACTED" in result_h["Cookie"])
    check("redact on: custom header untouched", result_h["X-Custom"] == "safe")

    # Non-string header values (CDP edge case)
    mixed_headers = {"Content-Length": 12345, "Authorization": "Basic xyz"}
    try:
        result_h = _redact_headers(mixed_headers)
        check("redact handles int values", "12345" in str(result_h["Content-Length"]))
    except Exception as e:
        check("redact handles int values", False, str(e))

    cfg.REDACT_SENSITIVE = original_redact

    # ─────────────────────────────────────────────────────────────────────────
    section("11. MCP Input Validation")
    # ─────────────────────────────────────────────────────────────────────────

    # Recreate fake workspace for validation tests
    val_workspace = tempfile.mkdtemp(prefix="autowrec_val_")
    val_session = os.path.join(val_workspace, "session_dump")
    os.makedirs(val_session)
    with open(os.path.join(val_session, "timeline.json"), "w") as f:
        json.dump([{"ts": 1}, {"ts": 2}, {"ts": 3}], f)
    with open(os.path.join(val_session, "test.txt"), "w") as f:
        f.write("hello")

    _mcp_state["workspace"] = val_session

    async def test_validation():
        # read_timeline with negative offset should not crash
        try:
            result = json.loads(_tool_text(await mcp.call_tool("read_timeline", {"offset": -5, "limit": 2})))
            check("read_timeline negative offset clamped", result["total"] == 3)
        except Exception as e:
            check("read_timeline negative offset clamped", False, str(e))

        # read_file with negative limit (raw mode) should not crash
        try:
            result = json.loads(_tool_text(await mcp.call_tool(
                "read_file", {"path": "test.txt", "mode": "raw", "limit": -1}
            )))
            check("read_file negative limit clamped", result["bytes_read"] > 0)
        except Exception as e:
            check("read_file negative limit clamped", False, str(e))

        # execute_code with timeout=0 should clamp to 1 (use fake sandbox for isolation)
        class _FakeSandbox:
            def __init__(self):
                self.last_kwargs = None
            def execute(self, code, **kwargs):
                self.last_kwargs = kwargs
                return "[Cell_1] Status: Success\n2"

        fake_sb = _FakeSandbox()
        old_sb = _mcp_state.get("sandbox")
        _mcp_state["sandbox"] = fake_sb
        try:
            result = _tool_text(await mcp.call_tool("execute_code", {"code": "print(1+1)", "timeout": 0}))
            check("execute_code timeout=0 clamped", "2" in result)
            check("execute_code passes custom_timeout=1", fake_sb.last_kwargs == {"custom_timeout": 1})
        except Exception as e:
            check("execute_code timeout=0 clamped", False, str(e))
        finally:
            _mcp_state["sandbox"] = old_sb

    asyncio.run(test_validation())
    shutil.rmtree(val_workspace, ignore_errors=True)
    _mcp_state["workspace"] = None

    # ─────────────────────────────────────────────────────────────────────────
    section("12. Binary body via read_file base64 (post-M4)")
    # ─────────────────────────────────────────────────────────────────────────
    # Bodies are no longer inlined into read_transaction; the AI must call
    # read_file in 'raw' mode for the actual body content.

    bin_workspace = tempfile.mkdtemp(prefix="autowrec_bin_")
    bin_session = os.path.join(bin_workspace, "session_dump")
    tx_dir_bin = os.path.join(bin_session, "requests", "000_GET_example.com")
    os.makedirs(tx_dir_bin)

    with open(os.path.join(tx_dir_bin, "transaction.json"), "w") as f:
        json.dump({
            "metadata": {"method": "GET", "url": "https://example.com"},
            "response": {"has_body": True, "content_detection": {"extension": "bin"}},
        }, f)
    with open(os.path.join(tx_dir_bin, "res_body.bin"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")  # binary PNG header

    _mcp_state["workspace"] = bin_session

    async def test_binary_body():
        try:
            tx = json.loads(_tool_text(await mcp.call_tool(
                "read_transaction",
                {"request_folder": "requests/000_GET_example.com", "level": "full"},
            )))
            check("transaction reports has_body=true", tx["response"]["has_body"] is True)
            body = json.loads(_tool_text(await mcp.call_tool(
                "read_file",
                {"path": "requests/000_GET_example.com/res_body.bin", "mode": "raw"},
            )))
            check("binary body via read_file uses base64", body["encoding"] == "base64")
        except Exception as e:
            check("binary body via read_file", False, str(e))

    asyncio.run(test_binary_body())
    shutil.rmtree(bin_workspace, ignore_errors=True)
    _mcp_state["workspace"] = None

    # ─────────────────────────────────────────────────────────────────────────
    section("13. Video Startup Failure Cleanup")
    # ─────────────────────────────────────────────────────────────────────────

    from unittest.mock import patch, MagicMock
    import autowrec.recorder as _rec_mod
    from autowrec.recorder.video_recorder import ActionVideoRecorder
    from autowrec import console as _console_mod
    from rich.console import Console as _RichConsole
    from pathlib import Path as _Path

    # Create isolated temp dir for this test
    vf_dir = tempfile.mkdtemp(prefix="autowrec_vfail_")
    vf_fd, vf_path = tempfile.mkstemp(suffix=".mp4", prefix="autowrec_", dir=vf_dir)
    os.close(vf_fd)

    # Isolate config output paths
    _test_output = tempfile.mkdtemp(prefix="autowrec_testout_")
    _saved_output = cfg.OUTPUT_DIR
    _saved_ws = cfg.WORKSPACE_DIR
    _saved_bl_dir = cfg.BLOCKLIST_DIR
    _saved_bl_db = cfg.BLOCKLIST_DB
    cfg.OUTPUT_DIR = _Path(_test_output)
    cfg.WORKSPACE_DIR = _Path(_test_output) / "workspace"
    cfg.BLOCKLIST_DIR = _Path(_test_output) / "blocklist"
    cfg.BLOCKLIST_DB = _Path(_test_output) / "blocklist.db"

    # Redirect console to avoid encoding errors during test
    _saved_console = _console_mod.console
    _console_mod.console = _RichConsole(theme=_console_mod._theme, highlight=False, file=io.StringIO())

    def fake_mkstemp(*args, **kwargs):
        return (99, vf_path)

    try:
        with patch.object(_rec_mod.tempfile, "mkstemp", side_effect=fake_mkstemp), \
             patch.object(_rec_mod.os, "close", return_value=None), \
             patch.object(ActionVideoRecorder, "start", return_value=False), \
             patch.object(_rec_mod, "_init_blocklist", return_value=None), \
             patch.object(_rec_mod, "compile_workspace", return_value=True), \
             patch.object(_rec_mod, "BrowserAgent") as mock_ba:
            mock_ba_inst = MagicMock()
            mock_ba_inst.stats = {"blocked_by_blocklist": 0}
            async def fake_session(*a, **kw):
                return {"metadata": {}, "requests": [], "actions": []}
            mock_ba_inst.run_session = fake_session
            mock_ba.return_value = mock_ba_inst

            result = _rec_mod.run_recording(url="about:blank", enable_video=True)

        check("video startup fail cleans temp file", not os.path.exists(vf_path))
        # Verify no full_record.mp4 was promoted (video never started)
        session_dump = os.path.join(_test_output, "workspace", "session_dump")
        promoted = os.path.join(session_dump, "full_record.mp4")
        check("no invalid video promoted to workspace", not os.path.exists(promoted))
    except Exception as e:
        check("video startup fail test ran", False, str(e))
    finally:
        _console_mod.console = _saved_console
        cfg.OUTPUT_DIR = _saved_output
        cfg.WORKSPACE_DIR = _saved_ws
        cfg.BLOCKLIST_DIR = _saved_bl_dir
        cfg.BLOCKLIST_DB = _saved_bl_db

    shutil.rmtree(vf_dir, ignore_errors=True)
    shutil.rmtree(_test_output, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────────
    section("RESULTS")
    # ─────────────────────────────────────────────────────────────────────────

    print(f"\n  Passed: {PASS}")
    print(f"  Failed: {FAIL}")
    print(f"  Total:  {PASS + FAIL}")
    print()

    if FAIL > 0:
        print("  SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("  ALL TESTS PASSED")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    run_tests()
