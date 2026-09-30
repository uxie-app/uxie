"""
Agent — provider-agnostic multi-turn agent loop.

Uses the `llm` module for chat/tool-calling so any provider supported by litellm
(OpenAI, Anthropic, Gemini, Groq, OpenRouter, Ollama, …) can drive the agent.
The system prompt still reads "GPT-4o"-era tool names, but those names are just
the OpenAI-style function schemas — every supported provider understands them
because litellm translates to each provider's native schema.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from datetime import datetime
from typing import Callable, Any

import config
import history
import llm as llm_module
import dictation as dictation_module
import oauth
from connectors import registry as connector_registry

log = logging.getLogger("agent")

# Windows port: every branch gated by this constant returns before any of the
# macOS-only code (osascript, `open -a`, mdfind, PyObjC) is reached. Mac code
# paths are unchanged.
_IS_WIN = sys.platform.startswith("win")
_broadcaster: Callable | None = None
_target_bundle_id: str | None = None
_target_page_url: str | None = None  # set for browser tabs at recording start

# ── Approval gate ─────────────────────────────────────────────────────────────
_approval_event: asyncio.Event | None = None
_approval_result: bool = False  # set by resolve_approval()
# When the user edits params inline in the review card (e.g. fixes the
# recipient or rewrites the body before clicking Send), the renderer ships
# the diff back through resolve_approval. _approval_gate applies these to
# the live `args` dict in place so all callers see the edits transparently.
_approval_edited_params: dict | None = None

# Tools that must be approved before execution
APPROVAL_REQUIRED_TOOLS = {
    "gmail_send", "gmail_reply", "gmail_send_email",
    "slack_send_message", "slack_context_reply", "slack_post",
    "create_calendar_event", "delete_file", "move_file",
    "linear_create_issue", "notion_create_page",
    "github_create_pr", "github_create_issue",
    "jira_create_issue",
}

TOOL_SUMMARIES = {
    "gmail_send":           lambda a: f"Send email to {a.get('to','?')} — \"{a.get('subject','?')}\"",
    "gmail_reply":          lambda a: f"Reply to email: \"{a.get('subject','?')}\"",
    "gmail_send_email":     lambda a: f"Send email to {a.get('to','?')}",
    "slack_send_message":   lambda a: f"Post to {a.get('channel','?')}: \"{a.get('text','?')[:80]}\"",
    "slack_context_reply":  lambda a: f"Reply in {a.get('channel','?')}: \"{a.get('text','?')[:80]}\"",
    "slack_post":           lambda a: f"Post to Slack: \"{a.get('text','?')[:80]}\"",
    "create_calendar_event":lambda a: f"Create calendar event: \"{a.get('title','?')}\"",
    "delete_file":          lambda a: f"Delete file: {a.get('path','?')}",
    "move_file":            lambda a: f"Move {a.get('source','?')} → {a.get('destination','?')}",
    "linear_create_issue":  lambda a: f"Create Linear issue: \"{a.get('title','?')}\"",
    "notion_create_page":   lambda a: f"Create Notion page: \"{a.get('title','?')}\"",
    "github_create_pr":     lambda a: f"Create PR: \"{a.get('title','?')}\"",
    "github_create_issue":  lambda a: f"Create GitHub issue: \"{a.get('title','?')}\"",
    "jira_create_issue":    lambda a: f"Create Jira issue: \"{a.get('summary','?')}\"",
}


async def _approval_gate(tool_name: str, args: dict) -> bool:
    """Emit approval-needed, block until user responds or 60s timeout.
    If the user edited any params in the review card, the edits are applied
    to `args` in place before this function returns, so callers naturally
    pick them up. Returns True if approved."""
    global _approval_event, _approval_result, _approval_edited_params
    summary = TOOL_SUMMARIES.get(tool_name, lambda a: tool_name)(args)
    _approval_event = asyncio.Event()
    _approval_result = False
    _approval_edited_params = None
    await _emit("approval-needed", {"tool": tool_name, "summary": summary, "params": args})
    try:
        await asyncio.wait_for(_approval_event.wait(), timeout=60.0)
    except asyncio.TimeoutError:
        log.warning(f"Approval timed out for {tool_name} — auto-cancelling")
        await _emit("approval-resolved", {"approved": False, "reason": "timeout"})
        return False
    if _approval_result and _approval_edited_params:
        # Apply user's edits to the live args dict. Only keys that already
        # exist are accepted — the UI shouldn't be inventing new params.
        for k, v in _approval_edited_params.items():
            if k in args:
                args[k] = v
        log.info(f"Approval: {tool_name} args edited by user → {list(_approval_edited_params.keys())}")
    return _approval_result


def resolve_approval(approved: bool, edited_params: dict | None = None):
    """Called from the IPC handler when the user clicks Do it / Cancel.
    `edited_params` is the diff the user changed inline before sending."""
    global _approval_result, _approval_event, _approval_edited_params
    _approval_result = approved
    _approval_edited_params = edited_params if isinstance(edited_params, dict) else None
    if _approval_event:
        _approval_event.set()
_selected_text: str = ""  # captured at session start for transform commands

TRANSFORM_KEYWORDS = {
    "polish", "polished", "fix", "clean", "cleanup",
    "concise", "shorten", "shorter", "summarize", "brief",
    "formal", "professional", "casual", "friendly", "friendly tone",
    "translate",
    "rewrite", "rephrase", "paraphrase",
}


def set_event_broadcaster(fn: Callable):
    global _broadcaster
    _broadcaster = fn


_BROWSER_URL_SCRIPTS: dict[str, str] = {
    "com.google.Chrome":         'tell application "Google Chrome" to get URL of active tab of front window',
    "com.apple.Safari":          'tell application "Safari" to get URL of current tab of front window',
    "org.mozilla.firefox":       'tell application "Firefox" to get URL of active tab of front window',
    "com.microsoft.edgemac":     'tell application "Microsoft Edge" to get URL of active tab of front window',
}

def set_target_app(bundle_id: str | None):
    global _target_bundle_id, _target_page_url
    _target_bundle_id = bundle_id
    _target_page_url = None
    if bundle_id in _BROWSER_URL_SCRIPTS:
        try:
            import subprocess
            _target_page_url = subprocess.check_output(
                ["osascript", "-e", _BROWSER_URL_SCRIPTS[bundle_id]],
                timeout=0.5, encoding="utf8"
            ).strip()
        except Exception:
            pass
    log.info(f"Target app: {bundle_id} url={_target_page_url}")


def capture_selected_text():
    """Read the current text selection from the frontmost app via the Accessibility API.
    Called at session start (before Waves connects) so we have it ready for transform commands."""
    global _selected_text
    _selected_text = _read_selected_text()
    if _selected_text:
        log.info(f"Captured selected text ({len(_selected_text)} chars)")


def _read_selected_text() -> str:
    """Use pyobjc Accessibility API to get AXSelectedText from the frontmost app."""
    if _IS_WIN:
        # Windows: no equivalent of AXSelectedText / NSWorkspace yet. Returning
        # "" makes the transform-text flow gracefully bail (it requires a
        # non-empty selection). Open/quit/launch commands are unaffected.
        return ""
    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            kAXErrorSuccess,
        )
        ws = AppKit.NSWorkspace.sharedWorkspace()
        front_app = ws.frontmostApplication()
        if not front_app:
            return ""
        pid = front_app.processIdentifier()
        app_ref = AXUIElementCreateApplication(pid)
        err, focused = AXUIElementCopyAttributeValue(app_ref, "AXFocusedUIElement", None)
        if err != kAXErrorSuccess or not focused:
            return ""
        err, value = AXUIElementCopyAttributeValue(focused, "AXSelectedText", None)
        if err != kAXErrorSuccess or not value:
            return ""
        return str(value).strip()
    except Exception as e:
        log.debug(f"_read_selected_text: {e}")
        return ""


async def _emit(event: str, payload: Any):
    if _broadcaster:
        await _broadcaster(event, payload)


# ── System prompt (ported 1:1 from agent.rs) ──

# Prepended as an additional system message ONLY on Windows. SYSTEM_PROMPT
# itself is not modified, so the Mac code path sends exactly the original
# message it always has.
WIN_PLATFORM_OVERRIDE = """[PLATFORM CONTEXT — READ FIRST]
This Uxie session is running on Windows, NOT macOS. The system prompt below
mentions "macOS", "Finder", "Safari", and macOS app names — those are stale.
On this machine:
- open_application launches Windows applications by name. Common targets:
  Notepad, Calculator, Chrome, Edge, Word, Excel, Spotify, Discord, Slack.
  Always call open_application for "open X" where X is an app — never return
  DICTATION for app-launch commands.
