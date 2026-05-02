import base64
import json
import os
import re
import shutil
import traceback
from urllib.parse import urlparse

from .. import config
from ..console import detail, error, info, log_exception, print_exception, rule, success, warn
from .video_recorder import ActionVideoRecorder

try:
    from magika import Magika

    magika_detector = Magika()
    MAGIKA_AVAILABLE = True
    info("Magika AI detector initialized successfully.")
except ImportError:
    magika_detector = None
    MAGIKA_AVAILABLE = False
    warn("Magika not installed. Skipping advanced content type detection.")

def _get_paths():
    """Derive workspace paths from current config (not cached at import time)."""
    workspace = str(config.WORKSPACE_DIR)
    output = os.path.join(workspace, "session_dump")
    return workspace, output, os.path.join(output, "clips"), os.path.join(output, "requests")


def sanitize_filename(name: str) -> str:
    name = name.replace("https://", "").replace("http://", "")
    return re.sub(r"[^\w\-\.]", "_", name)[:100]


def make_serializable(obj):
    if isinstance(obj, str | int | float | bool | type(None)):
        return obj
    if isinstance(obj, bytes):
        return ""
    if isinstance(obj, list):
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
        for line in raw.split("\n"):
            if "=" in line:
                names.add(line.split("=")[0].strip())
    return sorted(list(names))


def detect_content_type(content, is_base64=False):
    if content is None or not MAGIKA_AVAILABLE or magika_detector is None:
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
        result = magika_detector.identify_bytes(byte_content[:262144])
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


def merge_and_annotate_actions(
    actions: list[dict],
    full_video_path: str | None,
    video_start_unix: float | None,
) -> list[dict]:
    if not actions:
        return actions

    actions.sort(key=lambda x: x.get("timestamp_unix", 0))
    merged_clips = []
    current_cluster = []

    for action in actions:
        if not current_cluster:
            current_cluster.append(action)
        else:
            last_action_time = current_cluster[-1].get("timestamp_unix", 0)
            current_action_time = action.get("timestamp_unix", 0)

            if (current_action_time - last_action_time) <= config.MERGE_GAP_THRESHOLD_SECONDS:
                current_cluster.append(action)
            else:
                merged_clips.append(current_cluster)
                current_cluster = [action]

    if current_cluster:
        merged_clips.append(current_cluster)

    has_video = full_video_path and video_start_unix and os.path.exists(full_video_path)

    if not has_video:
        return actions

    recorder = ActionVideoRecorder(fps=config.FPS)

    _, _, clips_dir, _ = _get_paths()
    info(f"Splitting {len(merged_clips)} video action segments...")
    for idx, cluster in enumerate(merged_clips):
        first_action_time_relative = cluster[0]["timestamp_unix"] - video_start_unix
        clip_start = max(0, first_action_time_relative - config.SEGMENT_PAD_SECONDS)

        last_action_time_relative = cluster[-1]["timestamp_unix"] - video_start_unix
        clip_end = last_action_time_relative + config.SEGMENT_PAD_SECONDS

        clip_filename = f"action_clip_{idx:03d}.mp4"
        clip_path = os.path.join(clips_dir, clip_filename)

        clip_ok = recorder.split_video(full_video_path, clip_path, clip_start, clip_end)
        if clip_ok:
            for action in cluster:
                action["ai_video_file"] = f"clips/{clip_filename}"
                action["video_start_sec"] = round(clip_start, 2)
                action["video_end_sec"] = round(clip_end, 2)

    return actions


