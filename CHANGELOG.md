# Changelog

All notable changes to AutoWrec are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

---

## [1.4.0] — 2026-05-12

### Removed

- **Video recording removed entirely.** Screen capture, FFmpeg clip splitting,
  and the `extract_video_frames` MCP tool have been removed. AutoWrec now
  captures network traffic and user actions only.
- **`enable_video` parameter removed** from `record_session` tool and
  `run_recording()` function.
- **`MCP_VIDEO_ENABLED`**, `FPS`, `SEGMENT_PAD_SECONDS`,
  `MERGE_GAP_THRESHOLD_SECONDS` config constants removed.
- **`[mcp]` config section removed** (its only setting was `video_enabled`).
- **`video_recorder.py` deleted** — 384 lines including `ActionVideoRecorder`
  (with `split_video`, `start`, `stop`, `_record_loop`), plus module-level
  helpers `_find_chrome_window`, `_get_window_rect`, `_get_process_tree`.
- **Dependencies removed:** `mss`, `numpy`, `imageio-ffmpeg`.
- **`video()` log function** removed from console.py.
- **Workspace output changes:** `full_record.mp4` and `clips/` directory no
  longer produced. Timeline events no longer carry `ai_video_file`,
  `video_start_sec`, `video_end_sec` fields.

### Changed

- MCP tool count: **9 → 8** (extract_video_frames removed).
- `compile_workspace()` signature simplified (no video params).
- `data_compressor.merge_and_annotate_actions()` replaced with `_sort_actions()`.

### Notes

- Existing `~/.autowrec/config.toml` files with a `[mcp]` section are harmlessly
  ignored — the TOML loader silently skips unknown sections.
- The `v1.3.2` tag preserves the last video-capable release for anyone who needs it.

## [1.3.2] — 2026-05-04

Two follow-ups surfaced by an out-of-band code review pass against the
v1.3.1 sandbox + browser agent. Both are real defects v1.3.1 missed.

### Fixed (Critical)

- **Sandbox queue could hang indefinitely when the worker died abruptly.**
  When user code called `os._exit()` or triggered a C-extension
  segfault, `multiprocessing.Queue.get(timeout=...)` stopped honoring
  its timeout on Windows — observed wall-clock `sandbox.execute(...)`
  hang of 80+ seconds despite a 5 s configured timeout. This was a
  session-killer for any AI tool that triggered such a crash.

  Fix: `_read_for_cell` now polls the result queue in 100 ms slices.
  Each Empty-on-slice triggers a `process.is_alive()` check; if the
  worker has died, the read raises `queue.Empty` immediately so the
  existing hard-kill + restart path can recover. The same defensive
  `is_alive()` check guards the soft-timeout `interrupt_process` call.
  Verified: a `os._exit(0)` cell now resolves in ~1.8 s with clean
  recovery on the next call.

### Fixed

- **`orphan_extra_info` could grow unbounded.** B1's `_skipped_ids`
  LRU only catches blocked / data: requests where `RequestWillBeSent`
  fired and was filtered. `RequestWillBeSentExtraInfo` events for
  request_ids that *never* fire `WillBeSent` (aborted navigations,
  browser-extension cancellations, CDP races) accumulated forever.
  `orphan_extra_info` is now a 4096-entry LRU `OrderedDict` with FIFO
  eviction, matching the `_skipped_ids` pattern.

### Changed

- **Soft-timeout log clarified.** When `_read_for_cell`'s polling
  short-circuit fires due to a dead worker (rather than the nominal
  timeout elapsing), the log now reads `"Worker died before {cell}
  could respond. Recovering..."` instead of the misleading
  `"Soft Timeout (Ns) reached..."`.

---

## [1.3.1] — 2026-05-03

Targeted follow-up to v1.3.0 addressing the "MCP needs 3 calls to wake
up" report after a `/mcp` reconnect.

### Added

- **Server version in MCP `initialize` handshake.** `FastMCP(version=…)`
  is now wired to `config.VERSION`, so MCP clients (e.g. Claude Code's
  `/mcp` listing) can show the AutoWrec version directly from the
  protocol-level `serverInfo` without us paying tool-list token
  overhead for a custom `get_version` tool.

### Fixed

