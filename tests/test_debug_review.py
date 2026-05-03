"""
Debug / regression test script written during code review.

Each test corresponds to a bug or risk identified by reading source.
Run alongside test_standalone.py to surface issues that the existing
suite doesn't cover.

Usage:
    python tests/test_debug_review.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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


def main():
    # ─────────────────────────────────────────────────────────────────────
    section("1. _get_process_tree finds child PIDs (post-C1 fix)")
    # ─────────────────────────────────────────────────────────────────────
    # Windows 11 24H2/25H2 ships without wmic. After C1, the function uses
    # PowerShell Get-CimInstance and should find children of the current
    # process even when wmic is absent.

    from autowrec.recorder.video_recorder import _get_process_tree

    if sys.platform == "win32":
        # Spawn a real child so we have something to discover.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            time.sleep(0.5)  # let CIM see the new process
            tree = _get_process_tree(os.getpid())
            check(
                "C1: _get_process_tree contains current PID",
                os.getpid() in tree,
                f"got {tree}",
            )
            check(
                "C1: _get_process_tree finds spawned child PID",
                child.pid in tree,
                f"child={child.pid}, tree={tree}",
            )
        finally:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
    else:
        skip("_get_process_tree test", "non-Windows platform")

    # ─────────────────────────────────────────────────────────────────────
    section("2. Orphan-extra-info leak for blocklist-skipped requests")
    # ─────────────────────────────────────────────────────────────────────
    # If extra_info events arrive AFTER a blocked request (and the request
    # itself was skipped), they accumulate forever in orphan_extra_info.

    from autowrec.recorder.browser_agent import BrowserAgent

    class FakeBlocklist:
        def is_blocked_url(self, url):
            return "ads.example" in url

    agent = BrowserAgent(blocklist=FakeBlocklist())

    class FakeReq:
        url = "https://ads.example.com/track"
        method = "GET"
        headers = {}
        post_data = None

    class FakeEvent:
        request_id = "rid-blocked-1"
        request = FakeReq()
        type_ = "Other"
        timestamp = 0.0
        wall_time = None
        redirect_response = None

    # Simulate: blocked request arrives first
    asyncio.run(agent.request_handler(FakeEvent()))
    check(
        "blocked request not added to active_map",
        FakeEvent.request_id not in agent.active_map,
    )

    # Simulate: extra_info arrives later for the SAME blocked request_id
    class FakeAssocCookie:
        def to_json(self):
            return {"cookie": {"name": "x", "value": "y"}}

    class FakeExtraEvent:
        request_id = "rid-blocked-1"
        associated_cookies = [FakeAssocCookie()]

    asyncio.run(agent.req_extra_info(FakeExtraEvent()))

    leaked = agent.orphan_extra_info.get("rid-blocked-1") is not None
    check(
        "leak: orphan_extra_info retains entries for blocked request_ids",
        leaked,
        "this is a confirmed leak — entries are never cleaned up "
        "since the request is blocked",
    )

    # ─────────────────────────────────────────────────────────────────────
    section("3. mcp_server._safe_resolve case-sensitivity (Windows)")
    # ─────────────────────────────────────────────────────────────────────
    # Windows filesystems are case-insensitive; relying on string-prefix
    # comparison after realpath can be brittle. Test we don't accept a
    # case-twisted traversal that escapes.

    from autowrec.mcp_server import _build_server

    _, _state, _safe_resolve = _build_server()

    test_workspace = tempfile.mkdtemp(prefix="autowrec_safe_")
    try:
        # Should accept a normal subpath
        ok = _safe_resolve(test_workspace, "subdir")
        check("safe path resolves to subdir", ok.lower().endswith("subdir"))

        # Should block ".." traversal
        try:
            _safe_resolve(test_workspace, "../escape")
            check("traversal '../escape' blocked", False, "should have raised")
        except ValueError:
            check("traversal '../escape' blocked", True)

        # Should block absolute paths that don't share the prefix
        try:
            absolute = "C:\\Windows" if sys.platform == "win32" else "/etc"
            _safe_resolve(test_workspace, absolute)
            check("absolute outside-workspace blocked", False, "should have raised")
        except ValueError:
            check("absolute outside-workspace blocked", True)

        # Edge case: relative path with redundant separators
        try:
            r = _safe_resolve(test_workspace, "./foo/./bar")
            check("redundant separators OK", r.endswith("bar"))
        except ValueError as e:
            check("redundant separators OK", False, str(e))

    finally:
        shutil.rmtree(test_workspace, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("4. read_transaction picks first alpha req/res file")
    # ─────────────────────────────────────────────────────────────────────
    # If two req_payload* files exist, `sorted(...)[0]` picks alphabetic
    # first. With current convention this is fine, but it's worth pinning.

    ws = tempfile.mkdtemp(prefix="autowrec_tx_")
    sd = os.path.join(ws, "session_dump")
    folder = os.path.join(sd, "requests", "000_POST_x")
    os.makedirs(folder)
    with open(os.path.join(folder, "transaction.json"), "w") as f:
        json.dump({"metadata": {"method": "POST"}}, f)
    with open(os.path.join(folder, "req_payload.json"), "w") as f:
        f.write('{"primary": true}')
    with open(os.path.join(folder, "req_payload.bin"), "wb") as f:
        f.write(b"\x00\x01")  # secondary

    _state["workspace"] = sd
    mcp, _, _ = _build_server()
    _state2 = mcp  # silence

    async def t():
        from autowrec.mcp_server import _build_server as bs
        m, st, _ = bs()
        st["workspace"] = sd
        res = await m.call_tool(
            "read_transaction",
            {"request_folder": "requests/000_POST_x", "include_request_body": True},
        )
        body = json.loads(res.content[0].text).get("request_body", "")
        return body

    body = asyncio.run(t())
    # alphabetical: 'req_payload.bin' < 'req_payload.json', so .bin is picked.
    check(
        "read_transaction picks alphabetic-first payload file",
        body == "" or "primary" not in body,
        f"got body={body!r}",
    )
    print("    NOTE: with multiple req_payload.* files, the .bin sorts before "
          ".json which may not be the user's intent. Consider preferring the "
          "extension that matches the request_detection result.")

    shutil.rmtree(ws, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("5. read_timeline: full-file load on every call")
    # ─────────────────────────────────────────────────────────────────────
    # The current implementation reads + json.loads the entire file even
    # when the caller asks for a small slice. This burns memory and CPU
    # for every page request when the timeline is large.

    big_ws = tempfile.mkdtemp(prefix="autowrec_big_")
    bs = os.path.join(big_ws, "session_dump")
    os.makedirs(bs)
    big_events = [{"timestamp": float(i), "event_type": "x", "i": i} for i in range(20_000)]
    with open(os.path.join(bs, "timeline.json"), "w") as f:
        json.dump(big_events, f)

    _state["workspace"] = bs

    async def time_pages():
        m, st, _ = _build_server()
        st["workspace"] = bs

        t0 = time.perf_counter()
        for off in range(0, 1000, 100):
            await m.call_tool("read_timeline", {"offset": off, "limit": 100})
        dt = time.perf_counter() - t0
        return dt

    dt = asyncio.run(time_pages())
    print(f"    10 paginated calls over 20k-event timeline: {dt*1000:.1f} ms")
    check("paginated reads complete", dt < 30.0, f"took {dt:.2f}s")
    print("    PERF: every read_timeline call re-parses the entire timeline.json."
          " Suggest caching the parsed list keyed by mtime, OR keeping an offset"
          " index file (timeline.idx) for O(1) page seeks.")

    shutil.rmtree(big_ws, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("6. extract_video_frames duration math")
    # ─────────────────────────────────────────────────────────────────────
    # When duration is very small (< num_frames * 0.1), all timestamps
    # collapse to the same value, so the host AI sees identical frames.
    # Probe this without actually invoking ffmpeg.

    duration = 0.05
    num_frames = 4
    step = duration / num_frames
    timestamps = [
        max(0, min(duration - 0.1, step * i + step / 2))
        for i in range(num_frames)
    ]
    distinct = len(set(round(t, 4) for t in timestamps))
    check(
        "extract_video_frames: distinct timestamps for tiny clips",
        distinct > 1,
        f"all {num_frames} frames at the same time {timestamps[0]} for "
        f"{duration}s clip — host AI gets duplicate frames",
    )

    # ─────────────────────────────────────────────────────────────────────
    section("7. compile_workspace leaves staging dir on failure")
    # ─────────────────────────────────────────────────────────────────────
    # If an exception occurs after STAGING_DIR is created but before the
    # rename, the staging dir is orphaned. Verify by injecting a failure.

    from autowrec import config as cfg
    from autowrec.recorder import data_compressor as dc

    saved_output = cfg.OUTPUT_DIR
    saved_ws = cfg.WORKSPACE_DIR
    test_out = Path(tempfile.mkdtemp(prefix="autowrec_compile_"))
    cfg.OUTPUT_DIR = test_out
    cfg.WORKSPACE_DIR = test_out / "workspace"
    cfg.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    # Patch process_network_requests to raise mid-compile
    saved_proc = dc.process_network_requests
    def boom(*a, **k):
        raise RuntimeError("simulated mid-compile failure")
    dc.process_network_requests = boom

    try:
        ok = dc.compile_workspace(
            session_data={"metadata": {}, "actions": [], "requests": [{"url": "https://x", "method": "GET"}]},
            full_video_path=None,
            video_start_unix=None,
        )
        check("compile failure returns False", ok is False)

        staging = str(cfg.WORKSPACE_DIR / "session_dump_new")
        check(
            "staging dir orphaned on failure",
            os.path.exists(staging),
            "this is the leak — staging dir is not cleaned up",
        )
    finally:
        dc.process_network_requests = saved_proc
        cfg.OUTPUT_DIR = saved_output
        cfg.WORKSPACE_DIR = saved_ws
        shutil.rmtree(test_out, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("8. record_session re-entry while previous still alive")
    # ─────────────────────────────────────────────────────────────────────
    # Verify that a second record_session call while a recording is alive
    # is rejected (per documented behavior).

    async def reentry_test():
        from autowrec.mcp_server import _build_server as bs2
        m, st, _ = bs2()

        live = threading.Event()
        live.clear()
        done = threading.Event()

        class FakeThread:
            def __init__(self):
                self._alive = True
            def is_alive(self):
                return self._alive

        st["recording_thread"] = FakeThread()

        out = await m.call_tool("record_session", {"url": "about:blank"})
        text = out.content[0].text
        return text

    text = asyncio.run(reentry_test())
    check(
        "second record_session is rejected while one is alive",
        "already in progress" in text,
        f"got: {text[:80]}",
    )

    # ─────────────────────────────────────────────────────────────────────
    section("9. Failed record_session clears workspace pointer (post-C2 fix)")
    # ─────────────────────────────────────────────────────────────────────
    # Real flow: record_session() must invalidate the cached workspace at
    # call time. If the new recording fails, read_* tools must surface the
    # error rather than silently returning the prior session.

    async def stale_test():
        from autowrec.mcp_server import _build_server as bs2
        m, st, _ = bs2()

        # Pretend a prior record_session succeeded.
        old = tempfile.mkdtemp(prefix="autowrec_stale_old_")
        os.makedirs(os.path.join(old, "dummy"), exist_ok=True)
        st["workspace"] = old

        # Patch run_recording to simulate a failure on the next attempt.
        from autowrec import mcp_server as mcp_mod
        saved = mcp_mod._build_server  # noqa: F841 (kept for ref)

        # Drive record_session through its real path with a stub thread.
        # Easier: directly emulate the post-call state record_session
        # leaves behind: workspace cleared, error set.
        await m.call_tool("record_session", {"url": "about:blank"})
        # Replace the live thread with a finished one carrying the failure
        class FinishedThread:
            def is_alive(self):
                return False
        st["recording_thread"] = FinishedThread()
        st["recording_error"] = "boom"
        # record_session itself cleared workspace=None. Verify that.
        workspace_cleared = st.get("workspace") in (None, "")

        # Reading session_summary now should raise with the error message.
        cr = (await m.call_tool("check_recording", {})).content[0].text
        try:
            res = await m.call_tool("read_session_summary", {})
            text = res.content[0].text
            surfaces_error = ("boom" in text) or ("failed" in text.lower())
        except Exception as exc:
            surfaces_error = ("boom" in str(exc)) or ("failed" in str(exc).lower())

        shutil.rmtree(old, ignore_errors=True)
        return workspace_cleared, cr, surfaces_error

    cleared, cr, surfaces_error = asyncio.run(stale_test())
    check(
        "C2: record_session clears st['workspace'] at call time",
        cleared,
        f"workspace not cleared (still: {cr[:80]!r})",
    )
    check(
        "C2: check_recording surfaces 'boom' error",
        "boom" in cr.lower() or "error" in cr.lower(),
        f"got: {cr[:80]}",
    )
    check(
        "C2: read_session_summary refuses stale workspace, surfaces error",
        surfaces_error,
        "should raise/return error referencing the failed recording",
    )

    # ─────────────────────────────────────────────────────────────────────
    section("10. make_serializable on tuples / sets")
    # ─────────────────────────────────────────────────────────────────────
    from autowrec.recorder.data_compressor import make_serializable

    out = make_serializable((1, 2, 3))
    check(
        "tuple becomes string repr (lossy)",
        isinstance(out, str) and out == "(1, 2, 3)",
        f"got {out!r}",
    )
    out2 = make_serializable({1, 2, 3})
    check(
        "set becomes string repr (lossy)",
        isinstance(out2, str),
        f"got {out2!r}",
    )
    print("    NOTE: tuples and sets fall through to str(obj). If CDP ever "
          "returns these, they round-trip as opaque strings. Consider casting "
          "tuple→list explicitly.")

    # ─────────────────────────────────────────────────────────────────────
    section("11. read_file with offset > size")
    # ─────────────────────────────────────────────────────────────────────
    rf = tempfile.mkdtemp(prefix="autowrec_rf_")
    sd = os.path.join(rf, "session_dump")
    os.makedirs(sd)
    with open(os.path.join(sd, "small.txt"), "w") as f:
        f.write("abcd")  # 4 bytes

    async def rf_test():
        m, st, _ = _build_server()
        st["workspace"] = sd
        res = await m.call_tool("read_file", {"path": "small.txt", "offset": 9999})
        return json.loads(res.content[0].text)

    obj = asyncio.run(rf_test())
    check("read_file beyond EOF returns 0 bytes",
          obj["bytes_read"] == 0 and obj["has_more"] is False,
          f"got {obj}")
    shutil.rmtree(rf, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("12. format_output truncation pointer")
    # ─────────────────────────────────────────────────────────────────────
    from autowrec.ipython_sandbox.utils import format_output

    big = "\n".join(f"line {i:05d}" for i in range(2000))
    out = format_output(big, "Cell_X")
    check(
        "format_output truncates over MAX_BYTES (10KB)",
        "TRUNCATED" in out,
    )
    check(
        "format_output emits %view_output hint",
        "%view_output" in out,
    )

    # ─────────────────────────────────────────────────────────────────────
    section("13. blocklist DB: is_blocked() with empty hostname")
    # ─────────────────────────────────────────────────────────────────────
    from autowrec.recorder.blocklist_db import BlocklistDB, _reverse_domain

    db = BlocklistDB(":memory:")
    check("empty hostname → not blocked", db.is_blocked("") is False)
    check("dot-only hostname → not blocked", db.is_blocked(".") is False)
    check("reverse 'sub.empty..com' filters empties",
          _reverse_domain("sub.empty..com") == "com.empty.sub")
    db.close()

    # ─────────────────────────────────────────────────────────────────────
    section("14. console redirect leaks in MCP run path")
    # ─────────────────────────────────────────────────────────────────────
    # mcp_server.run_mcp_server() wraps stderr in TextIOWrapper but never
    # closes it. Verify that the wrapper at least redirects bin_manager output.
    from autowrec import bin_manager as bm
    from autowrec import console as console_mod
    from rich.console import Console as RichConsole

    saved = console_mod.console
    sink = io.StringIO()
    console_mod.console = RichConsole(theme=console_mod._theme, highlight=False, file=sink)
    try:
        bm.warn("debug-warn-marker")
        check("bin_manager output reaches replaced console",
              "debug-warn-marker" in sink.getvalue())
    finally:
        console_mod.console = saved

    # ─────────────────────────────────────────────────────────────────────
    section("15. config TOML loader reverts OUTPUT_DIR if value bad")
    # ─────────────────────────────────────────────────────────────────────
    # Existing tests cover bad scalars; this checks empty-string output.dir.
    from autowrec import config as cfg2

    badd = tempfile.mkdtemp(prefix="autowrec_emptyd_")
    badf = os.path.join(badd, "config.toml")
    with open(badf, "w") as f:
        f.write('[output]\ndir = ""\n')

    saved_cf = cfg2.CONFIG_FILE
    saved_od = cfg2.OUTPUT_DIR
    try:
        cfg2.CONFIG_FILE = Path(badf)
        cfg2._load_config_toml()
        # Empty string resolves to cwd via Path("").resolve() — that's a leak
        leaked = cfg2.OUTPUT_DIR == Path("").resolve()
        check(
            "BUG: empty output.dir resolves to CWD silently",
            leaked,
            f"OUTPUT_DIR resolved to {cfg2.OUTPUT_DIR}, "
            f"effectively pointing at the user's working directory",
        )
    finally:
        cfg2.CONFIG_FILE = saved_cf
        cfg2.OUTPUT_DIR = saved_od
        shutil.rmtree(badd, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    print()
    print(f"  Passed: {PASS}")
    print(f"  Failed: {FAIL}")
    print(f"  Skipped: {SKIP}")
    print(f"  Total:  {PASS + FAIL + SKIP}")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
