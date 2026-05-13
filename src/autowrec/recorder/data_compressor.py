import base64
import json
import os
import re
import shutil
import traceback
from urllib.parse import urlparse

from .. import config
from ..console import detail, error, info, log_exception, print_exception, rule, success, warn

try:
    from magika import Magika
    MAGIKA_AVAILABLE = True
except ImportError:
    Magika = None
    MAGIKA_AVAILABLE = False

_magika_detector = None


def _get_magika():
    global _magika_detector
    if _magika_detector is None and MAGIKA_AVAILABLE:
        _magika_detector = Magika()
        info("Magika AI detector initialized successfully.")
    return _magika_detector

def _get_paths():
    """Derive workspace paths from current config (not cached at import time)."""
    workspace = str(config.WORKSPACE_DIR)
    output = os.path.join(workspace, "session_dump")
    return workspace, output, os.path.join(output, "requests")


def sanitize_filename(name: str) -> str:
    name = name.replace("https://", "").replace("http://", "")
    return re.sub(r"[^\w\-\.]", "_", name)[:100]


def make_serializable(obj):
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, bytes):
        return {"__bytes_b64__": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [make_serializable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    return str(obj)


_REDACT_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-csrf-token"}


def _redact_headers(headers: dict) -> dict:
    if not headers or not config.REDACT_SENSITIVE:
        return headers
    redacted = {}
    for k, v in headers.items():
        v = str(v)
        if k.lower() in _REDACT_HEADERS:
            prefix = v.split(" ", 1)[0] if " " in v and k.lower() == "authorization" else ""
            tag = f"{prefix} " if prefix else ""
            redacted[k] = f"{tag}[REDACTED — {len(v)} chars]"
        else:
            redacted[k] = v
    return redacted


def _redact_cookie_details(details):
    """Redact cookie values in structured CDP cookie lists/dicts when REDACT_SENSITIVE is on.

    NOTE: Mutates the input in place for efficiency. Caller should not reuse the original data.
    """
    if not config.REDACT_SENSITIVE:
        return details
    if isinstance(details, list):
        for entry in details:
            cookie = entry.get("cookie") if isinstance(entry, dict) else None
            if isinstance(cookie, dict) and "value" in cookie:
                cookie["value"] = f"[REDACTED — {len(str(cookie['value']))} chars]"
    elif isinstance(details, dict):
        for key in ("blocked", "exempted"):
            for entry in details.get(key, []):
                cookie = entry.get("cookie") if isinstance(entry, dict) else None
                if isinstance(cookie, dict) and "value" in cookie:
                    cookie["value"] = f"[REDACTED — {len(str(cookie['value']))} chars]"
    return details


def get_header_val(headers, key):
    if not headers:
        return None
    key = key.lower()
    for k, v in headers.items():
        if k.lower() == key:
            return v
    return None


def extract_cookies_sent(item):
    names = set()
    details = item.get("cookies_sent_details", [])
    for ac in details:
        if ac.get("blockedReasons"):
            continue
        cookie = ac.get("cookie") or {}
        name = cookie.get("name")
        if name:
            names.add(name)
    if not names:
        raw = get_header_val(item.get("headers", {}), "cookie")
        if raw:
            for part in raw.split(";"):
                if "=" in part:
                    names.add(part.split("=")[0].strip())
    return sorted(names)


def extract_cookies_set(item):
    names = set()
    resp = item.get("response_data") or {}
    headers = resp.get("headers") or {}
    raw = get_header_val(headers, "set-cookie")
    if raw:
        # CDP folds multiple Set-Cookie headers into one '\n'-separated string,
        # so splitting on \n recovers the individual cookies. (Standard HTTP
        # would use a list of headers; CDP joins them.)
        for line in raw.split("\n"):
            if "=" in line:
                names.add(line.split("=")[0].strip())
    return sorted(list(names))


_MAGIKA_FASTPATH_THRESHOLD = 16  # bytes — below this, Magika is overkill


def detect_content_type(content, is_base64=False):
    if content is None or not MAGIKA_AVAILABLE:
        return None
    try:
        if is_base64:
            byte_content = base64.b64decode(content)
        elif isinstance(content, str):
            byte_content = content.encode("utf-8")
        elif isinstance(content, bytes):
            byte_content = content
        else:
            return None

        # Fast-path (P6): bodies below the threshold aren't worth a model
        # invocation. Returns the same shape as the Magika branch so callers
        # don't need a special case.
        if len(byte_content) < _MAGIKA_FASTPATH_THRESHOLD:
            return {
                "label": "tiny" if byte_content else "empty",
                "mime_type": "application/octet-stream",
                "extension": "bin",
                "all_extensions": ["bin"],
                "description": "Below Magika detection threshold",
                "confidence": 1.0,
                "is_text": all(b < 0x80 for b in byte_content),
                "group": "unknown",
            }

        detector = _get_magika()
        if detector is None:
            return None
        result = detector.identify_bytes(byte_content[:262144])
        return {
            "label": result.output.label,
            "mime_type": result.output.mime_type,
            "extension": result.output.extensions[0] if result.output.extensions else "bin",
            "all_extensions": result.output.extensions,
            "description": result.output.description,
            "confidence": result.score,
            "is_text": result.output.is_text,
            "group": result.output.group,
        }
    except Exception as e:
        warn(f"Magika error: {e}")
        return {"label": "unknown", "mime_type": "application/octet-stream", "extension": "bin", "error": str(e)}


def save_content(path, content, is_base64=False):
    if content is None:
        return
    mode = "wb"
    if is_base64:
        try:
            data = base64.b64decode(content)
        except Exception as exc:
            warn(f"Base64 decode failed for {path}, saving raw content instead: {exc}")
            data = str(content).encode("utf-8")
    elif isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = str(content).encode("utf-8")
    try:
        with open(path, mode) as f:
            f.write(data)
    except OSError as exc:
        error(f"Failed to write content to {path}: {exc}")
        log_exception()


def _sort_actions(actions: list[dict]) -> list[dict]:
    """Sort actions by timestamp in place."""
    if actions:
        actions.sort(key=lambda x: x.get("timestamp_unix", 0))
    return actions


def process_network_requests(requests: list[dict], output_dir: str | None = None, requests_dir: str | None = None) -> tuple[list[dict], dict]:
    """Process captured requests into transaction files and timeline events.

    NOTE: Mutates the input list in place — pops post_data and response body
    after saving to disk to free memory for large sessions.
    """
    timeline_requests = []
    detection_stats = {"request_detected": 0, "response_detected": 0, "mismatches": 0}
    if requests_dir is None:
        _, _, requests_dir = _get_paths()
    if output_dir is None:
        _, output_dir, _ = _get_paths()

    for idx, item in enumerate(requests):
        try:
            parsed_url = urlparse(item.get("url", ""))
            domain = parsed_url.netloc or "unknown"
            folder_name = f"{idx:03d}_{item.get('method', 'UNK')}_{sanitize_filename(domain)}"
            req_root = os.path.join(requests_dir, folder_name)
            os.makedirs(req_root, exist_ok=True)

            req_headers = _redact_headers(item.get("headers", {}))
            res_data = item.get("response_data") or {}
            res_headers = _redact_headers(res_data.get("headers") or {})

            request_detection = None
            if item.get("post_data"):
                request_detection = detect_content_type(item["post_data"])
                if request_detection and "error" not in request_detection:
                    detection_stats["request_detected"] += 1

            response_detection = None
            if res_data and res_data.get("body"):
                response_detection = detect_content_type(res_data["body"], res_data.get("base64_encoded", False))
                if response_detection and "error" not in response_detection:
                    detection_stats["response_detected"] += 1

            declared_mime = res_data.get("mime_type", "unknown")
            detected_mime = response_detection.get("mime_type", "unknown") if response_detection else "unknown"
            has_mime_mismatch = (
                bool(response_detection)
                and declared_mime != "unknown"
                and declared_mime != detected_mime
                and "error" not in response_detection
            )
            if has_mime_mismatch:
                detection_stats["mismatches"] += 1

            transaction_data = {
                "metadata": {
                    "index": idx,
                    "unique_id": item.get("unique_id"),
                    "method": item.get("method"),
                    "url": item.get("url"),
                    "status": res_data.get("status"),
                    "timing": {
                        "request_sent_unix": item.get("timestamp_unix"),
                        "response_received_unix": item.get("response_timing", {}).get("received_unix"),
                        "loading_finished_unix": item.get("response_timing", {}).get("finished_unix"),
                        "duration_ms": item.get("response_timing", {}).get("total_duration_ms"),
                    },
                    "security": {
                        "has_authorization": bool(get_header_val(req_headers, "authorization")),
                        "has_proxy_authorization": bool(get_header_val(req_headers, "proxy-authorization")),
                        "has_challenge": bool(get_header_val(res_headers, "www-authenticate")),
                    },
                },
                "request": {
                    "headers": req_headers,
                    "cookies_sent": extract_cookies_sent(item),
                    "cookies_sent_detailed": _redact_cookie_details(item.get("cookies_sent_details", [])),
                    "content_detection": request_detection,
                    "has_payload": bool(item.get("post_data")),
                },
                "response": {
                    "headers": res_headers,
                    "cookies_set": extract_cookies_set(item),
                    "cookies_set_detailed": _redact_cookie_details(item.get("cookies_received_details", {})),
                    "content_detection": response_detection,
                    "has_body": bool(res_data.get("body")),
                    "mime_mismatch": has_mime_mismatch,
                },
            }

            # transaction.json is machine-only — keep it compact (~25% smaller).
            with open(os.path.join(req_root, "transaction.json"), "w", encoding="utf-8") as f:
                json.dump(make_serializable(transaction_data), f, separators=(",", ":"))

            if item.get("post_data"):
                ext = request_detection.get("extension", "bin") if request_detection else "bin"
                save_content(os.path.join(req_root, f"req_payload.{ext}"), item["post_data"])
                item.pop("post_data", None)  # Free memory after saving to disk

            if res_data and res_data.get("body"):
                ext = response_detection.get("extension", "bin") if response_detection else "bin"
                save_content(
                    os.path.join(req_root, f"res_body.{ext}"), res_data["body"], res_data.get("base64_encoded", False)
                )
                res_data.pop("body", None)  # Free memory after saving to disk

            timeline_requests.append(
                {
                    "timestamp": item.get("timestamp_unix", 0),
                    "timestamp_iso": item.get("timestamp_iso"),
                    "event_type": "network_request",
                    "method": item.get("method"),
                    "url": item.get("url"),
                    "status": res_data.get("status", -1),
                    "folder": f"requests/{folder_name}",
                }
            )

        except Exception as e:
            error(f"Failed to process request at index {idx}: {e}")
            log_exception()
            error_filename = os.path.join(output_dir, f"CRASH_REPORT_{idx:03d}.txt")
            try:
                with open(error_filename, "w", encoding="utf-8") as debug_f:
                    debug_f.write(f"ERROR: {str(e)}\n" + "-" * 50 + "\n")
                    debug_f.write(traceback.format_exc() + "\n" + "-" * 50 + "\n")
                detail(f"  Crash report saved to {error_filename}")
            except OSError as write_exc:
                warn(f"Could not write crash report to {error_filename}: {write_exc}")
            continue

    return timeline_requests, detection_stats


def compile_workspace(session_data: dict) -> bool:
    rule("Compiling Workspace", style="bold cyan")
    info("Extracting data...")

    _, OUTPUT_DIR, REQUESTS_DIR = _get_paths()
    STAGING_DIR = OUTPUT_DIR + "_new"
    PREV_DIR = OUTPUT_DIR + "_prev"

    try:
        if os.path.exists(STAGING_DIR):
            shutil.rmtree(STAGING_DIR)
        os.makedirs(STAGING_DIR, exist_ok=True)
        os.makedirs(os.path.join(STAGING_DIR, "requests"), exist_ok=True)

        # Override paths to write into staging dir
        OUTPUT_DIR = STAGING_DIR
        REQUESTS_DIR = os.path.join(STAGING_DIR, "requests")

        metadata = session_data.get("metadata", {})
        requests = session_data.get("requests", [])
        actions = session_data.get("actions", [])
        timeline_events = []

        if actions:
            actions = _sort_actions(actions)
            for action in actions:
                timeline_events.append(
                    {
                        "timestamp": action.get("timestamp_unix", 0),
                        "timestamp_iso": action.get("timestamp_iso"),
                        "event_type": "user_action",
                        "action": action.get("type"),
                        "details": {
                            k: v
                            for k, v in action.items()
                            if k not in [
                                "timestamp_unix",
                                "timestamp_iso",
                                "type",
                                "ai_macro_summary",
                                "ai_elements_interacted",
                                "ai_action_success",
                            ]
                        },
                        "ai_macro_summary": action.get("ai_macro_summary"),
                        "ai_elements_interacted": action.get("ai_elements_interacted"),
                        "ai_action_success": action.get("ai_action_success"),
                    }
                )

        detection_stats = {}
        if requests:
            info(f"Extracting {len(requests)} network requests and building transactions...")
            network_events, detection_stats = process_network_requests(requests, output_dir=OUTPUT_DIR, requests_dir=REQUESTS_DIR)
            timeline_events.extend(network_events)

        timeline_events.sort(key=lambda x: x["timestamp"])
        # timeline.json is machine-only — compact format saves disk + tokens.
        with open(os.path.join(OUTPUT_DIR, "timeline.json"), "w", encoding="utf-8") as f:
            json.dump(make_serializable(timeline_events), f, separators=(",", ":"))

        session_flow = []
        seen_summaries = set()
        for action in actions:
            text = action.get("ai_macro_summary")
            if text and text not in seen_summaries:
                seen_summaries.add(text)
                session_flow.append(
                    {
                        "timestamp_iso": action.get("timestamp_iso"),
                        "timestamp_unix": action.get("timestamp_unix"),
                        "summary": text,
                    }
                )

        summary = {
            "session": metadata,
            "session_flow": session_flow,
            "statistics": {
                "total_requests": len(requests),
                "total_actions": len(actions),
                "methods": {},
                "domains": {},
                "status_codes": {},
                "with_auth": 0,
                "with_cookies": 0,
                "content_detection": detection_stats if MAGIKA_AVAILABLE else "Magika not available",
            },
        }

        for req in requests:
            method = req.get("method", "UNKNOWN")
            summary["statistics"]["methods"][method] = summary["statistics"]["methods"].get(method, 0) + 1
            domain = urlparse(req.get("url", "")).netloc
            if domain:
                summary["statistics"]["domains"][domain] = summary["statistics"]["domains"].get(domain, 0) + 1

            status = req.get("response_data", {}).get("status") if req.get("response_data") else None
            if status is not None:
                summary["statistics"]["status_codes"][str(status)] = (
                    summary["statistics"]["status_codes"].get(str(status), 0) + 1
                )
            if get_header_val(req.get("headers", {}), "authorization"):
                summary["statistics"]["with_auth"] += 1
            if req.get("cookies_sent_details"):
                summary["statistics"]["with_cookies"] += 1

        with open(os.path.join(OUTPUT_DIR, "SUMMARY.json"), "w", encoding="utf-8") as f:
            json.dump(make_serializable(summary), f, indent=2)

        with open(os.path.join(OUTPUT_DIR, "session_metadata.json"), "w", encoding="utf-8") as f:
            json.dump(make_serializable(metadata), f, indent=2)

        # Atomic swap: staging → final (old session preserved until new one is ready)
        _, final_output, _ = _get_paths()
        if os.path.exists(PREV_DIR):
            shutil.rmtree(PREV_DIR)
        if os.path.exists(final_output):
            os.rename(final_output, PREV_DIR)
        os.rename(STAGING_DIR, final_output)
        if os.path.exists(PREV_DIR):
            shutil.rmtree(PREV_DIR, ignore_errors=True)

        success(f"Workspace compiled successfully at {final_output}")
        if MAGIKA_AVAILABLE:
            detail(f"Payloads detected: {detection_stats.get('request_detected', 0)}")
            detail(f"Bodies detected: {detection_stats.get('response_detected', 0)}")
            detail(f"MIME mismatches: {detection_stats.get('mismatches', 0)}")
        rule(style="bold cyan")
        return True

    except Exception as e:
        error(f"Workspace compilation failed: {e}")
        print_exception()
        # Don't leave an orphaned staging directory behind on failure.
        if os.path.exists(STAGING_DIR):
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
        return False
