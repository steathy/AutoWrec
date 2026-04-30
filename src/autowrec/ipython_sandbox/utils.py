import itertools
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024
MAX_LINE_LENGTH = 1000


def compress_line_horizontally(line: str, threshold=30) -> str:
    if len(line) < threshold:
        return line
    original_len = len(line)

    prev_line = ""
    while prev_line != line:
        prev_line = line
        for match in re.finditer(r"(.+?)(?:\1){10,}", line):
            pattern = match.group(1)
            if not pattern.isspace():
                full_match = match.group(0)
                count = len(full_match) // len(pattern)
                replacement = f"{pattern}<{repr(pattern)} repeated {count - 2} times>{pattern}"
                line = line.replace(full_match, replacement)
                break

    if len(line) < threshold:
        if len(line) < original_len:
            logger.debug(f"Horizontal compression reduced line from {original_len} to {len(line)} chars.")
        return line

    compressed_parts = []
    for char, group in itertools.groupby(line):
        count = sum(1 for _ in group)
        if char.isspace():
            compressed_parts.append(char * count)
        elif count > threshold:
            compressed_parts.append(f"{char}<{repr(char)} repeated {count - 2} times>{char}")
        else:
            compressed_parts.append(char * count)

    final_line = "".join(compressed_parts)
    if len(final_line) < original_len:
        logger.debug(f"Horizontal compression reduced line from {original_len} to {len(final_line)} chars.")
    return final_line


def format_output(raw_string: str, cell_id: str, offset_line: int = 1, chunk_offset: int = 1) -> str:
    if not raw_string:
        return ""

    logger.debug(
        f"[{cell_id}] Formatting output. Raw size: {len(raw_string)} bytes. Offset: {offset_line}({chunk_offset})"
    )
    lines = raw_string.split("\n")

    raw_lines, total_bytes, has_more = [], 0, False
    stopped_line, stopped_chunk = None, None

    for line_idx, line in enumerate(lines, start=1):
        if line_idx < offset_line:
            continue

        if len(line) > MAX_LINE_LENGTH:
            chunks = [line[i : i + MAX_LINE_LENGTH] for i in range(0, len(line), MAX_LINE_LENGTH)]
            total_chunks = len(chunks)
            for chunk_idx, chunk in enumerate(chunks, start=1):
                if line_idx == offset_line and chunk_idx < chunk_offset:
                    continue

                label = f"{line_idx}({chunk_idx}/{total_chunks})"
                line_size = len(label) + 3 + len(chunk) + 1

                if total_bytes + line_size > MAX_BYTES:
                    has_more, stopped_line, stopped_chunk = True, line_idx, chunk_idx
                    break

                raw_lines.append((label, chunk))
                total_bytes += line_size
            if has_more:
                break
        else:
            label = str(line_idx)
            line_size = len(label) + 3 + len(line) + 1

            if total_bytes + line_size > MAX_BYTES:
                has_more, stopped_line, stopped_chunk = True, line_idx, None
                break

            raw_lines.append((label, line))
            total_bytes += line_size

        if has_more:
            break

    if has_more:
        logger.warning(f"[{cell_id}] Output exceeded 10KB limit. Truncated at line {stopped_line}.")

    compressed_lines_vertical = []
    collapsed_count = 0

    for _text, group_iter in itertools.groupby(raw_lines, key=lambda x: x[1]):
        items = list(group_iter)
        count = len(items)
        if count > 3:
            compressed_lines_vertical.extend(
                [items[0], ("", f"... [{count - 2} identical lines collapsed] ..."), items[-1]]
            )
            collapsed_count += count - 2
        else:
            compressed_lines_vertical.extend(items)

    if collapsed_count > 0:
        logger.debug(f"[{cell_id}] Vertical compression collapsed {collapsed_count} redundant lines.")

    final_lines = []
    for label, text in compressed_lines_vertical:
        if label == "":
            final_lines.append((label, text))
        else:
            final_lines.append((label, compress_line_horizontally(text)))

    formatted_strs = [f"{' ':>10} | {text}" if label == "" else f"{label:>10} | {text}" for label, text in final_lines]
    output = "\n".join(formatted_strs)

    if has_more:
        resume_cmd = (
            f"%view_output {cell_id} --offset {stopped_line}({stopped_chunk}/{total_chunks})"
            if stopped_chunk
            else f"%view_output {cell_id} --offset {stopped_line}"
        )
        output += (
            f"\n\n... [OUTPUT TRUNCATED: Reached {MAX_BYTES // 1024}KB limit] ...\n"
            f"💡 HINT: Use `{resume_cmd}` to continue reading."
        )

    logger.debug(f"[{cell_id}] Output formatting complete. Final size: {len(output)} bytes.")
    return output


def parse_offset(offset_str: str) -> tuple[int, int]:
    if offset_str.isdigit():
        return int(offset_str), 1
    match = re.match(r"^(\d+)\s*\(\s*(\d+)(?:/\d+)?\s*\)$", offset_str.strip())
    if match:
        return int(match.group(1)), int(match.group(2))
    logger.error(f"Failed to parse offset string: '{offset_str}'")
    raise ValueError(f"Invalid offset format: {offset_str}")


def interrupt_process(process, interrupt_event):
    if sys.platform == "win32":
        logger.debug("Attempting soft interrupt via Threading Event (Windows).")
        interrupt_event.set()
    else:
        import signal

        if process and process.pid:
            logger.debug(f"Attempting soft interrupt via SIGINT to PGID {os.getpgid(process.pid)} (POSIX).")
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except ProcessLookupError:
                logger.debug(f"Process PID {process.pid} not found for soft interrupt.")


def hard_kill_process(process):
    if not process or not process.is_alive():
        return

    logger.debug(f"Executing HARD KILL on PID {process.pid}")
    if sys.platform == "win32":
        try:
            import subprocess

            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            process.kill()
    else:
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.error(f"Process Group kill failed: {e}. Falling back to standard process.kill().")
            process.kill()
