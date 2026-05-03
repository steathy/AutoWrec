# Changelog

All notable changes to AutoWrec are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

---

## [Unreleased] — 1.3.0

In progress. Bug-fix and MCP-token-efficiency release driven by the v1.2
post-release code review (see `tests/test_debug_review.py` for the
regression suite that surfaced these defects).

### Fixed (Critical)

- **C1: Chrome-only video capture broken on Windows 11 24H2/25H2.**
  `_get_process_tree` shelled out to `wmic`, which Microsoft removed by
  default on recent Windows builds. The recursive subprocess calls
  silently returned only the parent PID, so `_find_chrome_window` could
  not match windows owned by Chrome's child processes and the recorder
  fell back to full-screen capture (or failed to lock at all). Replaced
  with a single PowerShell `Get-CimInstance Win32_Process` call plus a
  local BFS over the parent→children map. Also faster than the original
  recursive wmic version.
- **C2: Stale workspace pointer survived failed recordings.** When
  `record_session` was retried after a successful prior session and the
  new attempt failed, `_state["workspace"]` still held the previous
  session's path, and `read_session_summary` / `read_timeline` /
  `read_transaction` quietly returned data from the OLD recording.
  `record_session` now invalidates `_state["workspace"]` at call time;
  `_get_workspace` raises a clear "Last recording failed: ..." error if
  the most recent attempt errored without producing a workspace.

### Fixed

- **B1: Orphan-extra-info leak for blocked / data: URI requests.** When
  a request was skipped (data: URI or blocklist hit), subsequent
  `RequestWillBeSentExtraInfo` / `ResponseReceivedExtraInfo` events
  arriving for the same request_id accumulated forever in
  `orphan_extra_info` because they would never be flushed by a tracked
  request. Skipped IDs are now recorded in a 4096-entry LRU; the extra-
  info handlers short-circuit and the orphan map stays bounded under
  load (verified with a 10k-event flood test).
- **B2: `extract_video_frames` returned duplicate frames for short
  clips.** With `duration < num_frames * 0.1`, every timestamp clamped
  to 0, sending the host AI N base64 copies of the same frame. Tiny
  clips now degrade to a single mid-clip sample; longer clips spread N
  timestamps inside a small end-pad strictly within `(0, duration)`.
- **B3: Cached sandbox binaries were not re-verified on subsequent
  startups.** A binary that became corrupted or was tampered with
  after the first install would be used forever without complaint.
  `_ensure_rg/jq/sd` now re-verify `rg/jq/sd` against `_EXPECTED_HASHES`
  on every startup; busybox is matched against any known variant hash;
  mismatched files are deleted and re-downloaded.
- **B4: `compile_workspace` orphaned `session_dump_new/` on failure.**
  A mid-compile exception left the staging directory behind; across
  failed runs these accumulated. The failure path now removes the
  staging dir before returning False.
- **B5: Empty `output.dir = ""` resolved to user CWD.** Blank /
  whitespace-only strings now log a warning and keep the default
  output path; only non-empty strings are accepted.
- **B7: `read_transaction` picked the alphabetically-first body file.**
  When more than one `req_payload.*` or `res_body.*` file is present,
  the file matching `request.content_detection.extension` (or
  `response.content_detection.extension`) is preferred; alphabetic-
  first remains the fallback.
- **B8: `worker.py` derived `sh.exe` from `os.environ["PATH"]`.** This
  only worked because `apply_path_jail` overwrote PATH to a single
  entry; any future change adding a second entry would silently produce
  an invalid path. Now derived from `working_dir/.jailed_bin/sh.exe`.
- **B9:** Removed dead `old_req.pop("_meta", None)` from the redirect
  path — `_meta` was never set anywhere.
- **B10: `make_serializable` rendered tuples / sets / frozensets as
  opaque strings.** Now mapped to JSON lists alongside `list`. Future
  CDP shape changes won't silently round-trip as `"(1, 2, 3)"`.

---

## [1.2.0] - 2026-05-02

