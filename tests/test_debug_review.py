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
    section("2. orphan_extra_info short-circuits for blocked IDs (post-B1)")
    # ─────────────────────────────────────────────────────────────────────
    # After B1, request_handler marks blocked / data: request_ids in a
    # bounded LRU and req/res_extra_info short-circuit so the orphan map
    # never grows for skipped requests.

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

    asyncio.run(agent.request_handler(FakeEvent()))
    check(
        "B1: blocked request not added to active_map",
        FakeEvent.request_id not in agent.active_map,
    )
    check(
        "B1: blocked request_id recorded in skipped LRU",
        "rid-blocked-1" in agent._skipped_ids,
    )

    class FakeAssocCookie:
        def to_json(self):
            return {"cookie": {"name": "x", "value": "y"}}

    class FakeExtraEvent:
        request_id = "rid-blocked-1"
        associated_cookies = [FakeAssocCookie()]

    asyncio.run(agent.req_extra_info(FakeExtraEvent()))

    check(
        "B1: extra_info for blocked id does NOT enter orphan_extra_info",
        agent.orphan_extra_info.get("rid-blocked-1") is None,
    )

    # Bound check — flood with 10k blocked IDs and confirm LRU caps.
    class _Burst:
        def __init__(self, i):
            self.request_id = f"flood-{i}"
            self.request = FakeReq()
            self.type_ = "Other"
            self.timestamp = 0.0
            self.wall_time = None
            self.redirect_response = None

    for i in range(10_000):
        ev = _Burst(i)
        ev.request = FakeReq()
        ev.request.url = "https://ads.example.com/x"
        asyncio.run(agent.request_handler(ev))
    check(
        "B1: skipped LRU stays bounded under flood",
        len(agent._skipped_ids) <= agent._SKIPPED_LRU_MAX,
        f"got {len(agent._skipped_ids)}",
    )

    # Sanity follow-up: a blocked URL redirecting to an allowed URL must
    # un-mark the request_id so its extra_info isn't dropped.
    agent2 = BrowserAgent(blocklist=FakeBlocklist())

    class FakeReqAds:
        url = "https://ads.example.com/x"
        method = "GET"
        headers = {}
        post_data = None

    class FakeReqSafe:
        url = "https://safe.example.com/y"
        method = "GET"
        headers = {}
        post_data = None

    class FakeBlockedEvent:
        request_id = "rid-redirect"
        request = FakeReqAds()
        type_ = "Other"
        timestamp = 0.0
        wall_time = None
        redirect_response = None

    class FakeRedirectedEvent:
        request_id = "rid-redirect"
        request = FakeReqSafe()
        type_ = "Other"
        timestamp = 0.0
        wall_time = None
        redirect_response = None  # in real life would carry the 302

    asyncio.run(agent2.request_handler(FakeBlockedEvent()))
    asyncio.run(agent2.request_handler(FakeRedirectedEvent()))

    check(
        "B1: redirected-from-blocked URL clears the skip mark",
        "rid-redirect" not in agent2._skipped_ids
        and "rid-redirect" in agent2.active_map,
    )

    class FakeAssoc2:
        def to_json(self):
            return {"cookie": {"name": "after_redirect", "value": "ok"}}

    class FakeRedirectExtra:
        request_id = "rid-redirect"
        associated_cookies = [FakeAssoc2()]

    asyncio.run(agent2.req_extra_info(FakeRedirectExtra()))
    cookies = agent2.active_map["rid-redirect"]["cookies_sent_details"]
    check(
        "B1: extra_info for redirected request is captured (not dropped)",
        any(c.get("cookie", {}).get("name") == "after_redirect" for c in cookies),
        f"got cookies={cookies!r}",
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
    section("4. read_transaction surfaces detection.extension (post-B7+M3)")
    # ─────────────────────────────────────────────────────────────────────
    # Post-M3 read_transaction no longer inlines bodies. We verify the
    # 'full' level surfaces content_detection.extension so the host AI can
    # pick the right body file when calling read_file.

    ws = tempfile.mkdtemp(prefix="autowrec_tx_")
    sd = os.path.join(ws, "session_dump")
    folder = os.path.join(sd, "requests", "000_POST_x")
    os.makedirs(folder)
    with open(os.path.join(folder, "transaction.json"), "w") as f:
        json.dump({
            "metadata": {"method": "POST"},
            "request": {"content_detection": {"extension": "json"}, "has_payload": True},
            "response": {},
        }, f)
    with open(os.path.join(folder, "req_payload.json"), "w") as f:
        f.write('{"primary": true}')
    with open(os.path.join(folder, "req_payload.bin"), "wb") as f:
        f.write(b"\x00\x01")  # alphabetically first, but wrong extension

    async def t():
        from autowrec.mcp_server import _build_server as bs
        m, st, _ = bs()
        st["workspace"] = sd
        res = await m.call_tool(
            "read_transaction",
            {"request_folder": "requests/000_POST_x", "level": "full"},
        )
        full = json.loads(res.content[0].text)
        # AI then calls read_file on the matching file
        ext = (full.get("request") or {}).get("content_detection", {}).get("extension")
        body_res = await m.call_tool(
            "read_file",
            {"path": f"requests/000_POST_x/req_payload.{ext}", "mode": "raw"},
        )
        return ext, json.loads(body_res.content[0].text).get("content", "")

    ext, body = asyncio.run(t())
    check(
        "B7+M3: full transaction surfaces detection.extension='json'",
        ext == "json",
        f"got ext={ext!r}",
    )
    check(
        "M4: AI uses read_file to fetch the JSON body matching that extension",
        '"primary"' in body,
        f"got body={body!r}",
    )

    shutil.rmtree(ws, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("5. read_timeline cache + summary mode (post-M2)")
    # ─────────────────────────────────────────────────────────────────────
    # After M2, paginated calls should reuse a cached parsed list (keyed by
    # mtime + size). Default summary=True compresses each event.

    big_ws = tempfile.mkdtemp(prefix="autowrec_big_")
    bs = os.path.join(big_ws, "session_dump")
    os.makedirs(bs)
    # Mirror what compile_workspace actually produces: user_action entries
    # carry the full set of telemetry fields (locators dict, click attributes,
    # iframe flag, etc.). Network entries are already compact in the real flow.
    big_events = []
    for i in range(20_000):
        if i % 3 == 0:
            big_events.append({
                "timestamp": float(i),
                "timestamp_iso": f"2026-05-03T00:00:{i % 60:02d}",
                "event_type": "user_action",
                "action": "click",
                "details": {
                    "tag": "BUTTON",
                    "text": f"Submit_{i}",
                    "id": f"btn-{i}",
                    "href": "",
                    "role": "button",
                    "ariaLabel": f"Submit form {i}",
                    "ariaChecked": "",
                    "dataValue": "",
                    "inputType": "",
                    "is_iframe": False,
                    "url": f"https://example.com/page/{i}",
                    "title": f"Page {i} | Example.com",
                    "execution_context_id": 12 + (i % 4),
                    "locators": {
                        "css": f"html > body > div:nth-of-type(2) > main > form#login-form > div.controls > button#submit-{i}",
                        "aria": f"Submit form {i}[role=\"button\"]",
                        "text": f"Submit_{i}",
                    },
                },
                "ai_macro_summary": f"User clicked the Submit button on page {i}",
                "ai_elements_interacted": [{"type": "button", "id": f"btn-{i}"}],
                "ai_action_success": True,
            })
        else:
            big_events.append({
                "timestamp": float(i),
                "timestamp_iso": f"2026-05-03T00:00:{i % 60:02d}",
                "event_type": "network_request",
                "method": "GET",
                "url": f"https://x.example.com/p/{i}",
                "status": 200,
                "folder": f"requests/{i:05d}_GET_x.example.com",
            })
    with open(os.path.join(bs, "timeline.json"), "w") as f:
        json.dump(big_events, f)

    async def time_pages():
        m, st, _ = _build_server()
        st["workspace"] = bs

        # First call populates the cache
        t0 = time.perf_counter()
        first = await m.call_tool("read_timeline", {"offset": 0, "limit": 100})
        dt_cold = time.perf_counter() - t0

        # Subsequent paginated calls should hit the cache
        t0 = time.perf_counter()
        for off in range(100, 1100, 100):
            await m.call_tool("read_timeline", {"offset": off, "limit": 100})
        dt_warm = time.perf_counter() - t0

        return dt_cold, dt_warm, first.content[0].text

    dt_cold, dt_warm, sample = asyncio.run(time_pages())
    print(f"    cold call: {dt_cold*1000:.1f} ms; 10 warm pages: {dt_warm*1000:.1f} ms")
    check(
        "M2: warm-page latency < cold-call latency (cache effective)",
        dt_warm < dt_cold * 5,  # 10 warm pages should be < 5x a single cold call
        f"cold={dt_cold*1000:.1f}ms, warm={dt_warm*1000:.1f}ms",
    )

    sample_obj = json.loads(sample)
    first_event = sample_obj["events"][0] if sample_obj["events"] else {}
    check(
        "M2: summary mode compacts event payloads (no raw 'details' dict)",
        "details" not in first_event,
        f"got fields={list(first_event.keys())}",
    )
    check(
        "M2: summary mode uses compact 'ts'/'type' keys",
        "ts" in first_event and "type" in first_event,
        f"got fields={list(first_event.keys())}",
    )

    # 100-event slice in summary mode should be substantially smaller than full
    async def compare_modes():
        m, st, _ = _build_server()
        st["workspace"] = bs
        s_lean = (await m.call_tool(
            "read_timeline", {"offset": 0, "limit": 100, "summary": True}
        )).content[0].text
        s_full = (await m.call_tool(
            "read_timeline", {"offset": 0, "limit": 100, "summary": False}
        )).content[0].text
        return len(s_lean), len(s_full)

    lean_size, full_size = asyncio.run(compare_modes())
    print(f"    summary=True 100 events: {lean_size:,} B; summary=False: {full_size:,} B")
    check(
        "M2: summary mode response is <= 50% of full mode",
        lean_size * 2 <= full_size,
        f"lean={lean_size}, full={full_size}",
    )

    shutil.rmtree(big_ws, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("6. extract_video_frames duration math (post-B2)")
    # ─────────────────────────────────────────────────────────────────────
    # After B2: very short clips degrade to a single mid-clip sample, and
    # longer clips spread N timestamps inside a small end_pad.

    def _b2_timestamps(duration, num_frames):
        if duration < 0.5 or num_frames == 1:
            return [duration / 2]
        end_pad = min(0.05, duration * 0.02)
        span = max(duration - 2 * end_pad, 0.001)
        return [end_pad + span * (i + 0.5) / num_frames for i in range(num_frames)]

    tiny = _b2_timestamps(0.05, 4)
    check(
        "B2: tiny clip degrades to one frame",
        len(tiny) == 1,
        f"got {tiny}",
    )

    normal = _b2_timestamps(10.0, 4)
    distinct = len(set(round(t, 4) for t in normal))
    check(
        "B2: normal clip has all distinct timestamps",
        distinct == 4,
        f"got {normal}",
    )
    check(
        "B2: normal-clip timestamps stay strictly inside [0, duration]",
        all(0 < t < 10.0 for t in normal),
        f"got {normal}",
    )

    # ─────────────────────────────────────────────────────────────────────
    section("7. compile_workspace cleans staging on failure (post-B4)")
    # ─────────────────────────────────────────────────────────────────────
    # After B4, a mid-compile exception removes session_dump_new/.

    from autowrec import config as cfg
    from autowrec.recorder import data_compressor as dc

    saved_output = cfg.OUTPUT_DIR
    saved_ws = cfg.WORKSPACE_DIR
    test_out = Path(tempfile.mkdtemp(prefix="autowrec_compile_"))
    cfg.OUTPUT_DIR = test_out
    cfg.WORKSPACE_DIR = test_out / "workspace"
    cfg.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

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
        check("B4: compile failure returns False", ok is False)

        staging = str(cfg.WORKSPACE_DIR / "session_dump_new")
        check(
            "B4: staging dir cleaned on failure",
            not os.path.exists(staging),
            f"staging dir still present at {staging}",
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
    section("10. make_serializable on tuples / sets (post-B10)")
    # ─────────────────────────────────────────────────────────────────────
    from autowrec.recorder.data_compressor import make_serializable

    out = make_serializable((1, 2, 3))
    check(
        "B10: tuple becomes a JSON list",
        isinstance(out, list) and out == [1, 2, 3],
        f"got {out!r}",
    )
    out2 = make_serializable({1, 2, 3})
    check(
        "B10: set becomes a JSON list",
        isinstance(out2, list) and sorted(out2) == [1, 2, 3],
        f"got {out2!r}",
    )
    out3 = make_serializable({"a": (1, 2), "b": {3, 4}})
    check(
        "B10: nested tuple/set inside dict serializes",
        isinstance(out3, dict)
        and isinstance(out3["a"], list) and out3["a"] == [1, 2]
        and isinstance(out3["b"], list) and sorted(out3["b"]) == [3, 4],
        f"got {out3!r}",
    )

    # ─────────────────────────────────────────────────────────────────────
    section("11. read_file raw mode with offset > size (post-M5)")
    # ─────────────────────────────────────────────────────────────────────
    # offset only applies in raw mode; head mode ignores offset.
    rf = tempfile.mkdtemp(prefix="autowrec_rf_")
    sd = os.path.join(rf, "session_dump")
    os.makedirs(sd)
    with open(os.path.join(sd, "small.txt"), "w") as f:
        f.write("abcd")  # 4 bytes

    async def rf_test():
        m, st, _ = _build_server()
        st["workspace"] = sd
        res = await m.call_tool(
            "read_file", {"path": "small.txt", "mode": "raw", "offset": 9999}
        )
        return json.loads(res.content[0].text)

    obj = asyncio.run(rf_test())
    check("read_file raw mode beyond EOF returns 0 bytes",
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
    section("15. config TOML loader rejects empty output.dir (post-B5)")
    # ─────────────────────────────────────────────────────────────────────
    from autowrec import config as cfg2

    badd = tempfile.mkdtemp(prefix="autowrec_emptyd_")
    badf = os.path.join(badd, "config.toml")
    with open(badf, "w") as f:
        f.write('[output]\ndir = ""\n')

    saved_cf = cfg2.CONFIG_FILE
    saved_od = cfg2.OUTPUT_DIR
    sentinel = saved_od  # value before loading the bad TOML
    try:
        cfg2.CONFIG_FILE = Path(badf)
        cfg2._load_config_toml()
        check(
            "B5: empty output.dir leaves OUTPUT_DIR untouched",
            cfg2.OUTPUT_DIR == sentinel,
            f"got {cfg2.OUTPUT_DIR}",
        )
    finally:
        cfg2.CONFIG_FILE = saved_cf
        cfg2.OUTPUT_DIR = saved_od
        shutil.rmtree(badd, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("M. MCP token-budget acceptance (post-M1/M3/M4/M5/M6/M8)")
    # ─────────────────────────────────────────────────────────────────────
    # Build a fixture session with ~200 transactions and assert each tool's
    # default response stays under the v1.3 token budget. Compare to the
    # rough v1.2 sizes for context.

    fixture_ws = tempfile.mkdtemp(prefix="autowrec_tokens_")
    fixture_sd = os.path.join(fixture_ws, "session_dump")
    os.makedirs(os.path.join(fixture_sd, "requests"))

    domains = [f"d{i}.example.com" for i in range(20)]
    summary_payload = {
        "session": {
            "duration_seconds": 312.4,
            "actionable_requests": 88,
            "redirected_requests": 12,
            "failed_requests": 3,
        },
        "session_flow": [
            {"timestamp_iso": f"2026-05-03T00:0{i}:00", "summary": f"User did action {i}"}
            for i in range(8)
        ],
        "statistics": {
            "total_requests": 220,
            "total_actions": 47,
            "domains": {d: 10 + i for i, d in enumerate(domains)},
            "status_codes": {"200": 200, "302": 12, "404": 5, "500": 3},
            "with_auth": 22,
            "with_cookies": 60,
            "methods": {"GET": 180, "POST": 30, "PUT": 5, "DELETE": 5},
        },
    }
    with open(os.path.join(fixture_sd, "SUMMARY.json"), "w", encoding="utf-8") as f:
        json.dump(summary_payload, f, indent=2)

    timeline_events = []
    for i in range(200):
        timeline_events.append({
            "timestamp": float(i),
            "timestamp_iso": f"2026-05-03T00:00:{i % 60:02d}",
            "event_type": "network_request",
            "method": "GET",
            "url": f"https://{domains[i % len(domains)]}/path/{i}",
            "status": 200,
            "folder": f"requests/{i:03d}_GET_{domains[i % len(domains)]}",
        })
    with open(os.path.join(fixture_sd, "timeline.json"), "w", encoding="utf-8") as f:
        json.dump(timeline_events, f)

    big_headers = {f"x-header-{j}": f"value-{j}" * 4 for j in range(20)}
    big_tx_folder = os.path.join(fixture_sd, "requests", "000_GET_d0.example.com")
    os.makedirs(big_tx_folder)
    with open(os.path.join(big_tx_folder, "transaction.json"), "w", encoding="utf-8") as f:
        json.dump({
            "metadata": {
                "index": 0, "method": "GET", "url": "https://d0.example.com/path/0",
                "status": 200,
                "timing": {"duration_ms": 123.45},
                "security": {"has_authorization": True, "has_proxy_authorization": False, "has_challenge": False},
            },
            "request": {
                "headers": big_headers,
                "cookies_sent": ["sid", "csrf", "lang"],
                "cookies_sent_detailed": [{"cookie": {"name": "sid", "value": "x" * 80}} for _ in range(8)],
                "content_detection": None,
                "has_payload": False,
            },
            "response": {
                "headers": big_headers,
                "cookies_set": ["server_session"],
                "cookies_set_detailed": {"blocked": [], "exempted": []},
                "content_detection": {"extension": "json", "mime_type": "application/json"},
                "has_body": True,
                "mime_mismatch": False,
            },
        }, f, indent=2)

    # 200 entries in workspace (just SUMMARY.json + timeline.json + 1 folder),
    # so simulate the realistic case by listing the requests folder which has
    # many request folders. Create 200 empty request folders.
    for i in range(1, 200):
        os.makedirs(os.path.join(fixture_sd, "requests", f"{i:03d}_GET_d{i % 20}.example.com"))

    async def run_token_tests():
        m, st, _ = _build_server()
        st["workspace"] = fixture_sd

        digest = (await m.call_tool("read_session_summary", {})).content[0].text
        check("M1: lean summary <= 500 B", len(digest) <= 500, f"got {len(digest)} B")

        verbose = (await m.call_tool(
            "read_session_summary", {"verbose": True}
        )).content[0].text
        check("M1: verbose summary still works", "statistics" in verbose)

        timeline_resp = (await m.call_tool(
            "read_timeline", {"offset": 0, "limit": 50}
        )).content[0].text
        check(
            "M2: read_timeline(summary=True) <= 8 KB for 50 events",
            len(timeline_resp) <= 8_000,
            f"got {len(timeline_resp)} B",
        )

        tx = (await m.call_tool(
            "read_transaction",
            {"request_folder": "requests/000_GET_d0.example.com"},
        )).content[0].text
        check("M3: minimal transaction <= 300 B", len(tx) <= 300, f"got {len(tx)} B")

        tx_full = (await m.call_tool(
            "read_transaction",
            {"request_folder": "requests/000_GET_d0.example.com", "level": "full"},
        )).content[0].text
        check(
            "M4: full transaction does NOT inline body content",
            '"request_body"' not in tx_full and '"response_body"' not in tx_full,
        )

        files = (await m.call_tool("list_workspace_files", {"subdirectory": "requests"})).content[0].text
        # The 200 folder names use the descriptive "<idx>_GET_<host>" form; per
        # entry that's ~30 chars of payload, so 200 entries naturally land at
        # ~10 KB even with sizes/indent stripped. The relevant comparison is
        # vs. the v1.2 path (indent=2 + sizes) which exceeds ~19 KB.
        check(
            "M8: list_workspace_files <= 10 KB for 200-entry dir (~50% of v1.2)",
            len(files) <= 10_000,
            f"got {len(files)} B",
        )
        files_payload = json.loads(files)
        check(
            "M8: default response omits 'size' field",
            all("size" not in e for e in files_payload["entries"]),
        )

        files_with_sizes = (await m.call_tool(
            "list_workspace_files", {"subdirectory": "requests", "include_sizes": True}
        )).content[0].text
        with_sizes_payload = json.loads(files_with_sizes)
        # 200 entries are dirs → no size field added. Verify the API works.
        check(
            "M8: include_sizes=true is accepted",
            len(with_sizes_payload["entries"]) == len(files_payload["entries"]),
        )

        head = (await m.call_tool(
            "read_file", {"path": "SUMMARY.json"}
        )).content[0].text
        check("M5: read_file default head mode <= 1.5 KB", len(head) <= 1500, f"got {len(head)} B")
        stat = (await m.call_tool(
            "read_file", {"path": "SUMMARY.json", "mode": "stat"}
        )).content[0].text
        check("M5: stat mode <= 200 B", len(stat) <= 200, f"got {len(stat)} B")

    asyncio.run(run_token_tests())
    shutil.rmtree(fixture_ws, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("16. Cached binary re-verification on startup (post-B3)")
    # ─────────────────────────────────────────────────────────────────────
    # If a cached rg/jq/sd has the wrong hash, _verify_existing should
    # delete it so ensure_binaries re-downloads. Test the verify step
    # directly so we don't actually hit the network.

    from autowrec import bin_manager as bm

    bin_test_dir = tempfile.mkdtemp(prefix="autowrec_bin_verify_")
    fake_bin_dir = Path(bin_test_dir)

    # rg has a hash on record for windows/amd64 — write a wrong-content
    # fake binary and check that _verify_existing removes it.
    fake = fake_bin_dir / bm._exe("rg")
    fake.write_bytes(b"not a real rg binary " * 10)
    initial_size = fake.stat().st_size

    if ("rg", "windows", "amd64") in bm._EXPECTED_HASHES and sys.platform == "win32":
        ok = bm._verify_existing(fake, "rg", "windows", "amd64")
        check(
            "B3: tampered cached rg fails re-verify",
            ok is False,
            f"verify returned {ok}",
        )
        check(
            "B3: tampered cached rg is removed after re-verify",
            not fake.exists(),
        )
    else:
        skip("B3 cached-binary recheck", "no rg hash for this platform")

    shutil.rmtree(bin_test_dir, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────
    section("P. Phase 4 perf acceptance (P1, P3, P5, P6, P7)")
    # ─────────────────────────────────────────────────────────────────────
    import inspect

    # P1: confirm we lowered the CDP buffer ceilings (source-grep is enough
    # since the values are constants — runtime CDP test would need Chrome).
    import autowrec.recorder.browser_agent as ba
    ba_src = inspect.getsource(ba)
    check(
        "P1: max_resource_buffer_size lowered to 25 MB",
        "max_resource_buffer_size=25 * 1024 * 1024" in ba_src
        and "max_resource_buffer_size=100 * 1024 * 1024" not in ba_src,
    )
    check(
        "P1: max_total_buffer_size lowered to 250 MB",
        "max_total_buffer_size=250 * 1024 * 1024" in ba_src
        and "max_total_buffer_size=1000 * 1024 * 1024" not in ba_src,
    )

    # P3: confirm video recorder writes screenshot.bgra directly.
    import autowrec.recorder.video_recorder as vr
    vr_src = inspect.getsource(vr)
    check(
        "P3: _record_loop writes screenshot.bgra (skips np.array)",
        "writer.send(screenshot.bgra)" in vr_src
        and "np.array(screenshot).tobytes()" not in vr_src,
    )

    # P5: transaction.json + timeline.json written with compact separators.
    from autowrec.recorder import data_compressor as dc_mod
    dc_src = inspect.getsource(dc_mod)
    check(
        "P5: transaction.json uses separators=(',',':')",
        'transaction.json' in dc_src and "json.dump(make_serializable(transaction_data), f, separators" in dc_src,
    )
    check(
        "P5: timeline.json uses separators=(',',':')",
        "json.dump(make_serializable(timeline_events), f, separators" in dc_src,
    )

    # P6: Magika fast-path returns the expected shape for tiny bodies.
    from autowrec.recorder.data_compressor import detect_content_type
    tiny = detect_content_type(b"abc")
    check(
        "P6: tiny body skips Magika and returns 'tiny' label",
        tiny is not None and tiny["label"] == "tiny" and tiny["extension"] == "bin",
        f"got {tiny!r}",
    )
    empty = detect_content_type(b"")
    check(
        "P6: empty body returns 'empty' label",
        empty is not None and empty["label"] == "empty",
        f"got {empty!r}",
    )
    big = detect_content_type(b"<html><body>" + b"x" * 200 + b"</body></html>")
    check(
        "P6: large body still goes through Magika",
        big is not None and big["label"] not in ("tiny", "empty"),
        f"got label={big['label'] if big else None!r}",
    )

    # P7: compress_line_horizontally bounded under pathological input.
    from autowrec.ipython_sandbox.utils import compress_line_horizontally
    t0 = time.perf_counter()
    ugly = compress_line_horizontally("a" * 50_000)
    dt = time.perf_counter() - t0
    check(
        "P7: 50k-char pathological input completes in <1s",
        dt < 1.0,
        f"took {dt:.3f}s, output_len={len(ugly)}",
    )

    # ─────────────────────────────────────────────────────────────────────
    section("S. Sandbox stale-response race + warmup (post-A+B)")
    # ─────────────────────────────────────────────────────────────────────
    # B: every response from the worker carries cell_id; the sandbox reader
    #    drops responses tagged with anything other than the cell currently
    #    awaiting an answer. Verifies the stale-PONG-eats-Cell_2 race is gone.
    # A: pre-warm thread at MCP server boot doesn't block stdio.

    from autowrec.ipython_sandbox.sandbox import AgentSandbox

    sb_dir = tempfile.mkdtemp(prefix="autowrec_stale_")
    sb = AgentSandbox(working_dir=sb_dir, timeout_seconds=15)
    # Drive a basic execute through to confirm the worker boots cleanly under
    # the new tagging protocol.
    res = sb.execute("print('first cell')")
    check("A+B: first cell on real worker runs", "first cell" in res)

    # Now simulate the buggy race: pretend a stale response with an
    # unrelated cell_id was buffered before the next execute call.
    sb.result_queue.put({
        "cell_id": "__PING__",
        "status": "success",
        "exit_code": 0,
        "ret_val": "PONG",
    })
    res2 = sb.execute("print('second cell')")
    check(
        "B: stale PONG with foreign cell_id is discarded, not returned",
        "second cell" in res2 and "PONG" not in res2,
        f"got: {res2[:120]!r}",
    )

    # And a stale message with a *past* Cell_N id (e.g. Cell_2 actually
    # arrived after we'd already gotten a Cell_2-shaped response).
    sb.result_queue.put({
        "cell_id": "Cell_99",  # not what we'll ask for
        "status": "success",
        "exit_code": 0,
        "ret_val": "STALE",
    })
    res3 = sb.execute("print('third cell')")
    check(
        "B: stale Cell_N response with non-matching id is discarded",
        "third cell" in res3 and "STALE" not in res3,
        f"got: {res3[:120]!r}",
    )

    sb.close()
    shutil.rmtree(sb_dir, ignore_errors=True)

    # A: verify the warmup hook is wired through _state and is callable.
    from autowrec.mcp_server import _build_server
    _, st_a, _ = _build_server()
    check(
        "A: _state['_get_sandbox'] exposes the sandbox factory",
        callable(st_a.get("_get_sandbox")),
    )
    check(
        "A: warmup hook does not eagerly create sandbox at build time",
        st_a.get("sandbox") is None,
    )

    # ─────────────────────────────────────────────────────────────────────
    section("17. sh.exe path independent of $PATH (post-B8)")
    # ─────────────────────────────────────────────────────────────────────
    # Check the worker derives sh_path from working_dir, not $PATH.
    # Inspecting source is enough — the live process is hard to instrument.

    import autowrec.ipython_sandbox.worker as wk_mod
    import inspect
    src = inspect.getsource(wk_mod)
    check(
        "B8: worker no longer joins os.environ['PATH'] to build sh_path",
        "os.environ[\"PATH\"]" not in src
        or "os.path.join(os.environ[\"PATH\"], \"sh.exe\")" not in src,
    )
    check(
        "B8: worker derives sh_path from working_dir/.jailed_bin",
        ".jailed_bin" in src and "sh.exe" in src,
    )

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
