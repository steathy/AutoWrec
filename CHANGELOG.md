# Changelog

All notable changes to AutoWrec are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

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