Critical MCP server fix. The `record_session` tool was completely non-functional — Chrome never launched due to a Python import-lock deadlock.

### Added

- **`check_recording` MCP tool** — polls whether a background recording is still running or has finished. Call after `record_session` to know when the workspace is ready.

### Changed

- **`record_session` is now non-blocking** — launches Chrome in a background thread and returns immediately. The AI uses `check_recording` to poll for completion instead of blocking the MCP transport.
- **Recorder import moved to server build time** — the heavy import chain (zendriver, mss, numpy, magika) now runs during `_build_server()` at MCP server startup, not inside tool functions. This prevents import-lock deadlocks when FastMCP dispatches tools via thread pool executors.
- **MCP server stderr wrapped in UTF-8** — prevents `UnicodeEncodeError` from Rich box-drawing characters on Windows legacy consoles (cp1252). Without this, `rule()` and other Rich output crashed before Chrome could launch.

### Fixed

- **Import-lock deadlock in MCP mode** — `record_session` previously used a lazy `from .recorder import run_recording` inside a background thread. This deadlocked against FastMCP's internal thread pool over Python's per-module import locks. Chrome never launched on the first call; any subsequent MCP interaction (even cancellation) unblocked it. Root cause confirmed via timestamped debug logging.
- **Rich console encoding crash on Windows** — `LegacyWindowsTerm` renderer tried to encode Unicode box-drawing characters (`─`) through cp1252, crashing `rule()` calls before `asyncio.run()` could launch Chrome.

---

## [1.1.0] - 2026-05-02

Major hardening release. 50+ fixes from 19 rounds of code review. No new features — focused entirely on correctness, robustness, and security.

### Added

- **Binary SHA-256 verification** — downloaded sandbox binaries (rg, jq, sd, busybox) are now hash-verified after download. Tampered binaries are rejected and deleted. Coverage: Windows amd64, Linux amd64.
- **Opt-in redaction** (`--redact` / `[recording] redact_sensitive = true`) — sanitizes passwords, auth headers, cookies, and structured cookie details in captures. Off by default for throwaway-account workflows.
- **Blocklist toggle** (`--no-blocklist` / `[recording] blocklist_enabled`) — disable ad/tracker filtering when full network visibility is needed.
- **MCP `execute_code` timeout clamping** — negative/zero timeouts clamped to 1 second.
- **MCP input validation** — `read_timeline`, `read_file`, `extract_video_frames` clamp offset/limit/num_frames to safe ranges.
- **Config validation** — safe type casts with warnings, `_safe_table()` for non-table TOML sections, `output.dir` type checking, range validation on all numeric config values.
- **Video startup timeout** (10s) — prevents infinite hang if no display available.
- **Chrome window resize warning** — one-shot warning when the Chrome window is resized during recording.
- **Atomic workspace compilation** — writes to staging directory, then renames. A crash mid-compile preserves the previous session.
- **Ctrl+C responsiveness** — signal handler is fast-path only; `_wait_for_pending_requests` honors cancellation.
- **83 automated tests** covering imports, config, MCP tools, path traversal, workspace operations, sandbox execution, redaction, input validation, binary body encoding, and video cleanup.

### Changed

