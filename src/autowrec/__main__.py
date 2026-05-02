"""
CLI entry point for AutoWrec.

Usage:
    python -m autowrec record <url>   # Record a browser session
    python -m autowrec mcp            # Start the MCP server
"""

import argparse
import multiprocessing
import sys
import threading

from .console import error, info, rule

_preload_error = None


def _peek_command() -> str:
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            return arg
    return ""


def _preload():
    global _preload_error
    try:
        from . import config

        config.ensure_output_dirs()

        from .console import init_file_logger

        init_file_logger(str(config.LOGS_DIR))

        cmd = _peek_command()

        _is_verbose = "--verbose" in sys.argv

        if _is_verbose:
            config.VERBOSE = True
            import logging

            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            logging.getLogger("autowrec").addHandler(handler)
            logging.getLogger("autowrec").setLevel(logging.DEBUG)

        if cmd in ("record", "mcp"):
            import imageio_ffmpeg  # noqa: F401
            import mss  # noqa: F401
            import numpy  # noqa: F401
            import zendriver  # noqa: F401

    except Exception as exc:
        _preload_error = exc


def _apply_config_overrides(args):
    from . import config

    if getattr(args, "output_dir", None):
        from pathlib import Path

        config.OUTPUT_DIR = Path(args.output_dir).resolve()
        config.WORKSPACE_DIR = config.OUTPUT_DIR / "workspace"
        config.BLOCKLIST_DIR = config.OUTPUT_DIR / "blocklist"
        config.BLOCKLIST_DB = config.OUTPUT_DIR / "blocklist.db"
    if getattr(args, "sandbox_timeout", None) is not None:
        config.SANDBOX_TIMEOUT_SECONDS = max(1, args.sandbox_timeout)
    if getattr(args, "no_banner", False):
        config.BANNER_ENABLED = False
    if getattr(args, "no_blocklist", False):
        config.BLOCKLIST_ENABLED = False
    if getattr(args, "redact", False):
        config.REDACT_SENSITIVE = True
    if getattr(args, "verbose", False):
        config.VERBOSE = True


def cmd_record(args):
    _apply_config_overrides(args)
    from .recorder import run_recording

    success = run_recording(url=args.url)
    if not success:
        error("Recording failed or produced no output.")
        sys.exit(1)
    info("Recording complete.")


def cmd_mcp(args):
    _apply_config_overrides(args)
    from .mcp_server import run_mcp_server

    run_mcp_server()


def _print_rich_help():
    from rich.table import Table
    from rich.text import Text

    from . import config
    from .console import console

    console.print()
    ver = config.VERSION
    console.print(
        f"[bold]AutoWrec[/bold] [dim]v{ver}[/dim]"
        " — Record browser sessions and reverse-engineer them"
        " into automation scripts."
    )
    console.print()

    rule("USAGE", style="cyan")
    console.print("  autowrec <command> [options]")
    console.print()

    rule("COMMANDS", style="cyan")
    t = Table(show_header=False, box=None, collapse_padding=True)
    t.add_column(style="bold", min_width=16)
    t.add_column()
    t.add_row("record <url>", "Capture a browser session (screen + network + actions)")
    t.add_row("mcp", "Start the MCP server (for Claude Code / Codex integration)")
    console.print(t)
    console.print()

    rule("KEYBOARD SHORTCUTS", style="cyan")
    t2 = Table(show_header=False, box=None, collapse_padding=True)
    t2.add_column(style="bold", min_width=16)
    t2.add_column()
    t2.add_row(Text("RECORDING", style="bold bright_cyan"), "")
    t2.add_row("  Ctrl+C", "Stop recording and save session")
    t2.add_row("  Close browser", "Stop recording and save session")
    console.print(t2)
    console.print()

    rule("CONFIG", style="cyan")
    console.print("  [dim]~/.autowrec/config.toml[/dim]")
    t3 = Table(show_header=False, box=None, collapse_padding=True)
    t3.add_column(style="bold", min_width=16)
    t3.add_column()
    t3.add_row("  recording", "Capture FPS, clip padding, and merge thresholds")
    t3.add_row("  mcp", "MCP server settings (video toggle)")
    t3.add_row("  banner", "Startup animation toggle and speed")
    t3.add_row("  output", "Root directory for all generated output")
    console.print(t3)
    console.print()

    rule("OPTIONS", style="cyan")
    t4 = Table(show_header=False, box=None, collapse_padding=True)
    t4.add_column(style="bold", min_width=24)
    t4.add_column()
    t4.add_row("--output-dir PATH", "Root directory for all output (default: ./output)")
    t4.add_row("--sandbox-timeout SEC", "Timeout for IPython code execution (default: 60)")
    t4.add_row("--no-banner", "Skip the startup animation")
    t4.add_row("--no-blocklist", "Disable ad/tracker domain filtering")
    t4.add_row("--redact", "Redact passwords, auth headers, and cookies")
    t4.add_row("--verbose", "Show detailed diagnostic output")
    t4.add_row("-V, --version", "Show version")
    t4.add_row("-h, --help", "Show this help message")
    console.print(t4)
    console.print()


def main():
    _is_help = any(a in sys.argv for a in ("--help", "-h"))
    _is_version = any(a in sys.argv for a in ("--version", "-V"))

    if _is_version:
        from . import config

        print(f"autowrec {config.VERSION}")
        sys.exit(0)

    if _is_help:
        _print_rich_help()
        sys.exit(0)

    if len(sys.argv) < 2:
        _print_rich_help()
        sys.exit(0)

    preload_thread = threading.Thread(target=_preload, daemon=True)
    preload_thread.start()

    from . import config
    from .autowrec_banner import show_startup

    cmd = _peek_command()

    if config.BANNER_ENABLED and cmd == "record" and "--no-banner" not in sys.argv:
        show_startup(
            version=config.VERSION,
            model="MCP",
            recorder_model="",
            speed=config.BANNER_SPEED,
        )

    preload_thread.join()

    if _preload_error is not None:
        error(f"Startup init failed: {_preload_error}")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        prog="autowrec",
        add_help=False,
    )
    subparsers = parser.add_subparsers(dest="command")

    def _add_common_flags(p):
        p.add_argument("--sandbox-timeout", type=int, metavar="SECONDS")
        p.add_argument("--output-dir", metavar="PATH")
        p.add_argument("--no-banner", action="store_true", default=False)
        p.add_argument("--no-blocklist", action="store_true", default=False)
        p.add_argument("--redact", action="store_true", default=False)
        p.add_argument("--verbose", action="store_true", default=False)
        p.add_argument("-h", "--help", action="store_true", default=False, dest="help_flag")
        p.add_argument("-V", "--version", action="store_true", default=False)

    p_record = subparsers.add_parser("record", add_help=False)
    p_record.add_argument("url", nargs="?", default="about:blank")
    _add_common_flags(p_record)
    p_record.set_defaults(func=cmd_record)

    p_mcp = subparsers.add_parser("mcp", add_help=False)
    _add_common_flags(p_mcp)
    p_mcp.set_defaults(func=cmd_mcp)

    args = parser.parse_args()

    if getattr(args, "help_flag", False) or getattr(args, "version", False):
        if getattr(args, "version", False):
            print(f"autowrec {config.VERSION}")
        else:
            _print_rich_help()
        sys.exit(0)

    if not args.command:
        _print_rich_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
