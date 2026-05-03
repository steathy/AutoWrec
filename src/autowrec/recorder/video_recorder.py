import ctypes
import os
import subprocess
import sys
import threading
import time

import imageio_ffmpeg
import mss

from ..console import error, info, log_exception, warn
from ..console import video as log_video

FFMPEG_TIMEOUT = 120  # seconds — guard against hanging FFmpeg slice operations


def _find_chrome_window(target_pid: int | None = None) -> tuple[dict, int] | None:
    """Find a Chrome/Chromium window via the Windows API.

    Returns (bounds_dict, hwnd) on success — the HWND lets callers cache the
    handle and skip the EnumWindows + PowerShell scan on subsequent ticks.
    Returns None if no matching window is found.

    When *target_pid* is given, only windows owned by that process (or its
    descendants per `_get_process_tree`) are considered.
    """
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        EnumWindows = ctypes.windll.user32.EnumWindows
        GetWindowTextW = ctypes.windll.user32.GetWindowTextW
        GetWindowRect = ctypes.windll.user32.GetWindowRect
        IsWindowVisible = ctypes.windll.user32.IsWindowVisible
        GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        child_pids = None
        if target_pid is not None:
            child_pids = _get_process_tree(target_pid)

        result = None

        def callback(hwnd, _lparam):
            nonlocal result
            if not IsWindowVisible(hwnd):
                return True

            if child_pids is not None:
                pid = wintypes.DWORD()
                GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in child_pids:
                    return True

            buf = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, buf, 256)
            title = buf.value
            if "Chrome" in title or "Chromium" in title:
                rect = wintypes.RECT()
                GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 100 and h > 100:
                    result = (
                        {"left": rect.left, "top": rect.top, "width": w, "height": h},
                        int(hwnd),
                    )
                    return False
            return True

        EnumWindows(WNDENUMPROC(callback), 0)
        return result
    except Exception:
        return None


def _get_window_rect(hwnd: int) -> dict | None:
    """Cheap follow-up: read a known HWND's bounds without EnumWindows.

    Returns None if the window is gone or minimized (zero-size rect). Lets
    `_record_loop` poll bounds 30×/min without paying the PowerShell tax of
    `_find_chrome_window` every time.
    """
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.IsWindow(hwnd):
            return None
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return None
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        return {"left": rect.left, "top": rect.top, "width": w, "height": h}
    except Exception:
        return None


def _get_process_tree(parent_pid: int) -> set[int]:
    """Return {parent_pid} and all descendant PIDs.

    Uses PowerShell Get-CimInstance because wmic was removed on
    Windows 11 24H2+. One process snapshot per call, then a local BFS —
    avoids the recursive subprocess fan-out the wmic version did.
    """
    pids = {parent_pid}
    if sys.platform != "win32":
        return pids
    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId | "
            "ConvertTo-Csv -NoTypeInformation",
        ]
        out = subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return pids

    children: dict[int, list[int]] = {}
    for line in out.splitlines()[1:]:
        try:
            pid_s, ppid_s = (s.strip().strip('"') for s in line.split(",", 1))
            pid_i, ppid_i = int(pid_s), int(ppid_s)
        except (ValueError, IndexError):
            continue
        children.setdefault(ppid_i, []).append(pid_i)

    stack = [parent_pid]
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child not in pids:
                pids.add(child)
                stack.append(child)
    return pids


