import asyncio
import base64
import json
import os
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime

import zendriver as zd
from zendriver import cdp

from ..console import action as log_action
from ..console import error, info, log_exception, print_exception, warn
from .blocklist_db import BlocklistDB


class TimestampConverter:
    """Converts CDP MonotonicTime to human-readable ISO 8601 timestamps."""

    def __init__(self):
        self.monotonic_to_wall_offset: float | None = None
        self.offsets_collected = []

    def calibrate(self, monotonic_time: float, wall_time: float) -> None:
        if len(self.offsets_collected) >= 5:
            return
        offset = wall_time - monotonic_time
        self.offsets_collected.append(offset)
        self.monotonic_to_wall_offset = sum(self.offsets_collected) / len(self.offsets_collected)

    def to_unix_timestamp(self, monotonic_time: float) -> float:
        if self.monotonic_to_wall_offset is None:
            self.monotonic_to_wall_offset = time.time() - monotonic_time
        return monotonic_time + self.monotonic_to_wall_offset

    def to_iso8601(self, monotonic_time: float) -> str:
        unix_timestamp = self.to_unix_timestamp(monotonic_time)
        dt = datetime.fromtimestamp(unix_timestamp, tz=UTC)
        return dt.isoformat(timespec="milliseconds")

    def current_iso8601(self) -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")