def process_network_requests(requests: list[dict]) -> tuple[list[dict], dict]:
    timeline_requests = []
    detection_stats = {"request_detected": 0, "response_detected": 0, "mismatches": 0}
    _, _, _, requests_dir = _get_paths()

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
                if request_detection:
                    detection_stats["request_detected"] += 1

            response_detection = None
            if res_data and res_data.get("body"):
                response_detection = detect_content_type(res_data["body"], res_data.get("base64_encoded", False))
                if response_detection:
                    detection_stats["response_detected"] += 1

            declared_mime = res_data.get("mime_type", "unknown")
            detected_mime = response_detection.get("mime_type", "unknown") if response_detection else "unknown"
            if declared_mime != detected_mime and declared_mime != "unknown":
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
                    "cookies_sent_detailed": item.get("cookies_sent_details", []),
                    "content_detection": request_detection,
                    "has_payload": bool(item.get("post_data")),
                },
                "response": {
                    "headers": res_headers,
                    "cookies_set": extract_cookies_set(item),
                    "cookies_set_detailed": item.get("cookies_received_details", {}),
                    "content_detection": response_detection,
                    "has_body": bool(res_data.get("body")),
                    "mime_mismatch": declared_mime != detected_mime
                    if (response_detection and declared_mime != "unknown")
                    else False,
                },
            }

            with open(os.path.join(req_root, "transaction.json"), "w") as f:
                json.dump(make_serializable(transaction_data), f, indent=2)

            if item.get("post_data"):
                ext = request_detection.get("extension", "bin") if request_detection else "bin"
                save_content(os.path.join(req_root, f"req_payload.{ext}"), item["post_data"])

            if res_data and res_data.get("body"):
                ext = response_detection.get("extension", "bin") if response_detection else "bin"
                save_content(
                    os.path.join(req_root, f"res_body.{ext}"), res_data["body"], res_data.get("base64_encoded", False)
                )
                res_data.pop("body", None)

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
            _, output_dir, _, _ = _get_paths()
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


def compile_workspace(
    session_data: dict,
    full_video_path: str | None = None,
    video_start_unix: float | None = None,
) -> bool:
    rule("Compiling Workspace", style="bold cyan")
    info("Extracting data...")

    _, OUTPUT_DIR, CLIPS_DIR, REQUESTS_DIR = _get_paths()

    try:
        if os.path.exists(OUTPUT_DIR):
            shutil.rmtree(OUTPUT_DIR)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        os.makedirs(CLIPS_DIR, exist_ok=True)
        os.makedirs(REQUESTS_DIR, exist_ok=True)

        metadata = session_data.get("metadata", {})
        requests = session_data.get("requests", [])
        actions = session_data.get("actions", [])
        timeline_events = []

        if actions:
            actions = merge_and_annotate_actions(actions, full_video_path, video_start_unix)
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
                            if k
                            not in [
                                "timestamp_unix",
                                "timestamp_iso",
                                "type",
                                "ai_macro_summary",
                                "ai_elements_interacted",
                                "ai_action_success",
                                "ai_video_file",
                                "video_start_sec",
                                "video_end_sec",
                            ]
                        },
                        "ai_macro_summary": action.get("ai_macro_summary"),
                        "ai_elements_interacted": action.get("ai_elements_interacted"),
                        "ai_action_success": action.get("ai_action_success"),
                        "ai_video_file": action.get("ai_video_file"),
                        "video_start_sec": action.get("video_start_sec"),
                        "video_end_sec": action.get("video_end_sec"),
                    }
                )

        detection_stats = {}
        if requests:
            info(f"Extracting {len(requests)} network requests and building transactions...")
            network_events, detection_stats = process_network_requests(requests)
            timeline_events.extend(network_events)

        timeline_events.sort(key=lambda x: x["timestamp"])
        with open(os.path.join(OUTPUT_DIR, "timeline.json"), "w") as f:
            json.dump(make_serializable(timeline_events), f, indent=2)

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
            if status:
                summary["statistics"]["status_codes"][str(status)] = (
                    summary["statistics"]["status_codes"].get(str(status), 0) + 1
                )
            if get_header_val(req.get("headers", {}), "authorization"):
                summary["statistics"]["with_auth"] += 1
            if req.get("cookies_sent_details"):
                summary["statistics"]["with_cookies"] += 1

        with open(os.path.join(OUTPUT_DIR, "SUMMARY.json"), "w") as f:
            json.dump(make_serializable(summary), f, indent=2)

        with open(os.path.join(OUTPUT_DIR, "session_metadata.json"), "w") as f:
            json.dump(make_serializable(metadata), f, indent=2)

        success(f"Workspace compiled successfully at {OUTPUT_DIR}")
        if MAGIKA_AVAILABLE:
            detail(f"Payloads detected: {detection_stats.get('request_detected', 0)}")
            detail(f"Bodies detected: {detection_stats.get('response_detected', 0)}")
            detail(f"MIME mismatches: {detection_stats.get('mismatches', 0)}")
        rule(style="bold cyan")
        return True

    except Exception as e:
        error(f"Workspace compilation failed: {e}")
        print_exception()
        return False
