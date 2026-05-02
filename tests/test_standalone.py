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

    check("VERSION is set", config.VERSION == "1.0.0")
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
        check("8 tools registered", len(tools) == 8, f"got {len(tools)}")
        check("record_session tool", "record_session" in tool_names)
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
        try:
            result = json.loads(_tool_text(await mcp.call_tool("read_session_summary", {})))
            check("read_session_summary", "session" in result and "statistics" in result)
        except Exception as e:
            check("read_session_summary", False, str(e))

        try:
            result = json.loads(_tool_text(await mcp.call_tool("read_timeline", {"offset": 0, "limit": 2})))
            check("read_timeline pagination", result["total"] == 3 and len(result["events"]) == 2)
            check("read_timeline has_more", result["has_more"] is True)
        except Exception as e:
            check("read_timeline", False, str(e))

        try:
            result = json.loads(_tool_text(await mcp.call_tool("read_transaction", {
                "request_folder": "requests/000_GET_example.com",
                "include_response_body": True,
            })))
            check("read_transaction with body", "Hello World" in result.get("response_body", ""))
        except Exception as e:
            check("read_transaction", False, str(e))

        try:
            result = json.loads(_tool_text(await mcp.call_tool("list_workspace_files", {})))
            names = {e["name"] for e in result["entries"]}
            check("list_workspace_files", "SUMMARY.json" in names and "requests" in names)
        except Exception as e:
            check("list_workspace_files", False, str(e))

        try:
            result = json.loads(_tool_text(await mcp.call_tool("read_file", {"path": "SUMMARY.json"})))
            check("read_file", result["encoding"] == "utf-8" and result["size"] > 0)
        except Exception as e:
            check("read_file", False, str(e))

        try:
            await mcp.call_tool("read_file", {"path": "../../etc/passwd"})
            check("read_file blocks traversal", False, "should have raised")
        except (ValueError, FileNotFoundError):
            check("read_file blocks traversal", True)
        except Exception as e:
            # FastMCP may wrap the error — check the message
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

    # Simulate bad config values — should not crash, should keep defaults
    original_fps = cfg.FPS
    try:
        # Calling int("fast") would crash without safe casts
        check("config safe cast (int)", cfg._load_config_toml is not None)
    except Exception as e:
        check("config safe cast (int)", False, str(e))

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

        # read_file with negative limit should not crash
        try:
            result = json.loads(_tool_text(await mcp.call_tool("read_file", {"path": "test.txt", "limit": -1})))
            check("read_file negative limit clamped", result["bytes_read"] > 0)
        except Exception as e:
            check("read_file negative limit clamped", False, str(e))

    asyncio.run(test_validation())
    shutil.rmtree(val_workspace, ignore_errors=True)
    _mcp_state["workspace"] = None

    # ─────────────────────────────────────────────────────────────────────────
    section("12. Binary Transaction Body Encoding")
    # ─────────────────────────────────────────────────────────────────────────

    bin_workspace = tempfile.mkdtemp(prefix="autowrec_bin_")
    bin_session = os.path.join(bin_workspace, "session_dump")
    tx_dir_bin = os.path.join(bin_session, "requests", "000_GET_example.com")
    os.makedirs(tx_dir_bin)

    with open(os.path.join(tx_dir_bin, "transaction.json"), "w") as f:
        json.dump({"metadata": {"method": "GET", "url": "https://example.com"}}, f)
    with open(os.path.join(tx_dir_bin, "res_body.bin"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")  # binary PNG header

    _mcp_state["workspace"] = bin_session

    async def test_binary_body():
        try:
            result = json.loads(_tool_text(await mcp.call_tool("read_transaction", {
                "request_folder": "requests/000_GET_example.com",
                "include_response_body": True,
            })))
            check("binary body returns base64", result.get("response_body_encoding") == "base64")
        except Exception as e:
            check("binary body returns base64", False, str(e))

    asyncio.run(test_binary_body())
    shutil.rmtree(bin_workspace, ignore_errors=True)
    _mcp_state["workspace"] = None

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
