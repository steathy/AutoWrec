import ctypes
import os
import subprocess
import sys
import threading
import time

import imageio_ffmpeg
import mss
import numpy as np

from ..console import error, info, log_exception, warn
from ..console import video as log_video

FFMPEG_TIMEOUT = 120  # seconds — guard against hanging FFmpeg slice operations


def _find_chrome_window(target_pid: int | None = None) -> dict | None:
    """Find a Chrome/Chromium window's bounds using the Windows API.

    When *target_pid* is given, only windows owned by that process (or its
    children) are considered. This avoids grabbing an unrelated Chrome
    window the user already had open.
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
                    result = {"left": rect.left, "top": rect.top, "width": w, "height": h}
                    return False
            return True

        EnumWindows(WNDENUMPROC(callback), 0)
        return result
    except Exception:
        return None


def _get_process_tree(parent_pid: int) -> set[int]:
    """Return the set containing *parent_pid* and all its descendant PIDs."""
    pids = {parent_pid}
    try:
        import subprocess

        out = subprocess.check_output(
            ["wmic", "process", "where", f"(ParentProcessId={parent_pid})", "get", "ProcessId"],
            text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.strip().splitlines()[1:]:
            line = line.strip()
            if line.isdigit():
                child = int(line)
                pids.add(child)
                pids |= _get_process_tree(child)
    except Exception:
        pass
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
                            chrome_bounds = _find_chrome_window(target_pid=pid)
                            if chrome_bounds and chrome_bounds["width"] > 100 and chrome_bounds["height"] > 100:
                                capture_region = chrome_bounds
                                chrome_locked = True
                                log_video(
                                    f"Capturing Chrome window (PID {pid}): "
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

                    # Periodically re-check Chrome window position
                    if chrome_locked and (loop_start - last_bounds_check) >= bounds_check_interval:
                        new_bounds = _find_chrome_window(target_pid=self._target_pid)
                        if new_bounds and new_bounds["width"] > 100 and new_bounds["height"] > 100:
                            if (new_bounds["left"] != capture_region["left"]
                                    or new_bounds["top"] != capture_region["top"]):
                                capture_region["left"] = new_bounds["left"]
                                capture_region["top"] = new_bounds["top"]
                            if (abs(new_bounds["width"] - capture_region["width"]) > 10
                                    or abs(new_bounds["height"] - capture_region["height"]) > 10):
                                warn("Chrome window resized during recording — video may be cropped or padded.")
                        last_bounds_check = loop_start

                    screenshot = sct.grab(capture_region)
                    writer.send(np.array(screenshot).tobytes())

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

    def split_video(self, input_file: str, output_file: str, start_time_sec: float, end_time_sec: float) -> bool:
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

            if os.path.exists(output_file) and os.path.getsize(output_file) > 5000:
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
