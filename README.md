# AutoWrec v1.5

A browser session recorder that captures network traffic and user actions — then exposes everything as tools for AI coding assistants.

Works as a **standalone CLI** or as an **MCP server** for Claude Code, Codex, and other MCP-compatible AI tools. No API keys needed.

Based on [AutomatiQ](https://github.com/StoneSteel27/AutomatiQ) by Kanishq Vijay, licensed under MIT.

## How It Works

```
You browse a website         AutoWrec captures everything          AI explores via MCP tools
    (Chrome)         -->        (network, actions)           -->    (Claude Code, Codex, etc.)
```

**Record** — AutoWrec launches Chrome with CDP instrumentation. You browse normally. Every HTTP request/response, click, keystroke, and page navigation is captured.

**Explore** — The AI tool reads the structured workspace through MCP tools: session summaries, paginated timelines, individual HTTP transactions, and file contents.

**Build** — The AI tool uses the persistent Python environment to test hypotheses against the live site and assemble a standalone automation script.

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

After adding the config, restart Claude Code. You should see `autowrec` listed when you run `/mcp` in Claude Code. The AI tool now has access to 9 tools:

| Tool | Purpose | Key parameters |
|------|---------|----------------|
| `record_session` | Launch browser and start recording (non-blocking) | `url`, `proxy`, `chrome_path` |
| `check_recording` | Poll whether the recording is still running or finished | — |
| `read_session_summary` | Lean session digest by default | `verbose=true` for full SUMMARY.json |
| `read_timeline` | Paginated time-sorted user actions + network requests | `offset`, `limit`, `summary=false` for full events |
| `read_transaction` | HTTP transaction metadata (headers, cookies; **never** body content) | `level=minimal\|headers\|full` |
| `list_workspace_files` | Browse the workspace directory | `subdirectory`, `include_sizes=true` |
| `read_file` | Read a workspace file | `mode=stat\|head\|raw`, `offset`, `limit` (raw only) |
| `execute_code` | Run Python in a persistent IPython environment | `code`, `timeout` |
| `download_chrome` | Download Chrome 136 for authenticated proxy support | — |

> **v1.3 note:** `read_transaction` no longer inlines body content. To read a request payload or response body, call `read_file` on `<request_folder>/req_payload.<ext>` or `<request_folder>/res_body.<ext>` — the extension is in `request.content_detection.extension` (visible at `level=full`).

#### Example MCP Workflow

Once connected, ask the AI tool to:

1. **Record**: "Use autowrec to record a session on https://example.com" — Chrome opens, you browse, close the browser to stop.
2. **Explore**: "Read the session summary" — the AI calls `read_session_summary` (lean digest by default) and reviews counts + top domains.
3. **Drill down**: "Show me the login POST request" — the AI uses `read_timeline` (compact summary) to find it, calls `read_transaction` for metadata, then `read_file` for the request payload.
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
--proxy URL           Proxy for browser traffic (http, socks4, socks5)
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
blocklist_enabled = true   # Filter ad/tracker domains from captures
redact_sensitive = false   # Redact passwords, auth headers, cookies

[agent]
sandbox_timeout = 60       # Seconds per IPython cell

[banner]
enabled = true
speed = 1.0
```

### Proxy Support

AutoWrec can route all browser traffic through an HTTP, HTTPS, SOCKS4, or SOCKS5 proxy.

```bash
# Via CLI flag
autowrec record https://example.com --proxy http://proxy.corp:8080

# Via environment variable
export AUTOWREC_PROXY=http://user:pass@proxy.corp:3128
autowrec record https://example.com
```

Or via config.toml:

```toml
[proxy]
url = "http://proxy.corp:8080"
```

**Authenticated HTTP proxies** are supported — embed credentials in the URL:
`http://user:pass@proxy.corp:3128`. Credentials are handled via CDP at the protocol
level (not leaked to Chrome's UI).

**SOCKS4/SOCKS5 proxies** work for unauthenticated connections: `socks5://host:port` or `socks4://host:port`.
SOCKS5 with username/password auth is a Chrome limitation (Chromium #256785) and
is not supported. Use IP whitelisting or a local proxy forwarder.

**Chrome < 137 required for authenticated proxies:** Chrome 137+ removed `--load-extension`
support needed for proxy auth. AutoWrec checks the Chrome version automatically. Options:
- If your system Chrome is < 137, it's used directly (no extra download).
- Otherwise, use the `download_chrome` MCP tool:
  - **Windows/macOS:** downloads consumer Chrome 136 (auto-extraction on Windows; manual on macOS).
  - **Linux:** downloads consumer Chrome 136 .deb from UChicago CS mirror (auto-extraction with `dpkg-deb`; Chrome for Testing fallback if mirror unavailable).
- Or pass the path to any Chrome < 137 via `--chrome-path` CLI flag or MCP `chrome_path`.

**Priority:** `--proxy` CLI flag or MCP `record_session(proxy=...)` parameter >
`AUTOWREC_PROXY` env var > `[proxy] url` in config.toml.

## Testing

```bash
python tests/test_standalone.py     # primary integration suite
python tests/test_debug_review.py   # regression suite from review passes
```

`test_standalone.py` covers imports, config validation, MCP tools, path traversal protection, workspace operations, IPython execution, console redirect safety, redaction, input validation, and binary body encoding via `read_file`.

`test_debug_review.py` is the regression suite seeded by the v1.2 → v1.3 code-review passes. It pins behavior for fixes in C1, C2, B1–B10, M-series token budgets, and P-series perf items.

## Architecture

```
src/autowrec/
├── mcp_server.py          # FastMCP server (9 tools over stdio)
├── config.py              # Global configuration + TOML loader
├── console.py             # Rich terminal output
├── bin_manager.py         # Downloads rg, jq, sd for execution environment
├── recorder/
│   ├── __init__.py        # Recording orchestration
│   ├── browser_agent.py   # Chrome CDP instrumentation (zendriver)
│   ├── data_compressor.py # Workspace compilation
│   ├── blocklist_db.py    # Ad/tracker domain filter (SQLite)
│   └── js/telemetry.js    # Injected browser event tracking
└── ipython_sandbox/       # Persistent Python execution environment
    ├── sandbox.py         # Process manager
    ├── worker.py          # IPython kernel (subprocess)
    └── utils.py           # Output formatting + process control

tests/
├── test_standalone.py     # Unit/integration tests
├── test_debug_review.py   # Regression suite from review passes
└── demo_record.py         # Interactive recording demo
```

## Key Features

- **Browser close detection** — closing the Chrome window automatically stops the recording (no Ctrl+C needed).
- **Zero LLM dependencies** — no API keys, no litellm, no instructor. The host AI provides all intelligence.
- **Path traversal protection** — all file access tools validate paths stay within the workspace.
- **Stderr-safe console** — all Rich output redirected to stderr in MCP mode so stdout remains clean for JSON-RPC.
- **Persistent Python environment** — IPython state (variables, imports) persists across `execute_code` calls. Runs with local user permissions. On Windows, the PATH is jailed to bundled binaries (`rg`, `jq`, `sd`); system commands like `pip` and `git` are not available inside `execute_code`.
- **Ad/tracker filtering** — SQLite-backed domain blocklist with LRU cache filters noise from captured traffic. Toggleable via config or `--no-blocklist`.
- **Opt-in redaction** — `--redact` flag or config to sanitize passwords, auth headers, and cookies in captures. Off by default for throwaway-account testing.

## Requirements

- Python 3.11+
- Chrome/Chromium (for recording)

## License

MIT License. See [LICENSE](LICENSE).

Based on [AutomatiQ](https://github.com/StoneSteel27/AutomatiQ) by Kanishq Vijay (StoneSteel27).