class BrowserAgent:
    """Manages the headless/UI browser session, CDP event handlers, and data collection."""

    def __init__(self, telemetry_js_path=None, blocklist: BlocklistDB | None = None, proxy_url: str | None = None, chrome_path: str | None = None):
        _js_dir = os.path.join(os.path.dirname(__file__), "js")
        self.telemetry_js_path = telemetry_js_path or os.path.join(_js_dir, "telemetry.js")
        self.blocklist = blocklist
        self.proxy_url = proxy_url
        self._proxy_creds: tuple[str, str] | None = None
        self._proxy_ext_dir = None
        self.chrome_path = chrome_path
        if proxy_url:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(proxy_url)
            if parsed.scheme not in ("http", "https", "socks5", "socks4"):
                warn(f"Unsupported proxy scheme {parsed.scheme!r} — ignoring proxy. Use http://, https://, socks4://, or socks5://")
                self.proxy_url = None
            elif not parsed.hostname:
                warn(f"Invalid proxy URL (no hostname) — ignoring proxy")
                self.proxy_url = None
            else:
                try:
                    _ = parsed.port
                except ValueError:
                    warn(f"Invalid proxy port — ignoring proxy")
                    self.proxy_url = None
            if self.proxy_url and parsed.username:
                if parsed.scheme.lower().startswith("http"):
                    self._proxy_creds = (unquote(parsed.username), unquote(parsed.password or ""))
                else:
                    warn(f"Proxy credentials ignored — {parsed.scheme} does not support extension-based auth. Use IP whitelisting.")
        self.recording_active = False
        self.browser = None
        self.tab = None
        self.recording_start = None

        self.ts_converter = TimestampConverter()

        self.captured_requests = []
        self.captured_actions = []
        self.active_map = {}
        self.orphan_extra_info: OrderedDict[str, dict] = OrderedDict()
        self._ORPHAN_LRU_MAX = 4096
        self._streamed_bodies: dict[str, list[bytes]] = {}  # request_id -> list of raw chunks
        self._request_tab: dict = {}  # request_id -> tab session for correct CDP calls
        # Bounded LRU of request_ids we've decided to skip (data: URIs, blocklist
        # hits). Lets req_extra_info / res_extra_info short-circuit so they don't
        # accumulate orphaned entries for requests that will never be tracked.
        self._skipped_ids: OrderedDict[str, None] = OrderedDict()
        self._SKIPPED_LRU_MAX = 4096
        self.stats = {
            "total_requests": 0,
            "actionable_requests": 0,
            "completed": 0,
            "failed": 0,
            "incomplete": 0,
            "body_success": 0,
            "body_failed": 0,
            "body_skip_no_content": 0,
            "body_skip_redirect": 0,
            "body_skip_cached": 0,
            "body_from_stream": 0,
            "blocked_by_blocklist": 0,
            "redirected": 0,
        }

        self.telemetry_script = ""

    def _mark_skipped(self, request_id) -> None:
        """Record that *request_id* was filtered out, and drop any orphaned
        extra-info we might have already buffered for it."""
        rid = str(request_id)
        self._skipped_ids[rid] = None
        if len(self._skipped_ids) > self._SKIPPED_LRU_MAX:
            self._skipped_ids.popitem(last=False)
        self.orphan_extra_info.pop(request_id, None)

    @staticmethod
    def _proxy_netloc(parsed) -> str:
        """Format hostname:port, re-wrapping IPv6 brackets that urlparse strips."""
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{parsed.port}" if parsed.port else host

    def _load_scripts(self) -> bool:
        """Loads the injected JavaScript files from disk."""
        try:
            with open(self.telemetry_js_path, encoding="utf-8") as f:
                self.telemetry_script = f.read()
            return True
        except FileNotFoundError as e:
            error(f"Missing JS dependencies: {e}")
            return False

    @staticmethod
    def merge_headers(req, extra_headers):
        if not extra_headers:
            return
        if not req["response_data"]:
            req["response_data"] = {
                "status": 0,
                "headers": {},
                "body": None,
                "base64_encoded": False,
                "charset": "utf-8",
            }
        current = req["response_data"]["headers"]
        for k, v in extra_headers.items():
            current[k] = v

    async def binding_handler(self, event: cdp.runtime.BindingCalled):
        if event.name == "sendActionToPython":
            try:
                payload = json.loads(event.payload)
                payload["timestamp_iso"] = self.ts_converter.current_iso8601()
                payload["timestamp_unix"] = time.time()
                payload["execution_context_id"] = event.execution_context_id

                self.captured_actions.append(payload)

                action_type = payload.get("type")
                if action_type == "keypress":
                    log_action(f"keypress: {payload.get('key')}")
                elif action_type == "click":
                    log_action(f"click: {payload.get('text', '')[:50]}")
                else:
                    log_action(f"{action_type}: {payload.get('value', payload.get('newUrl', ''))[:50]}")
            except Exception as e:
                error(f"Binding handler failed: {e}")
                log_exception()

    async def request_handler(self, event: cdp.network.RequestWillBeSent):
        if event.wall_time is not None:
            self.ts_converter.calibrate(event.timestamp, event.wall_time)

        if event.request_id in self.active_map:
            old_req = self.active_map[event.request_id]
            if event.redirect_response:
                rd = event.redirect_response.to_json()
                old_req["response_data"] = {"status": rd["status"], "headers": rd.get("headers", {}), "body": None}
                old_req["request_state"] = "redirected"
                old_req["redirect_target"] = event.request.url
                self.stats["redirected"] += 1

        unique_id = f"{event.request_id}_{uuid.uuid4().hex[:8]}"

        request_obj = {
            "unique_id": unique_id,
            "request_id": event.request_id,
            "timestamp_iso": self.ts_converter.to_iso8601(event.timestamp),
            "timestamp_unix": self.ts_converter.to_unix_timestamp(event.timestamp),
            "timestamp_monotonic": event.timestamp,
            "url": event.request.url,
            "method": event.request.method,
            "resource_type": str(event.type_),
            "headers": dict(event.request.headers),
            "post_data": event.request.post_data,
            "cookies_sent_details": [],
            "cookies_received_details": {},
            "response_data": None,
            "response_timing": {},
            "request_state": "pending",
            "body_fetch_error": None,
        }

        if event.request_id in self.orphan_extra_info:
            data = self.orphan_extra_info.pop(event.request_id)
            if "sent" in data:
                request_obj["cookies_sent_details"] = data["sent"]
            if "received" in data:
                request_obj["cookies_received_details"] = data["received"]
            if "raw_headers" in data:
                self.merge_headers(request_obj, data["raw_headers"])

        # Skip data: URIs (base64-encoded inline resources) — they add noise, not useful context
        if event.request.url.startswith("data:"):
            self._mark_skipped(event.request_id)
            return

        # Skip domains on the blocklist (ads, trackers, telemetry)
        if self.blocklist and self.blocklist.is_blocked_url(event.request.url):
            self.stats["blocked_by_blocklist"] += 1
            self._mark_skipped(event.request_id)
            return

        self.captured_requests.append(request_obj)
        self.active_map[event.request_id] = request_obj
        # If a redirect went from a blocked URL to an allowed one, the
        # request_id was previously marked as skipped. Now that we're
        # tracking it, drop the skip mark so its extra_info isn't dropped.
        self._skipped_ids.pop(str(event.request_id), None)
        self.stats["total_requests"] += 1
        if event.type_ in (
            cdp.network.ResourceType.DOCUMENT,
            cdp.network.ResourceType.XHR,
            cdp.network.ResourceType.FETCH,
        ):
            self.stats["actionable_requests"] += 1

    async def data_received_handler(self, event: cdp.network.DataReceived):
        """Accumulate streamed response chunks for requests we're tracking."""
        rid = str(event.request_id)
        if rid in self._streamed_bodies and event.data:
            try:
                self._streamed_bodies[rid].append(base64.b64decode(event.data))
            except Exception as exc:
                warn(f"Failed to decode streamed body chunk for request {rid}: {exc}")

    async def response_handler(self, event: cdp.network.ResponseReceived):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "received"
            req["response_timing"]["received_iso"] = self.ts_converter.to_iso8601(event.timestamp)
            req["response_timing"]["received_unix"] = self.ts_converter.to_unix_timestamp(event.timestamp)

            if "timestamp_unix" in req:
                duration_ms = (req["response_timing"]["received_unix"] - req["timestamp_unix"]) * 1000
                req["response_timing"]["duration_ms"] = round(duration_ms, 2)

            resp = event.response.to_json()

            if not req["response_data"]:
                req["response_data"] = {
                    "status": resp["status"],
                    "headers": {},
                    "body": None,
                    "base64_encoded": False,
                    "charset": resp.get("charset", "utf-8") or "utf-8",
                    "mime_type": resp.get("mimeType", "unknown"),
                    "from_disk_cache": resp.get("fromDiskCache", False),
                    "from_service_worker": resp.get("fromServiceWorker", False),
                    "from_prefetch_cache": resp.get("fromPrefetchCache", False),
                }
            else:
                req["response_data"]["status"] = resp["status"]
                req["response_data"]["mime_type"] = resp.get("mimeType", "unknown")
                req["response_data"]["from_disk_cache"] = resp.get("fromDiskCache", False)
                req["response_data"]["from_service_worker"] = resp.get("fromServiceWorker", False)
                req["response_data"]["from_prefetch_cache"] = resp.get("fromPrefetchCache", False)

            self.merge_headers(req, resp.get("headers", {}))

            # Activate streaming for this request so DataReceived events
            # carry actual body data — acts as a fallback when
            # getResponseBody fails for large/evicted responses.
            rid = str(event.request_id)
            self._streamed_bodies[rid] = []
            tab = self._request_tab.get(event.request_id, self.tab)
            try:
                buffered = await tab.send(cdp.network.stream_resource_content(request_id=event.request_id))
                # buffered contains any data Chrome already received before
                # we enabled streaming — store it as the first chunk
                if buffered:
                    self._streamed_bodies[rid].append(base64.b64decode(buffered))
            except Exception:
                # Streaming not supported or request already done — that's fine,
                # getResponseBody will still work for most responses.
                # Log to file only (not terminal) since this is expected for many requests.
                log_exception()

    async def loading_finished_handler(self, event: cdp.network.LoadingFinished):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "finished"
            self.stats["completed"] += 1

            req["response_timing"]["finished_iso"] = self.ts_converter.to_iso8601(event.timestamp)
            req["response_timing"]["finished_unix"] = self.ts_converter.to_unix_timestamp(event.timestamp)

            if "timestamp_unix" in req:
                total_ms = (req["response_timing"]["finished_unix"] - req["timestamp_unix"]) * 1000
                req["response_timing"]["total_duration_ms"] = round(total_ms, 2)

            if req["response_data"]:
                status = req["response_data"].get("status", 0)
                from_cache = (
                    req["response_data"].get("from_disk_cache", False)
                    or req["response_data"].get("from_service_worker", False)
                    or req["response_data"].get("from_prefetch_cache", False)
                )

                should_skip = False
                skip_reason = None

                if 300 <= status < 400:
                    should_skip = True
                    skip_reason = f"Redirect status {status}"
                    self.stats["body_skip_redirect"] += 1
                elif status in (204, 205, 304):
                    should_skip = True
                    skip_reason = f"No content status {status}"
                    self.stats["body_skip_no_content"] += 1

                if should_skip:
                    req["body_fetch_error"] = skip_reason
                else:
                    body_captured = False
                    # Primary: try getResponseBody (works for small/buffered responses)
                    tab = self._request_tab.get(event.request_id, self.tab)
                    try:
                        result = await tab.send(cdp.network.get_response_body(request_id=event.request_id))
                        if isinstance(result, tuple):
                            body, is_base64 = result
                            req["response_data"]["body"] = body
                            req["response_data"]["base64_encoded"] = is_base64
                        else:
                            req["response_data"]["body"] = result.body
                            req["response_data"]["base64_encoded"] = result.base64_encoded
                        self.stats["body_success"] += 1
                        body_captured = True
                    except Exception as e:
                        error_msg = str(e)
                        req["body_fetch_error"] = error_msg

                    # Fallback: use streamed body chunks if getResponseBody failed
                    rid = str(event.request_id)
                    if not body_captured and rid in self._streamed_bodies:
                        chunks = self._streamed_bodies[rid]
                        if chunks:
                            raw = b"".join(chunks)
                            req["response_data"]["body"] = base64.b64encode(raw).decode("ascii")
                            req["response_data"]["base64_encoded"] = True
                            req["body_fetch_error"] = None
                            self.stats["body_from_stream"] += 1
                            body_captured = True

                    if not body_captured:
                        if "No resource with given identifier" in (req.get("body_fetch_error") or ""):
                            if from_cache:
                                self.stats["body_skip_cached"] += 1
                            else:
                                self.stats["body_failed"] += 1
                        else:
                            self.stats["body_failed"] += 1
                        if not from_cache:
                            warn(f"Body fetch failed for {req['url'][:60]}: {req.get('body_fetch_error', 'unknown')}")

            self._streamed_bodies.pop(str(event.request_id), None)
            self._request_tab.pop(event.request_id, None)
            self.active_map.pop(event.request_id, None)

    async def loading_failed_handler(self, event: cdp.network.LoadingFailed):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "failed"
            req["loading_failed"] = True
            req["error_text"] = event.error_text
            req["canceled"] = event.canceled
            req["blocked_reason"] = str(event.blocked_reason) if event.blocked_reason else None
            self.stats["failed"] += 1
            self._streamed_bodies.pop(str(event.request_id), None)
            self._request_tab.pop(event.request_id, None)
            self.active_map.pop(event.request_id, None)
            warn(f"Request failed: {req['url'][:60]} - {event.error_text}")

    async def req_extra_info(self, event: cdp.network.RequestWillBeSentExtraInfo):
        if str(event.request_id) in self._skipped_ids:
            return
        cookies = [ac.to_json() for ac in event.associated_cookies]
        if event.request_id in self.active_map:
            self.active_map[event.request_id]["cookies_sent_details"] = cookies
        else:
            if event.request_id not in self.orphan_extra_info:
                self.orphan_extra_info[event.request_id] = {}
                if len(self.orphan_extra_info) > self._ORPHAN_LRU_MAX:
                    self.orphan_extra_info.popitem(last=False)
            self.orphan_extra_info[event.request_id]["sent"] = cookies

    async def res_extra_info(self, event: cdp.network.ResponseReceivedExtraInfo):
        if str(event.request_id) in self._skipped_ids:
            return
        cookie_data = {
            "blocked": [c.to_json() for c in event.blocked_cookies],
            "exempted": [c.to_json() for c in (event.exempted_cookies or [])],
        }
        headers = dict(event.headers)
        if event.request_id in self.active_map:
            self.active_map[event.request_id]["cookies_received_details"] = cookie_data
            self.merge_headers(self.active_map[event.request_id], headers)
        else:
            if event.request_id not in self.orphan_extra_info:
                self.orphan_extra_info[event.request_id] = {}
                if len(self.orphan_extra_info) > self._ORPHAN_LRU_MAX:
                    self.orphan_extra_info.popitem(last=False)
            self.orphan_extra_info[event.request_id]["received"] = cookie_data
            self.orphan_extra_info[event.request_id]["raw_headers"] = headers

    @staticmethod
    def _get_chrome_version(chrome_path: str) -> int | None:
        """Return the major version of a Chrome binary, or None if unreadable."""
        import subprocess
        import sys
        from pathlib import Path
        path = Path(chrome_path).expanduser() if not isinstance(chrome_path, Path) else chrome_path.expanduser()
        if not path.exists():
            return None
        try:
            if sys.platform == "win32":
                import ctypes
                size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
                if size:
                    data = ctypes.create_string_buffer(size)
                    ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, data)
                    buf = ctypes.c_wchar_p()
                    buf_len = ctypes.c_uint()
                    ctypes.windll.version.VerQueryValueW(data, r"\StringFileInfo\040904B0\ProductVersion", ctypes.byref(buf), ctypes.byref(buf_len))
                    if buf.value:
                        return int(buf.value.split(".")[0])
            out = subprocess.check_output([str(path), "--version"], text=True, timeout=5, stderr=subprocess.DEVNULL).strip()
            for part in out.split():
                if "." in part:
                    return int(part.split(".")[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _find_system_chrome() -> str | None:
        """Find the system Google Chrome binary."""
        import sys
        if sys.platform == "win32":
            for p in [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            ]:
                if os.path.isfile(p):
                    return p
        elif sys.platform == "darwin":
            p = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.isfile(p):
                return p
        else:
            import shutil
            return shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        return None

    def _create_proxy_auth_extension(self) -> str | None:
        """Generate a temp Chrome extension for authenticated proxy auth only.

        Proxy routing is handled by --proxy-server flag. This extension only
        responds to chrome.webRequest.onAuthRequired for proxy challenges.
        Returns the extension directory path, or None if no auth needed.
        """
        if not self._proxy_creds:
            return None

        import json
        import tempfile
        from urllib.parse import urlparse

        parsed = urlparse(self.proxy_url)
        username, password = self._proxy_creds
        proxy_port = parsed.port or (443 if parsed.scheme == "https" else 80)

        manifest = {
            "version": "1.0.0",
            "manifest_version": 3,
            "name": "AutoWrec Proxy Auth",
            "permissions": ["webRequest", "webRequestAuthProvider"],
            "host_permissions": ["<all_urls>"],
            "background": {"service_worker": "background.js"},
            "minimum_chrome_version": "108",
        }

        proxy_cfg = json.dumps({"host": parsed.hostname, "port": proxy_port})
        creds_cfg = json.dumps({"username": username, "password": password})

        background_js = f"""
const PROXY = {proxy_cfg};
const CREDS = {creds_cfg};
const tried = new Set();

chrome.webRequest.onAuthRequired.addListener(
    function(details) {{
        if (!details.isProxy) return {{}};
        if (details.challenger && (details.challenger.host !== PROXY.host || details.challenger.port !== PROXY.port)) return {{}};
        if (tried.has(details.requestId)) return {{ cancel: true }};
        tried.add(details.requestId);
        return {{ authCredentials: CREDS }};
    }},
    {{urls: ["<all_urls>"]}},
    ["blocking"]
);
"""

        ext_dir = tempfile.mkdtemp(prefix="autowrec_proxy_ext_")
        with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
        with open(os.path.join(ext_dir, "background.js"), "w") as f:
            f.write(background_js)
        return ext_dir

    async def target_created_handler(self, event: cdp.target.AttachedToTarget):
        target_info = event.target_info

        # We only care about full pages (not service workers or iframes)
        if target_info.type_ == "page":
            info(f"New Tab/Window Opened: {target_info.url}")

            # Poll for zendriver to register the new tab (up to 3s).
            # Check-then-sleep so the common case (target already registered)
            # exits with zero latency, instead of the old 100ms first-tick lag.
            tab_session = None
            deadline = asyncio.get_running_loop().time() + 3.0
            while True:
                if not self.recording_active or not self.browser or self.browser.stopped or self.browser.connection.closed:
                    return
                for t in self.browser.targets:
                    if (
                        getattr(t, "session_id", None) == event.session_id
                        or getattr(t, "target_id", None) == target_info.target_id
                    ):
                        tab_session = t
                        break
                if tab_session or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.05)

            if not tab_session:
                warn(f"Could not resolve Tab object for session {event.session_id} within 3s")
                return

            info(f"Successfully bound CDP to new tab: {target_info.target_id}")

            try:
                # Now we can send CDP commands directly to this specific tab!
                await tab_session.send(cdp.page.enable())
                await tab_session.send(cdp.page.set_bypass_csp(enabled=True))
                await tab_session.send(
                    cdp.network.enable(
                        max_resource_buffer_size=25 * 1024 * 1024, max_total_buffer_size=250 * 1024 * 1024
                    )
                )
                await tab_session.send(cdp.runtime.enable())

                await tab_session.send(cdp.runtime.add_binding(name="sendActionToPython"))

                # Bind handlers — wrap request/response/loading to capture tab_session
                # so CDP body-fetching commands go to the correct session
                def _make_request_handler(ts):
                    async def handler(event):
                        await self.request_handler(event)
                        if event.request_id in self.active_map:
                            self._request_tab[event.request_id] = ts
                    return handler

                req_handler = _make_request_handler(tab_session)
                tab_session.add_handler(cdp.runtime.BindingCalled, self.binding_handler)
                tab_session.add_handler(cdp.network.RequestWillBeSent, req_handler)
                tab_session.add_handler(cdp.network.ResponseReceived, self.response_handler)
                tab_session.add_handler(cdp.network.DataReceived, self.data_received_handler)
                tab_session.add_handler(cdp.network.LoadingFinished, self.loading_finished_handler)
                tab_session.add_handler(cdp.network.LoadingFailed, self.loading_failed_handler)
                tab_session.add_handler(cdp.network.RequestWillBeSentExtraInfo, self.req_extra_info)
                tab_session.add_handler(cdp.network.ResponseReceivedExtraInfo, self.res_extra_info)

                # Inject the JS scripts so actions in the new tab are also recorded
                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
                )
            except Exception as exc:
                warn(f"Failed to initialise CDP on new tab {target_info.target_id}: {exc}")
                log_exception()

    async def _bring_browser_to_front(self) -> None:
        """Bring Chrome to foreground via CDP."""
        try:
            await self.tab.send(cdp.page.bring_to_front())
        except Exception:
            pass

    async def run_session(self, url: str) -> dict:
        if not self._load_scripts():
            return {}

        # Auth proxy preflight — before broad try so errors propagate to MCP
        browser_args = ["--disable-popup-blocking"]
        if not self._proxy_creds:
            browser_args.insert(0, "--incognito")
        launch_chrome = self.chrome_path

        if self._proxy_creds:
            chrome_exe = self.chrome_path or self._find_system_chrome()
            if not chrome_exe:
                raise ValueError(
                    "Authenticated proxy requires a Chrome binary. "
                    "Provide one via chrome_path, or use the download_chrome MCP tool."
                )
            ver = self._get_chrome_version(chrome_exe)
            if ver is None:
                raise ValueError(
                    f"Could not read Chrome version at {chrome_exe}. "
                    "Verify the path is correct and the binary is executable."
                )
            if ver >= 137:
                raise ValueError(
                    f"Chrome {ver} at {chrome_exe} does not support --load-extension (need < 137). "
                    "Provide a Chrome < 137 via chrome_path, or use download_chrome."
                )
            launch_chrome = chrome_exe
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(self.proxy_url)
            clean = urlunparse((parsed.scheme, self._proxy_netloc(parsed), "", "", "", ""))
            browser_args.append(f"--proxy-server={clean}")
        elif self.proxy_url:
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(self.proxy_url)
            clean = urlunparse((parsed.scheme, self._proxy_netloc(parsed), "", "", "", ""))
            browser_args.append(f"--proxy-server={clean}")

        try:
            if self._proxy_creds:
                self._proxy_ext_dir = self._create_proxy_auth_extension()
                if self._proxy_ext_dir:
                    browser_args.append(f"--load-extension={self._proxy_ext_dir}")
                    browser_args.append(f"--disable-extensions-except={self._proxy_ext_dir}")

            self.browser = await zd.start(
                headless=False,
                browser_executable_path=launch_chrome,
                browser_args=browser_args,
            )
            self.recording_start = datetime.now(UTC)
            self.recording_active = True

            self.tab = await self.browser.get("about:blank")

            info("Enabling CDP domains and binding handlers...")
            await self.tab.send(cdp.page.enable())
            await self.tab.send(cdp.page.set_bypass_csp(enabled=True))
            await self.tab.send(
                cdp.network.enable(max_resource_buffer_size=25 * 1024 * 1024, max_total_buffer_size=250 * 1024 * 1024)
            )
            await self.tab.send(cdp.runtime.enable())

            await self.tab.send(cdp.runtime.add_binding(name="sendActionToPython"))
            self.tab.add_handler(cdp.runtime.BindingCalled, self.binding_handler)
            self.tab.add_handler(cdp.network.RequestWillBeSent, self.request_handler)
            self.tab.add_handler(cdp.network.ResponseReceived, self.response_handler)
            self.tab.add_handler(cdp.network.DataReceived, self.data_received_handler)
            self.tab.add_handler(cdp.network.LoadingFinished, self.loading_finished_handler)
            self.tab.add_handler(cdp.network.LoadingFailed, self.loading_failed_handler)
            self.tab.add_handler(cdp.network.RequestWillBeSentExtraInfo, self.req_extra_info)
            self.tab.add_handler(cdp.network.ResponseReceivedExtraInfo, self.res_extra_info)

            from .. import config as _cfg
            redact_js = f"window.__autowrec_redact = {'true' if _cfg.REDACT_SENSITIVE else 'false'};"
            await self.tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=redact_js + self.telemetry_script, run_immediately=True)
            )

            await self.tab.send(cdp.runtime.evaluate(expression=redact_js + self.telemetry_script))

            await self.browser.connection.send(
                cdp.target.set_auto_attach(auto_attach=True, wait_for_debugger_on_start=False, flatten=True)
            )
            self.browser.connection.add_handler(cdp.target.AttachedToTarget, self.target_created_handler)
            await self.browser.connection.send(cdp.target.set_discover_targets(discover=True))

            info(f"Navigating to {url}")
            await self.tab.send(cdp.page.navigate(url=url))

            await self._bring_browser_to_front()

            while self.recording_active:
                if self.browser and (self.browser.stopped or self.browser.connection.closed):
                    info("Browser closed. Stopping recording...")
                    self.recording_active = False
                    break
                await asyncio.sleep(0.1)

        except Exception as e:
            error(f"Session encountered an error: {e}")
            print_exception()
            if self._proxy_ext_dir and not self.browser:
                import shutil
                shutil.rmtree(self._proxy_ext_dir, ignore_errors=True)
                self._proxy_ext_dir = None
            if not self.recording_start:
                raise

        return await self._cleanup_and_build_report()

    def stop(self):
        """Safely signals the asynchronous run_session loop to terminate."""
        info("Halting browser agent session...")
        self.recording_active = False

    async def _wait_for_pending_requests(self, timeout: float = 10.0, idle_time: float = 1.0) -> None:
        """Drain pending requests before building the session report.

        Exit paths:
        - User cancelled (recording_active=False): exits immediately, no wait.
        - Network idle for `idle_time` seconds: assumes stragglers won't resolve.
        - All requests resolved: normal completion.
        - Hard timeout after `timeout` seconds: gives up.

        On the common paths (Ctrl+C, browser close), recording_active is already
        False so this function returns near-instantly. The drain only runs when
        the session ended via an exception with the browser still connected.
        """
        if not self.active_map:
            return

        if not self.recording_active:
            return

        pending = len(self.active_map)
        info(f"Waiting for {pending} pending request(s) to complete (timeout={timeout}s, idle={idle_time}s)...")

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        deadline = start_time + timeout
        last_change = loop.time()
        prev_count = pending

        while self.active_map and loop.time() < deadline:
            if not self.recording_active:
                break
            current_count = len(self.active_map)
            if current_count != prev_count:
                last_change = loop.time()
                prev_count = current_count

            if loop.time() - last_change >= idle_time:
                info(f"Network idle for {idle_time}s with {current_count} request(s) still pending — moving on.")
                return

            await asyncio.sleep(0.1)

        remaining = len(self.active_map)
        elapsed = loop.time() - start_time
        if remaining:
            warn(f"Drain finished with {remaining} request(s) still pending after {elapsed:.1f}s.")
        else:
            info("All pending requests resolved.")

    async def _cleanup_and_build_report(self) -> dict:
        # Let in-flight requests settle before tearing down
        await self._wait_for_pending_requests()

        info("Processing incomplete network requests...")

        incomplete_count = len(self.active_map)
        if incomplete_count > 0:
            warn(f"Found {incomplete_count} incomplete requests.")
            for _request_id, req in self.active_map.items():
                current_state = req.get("request_state", "unknown")
                if current_state == "pending":
                    req["request_state"] = "incomplete_no_response"
                    req["incomplete_reason"] = "Recording stopped before response received"
                elif current_state == "received":
                    req["request_state"] = "incomplete_loading"
                    req["incomplete_reason"] = "Recording stopped during response loading"
                else:
                    req["request_state"] = "incomplete_unknown"
                    req["incomplete_reason"] = f"Recording stopped while in state: {current_state}"

                self.stats["incomplete"] += 1

        if self.tab:
            self.tab.remove_handlers()

        try:
            if self.browser:
                await self.browser.stop()
        except Exception as exc:
            warn(f"Failed to stop browser cleanly: {exc}")
            log_exception()

        if self._proxy_ext_dir:
            import shutil
            shutil.rmtree(self._proxy_ext_dir, ignore_errors=True)
            self._proxy_ext_dir = None

        recording_end = datetime.now(UTC)
        duration = (recording_end - self.recording_start).total_seconds() if self.recording_start else 0.0

        info(
            f"Collection Complete. Captured {len(self.captured_requests)} requests "
            f"and {len(self.captured_actions)} actions over {duration:.2f}s."
        )

        return {
            "metadata": {
                "recording_started": self.recording_start.isoformat(timespec="milliseconds")
                if self.recording_start
                else None,
                "recording_ended": recording_end.isoformat(timespec="milliseconds"),
                "duration_seconds": round(duration, 2),
                "total_requests": self.stats["total_requests"],
                "actionable_requests": self.stats["actionable_requests"],
                "completed_requests": self.stats["completed"],
                "failed_requests": self.stats["failed"],
                "redirected_requests": self.stats["redirected"],
                "incomplete_requests": self.stats["incomplete"],
                "total_actions": len(self.captured_actions),
                "blocked_by_blocklist": self.stats["blocked_by_blocklist"],
                "timestamp_format": "ISO 8601 (YYYY-MM-DDTHH:MM:SS.sssZ)",
                "timezone": "UTC",
                "body_capture_stats": {
                    "success": self.stats["body_success"],
                    "from_stream": self.stats["body_from_stream"],
                    "failed": self.stats["body_failed"],
                    "skip_redirect": self.stats["body_skip_redirect"],
                    "skip_no_content": self.stats["body_skip_no_content"],
                    "skip_cached": self.stats["body_skip_cached"],
                },
            },
            "requests": self.captured_requests,
            "actions": self.captured_actions,
        }
