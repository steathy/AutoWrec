"""
Centralized rich console for all AutoWrec output.

Every module imports from here instead of using bare print() calls.
This gives us consistent styling, color-coded log levels, and
nice panels for agent output — all from a single shared Console.
"""

import logging
import time
import traceback
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

_theme = Theme(
    {
        "info": "bold cyan",
        "warn": "bold yellow",
        "error": "bold red",
        "success": "bold green",
        "action": "magenta",
        "ai": "bold magenta",
        "think": "italic",
        "exec": "bold yellow",
        "output": "white",
        "dim": "dim",
    }
)

console = Console(theme=_theme, highlight=False)

# ---------------------------------------------------------------------------
# File logger — writes timestamped entries + full tracebacks to
# output/logs/session_<timestamp>.log.  Initialized lazily by
# init_file_logger() which main.py calls after ensure_output_dirs().
# ---------------------------------------------------------------------------

_file_logger: logging.Logger | None = None


def init_file_logger(logs_dir: str) -> None:
    """Create the session log file and attach a file handler."""
    global _file_logger

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"{logs_dir}/session_{stamp}.log"

    _file_logger = logging.getLogger("autowrec.session")
    _file_logger.setLevel(logging.DEBUG)
    _file_logger.propagate = False

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-5s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    _file_logger.addHandler(handler)


def _log(level: int, msg: str) -> None:
    if _file_logger:
        _file_logger.log(level, msg)


def info(msg: str) -> None:
    console.print(f"[info]\\[INFO][/info] {escape(msg)}")
    _log(logging.INFO, msg)


def warn(msg: str) -> None:
    console.print(f"[warn]\\[WARN][/warn] {escape(msg)}")
    _log(logging.WARNING, msg)


def error(msg: str) -> None:
    console.print(f"[error]\\[ERROR][/error] {escape(msg)}")
    _log(logging.ERROR, msg)


def success(msg: str) -> None:
    console.print(f"[success]\\[SUCCESS][/success] {escape(msg)}")
    _log(logging.INFO, msg)


def action(msg: str) -> None:
    console.print(f"[action]\\[ACTION][/action] {escape(msg)}")
    _log(logging.INFO, msg)


def ai(msg: str) -> None:
    console.print(f"[ai]\\[AI][/ai] {escape(msg)}")
    _log(logging.INFO, msg)


def think(text: str) -> None:
    quoted = Panel(Markdown(text), title="[think]Thinking[/think]", border_style="dim", padding=(0, 1))
    console.print(quoted)


def countdown(seconds: int, message: str = "Retrying", cancel_check=None) -> bool:
    """Live countdown on a single line. Returns True if cancelled via *cancel_check*."""
    with Live(
        Text(f"{message} in {seconds}s ...", style="dim"), console=console, refresh_per_second=2, transient=True
    ) as live:
        for remaining in range(seconds, 0, -1):
            if cancel_check and cancel_check():
                return True
            live.update(Text(f"{message} in {remaining}s ...", style="dim"))
            time.sleep(1)
    return False


def code_block(code: str, lang: str = "python") -> None:
    syntax = Syntax(code, lang, theme="monokai", line_numbers=True, word_wrap=True)
    console.print(Panel(syntax, title="[exec]EXEC[/exec]", border_style="yellow", padding=(0, 0)))


def output_panel(text: str) -> None:
    console.print(Panel(escape(text), title="[output]OUTPUT[/output]", border_style="dim", padding=(0, 1)))


def step_info(step: int, prompt_tokens: int) -> None:
    console.print(f"[dim]Step {step} | Prompt tokens: {prompt_tokens}[/dim]")


def rule(title: str = "", style: str = "dim") -> None:
    console.print(Rule(title=title, style=style))


def detail(msg: str) -> None:
    _log(logging.DEBUG, msg)
    from . import config

    if config.VERBOSE:
        console.print(f"  [dim]{escape(msg)}[/dim]")


def print_exception() -> None:
    """Rich traceback to terminal + plain traceback to log file."""
    console.print_exception(show_locals=False)
    if _file_logger:
        _file_logger.error(traceback.format_exc())


def log_exception() -> None:
    """Plain traceback to the log file only — nothing on terminal."""
    if _file_logger:
        _file_logger.error(traceback.format_exc())


def spinner(message: str = "Working..."):
    """Returns a rich Status context manager for use with `with` blocks."""
    return console.status(f"[dim]{message}[/dim]", spinner="aesthetic", spinner_style="cyan")


def prompt() -> str:
    return console.input("[bold green]>>> [/bold green]")