- **MCP server startup deferred** — all heavy imports (FastMCP, Rich, config) moved inside `run_mcp_server()`. Module import dropped from ~3500ms to ~20ms, fixing uvx timeout issues.
- **`record_session` video default** now reads from `config.MCP_VIDEO_ENABLED` instead of hard-coded `False`.
- **`make_serializable`** — `bytes` values now serialize to `{"__bytes_b64__": "..."}` instead of empty string.
- **`video_start_unix`** reset on Chrome window lock-in for accurate clip alignment.
- **`stop()` always joins thread** before returning, regardless of `is_recording` state.
- **`_wait_for_pending_requests`** — reports actual elapsed time in logs, exits immediately on user cancellation.
- **Request statistics** — split into `total_requests` (all captured) and `actionable_requests` (DOCUMENT/XHR/FETCH only, counted after skip checks).
- **Redirect requests** finalized with `"redirected"` state, redirect target URL, and count in metadata.
- **`split_video`** converted to `@staticmethod` — no longer instantiates a full recorder.
- **Magika** lazy-initialized on first use instead of import time.
- **ANSI escape regex** hoisted to module-level compiled constant.
- **`asyncio.get_event_loop()`** replaced with `asyncio.get_running_loop()` (deprecation fix).
- **`--output-dir`** now calls `.resolve()` for consistent path handling.
- **Polling loop** for new tab detection replaces fixed 0.5s sleep (up to 3s with early-out).
- **JSON writes** all specify `encoding="utf-8"` explicitly.
- **`process_network_requests`** — pops both `post_data` and response body after saving (symmetrical memory recovery).
- **Blocklist download** uses explicit 30s timeout. Binary downloads use 60s timeout.
- **README** — removed fixed test counts, "sandbox" terminology replaced with "Python environment", Windows PATH jail documented.

### Fixed

- **`%restore` deadlock** — `threading.Lock()` changed to `threading.RLock()` so recursive `execute()` calls work.
- **Sandbox hard-timeout crash** — uninitialized `status`/`code_exit`/`ret_val` variables now set correctly.
- **Binary body corruption in `read_transaction`** — `errors="replace"` replaced with strict UTF-8 decode + base64 fallback.
- **Multi-tab CDP body capture** — response/body fetching now uses the correct tab session via `_request_tab` mapping.
- **Cookie value leak with `--redact`** — structured `cookies_sent_detailed`/`cookies_received_details` fields now redacted alongside headers.
- **`_request_tab` memory leak** — entries cleaned up on request finish/fail; only populated for captured (non-skipped) requests.
- **Workspace deletion scope** — only `session_dump/` deleted, not the entire workspace root.
- **Video temp file leak** — `tempfile.mkstemp()` replaces unsafe `mktemp()`; cleanup on all failure paths with warnings.
- **Invalid video promotion** — empty/sub-1KB placeholder files never moved to workspace; requires `video_start_unix` + file existence + size > 5KB + thread finalized.
- **`--no-banner` timing** — checked in `sys.argv` before banner display, not after argparse.
- **CLI `--sandbox-timeout`** clamped with `max(1, ...)`.
- **Status code 0** no longer dropped from SUMMARY statistics.
- **`event.wall_time` check** — `is not None` instead of truthy (handles legitimate 0.0).
- **Spurious `mime_mismatch`** — Magika error results excluded from both stats counter and per-transaction field.
- **Blocklist `_reverse_domain`** — filters empty domain components (`test..com` → `com.test`).
- **`_redact_headers`** — handles non-string header values via `str()` cast.
- **Duplicate SIGINT restore** removed.
- **Video recorder early-return** — clears `is_recording` in `finally` block; `start()` joins thread on timeout.
- **`_extract_binary_from_archive`** — fixed false match on filenames ending with the binary name (e.g., `_sd` matching `sd`).

### Removed

- Stale "sandbox" / "isolated" terminology from user-facing text.
- Dead Esc-listener code (leftover from deleted AI analysis).
- Fixed temp video filename (`autowrec_full_record.mp4`).

---

## [1.0.0] - 2026-04-29

Initial release. Browser session recorder with MCP server integration.

### Added

- Chrome CDP browser instrumentation via zendriver.
- Network traffic capture (headers, cookies, bodies, timing).
- User action tracking (clicks, typing, navigation) via injected telemetry.
- Optional Chrome-only video recording (Windows) with PID-based window targeting.
- 8 MCP tools: `record_session`, `read_session_summary`, `read_timeline`, `read_transaction`, `list_workspace_files`, `read_file`, `extract_video_frames`, `execute_code`.
- Persistent IPython execution environment with PATH-jailed binaries.
- SQLite-backed ad/tracker domain blocklist with LRU cache.
- CLI with `record` and `mcp` subcommands.
- FastMCP server over stdio transport.
- Path traversal protection on all file access tools.
- Stderr-safe Rich console output in MCP mode.