class ActionVideoRecorder:
    """Handles background screen recording and precise FFmpeg video slicing."""

    def __init__(self, fps: int = 10, output_path: str = "full_record.mp4", chrome_only: bool = True):
        self.fps = fps
        self.output_path = output_path
        self.chrome_only = chrome_only
        self.is_recording = False
        self.video_start_unix: float | None = None
        self.thread: threading.Thread | None = None
        self._target_pid: int | None = None

    def set_target_pid(self, pid: int) -> None:
        """Set the Chrome process PID so the recorder captures the right window."""
        self._target_pid = pid

    def start(self) -> bool:
        """Starts the screen recording in a background thread. Returns True on success."""
        if self.is_recording:
            warn("Recording is already active.")
            return True

        output_dir = os.path.dirname(self.output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        self.is_recording = True
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()

        deadline = time.time() + 10.0
        while self.video_start_unix is None and self.is_recording:
            if time.time() > deadline:
                warn("Video recorder failed to start within 10s — disabling video.")
                self.is_recording = False
                if self.thread:
                    self.thread.join(timeout=2)
                return False
            time.sleep(0.01)

        return self.is_recording

    def _record_loop(self) -> None:
        """The core recording loop executed by the background thread."""
        writer = None

        try:
            with mss.mss() as sct:
                if not sct.monitors or len(sct.monitors) < 2:
                    error("No monitors detected — cannot record screen.")
                    self.is_recording = False
                    return

                # Start with full-screen capture immediately so start() unblocks.
                # We'll switch to the Chrome window once the PID arrives.
                monitor = sct.monitors[1]
                capture_region = {
                    "left": monitor["left"], "top": monitor["top"],
                    "width": monitor["width"], "height": monitor["height"],
                }
                chrome_locked = False
                cached_hwnd: int | None = None
                resize_warned = False

                frame_duration = 1.0 / self.fps
                bounds_check_interval = 2.0
                last_bounds_check = 0.0

                self.video_start_unix = time.time()

                while self.is_recording:
                    loop_start = time.time()

                    # Once the PID is available, find the Chrome window and
                    # restart the writer with the correct frame size.
                    if self.chrome_only and not chrome_locked:
                        pid = self._target_pid
                        if pid:
                            found = _find_chrome_window(target_pid=pid)
                            if found:
                                chrome_bounds, cached_hwnd = found
                                if chrome_bounds["width"] > 100 and chrome_bounds["height"] > 100:
                                    capture_region = chrome_bounds
                                    chrome_locked = True
                                    log_video(
                                        f"Capturing Chrome window (PID {pid}, HWND {cached_hwnd}): "
                                        f"{capture_region['width']}x{capture_region['height']}"
                                    )
                                    # Restart writer with the Chrome window dimensions
                                    if writer is not None:
                                        try:
                                            writer.close()
                                        except Exception:
                                            pass
                                        writer = None
                                    self.video_start_unix = time.time()

                    # Lazily create the writer (or recreate after Chrome lock-in)
                    if writer is None:
                        width = capture_region["width"]
                        height = capture_region["height"]
                        # Round up to macro_block_size=16 to avoid FFmpeg resizing warnings
                        width = (width + 15) // 16 * 16
                        height = (height + 15) // 16 * 16
                        capture_region["width"] = width
                        capture_region["height"] = height
                        info(f"Video writer: {width}x{height} @ {self.fps} FPS")
                        writer = imageio_ffmpeg.write_frames(
                            self.output_path,
                            size=(width, height),
                            fps=self.fps,
                            pix_fmt_in="bgra",
                            pix_fmt_out="yuv420p",
                            codec="libx264",
                        )
                        writer.send(None)

                    # Periodically re-check Chrome window position. Use the
                    # cached HWND for the cheap path; fall back to full
                    # _find_chrome_window only if the window is gone.
                    if chrome_locked and (loop_start - last_bounds_check) >= bounds_check_interval:
                        new_bounds = _get_window_rect(cached_hwnd) if cached_hwnd else None
                        if new_bounds is None:
                            # HWND went away (window closed/minimized) — re-resolve.
                            found = _find_chrome_window(target_pid=self._target_pid)
                            if found:
                                new_bounds, cached_hwnd = found
                        if new_bounds and new_bounds["width"] > 100 and new_bounds["height"] > 100:
                            if (new_bounds["left"] != capture_region["left"]
                                    or new_bounds["top"] != capture_region["top"]):
                                capture_region["left"] = new_bounds["left"]
                                capture_region["top"] = new_bounds["top"]
                            if not resize_warned and (
                                    abs(new_bounds["width"] - capture_region["width"]) > 10
                                    or abs(new_bounds["height"] - capture_region["height"]) > 10):
                                warn("Chrome window resized during recording — video may be cropped or padded.")
                                resize_warned = True
                        last_bounds_check = loop_start

                    screenshot = sct.grab(capture_region)
                    # Skip the np.array round-trip — mss.ScreenShot.bgra is
                    # already a bytes view in BGRA layout (P3, ~3ms/frame win).
                    writer.send(screenshot.bgra)

                    elapsed = time.time() - loop_start
                    sleep_time = frame_duration - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)

        except Exception as e:
            error(f"Video recording thread failed: {e}")
            log_exception()
        finally:
            self.is_recording = False
            if writer is not None:
                try:
                    writer.close()
                    log_video(f"Recording finalized and saved to {self.output_path}")
                except Exception as e:
                    error(f"Failed to close video writer: {e}")
                    log_exception()

    def stop(self) -> float | None:
        """Stops the recording thread and returns the exact start timestamp for alignment."""
        if self.is_recording:
            info("Halting video recording...")
            self.is_recording = False

        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                warn("Video recording thread did not exit within 5s.")
                return None

        return self.video_start_unix

    @staticmethod
    def split_video(input_file: str, output_file: str, start_time_sec: float, end_time_sec: float) -> bool:
        """
        Slices a video using FFmpeg.
        Re-encodes the tiny chunk to ensure exact timestamps and prevent 1KB empty files.
        """
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

            duration = end_time_sec - start_time_sec

            cmd = [
                ffmpeg_exe,
                "-y",
                # Put -ss BEFORE -i for fast, accurate seeking
                "-ss",
                str(start_time_sec),
                "-i",
                input_file,
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                output_file,
            ]

            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=FFMPEG_TIMEOUT,
            )

            if os.path.exists(output_file) and os.path.getsize(output_file) > 1000:
                return True
            else:
                error(f"FFmpeg produced an empty or invalid clip for {output_file}.")
                error(f"FFmpeg Error Log: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            error(f"FFmpeg video split timed out after {FFMPEG_TIMEOUT}s for {output_file}")
            return False
        except Exception as e:
            error(f"Unexpected error during video split: {e}")
            log_exception()
            return False