- quit_application closes a running Windows application.
- open_browser_tab opens a URL in the default Windows browser (typically Edge
  or Chrome).
- search_google opens a Google search in the default browser.
- open_finder opens a folder in File Explorer (the tool name is historical;
  it works on Windows).
- create_file / move_file / clipboard_read / clipboard_write all work on
  Windows.
Mapping examples:
- "Open Notepad" → open_application(name="Notepad")
- "Open Chrome" → open_application(name="Chrome")
- "Open YouTube" → open_browser_tab(url="https://youtube.com")
- "Open downloads folder" → open_finder(path="downloads")
- "Quit Chrome" → quit_application(name="Chrome")
Treat any "Finder" reference in the prompt below as "File Explorer", and any
macOS app reference as the closest Windows equivalent. Otherwise the rules
below apply unchanged."""

SYSTEM_PROMPT = """You are Uxie, a voice-powered desktop agent for macOS. The user speaks and you decide what to do.

MULTILINGUAL SUPPORT:
The user may speak in English, Hindi, or Spanish. Understand commands in all three and map them to the correct tools. Keep parameters in the user's language except macOS app names (always English: "Google Chrome", "Safari", "Finder").

Examples of equivalent commands:
- EN: "Open YouTube" / HI: "YouTube खोलो" / ES: "Abre YouTube" → open_browser_tab
- EN: "Search for restaurants" / HI: "रेस्टोरेंट खोजो" / ES: "Busca restaurantes" → search_google
- EN: "Open Finder" / HI: "Finder खोलो" → open_application
- EN: "Open Downloads" / HI: "Downloads फोल्डर खोलो" → open_finder (path: ~/Downloads)
- EN: "Quit Safari" / HI: "Safari बंद करो" / ES: "Cierra Safari" → quit_application
- EN: "Book a table at Nobu Friday 8pm" → browser_navigate + browser_click (Playwright)
- EN: "Create a file notes.txt" / HI: "notes.txt बनाओ" → create_file

── LOCAL TOOLS (always available) ──────────────────────────────────────────
1. open_browser_tab   — open a URL in Chrome
2. search_google      — search Google (opens in Chrome)
3. open_application   — launch a macOS app by name
4. quit_application   — quit a running macOS app
5. clipboard_write    — write text to clipboard
6. clipboard_read     — read clipboard contents
7. open_finder        — open a folder in Finder
8. create_file        — create a file with optional content
9. move_file          — move or rename a file

