# AutoWrec v1.0

A browser session recorder that captures network traffic, user actions, and screen video — then exposes everything as tools for AI coding assistants.

Works as a **standalone CLI** or as an **MCP server** for Claude Code, Codex, and other MCP-compatible AI tools. No API keys needed.

Based on [AutomatiQ](https://github.com/StoneSteel27/AutomatiQ) by Kanishq Vijay, licensed under MIT.

## How It Works

```
You browse a website         AutoWrec captures everything          AI explores via MCP tools
    (Chrome)         -->     (network, actions, video)      -->    (Claude Code, Codex, etc.)
```

**Record** — AutoWrec launches Chrome with CDP instrumentation. You browse normally. Every HTTP request/response, click, keystroke, and page navigation is captured. Screen video is optional and captures only the Chrome window (not your full desktop).

**Explore** — The AI tool reads the structured workspace through MCP tools: session summaries, paginated timelines, individual HTTP transactions, file contents, and extracted video frames.

**Build** — The AI tool uses the persistent IPython sandbox to test hypotheses against the live site and assemble a standalone automation script.

## Installation

```bash
pip install git+https://github.com/steathy/AutoWrec.git
```

For local development:

```bash
git clone https://github.com/steathy/AutoWrec.git
cd AutoWrec
pip install -e .
```

## Usage

### As an MCP Server

AutoWrec runs as an MCP (Model Context Protocol) server over stdio. AI tools like Claude Code and Codex connect to it and call its tools directly — no API keys needed, AutoWrec makes zero LLM calls.

#### Setup with Claude Code

**Recommended — pip install + direct invocation** (fastest startup):

```bash
pip install git+https://github.com/steathy/AutoWrec.git
```

Add to your project's `.mcp.json` or global `~/.claude.json`:

```json
{
  "mcpServers": {
    "autowrec": {
      "command": "python",
      "args": ["-m", "autowrec.mcp_server"]
    }
  }
}
```

**Alternative — uvx** (no pip install needed, but slower startup on Windows):

```json
{
  "mcpServers": {
    "autowrec": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/steathy/AutoWrec.git", "autowrec-mcp"]
    }
  }
}
```

#### Verify the MCP Server

After adding the config, restart Claude Code. You should see `autowrec` listed when you run `/mcp` in Claude Code. The AI tool now has access to 8 tools:

| Tool | Purpose |
|------|---------|
| `record_session` | Launch browser, capture session, compile workspace |
| `read_session_summary` | Session metadata, action flow, statistics |
| `read_timeline` | Paginated interleaved user actions + network requests |
| `read_transaction` | HTTP transaction details (headers, cookies, bodies) |
| `list_workspace_files` | Browse the workspace directory |
| `read_file` | Read any file with byte-level pagination |
| `extract_video_frames` | Get base64 JPEG frames from video clips |
| `execute_code` | Run Python in a persistent IPython sandbox |

#### Example MCP Workflow

Once connected, ask the AI tool to:

1. **Record**: "Use autowrec to record a session on https://example.com" — Chrome opens, you browse, close the browser to stop.
2. **Explore**: "Read the session summary" — the AI calls `read_session_summary` and reviews what was captured.
3. **Drill down**: "Show me the login POST request" — the AI uses `read_timeline` to find it, then `read_transaction` for headers/body.
4. **Build**: "Write a Python script that automates this login" — the AI uses `execute_code` to prototype against the live site.

### As a Standalone CLI

```bash
autowrec record https://example.com   # Record a session
autowrec mcp                          # Start the MCP server
```

Options:

```
--output-dir PATH     Root directory for all output (default: ./output)
--no-banner           Skip the startup animation
--no-blocklist        Disable ad/tracker domain filtering
--redact              Redact passwords, auth headers, and cookies
--verbose             Show detailed diagnostic output
-V, --version         Show version
-h, --help            Show help
```

### Demo Script

```bash
python tests/demo_record.py https://httpbin.org/forms/post
```

Records a session, then prints a detailed report of everything captured.

## What Gets Captured

```
output/workspace/session_dump/
├── SUMMARY.json              # Session metadata + statistics
├── timeline.json             # Time-sorted actions + network events
├── full_record.mp4           # Full screen recording (if video enabled)
├── clips/                    # Per-action video segments
│   └── action_clip_000.mp4
└── requests/                 # One folder per HTTP transaction
    └── 000_GET_example.com/
        ├── transaction.json  # Headers, cookies, timing, security flags
        ├── req_payload.*     # Request body
        └── res_body.*        # Response body
```

## Configuration

`~/.autowrec/config.toml` (created on first run):

```toml
[recording]
fps = 3                    # Frames per second for video capture
segment_pad = 2            # Seconds of padding around action clips
merge_gap_threshold = 1.5  # Merge clips closer than this
blocklist_enabled = true   # Filter ad/tracker domains from captures
redact_sensitive = false   # Redact passwords, auth headers, cookies

[agent]
sandbox_timeout = 60       # Seconds per IPython cell

[mcp]
video_enabled = false      # Enable video in MCP mode

[banner]
enabled = true
speed = 1.0
```

## Testing

```bash
python tests/test_standalone.py
```

Runs 54 tests covering imports, config, MCP tools, path traversal protection, workspace operations, IPython sandbox, and console redirect safety.

## Architecture

```
src/autowrec/
├── mcp_server.py          # FastMCP server (8 tools over stdio)
├── config.py              # Global configuration + TOML loader
├── console.py             # Rich terminal output
├── bin_manager.py         # Downloads rg, jq, sd for sandbox
├── recorder/
│   ├── __init__.py        # Recording orchestration
│   ├── browser_agent.py   # Chrome CDP instrumentation (zendriver)
│   ├── video_recorder.py  # Chrome window capture (MSS + FFmpeg)
│   ├── data_compressor.py # Workspace compilation
│   ├── blocklist_db.py    # Ad/tracker domain filter (SQLite)
│   └── js/telemetry.js    # Injected browser event tracking
└── ipython_sandbox/       # Isolated Python execution environment
    ├── sandbox.py         # Process manager
    ├── worker.py          # IPython kernel (subprocess)
    └── utils.py           # Output formatting + process control

tests/
├── test_standalone.py     # 54 unit/integration tests
└── demo_record.py         # Interactive recording demo
```

## Key Features

- **Chrome-only video capture (Windows)** — records only the Chrome window, not your full desktop. Tracks window position if you move it. On Linux/macOS, falls back to full-screen capture.
- **PID-based window targeting** — correctly identifies the recording Chrome instance even if you have other Chrome windows open (Windows).
- **Browser close detection** — closing the Chrome window automatically stops the recording (no Ctrl+C needed).
- **Zero LLM dependencies** — no API keys, no litellm, no instructor. The host AI provides all intelligence.
- **Path traversal protection** — all file access tools validate paths stay within the workspace.
- **Stderr-safe console** — all Rich output redirected to stderr in MCP mode so stdout remains clean for JSON-RPC.
- **Persistent sandbox** — IPython state (variables, imports) persists across `execute_code` calls.
- **Ad/tracker filtering** — SQLite-backed domain blocklist with LRU cache filters noise from captured traffic. Toggleable via config or `--no-blocklist`.
- **Opt-in redaction** — `--redact` flag or config to sanitize passwords, auth headers, and cookies in captures. Off by default for throwaway-account testing.

## Requirements

- Python 3.11+
- Chrome/Chromium (for recording)
- FFmpeg (bundled via imageio-ffmpeg)

## License

MIT License. See [LICENSE](LICENSE).

Based on [AutomatiQ](https://github.com/StoneSteel27/AutomatiQ) by Kanishq Vijay (StoneSteel27).
