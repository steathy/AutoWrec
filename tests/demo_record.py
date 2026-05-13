"""
AutoWrec Recording Demo
=========================

This script records a browser session and then inspects the captured results.

HOW TO RUN:
    python demo_record.py [url]

    Example:
        python demo_record.py https://github.com/login
        python demo_record.py https://httpbin.org/forms/post

WHAT HAPPENS:
    1. Chrome opens with the URL
    2. You browse, click, type — do whatever you want to capture
    3. Close the browser window OR press Ctrl+C to stop
    4. The script compiles the workspace and prints a report of what was captured

OUTPUT:
    Everything is saved to ./output/workspace/session_dump/
"""

import json
import multiprocessing
import os
import sys


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://httpbin.org/forms/post"

    print(f"""
{'=' * 60}
  AutoWrec Recording Demo
{'=' * 60}

  Target URL: {url}

  INSTRUCTIONS:
    1. A Chrome window will open shortly
    2. Browse the site — log in, fill forms, click around
    3. When done: CLOSE THE BROWSER or press Ctrl+C here
    4. Wait for compilation, then review the report below

{'=' * 60}
""")

    input("  Press Enter to start recording...")
    print()

    # ── Record ──────────────────────────────────────────────────────────────

    from autowrec import config
    config.ensure_output_dirs()

    from autowrec.recorder import run_recording

    result = run_recording(url=url)

    if not result:
        print("\n  [ERROR] Recording failed or produced no output.")
        sys.exit(1)

    workspace = result
    print(f"\n  Recording saved to: {workspace}\n")

    # ── Inspect Results ─────────────────────────────────────────────────────

    print(f"{'=' * 60}")
    print("  CAPTURE REPORT")
    print(f"{'=' * 60}")

    # SUMMARY
    summary_path = os.path.join(workspace, "SUMMARY.json")
    if os.path.exists(summary_path):
        with open(summary_path, encoding="utf-8") as f:
            summary = json.load(f)

        stats = summary.get("statistics", {})
        session = summary.get("session", {})

        print(f"""
  Session Duration:  {session.get('duration_seconds', '?')}s
  Total Requests:    {stats.get('total_requests', 0)}
  Total Actions:     {stats.get('total_actions', 0)}
  Domains:           {len(stats.get('domains', {}))}
  With Auth Headers: {stats.get('with_auth', 0)}
  With Cookies:      {stats.get('with_cookies', 0)}
""")

        # Top domains
        domains = stats.get("domains", {})
        if domains:
            print("  Top Domains:")
            for domain, count in sorted(domains.items(), key=lambda x: -x[1])[:10]:
                print(f"    {domain}: {count} requests")
            print()

        # Status codes
        codes = stats.get("status_codes", {})
        if codes:
            print("  Status Codes:")
            for code, count in sorted(codes.items()):
                print(f"    {code}: {count}")
            print()

        # Session flow (user actions)
        flow = summary.get("session_flow", [])
        if flow:
            print("  Action Flow:")
            for item in flow:
                print(f"    [{item.get('timestamp_iso', '?')}] {item.get('summary', '?')}")
            print()

    # TIMELINE
    timeline_path = os.path.join(workspace, "timeline.json")
    if os.path.exists(timeline_path):
        with open(timeline_path, encoding="utf-8") as f:
            timeline = json.load(f)

        actions = [e for e in timeline if e.get("event_type") == "user_action"]
        requests = [e for e in timeline if e.get("event_type") == "network_request"]

        print(f"  Timeline: {len(timeline)} events ({len(actions)} actions, {len(requests)} network)")
        print()

        if actions:
            print("  User Actions (first 15):")
            for a in actions[:15]:
                action_type = a.get("action", "?")
                details = a.get("details", {})
                label = details.get("text", details.get("value", details.get("newUrl", "")))
                if label:
                    label = label[:60]
                print(f"    [{action_type}] {label}")
            if len(actions) > 15:
                print(f"    ... and {len(actions) - 15} more")
            print()

    # FILES
    print("  Workspace Contents:")
    for root, dirs, files in os.walk(workspace):
        level = root.replace(workspace, "").count(os.sep)
        indent = "    " + "  " * level
        dirname = os.path.basename(root)
        if level == 0:
            dirname = "session_dump/"
        print(f"{indent}{dirname}/")
        if level < 2:
            for f in sorted(files)[:20]:
                size = os.path.getsize(os.path.join(root, f))
                print(f"{indent}  {f} ({size:,} bytes)")
            if len(files) > 20:
                print(f"{indent}  ... and {len(files) - 20} more files")

    print(f"""
{'=' * 60}
  DONE. Workspace ready at:
  {workspace}

  To use with Claude Code, add the MCP server and ask it to
  explore this workspace.
{'=' * 60}
""")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
