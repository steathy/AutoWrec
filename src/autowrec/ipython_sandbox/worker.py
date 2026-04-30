import multiprocessing
import os
import sys
import threading

BUSYBOX_COMMANDS = [
    "ls",
    "rm",
    "cp",
    "mv",
    "mkdir",
    "rmdir",
    "pwd",
    "cat",
    "wc",
    "sort",
    "uniq",
    "echo",
    "touch",
    "df",
    "du",
    "base64",
    "basename",
    "dirname",
    "env",
    "sleep",
    "grep",
    "tr",
    "tee",
    "mktemp",
    "seq",
    "awk",
    "head",
    "tail",
    "sh",
]

STANDALONE_COMMANDS = ["rg", "jq", "sd"]


def apply_path_jail(bin_dir: str, workspace: str):
    """Creates an isolated PATH environment.

    Windows: BusyBox multicall binary provides coreutils; PATH is wiped to jailed_bin only.
    POSIX:   System coreutils already exist in /usr/bin; jailed_bin is prepended to PATH
             so our standalone binaries (rg, jq, sd) take priority.
    """
    jailed_bin = os.path.join(workspace, ".jailed_bin")
    os.makedirs(jailed_bin, exist_ok=True)

    if sys.platform == "win32":
        bb_src = os.path.join(bin_dir, "busybox.exe")
        if os.path.exists(bb_src):
            for cmd in BUSYBOX_COMMANDS:
                dst = os.path.join(jailed_bin, f"{cmd}.exe")
                if not os.path.exists(dst):
                    try:
                        os.link(bb_src, dst)
                    except OSError:
                        import shutil

                        shutil.copy2(bb_src, dst)
        else:
            print(f"[WARN] Path jail failed: Could not find {bb_src}")

    for cmd in STANDALONE_COMMANDS:
        exe_name = f"{cmd}.exe" if sys.platform == "win32" else cmd
        src = os.path.join(bin_dir, exe_name)
        dst = os.path.join(jailed_bin, exe_name)

        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.link(src, dst)
            except OSError:
                import shutil

                shutil.copy2(src, dst)

    if sys.platform == "win32":
        os.environ["PATH"] = jailed_bin
        os.environ["COMSPEC"] = os.path.join(jailed_bin, "sh.exe")
    else:
        # Prepend jailed_bin so our binaries take priority, but keep system PATH
        # for coreutils (/usr/bin/ls, /usr/bin/grep, etc.)
        os.environ["PATH"] = jailed_bin + os.pathsep + os.environ.get("PATH", "/usr/bin")
        os.environ["SHELL"] = "/bin/sh"


def _win_poller_worker(interrupt_event):
    import _thread

    while True:
        if interrupt_event.wait(1.0):
            interrupt_event.clear()
            _thread.interrupt_main()


def ipython_worker(
    command_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    working_dir: str,
    output_file: str,
    interrupt_event,
    bin_path: str,
):
    if sys.platform != "win32":
        import signal

        os.setpgrp()
        signal.signal(signal.SIGINT, signal.default_int_handler)

    try:
        os.chdir(os.path.realpath(working_dir))
    except Exception as e:
        result_queue.put({"status": "crash", "exit_code": 1, "ret_val": f"Failed to enter directory: {e}"})
        return

    if bin_path and os.path.exists(bin_path):
        apply_path_jail(bin_path, working_dir)

    if sys.platform == "win32" and interrupt_event is not None:
        poller = threading.Thread(target=_win_poller_worker, args=(interrupt_event,), daemon=True)
        poller.start()

    from IPython.core.interactiveshell import InteractiveShell

    InteractiveShell.colors = "nocolor"
    InteractiveShell.color_info = False
    shell = InteractiveShell.instance()

    if sys.platform == "win32":
        import subprocess

        from IPython.utils.text import SList

        sh_path = os.path.join(os.environ["PATH"], "sh.exe")

        def busybox_system(cmd):
            """Handles standard interactive shell commands: !ls"""
            subprocess.run([sh_path, "-c", cmd], stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.DEVNULL)

        def busybox_getoutput(cmd, split=True, depth=0):
            """Handles captured shell commands: x = !ls"""
            try:
                result = subprocess.run(
                    [sh_path, "-c", cmd],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                out = result.stdout
            except Exception as e:
                out = str(e)

            if split:
                return SList(out.splitlines())
            return out

        shell.system = busybox_system
        shell.getoutput = busybox_getoutput

    shell.displayhook.write_output_prompt = lambda: None
    shell.displayhook.write_format_data = lambda *args, **kwargs: None

    while True:
        try:
            cell_id, command = command_queue.get()
            if cell_id == "__PING__":
                result_queue.put({"status": "success", "exit_code": 0, "ret_val": "PONG"})
                continue

            with open(output_file, "a", encoding="utf-8", buffering=1) as f:
                original_stdout_fd = os.dup(1)
                original_stderr_fd = os.dup(2)
                original_stdout = sys.stdout
                original_stderr = sys.stderr

                os.dup2(f.fileno(), 1)
                os.dup2(f.fileno(), 2)
                sys.stdout = f
                sys.stderr = f

                try:
                    result = shell.run_cell(command, cell_id=cell_id)
                except KeyboardInterrupt:
                    raise
                finally:
                    if hasattr(sys.stdout, "flush"):
                        sys.stdout.flush()
                    if hasattr(sys.stderr, "flush"):
                        sys.stderr.flush()

                    sys.stdout = original_stdout
                    sys.stderr = original_stderr

                    os.dup2(original_stdout_fd, 1)
                    os.dup2(original_stderr_fd, 2)
                    os.close(original_stdout_fd)
                    os.close(original_stderr_fd)

            ret_val = ""
            if result.error_in_exec:
                ret_val = str(result.error_in_exec)
            elif result.result is not None:
                ret_val = str(result.result)

            result_queue.put(
                {
                    "status": "error" if result.error_in_exec else "success",
                    "exit_code": 1 if result.error_in_exec else 0,
                    "ret_val": ret_val,
                }
            )

        except KeyboardInterrupt:
            result_queue.put({"status": "error", "exit_code": 1, "ret_val": "KeyboardInterrupt"})

        except BaseException as e:
            result_queue.put({"status": "crash", "exit_code": 1, "ret_val": f"Shell Error: {str(e)}"})