── BROWSER AUTOMATION (always available via Playwright) ────────────────────
You always have browser tools (browser_navigate, browser_click, browser_type, browser_snapshot, etc.).
WORKFLOW: browser_navigate → browser_snapshot (see the page) → browser_click/type → snapshot again → repeat.

GMAIL (browser_navigate to https://mail.google.com):
  "send email / compose / write email" → click Compose → fill To / Subject / Body → click Send
  "read emails / what's in my inbox" → snapshot inbox, read subjects and senders
  "search emails about X" → click search bar → type query → snapshot results

GOOGLE CALENDAR (browser_navigate to https://calendar.google.com):
  "schedule meeting / create event / book time" → click Create → fill details → Save
  "what's on my calendar / what do I have today" → snapshot calendar view

SLACK (browser_navigate to https://app.slack.com):
  "post to #channel / message someone" → find channel in sidebar → click it → type → Enter
  "what's in #channel / summarize Slack" → open channel → snapshot recent messages → summarize

ANY WEBSITE — booking, forms, reading, ordering:
  "book a table at X", "order from Y", "fill out Z form" → navigate to site → interact with UI

── CONNECTED SERVICE TOOLS (only when shown in your tools list) ────────────

GMAIL (gmail_search, gmail_read, gmail_send, gmail_reply, gmail_draft):
  CRITICAL: "email", "mail", "gmail", "मेल", "correo" → MUST use gmail tool.
  - Send → gmail_send  |  Draft → gmail_draft  |  Search → gmail_search
  - Summary format: "From: X | Subject: Y | Summary: …"

  CONTEXT FROM PRIOR THREADS — ALWAYS pull context before composing if any of
  these apply. Don't ship a blind email when the thread already exists.

  When to fetch context FIRST (gmail_search → gmail_read → then send/reply):
    * "reply to X" / "respond to X" / "answer X"  → search for the open thread
    * "follow up with X about Y" / "ping X again" → search "from:X Y" recent
    * "send X about Y" where Y is an ongoing topic → search "Y" first
    * any phrasing that implies a continuation: "as we discussed", "the
      proposal", "that meeting", "your last email" → search to recover
      what they mean
    * if a thread is mentioned by subject, use gmail_search to locate it,
      gmail_read to load the body, then compose with that context inline
      in the new message

  When NOT to bother (skip search, go straight to gmail_send):
    * brand-new outreach to a contact: "email john@x.com saying hi"
    * the user dictates the full body themselves
    * forward-only / share-link content with no thread context

  Reply mechanics: gmail_search → gmail_read (returns threadId) → gmail_reply
  with that threadId. For a fresh-but-context-aware send, summarize the
  prior thread in your message body so the recipient sees the lineage.

GOOGLE CALENDAR (calendar_list_events, calendar_create_event, calendar_check_availability):
  CRITICAL: "meeting", "schedule", "book time", "calendar", "appointment" → use calendar tool.
  - Default duration: 1 hour. Default timezone: America/New_York.

SLACK (slack_send_message, slack_read_channel, slack_summarize, slack_list_channels):
  CRITICAL: "Slack", "#channel", "post to" → use slack tool.
  - Summaries: slack_summarize (reads channel, returns summary, does NOT post).

GITHUB — triggered by: "GitHub", "issue", "PR", "repo"
LINEAR — triggered by: "Linear", "ticket", "story"
NOTION  — triggered by: "Notion", "page", "database"

── DECISION RULES ─────────────────────────────────────────────────────────
1. Local tool match → use it.
2. Gmail / Calendar / Slack connected → use their tools (gmail_*, calendar_*, slack_*).
3. GitHub / Linear / Notion connected → use MCP tools from your tools list.
4. Web task with no dedicated tool → use Playwright browser tools.
5. Pure dictation → respond ONLY with the word "DICTATION".

Hindi patterns: "खोलो" (open), "बंद करो" (quit), "भेजो" (send), "खोजो" (search), "बनाओ" (create), "बुक करो" (book).
Spanish patterns: "abre" (open), "cierra" (quit), "envía" (send), "busca" (search), "crea" (create), "reserva" (book).

── TEXT FORMATTING (when returning DICTATION) ───────────────────────────────
- "bullet points" / "list" → bullet list with "- " prefix
- "numbered" / "step by step" → numbered list "1. "
- Plain dictation → ONLY the word "DICTATION"

── FILE CONTEXT ─────────────────────────────────────────────────────────────
When [FILE CONTEXT: ...] blocks are present, use the actual file content.
Output the FULL modified file when making changes. Never return "DICTATION" for code tasks."""


# ── Tool definitions ──

LOCAL_TOOLS = [
    {"type": "function", "function": {"name": "open_browser_tab", "description": "Open a URL in Google Chrome", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "search_google", "description": "Search Google", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "open_application", "description": "Launch a macOS application", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "quit_application", "description": "Quit a macOS application", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "clipboard_write", "description": "Write text to clipboard", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "clipboard_read", "description": "Read clipboard contents", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "open_finder",
        "description": (
            "Open a folder in Finder. Use this for any 'open the X folder', "
            "'show me the X directory', 'go to X' command. Common paths: "
            "~ (home), ~/Downloads, ~/Desktop, ~/Documents, ~/Movies, ~/Music, "
            "~/Pictures, ~/Public, ~/Library, /Applications, /System/Applications, "
            "/Volumes (mounted disks), /tmp. Also accepts absolute paths "
            "(/Users/foo/bar) or tilde-prefixed paths (~/Code/my-repo). "
            "If the user says a bare folder name you recognize "
            "(e.g. 'downloads', 'desktop'), map it to ~/<Capitalized>."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Folder path to open. Defaults to home (~)."}
        }, "required": []},
    }},
    {"type": "function", "function": {"name": "create_file", "description": "Create a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string", "default": ""}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "move_file", "description": "Move or rename a file", "parameters": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}}, "required": ["from", "to"]}}},
    {"type": "function", "function": {
        "name": "start_background_task",
        "description": (
            "Hand a multi-step or long-running request to Uxie's cloud background agent "
            "(keeps running if the laptop sleeps; user gets a notification when done). "
            "Use for research, summarizing many emails/docs, or anything needing several "
            "searches and reads. Do NOT use for single quick actions you can do directly."
        ),
        "parameters": {"type": "object", "properties": {"goal": {"type": "string", "description": "The full request, self-contained"}}, "required": ["goal"]},
    }},
]


async def _start_background_task(goal: str) -> tuple[bool, str]:
    """POST the goal to Railway /tasks/create (durable task runtime)."""
    import httpx
    jwt = config.get_jwt()
    if not jwt:
        return False, "Sign in to Uxie to use background tasks."
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{config.get_uxie_backend_url()}/tasks/create",
                headers={"Authorization": f"Bearer {jwt}"},
                json={"prompt": goal[:4000]},
            )
        if r.status_code == 429:
            return False, "Background task limit reached — try again later."
        r.raise_for_status()
        return True, f"On it — running in the background (task {r.json().get('id', '')[-6:]}). I'll notify you when it's done."
    except httpx.HTTPError as e:
        return False, f"Couldn't start background task: {e}"


# ── App focus helper ──

async def _activate_target_app():
    """Re-activate the app that was frontmost when Fn was pressed.
    Runs blocking calls in a thread so we don't freeze the asyncio event loop.
    """
    if not _target_bundle_id:
        log.info("_activate_target_app: no target bundle ID stored, skipping")
        return
    log.info(f"_activate_target_app: activating {_target_bundle_id}")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", f'tell application id "{_target_bundle_id}" to activate'],
            timeout=2, capture_output=True
        )
        if result.returncode != 0:
            log.warning(f"_activate_target_app: osascript exited {result.returncode}: {result.stderr.decode().strip()}")
        # Wait for focus to settle — 300ms is enough for most apps including Electron/browsers
        await asyncio.sleep(0.30)
        log.info("_activate_target_app: focus settled, ready to type")
    except Exception as e:
        log.warning(f"_activate_target_app: {e}")


# ── Local action execution ──

def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or result.stderr.strip() or "ok"
    except Exception as e:
        return str(e)


def _execute_local_windows(name: str, args: dict) -> tuple[bool, str] | None:
    """Windows implementations of the 5 OS-shell tools that on Mac call
    osascript / `open -a`. Returns None for tools handled by the (unchanged)
    Mac block below — those are cross-platform (clipboard via pyperclip,
    create_file/move_file via plain Python) so they fall through.
    """
    import os
    if name == "open_application":
        # `start "" <name>` lets the Windows shell resolve App Paths registry,
        # PATH, and known apps (e.g. "notepad" → notepad.exe). DETACHED_PROCESS
        # so the child doesn't die when our subprocess exits.
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", args["name"]],
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            return True, f"Opened {args['name']}"
        except Exception as e:
            return False, f"Failed to open {args['name']}: {e}"
    if name == "quit_application":
        # taskkill matches by image name; users say "Notepad" but the exe is
        # "notepad.exe", so try both spellings.
        target = args["name"]
        candidates = [target, target if target.lower().endswith(".exe") else f"{target}.exe"]
        for image in candidates:
            r = subprocess.run(
                ["taskkill", "/IM", image, "/F"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return True, f"Quit {target}"
        return False, f"Could not quit {target} (not running?)"
    if name == "open_browser_tab":
        # os.startfile on a URL hands it to the user's default browser. We
        # don't force Chrome here because Edge/Firefox/etc. are equally fine
        # on Windows and the user's default is usually what they want.
        try:
            os.startfile(args["url"])
            return True, f"Opened {args['url']}"
        except Exception as e:
            return False, f"Failed to open URL: {e}"
    if name == "search_google":
        import urllib.parse
        url = f"https://www.google.com/search?q={urllib.parse.quote(args['query'])}"
        try:
            os.startfile(url)
            return True, f"Searched for: {args['query']}"
        except Exception as e:
            return False, f"Failed to open search: {e}"
    if name == "open_finder":
        # Windows Explorer equivalents of the macOS shortcuts. We only map
        # the ones with a real Windows analog; "/Applications", "iCloud",
        # ".Trash" don't translate.
        raw = (args.get("path") or "~").strip()
        win_shortcuts = {
            "home": "~", "~": "~",
            "downloads": "~\\Downloads", "download": "~\\Downloads",
            "desktop": "~\\Desktop",
            "documents": "~\\Documents", "docs": "~\\Documents",
            "movies": "~\\Videos", "videos": "~\\Videos",
            "music": "~\\Music",
            "pictures": "~\\Pictures", "photos": "~\\Pictures",
            "tmp": os.environ.get("TEMP", "C:\\Windows\\Temp"),
            "temp": os.environ.get("TEMP", "C:\\Windows\\Temp"),
        }
        path = win_shortcuts.get(raw.lower(), raw)
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return False, f"No such folder: {path}"
        try:
            subprocess.Popen(["explorer", path])
            return True, f"Opened Explorer at {path}"
        except Exception as e:
            return False, f"Failed to open Explorer: {e}"
    return None  # Fall through to cross-platform handlers below.


def _execute_local(name: str, args: dict) -> tuple[bool, str]:
    if _IS_WIN:
        windows_result = _execute_local_windows(name, args)
        if windows_result is not None:
            return windows_result
        # Else fall through: clipboard_*, create_file, move_file all work on
        # Windows via the cross-platform pyperclip / plain-Python paths below.
    import pyperclip
    try:
        if name == "open_browser_tab":
            _run(["open", "-a", "Google Chrome", args["url"]])
            return True, f"Opened {args['url']}"
        elif name == "search_google":
            import urllib.parse
            url = f"https://www.google.com/search?q={urllib.parse.quote(args['query'])}"
            _run(["open", "-a", "Google Chrome", url])
            return True, f"Searched for: {args['query']}"
        elif name == "open_application":
            _run(["open", "-a", args["name"]])
            return True, f"Opened {args['name']}"
        elif name == "quit_application":
            _run(["osascript", "-e", f'quit app "{args["name"]}"'])
            return True, f"Quit {args['name']}"
        elif name == "clipboard_write":
            pyperclip.copy(args["text"])
            return True, "Copied to clipboard"
        elif name == "clipboard_read":
            return True, pyperclip.paste()
        elif name == "open_finder":
            import os
            raw = (args.get("path") or "~").strip()
            # Friendly shortcut names the LLM may emit instead of a real path.
            shortcuts = {
                "home": "~", "~": "~",
                "downloads": "~/Downloads", "download": "~/Downloads",
                "desktop": "~/Desktop",
                "documents": "~/Documents", "docs": "~/Documents",
                "movies": "~/Movies", "music": "~/Music",
                "pictures": "~/Pictures", "photos": "~/Pictures",
                "public": "~/Public", "library": "~/Library",
                "applications": "/Applications", "apps": "/Applications",
                "system applications": "/System/Applications",
                "trash": "~/.Trash", "tmp": "/tmp", "temp": "/tmp",
                "icloud": "~/Library/Mobile Documents/com~apple~CloudDocs",
            }
            path = shortcuts.get(raw.lower(), raw)
            path = os.path.expanduser(path)         # ~ → /Users/<name>
            if not os.path.exists(path):
                return False, f"No such folder: {path}"
            _run(["open", path])
            return True, f"Opened Finder at {path}"
        elif name == "create_file":
            import os
            path = os.path.expanduser(args["path"])
            with open(path, "w") as f:
                f.write(args.get("content", ""))
            return True, f"Created {path}"
        elif name == "move_file":
            import os, shutil
            shutil.move(os.path.expanduser(args["from"]), os.path.expanduser(args["to"]))
            return True, f"Moved {args['from']} → {args['to']}"
        return False, f"__unknown__:{name}"
    except Exception as e:
        return False, str(e)


# ── File tagging ──

CODE_EXTS = {
    "ts", "tsx", "js", "jsx", "rs", "py", "go", "rb", "java", "cpp", "c",
    "h", "cs", "swift", "kt", "vue", "svelte", "html", "css", "scss",
    "json", "yaml", "yml", "toml", "md", "sh", "bash", "txt", "sql",
}
SKIP_DIRS = {"/node_modules/", "/.git/", "/dist/", "/build/", "/.cache/",
             "/target/", "/.Trash/", "/Library/", "/.venv/"}


def _extract_filenames(text: str) -> list[str]:
    found = []
    for word in text.split():
        clean = word.strip(",.\"'()")
        if "." in clean:
            ext = clean.rsplit(".", 1)[-1].lower()
            if ext in CODE_EXTS and clean not in found:
                found.append(clean)
    return found


def _find_and_read(filename: str) -> tuple[str, str] | None:
    if _IS_WIN:
        # mdfind is macOS-only. File-context auto-injection is a nice-to-have,
        # not required for command-mode actions; skip on Windows for v1.
        return None
    import os
    home = os.path.expanduser("~")
    result = subprocess.run(
        ["mdfind", "-name", filename, "-onlyin", home],
        capture_output=True, text=True, timeout=5
    )
    candidates = [
        p for p in result.stdout.splitlines()
        if not any(skip in p for skip in SKIP_DIRS)
    ]
    if not candidates:
        return None
    best = next(
        (p for p in candidates if any(d in p for d in ("/src/", "/lib/", "/app/"))),
        candidates[0]
    )
    try:
        content = open(best).read()
        if len(content) > 8000:
            content = content[:8000] + "…[truncated]"
        return best, content
    except Exception:
        return None


def _inject_file_context(text: str) -> str:
    # mdfind is slow (50–300 ms per lookup). Skip entirely when the text has no
    # obvious file pattern — voice commands almost never reference files.
    if "." not in text:
        return text
    filenames = _extract_filenames(text)
    if not filenames:
        return text
    blocks = []
    for fname in filenames:
        result = _find_and_read(fname)
        if result:
            path, content = result
            blocks.append(f"[FILE CONTEXT: {fname} | {path}]\n```\n{content}\n```\n[END FILE CONTEXT]")
            log.info(f"Injected file: {fname} ({path})")
    if blocks:
        prefix = "\n\n".join(blocks)
        text = f"{prefix}\n\n{text}\n\n[SYSTEM: File(s) injected above. Use their actual content to perform the requested operation. Output modified code in full.]"
    return text


# ── Dictation-only: grammar correction (no tools, no rewrite) ──

APP_CONTEXT_MAP: dict[str, str] = {
    # Email
    "com.apple.mail":                   "email",
    "com.microsoft.Outlook":            "email",
    # Browsers handled by URL detection in _infer_format_context
    # Notes / markdown
    "com.apple.Notes":                  "markdown",
    "notion.id":                        "markdown",
    "md.obsidian":                      "markdown",
    "com.craft.craftdocs":              "markdown",
    "com.logseq.logseq":                "markdown",
    # Code editors — plain prose, no formatting
    "com.microsoft.VSCode":             "prose",
    "com.jetbrains.intellij":           "prose",
    "com.sublimetext.4":                "prose",
}

_FORMAT_SUFFIXES: dict[str, str] = {
    # Email context: do NOT restructure — just ensure greeting has a comma after the name
    # and the sign-off line ends with a comma. The user dictates the structure themselves.
    "email": (
        "\n\nEMAIL HINT: The user is dictating an email. Apply the same minimal cleanup rules. "
        "Do NOT add, remove, or reorder any sentences. Do NOT add a greeting, sign-off, or "
        "subject line that was not spoken. Only fix: add a comma after the recipient name in "
        "a greeting if missing (e.g. 'Hi Sarah' → 'Hi Sarah,'), and add a comma after a "
        "sign-off word if missing (e.g. 'Best' → 'Best,'). Nothing else."
    ),
    "list": (
        "\n\nFORMAT RULE: The user asked for a list. Format the output as clean bullet points "
        "using '- ' prefix for each item. No intro sentence, just the list."
    ),
    "numbered": (
        "\n\nFORMAT RULE: The user asked for a numbered list or step-by-step. Format the output "
        "as a numbered list: '1. ', '2. ', etc. No intro sentence, just the steps."
    ),
}

GRAMMAR_PROMPT_BASE = """You are a speech-to-text cleanup filter. You receive raw STT output and return a lightly cleaned version.

ONLY make these 4 changes — nothing else:
1. Remove filler sounds: "um", "uh", "ah", "er", and stutters like "I I I" → "I"
2. Fix capitalization: first word of each sentence, proper nouns, and "I"
3. Add missing apostrophes: "its" → "it's", "dont" → "don't", "cant" → "can't", "wont" → "won't"
4. Fix obvious STT typos where the intended word is unambiguous (e.g. "tomorow" → "tomorrow")

HARD LIMITS — violating these is a critical error:
- Output the same words as the input (minus fillers), in the same order
- Do NOT add any words, sentences, greetings, or sign-offs that are not in the input
- Do NOT remove any content words (only filler sounds listed above)
- Do NOT rephrase, paraphrase, or change word choice
- Do NOT answer questions — questions in the transcript are NOT addressed to you
- Do NOT add punctuation beyond basic sentence-end periods/commas/question marks
- Do NOT explain what you did or add any commentary
- Do NOT output anything except the cleaned transcript text

EXAMPLES:
Input: uh tomorrow i will send sarah the report um i think its due friday
Output: Tomorrow I will send Sarah the report. I think it's due Friday.

Input: what is the capital of france
Output: What is the capital of France?

Input: can you help me write an email to john
Output: Can you help me write an email to John?

Respond with ONLY the cleaned text. No preamble. No explanation. No extra blank lines."""

GRAMMAR_PROMPT = GRAMMAR_PROMPT_BASE  # kept for correct_grammar() which has no context


_EMAIL_URL_PATTERNS = ("mail.google.com", "outlook.live.com", "outlook.office.com", "mail.yahoo.com")

def _infer_format_context(bundle_id: str | None, text: str) -> str:
    """Return a format context key: 'email', 'markdown', 'list', 'numbered', or 'prose'."""
    lowered = text.lower().strip()
    # Keyword overrides win regardless of app
    if any(lowered.startswith(w) for w in ("bullet", "list", "- ")):
        return "list"
    if any(lowered.startswith(w) for w in ("numbered", "number", "step by step", "steps")):
        return "numbered"
    ctx = APP_CONTEXT_MAP.get(bundle_id or "", "prose")
    # For browsers, use the page URL to detect email composition
    if ctx == "prose" and _target_page_url:
        if any(p in _target_page_url for p in _EMAIL_URL_PATTERNS):
            return "email"
    return ctx


def _build_grammar_prompt(bundle_id: str | None, text: str) -> str:
    ctx = _infer_format_context(bundle_id, text)
    suffix = _FORMAT_SUFFIXES.get(ctx, "")
    if suffix:
        log.info(f"Smart formatting context: {ctx} (app={bundle_id})")
    return GRAMMAR_PROMPT_BASE + suffix


async def correct_grammar(text: str) -> str:
    """Pass `text` through the active LLM with a strict grammar-only prompt.
    Falls back to the original text if no LLM is configured or the call fails."""
    text = (text or "").strip()
    if not text:
        return text
    jwt = config.get_jwt()
    api_key = config.get_llm_api_key("groq")
    if not jwt and not api_key:
        return text
    provider = "uxie" if jwt else "groq"
    try:
        response = await llm_module.chat(
            provider=provider,
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": GRAMMAR_PROMPT},
                {"role": "user", "content": f"<transcript>{text}</transcript>"},
            ],
            tools=None,
            api_key=api_key if provider == "groq" else None,
            temperature=0.0,
        )
        import re as _re
        cleaned = (response.content or "").strip().lstrip("\n")
        if len(cleaned) >= 2 and cleaned[0] in "\"'" and cleaned[-1] == cleaned[0]:
            cleaned = cleaned[1:-1]
        cleaned = _re.sub(r" {2,}", " ", cleaned)
        return cleaned or text
    except Exception as e:
        log.warning(f"correct_grammar failed, returning original: {e}")
        return text


async def dictate(text: str) -> list[dict]:
    """Grammar-correct + emit as dictation so the native helper types it.

    This is the dictation-only path used by voice capture. No tools, no agent loop.
    """
    await _emit("agent-status", "processing")
    corrected = await correct_grammar(text)
    result = {"action": "dictation", "success": True, "message": corrected}
    await _emit("action-result", result)
    history.append_entry(
        transcript=text,
        entry_type="dictation",
        actions=[result],
        success=True,
    )
    await _emit("agent-status", "idle")
    return [result]


async def dictate_streaming(text: str, emit) -> str:
    """Streaming grammar-corrected dictation.

    Yields chunks via `action-chunk` events (each chunk gets typed by the helper
    immediately), then emits a single final `action-result` once the stream
    completes. Returns the full corrected text.

    `emit` is the broadcaster (audio.py's `_emit` or `manager.broadcast`).
    """
    text = (text or "").strip()
    if not text:
        return text

    jwt = config.get_jwt()
    api_key = config.get_llm_api_key("groq")
    if not jwt and not api_key:
        await emit("action-result", {"action": "dictation", "success": True, "message": text})
        return text

    provider = "uxie" if jwt else "groq"
    prompt = _build_grammar_prompt(_target_bundle_id, text)
    full = ""
    try:
        async for piece in llm_module.chat_stream(
            provider=provider,
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"<transcript>{text}</transcript>"},
            ],
            api_key=api_key if provider == "groq" else None,
            temperature=0.0,
        ):
            if not piece:
                continue
            full += piece
            await emit("action-chunk", {"chunk": piece})
    except Exception as e:
        log.warning(f"dictate_streaming LLM failed, falling back to raw: {e}")
        await emit("action-result", {"action": "dictation", "success": True, "message": text})
        return text

    import re as _re
    cleaned = full.strip()
    if len(cleaned) >= 2 and cleaned[0] in "\"'" and cleaned[-1] == cleaned[0]:
        cleaned = cleaned[1:-1]
    # Collapse multiple spaces into one (LLM occasionally emits double-spaces)
    cleaned = _re.sub(r" {2,}", " ", cleaned)
    # Strip leading blank lines that chatty models sometimes prepend
    cleaned = cleaned.lstrip("\n")

    await emit("debug", {
        "type": "llm",
        "text": cleaned,
        "app": _target_bundle_id or "unknown",
    })

    # Emit final marker (UI uses this to update the "Last transcript" card and
    # to dedupe if any chunks got dropped mid-stream).
    await emit("action-result", {"action": "dictation-final", "success": True, "message": cleaned})
    return cleaned


# ── Text selection transform ──────────────────────────────────────────────────

def _is_transform_command(text: str) -> bool:
    """Return True if the spoken command is a text-transform intent."""
    lowered = text.lower().strip()
    for kw in TRANSFORM_KEYWORDS:
        if kw in lowered:
            return True
    return False


TRANSFORM_SYSTEM_PROMPT = """You are a writing assistant. The user has selected some text and asked you to transform it.
Return ONLY the transformed text — no preamble, no explanation, no quotes around it.
Apply the transformation faithfully:
- "polish" / "fix" / "clean up": fix grammar, clarity, and flow while keeping the meaning
- "concise" / "shorten" / "shorter": remove filler, keep all key info
- "formal" / "professional": elevate register, avoid contractions
- "casual" / "friendly": conversational tone, contractions ok
- "translate to <lang>": translate to the specified language
- "rewrite" / "rephrase": express the same idea differently
"""


async def _execute_text_transform(command: str, selected: str) -> list[dict]:
    """Call the LLM to transform `selected` text per `command`, then paste the result back."""
    await _emit("agent-status", "processing")
    log.info(f"Transform command: '{command[:60]}' on {len(selected)} chars of selected text")

    jwt = config.get_jwt()
    openai_key = config.get_llm_api_key("openai")
    provider = "uxie" if jwt else ("openai" if openai_key else None)
    if not provider:
        await _emit("action-result", {"action": "transform-error", "success": False,
                                       "message": "No LLM configured"})
        await _emit("agent-status", "idle")
        return [{"action": "transform-error", "success": False, "message": "No LLM configured"}]

    messages = [
        {"role": "system", "content": TRANSFORM_SYSTEM_PROMPT},
        {"role": "user", "content": f"Command: {command}\n\nSelected text:\n{selected}"},
    ]
    try:
        response = await llm_module.chat(
            provider=provider,
            model="gpt-4o",
            messages=messages,
            api_key=openai_key if provider == "openai" else None,
            temperature=0.3,
        )
        transformed = (response.content or "").strip()
    except Exception as e:
        log.error(f"Transform LLM call failed: {e}")
        await _emit("action-result", {"action": "transform-error", "success": False, "message": str(e)})
        await _emit("agent-status", "idle")
        return [{"action": "transform-error", "success": False, "message": str(e)}]

    if not transformed:
        await _emit("action-result", {"action": "transform-error", "success": False,
                                       "message": "LLM returned empty response"})
        await _emit("agent-status", "idle")
        return [{"action": "transform-error", "success": False, "message": "Empty response"}]

    # Paste the transformed text back into the source app
    import pyperclip
    old_clipboard = pyperclip.paste()
    pyperclip.copy(transformed)
    await _activate_target_app()
    # Cmd+V to paste (replaces the selection)
    _run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'])
    await asyncio.sleep(0.1)
    # Restore clipboard
    pyperclip.copy(old_clipboard)

    await _emit("action-result", {"action": "text-transform", "success": True, "message": transformed})
    await _emit("agent-status", "idle")
    result = [{"action": "text-transform", "success": True, "message": transformed}]
    history.append_entry(
        transcript=command, entry_type="command",
        actions=result, success=True,
    )
    return result


# ── Main agent loop (tool-calling, invoked from the command bar) ──

async def execute_command(text: str) -> list[dict]:
    await _emit("agent-status", "processing")

    jwt = config.get_jwt()
    openai_key = config.get_llm_api_key("openai")
    if not jwt and not openai_key:
        log.info("No OpenAI key or Uxie JWT; emitting raw dictation for command")
        result = [{"action": "dictation", "success": True, "message": text}]
        await _emit("action-result", {"action": "dictation", "success": True, "message": text})
        history.append_entry(transcript=text, entry_type="dictation", actions=result, success=True)
        await _emit("agent-status", "idle")
        return result
    cmd_provider = "uxie" if jwt else "openai"

    user_name = config.get_user_name()
    today = datetime.now().strftime("%A, %B %d, %Y")

    user_msg = _inject_file_context(text)

    # If a transform keyword is spoken AND text was selected, inject the selection
    # and switch to a direct transform flow (no tool-calling needed).
    if _selected_text and _is_transform_command(text):
        return await _execute_text_transform(text, _selected_text)

    if user_name:
        user_msg = f"[User name: {user_name}]\n[Today: {today}]\n{user_msg}"
    else:
        user_msg = f"[Today: {today}]\n{user_msg}"

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    if _IS_WIN:
        # Override the macOS framing with Windows-specific tool guidance.
        # Appended AFTER the main prompt so it has the last word.
        messages.append({"role": "system", "content": WIN_PLATFORM_OVERRIDE})
    messages.append({"role": "user", "content": user_msg})

    # Build tool list: local + OAuth connectors (Google, Slack) + MCP (GitHub, Linear, Notion, Playwright)
    import mcp_client
    connected = oauth.get_connected_providers()
    tools = list(LOCAL_TOOLS) + connector_registry.get_tools_for_providers(connected) + mcp_client.get_tools()

    # Route multi-connector commands to the orchestrator (boss + parallel workers)
    import orchestrator
    if orchestrator.should_orchestrate(text, tools):
        log.info("Routing to orchestrator (multi-connector command)")
        results = await orchestrator.run(text, tools, _emit, _approval_gate)
        history.append_entry(
            transcript=text, entry_type="command",
            actions=results, success=all(r["success"] for r in results),
        )
        await _emit("agent-status", "idle")
        return results

    action_results: list[dict] = []
    max_turns = 4  # most voice commands resolve in 1–2 turns; bail fast if not

    for _ in range(max_turns):
        try:
            response = await llm_module.chat(
                provider=cmd_provider,
                model="gpt-4o",
                messages=messages,
                tools=tools,
                api_key=openai_key if cmd_provider == "openai" else None,
                temperature=0.0,
            )
        except Exception as e:
            log.error(f"LLM call failed: {e}")
            await _emit("action-result", {"action": "llm-error", "success": False, "message": str(e)})
            action_results.append({"action": "llm-error", "success": False, "message": str(e)})
            break

        if not response.tool_calls:
            log.info(f"Emitting dictation action: '{text[:60]}'")
            await _emit("debug", {
                "type": "inject",
                "text": text,
                "app": _target_bundle_id or "unknown",
                "success": True,
            })
            await _emit("action-result", {"action": "dictation", "success": True, "message": text})
            action_results.append({"action": "dictation", "success": True, "message": text})
            break

        messages.append({"role": "assistant", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments_json}}
            for tc in response.tool_calls
        ]})

        for tc in response.tool_calls:
            fn_name = tc.name
            try:
                args = json.loads(tc.arguments_json)
            except Exception:
                args = {}

            # Gate external/destructive tools behind user approval
            if fn_name in APPROVAL_REQUIRED_TOOLS:
                approved = await _approval_gate(fn_name, args)
                if not approved:
                    result = {"action": fn_name, "success": False, "message": "Cancelled by user"}
                    action_results.append(result)
                    await _emit("action-result", result)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": "User cancelled this action."})
                    continue

            # Route: local → OAuth connector (Google/Slack) → MCP (GitHub/Linear/Notion/Playwright)
            if fn_name == "start_background_task":
                success, result_msg = await _start_background_task(args.get("goal", ""))
            else:
                success, result_msg = _execute_local(fn_name, args)
            if result_msg == f"__unknown__:{fn_name}":
                success, result_msg = connector_registry.execute_connector_tool(fn_name, args, oauth.get_token)
            if not success and "No connector found" in result_msg:
                success, result_msg = await mcp_client.call_tool(fn_name, args)

            action_results.append({"action": fn_name, "success": success, "message": result_msg})
            await _emit("action-result", {"action": fn_name, "success": success, "message": result_msg})

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_msg,
            })

    history.append_entry(
        transcript=text,
        entry_type="command" if any(r["action"] != "dictation" for r in action_results) else "dictation",
        actions=action_results,
        success=all(r["success"] for r in action_results),
    )

    await _emit("agent-status", "idle")
    return action_results