- **Sandbox cold-start race that ate the AI's first `execute_code` and
  fed back a stale `PONG` on the second.** Two distinct issues
  (described in [the v1.2 review thread](#)) were combining:
  - On a slow IPython cold-start, `start_process`'s 15 s ping wait
    expired, then `_ready` was set anyway. The first `execute_code`'s
    60 s budget then had to absorb the remaining kernel-boot time.
  - The `PONG` response from that ping eventually arrived in the
    `result_queue` *after* the next `execute_code` had already sent
    its command, and was consumed as if it were that cell's answer.
    The cell's actual response sat in the queue until the cell after
    it drained the queue.

  Fix: every response from the worker now carries the `cell_id` of the
  command being answered. The sandbox reader drops responses tagged
  with anything other than the cell currently awaiting an answer, so
  late-arriving `PONG`s and other control messages can never corrupt
  cell results. (Plan ref: Fix B.)

### Changed

- **MCP server pre-warms the IPython sandbox in a daemon thread.**
  `run_mcp_server` now spawns a background thread that triggers
  `_get_sandbox()` immediately after `_build_server()` returns,
  shifting the binary-download / process-spawn / IPython-import cost
  into the dead time between MCP connect and the AI's first
  `execute_code` call. The stdio handshake is **not** blocked — the
  warmup runs in parallel with `mcp.run`, so Claude Code's MCP connect
  timeout is unaffected. Also added a lock around `_get_sandbox` so
  the warmup thread and a racing first `execute_code` don't both spawn
  worker subprocesses. (Plan ref: Fix A — async variant.)

---

## [1.3.0] — 2026-05-03

Bug-fix and MCP-token-efficiency release driven by the v1.2 post-release
code review (see `tests/test_debug_review.py` for the regression suite
that surfaced these defects). Two **breaking** MCP-tool API changes
(`read_transaction`, `read_file`); see "Changed (MCP tools — breaking)"
below for migration notes.

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

### Changed (MCP tools — breaking)

These changes shrink the per-call token footprint on the host AI by
shifting from "always-verbose" to "opt-in to verbose". Existing AI
prompts may need a one-line update.

- **M1: `read_session_summary` defaults to a ~10-line digest.** Returns
  duration, action / request counts, top 5 domains, auth-presence flag.
  Pass `verbose=true` for the full SUMMARY.json. Typical reduction:
  6 KB → ~400 B.
- **M3 + M4: `read_transaction` reshaped.** Dropped the
  `include_request_body` / `include_response_body` parameters — bodies
  are no longer inlined. New `level` parameter: `"minimal"` (default,
  ~200 B: method/url/status/timing/has_body), `"headers"` (adds req+res
  headers), `"full"` (entire transaction.json). Read body content via
  `read_file` on `request_folder + "/req_payload.<ext>"` /
  `"/res_body.<ext>"` — the extension comes from
  `request.content_detection.extension`.
- **M5: `read_file` reshaped.** New `mode` parameter:
  - `"stat"`: just `{path, size, ext}` — no content read.
  - `"head"` (default): first 1 KB; binary returns a 64-byte hex
    preview rather than a 4/3-expanded base64 blob.
  - `"raw"`: today's behavior with `offset` / `limit` for pagination.
- **M6: `extract_video_frames` defaults trimmed.** `num_frames` default
  4 → 2. New `quality` parameter (`"low"` default, `"med"`, `"high"`).
  Low quality is `scale=480:-1`, `-q:v 5` — about 8× smaller than the
  former 1280-px / `-q:v 2` output.
- **M8: `list_workspace_files` defaults trimmed.** `size` field is now
  opt-in via `include_sizes=true`. Hidden (dot-prefix) entries are
  excluded.

### Changed (MCP tools — non-breaking)

- **M2: `read_timeline` now caches the parsed list by mtime + size and
  defaults to a compact event projection.** Consecutive paginated calls
  no longer re-parse `timeline.json`. New `summary` parameter (default
  `True`) emits one of:
  - `{ts, type, method, url, status, folder}` for network events
  - `{ts, type, action, label}` for user actions
  Pass `summary=false` for the full event payload. Typical reduction:
  18 KB → ~3 KB for a 100-event page.
- **M7: Tool docstrings tightened to ≤2 sentences.** They live in the
  host AI's system prompt for the entire conversation; trimming saves
  ~1 KB of perma-context across the 9 tools.
- **M9: All tool JSON responses use `separators=(",",":")`** instead of
  `indent=2`. Same data, ~15-25% fewer tokens, no human readers in the
  loop.
- **M11: `FastMCP(instructions=...)` text trimmed** from ~70 to ~30
  words.

### Performance

- **P1: CDP network buffer ceilings reduced.** `max_resource_buffer_size`
  100 MB → 25 MB; `max_total_buffer_size` 1 GB → 250 MB. Chrome reserves
  real memory for these; the streaming-fallback path
  (`_streamed_bodies` + `getResponseBody`) covers the rare large-body
  case anyway. ~75% Chrome memory reduction for the recording session.
- **P2: New-tab discovery polling tightened.** The 30-iteration ×100 ms
  loop now check-then-sleeps with 50 ms granularity, so the common case
  (target already registered) exits with zero latency instead of the
  former 100 ms first-tick lag.
- **P3: Skipped `np.array` round-trip in the record loop.** mss's
  `ScreenShot.bgra` is already a `bytes` view in BGRA layout; sending
  it directly to the FFmpeg writer saves ~3 ms/frame at 1920×1080.
  `numpy` import removed from `video_recorder.py` (still listed in
  pyproject deps as it's used transitively by `mss`).
- **P4: HWND cached between 2-second bounds checks.** Once Chrome is
  locked, periodic position polls call `GetWindowRect(hwnd)` directly
  instead of re-running `EnumWindows` + `Get-CimInstance`. Falls back
  to a full `_find_chrome_window` re-resolve if the window is gone or
  minimized. Cuts the per-tick cost from ~150–300 ms down to ~0.1 ms
  on a busy session.
- **P5: `transaction.json` and `timeline.json` written compact.**
  Dropped `indent=2` from the per-request transaction file (machine-
  only) and the timeline (machine-only). `SUMMARY.json` and
  `session_metadata.json` remain pretty-printed for human glance.
  ~25% smaller workspace on disk.
- **P6: Magika fast-path for tiny bodies.** Bodies <16 bytes return a
  pre-shaped `{label: "tiny"|"empty", extension: "bin", ...}` dict
  without invoking the model. For sessions with hundreds of small XHR
  responses (heartbeats, ping endpoints), this is a sizeable cumulative
  win during workspace compilation.
- **P7: Bounded `compress_line_horizontally` iterations.** Pathological
  inputs (e.g. 1 MB of `"a"`) could otherwise loop quadratically and
  stall the IPython worker. Capped at 5 passes — enough for any
  realistic pattern overlap.

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
