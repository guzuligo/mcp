# WebReader MCP Server — Developer Guide

## Overview

This MCP server provides tools for web browsing and page fetching. It is organized into two categories of tools:

1. **Stateless Tools** — One-shot page fetching, no session management
2. **Stateful Tools** — Interactive browser sessions with explicit `session_id`

## Architecture

### Session Registry

All interactive browsing tools share state through a session registry:

```python
_sessions: dict = {}          # session_id -> {browser, context, page, last_activity}
_sessions_lock = asyncio.Lock()  # Thread-safe access
_SESSION_TIMEOUT = timedelta(minutes=10)  # Auto-cleanup
```

Each session entry contains:
- `browser`: Playwright `Browser` instance (Chromium)
- `context`: Playwright `BrowserContext` (isolated storage state)
- `page`: Playwright `Page` object
- `last_activity`: `datetime` timestamp for expiry tracking

### Playwright Lifecycle

Each session creates its own Playwright instance to avoid the `PlaywrightContextManager` bug:

```python
# ✅ CORRECT — each session gets its own Playwright instance
pw = await async_playwright().start()
browser = await pw.chromium.launch(headless=True)
context = await browser.new_context()
page = await context.new_page()

# Store in session registry
_sessions[session_id] = {
    "browser": browser,
    "context": context,
    "page": page,
    "last_activity": datetime.now(),
}
```

**Never use** `async with async_playwright() as p:` as a context manager inside tools — it calls `p.stop()` on exit, killing all browsers and pages.

## Tool Categories

### Stateless Tools

Each call is fully independent. No session, no persistence.

| Tool | Description |
|------|-------------|
| `basic_page_fetch` | Fetch page text via HTTP (fastest). Falls back to Playwright if HTTP fails. |
| `fetch_page_sections` | Fetch + extract structured, classified sections. |
| `fetch_page_progressive` | Fetch sections in batches for long pages. |
| `fetch_page_section_by_id` | Get one section by its ID from `fetch_page_sections`. |

**Implementation pattern:**
```python
@mcp.tool()
async def basic_page_fetch(url: str, force_playwright: bool = False) -> str:
    # 1. Try HTTP first (if not force_playwright)
    # 2. If HTTP fails or force_playwright=True, use short-lived Playwright
    # 3. Always clean up: browser.close() + pw.stop()
```

### Stateful Tools

Interactive browsing requires an explicit `session_id` from `browser_open()`.

| Tool | Description |
|------|-------------|
| `browser_open` | Open URL, returns `session_id` |
| `browser_navigate` | Navigate within session |
| `browser_click` | Click element |
| `browser_fill` | Fill form field |
| `browser_get_state` | Read page content |
| `browser_go_back` | Go back in history |
| `browser_go_forward` | Go forward in history |
| `browser_screenshot` | Capture screenshot |
| `browser_close` | Close session |

**Implementation pattern:**
```python
@mcp.tool()
async def browser_click(session_id: str, selector: str) -> str:
    try:
        session = _validate_session(session_id)  # Check existence + expiry
        page = session["page"]
        
        # Wait for visibility with fallback
        try:
            await page.wait_for_selector(selector, state="visible", timeout=10000)
        except Exception:
            pass  # Fall back to direct interaction
        
        await page.click(selector)
        # ... return result
    except ConnectionError as e:
        return error_response("not found or expired")
    except Exception as e:
        return error_response(str(e))
```

## Core Functions

### `_validate_session(session_id: str) -> dict`

Checks session existence and expiry. Updates `last_activity` timestamp.

```python
def _validate_session(session_id: str) -> dict:
    if session_id not in _sessions:
        raise ConnectionError("Session not found. Call browser_open() first.")
    session = _sessions[session_id]
    if datetime.now() - session["last_activity"] > _SESSION_TIMEOUT:
        asyncio.create_task(_destroy_session(session_id, "expired"))
        raise ConnectionError("Session expired (10 min idle).")
    session["last_activity"] = datetime.now()
    return session
```

### `_destroy_session(session_id: str, reason: str)`

Closes page, context, browser and removes from registry. Thread-safe via `_sessions_lock`.

### `_cleanup_expired_sessions()`

Background task to remove idle sessions. Called at the start of `browser_open()`.

## Adding a New Tool

### Stateless Tool

```python
@mcp.tool()
async def my_new_tool(url: str, force_playwright: bool = False) -> str:
    """Docstring explaining this is STATELESS, ONE-SHOT."""
    # Use short-lived Playwright or httpx
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        # ... do work
        return json.dumps({"status": "success", ...}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        if browser:
            await browser.close()
        await pw.stop()
```

### Stateful Tool

```python
@mcp.tool()
async def my_new_browser_tool(session_id: str, ...args) -> str:
    """Docstring explaining this requires session_id from browser_open()."""
    try:
        session = _validate_session(session_id)
        page = session["page"]
        # ... do work on page
        return json.dumps({
            "tool": "my_new_browser_tool",
            "status": "success",
            "session_id": session_id,
            ...
        }, indent=2)
    except ConnectionError as e:
        return json.dumps({
            "tool": "my_new_browser_tool",
            "status": "error",
            "message": str(e)
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "tool": "my_new_browser_tool",
            "status": "error",
            "session_id": session_id,
            "message": str(e)
        }, indent=2)
```

## Best Practices

### Page Loading

Always use `domcontentloaded` instead of `networkidle`:

```python
try:
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
except Exception:
    try:
        await page.goto(url, timeout=15000)  # Fallback: no wait strategy
    except Exception:
        pass
```

### Element Interaction

Always wait for visibility with fallback:

```python
try:
    await page.wait_for_selector(selector, state="visible", timeout=10000)
except Exception:
    pass  # Element may exist but be hidden
await page.click(selector)  # or page.fill()
```

### Error Handling

Always return JSON with `"status": "success"` or `"status": "error"`. Include helpful `"message"` and `"next_steps"` fields for LLMs.

### Session Expiry

10 minutes is a reasonable default. Adjust `_SESSION_TIMEOUT` if needed:

```python
_SESSION_TIMEOUT = timedelta(minutes=30)  # Longer sessions
```

## Testing

### Manual Testing Workflow

```python
# 1. Stateless tools
basic_page_fetch("https://example.com")
fetch_page_sections("https://example.com/article")

# 2. Stateful tools
result = browser_open("https://example.com")
session_id = result["session_id"]

browser_get_state(session_id=session_id)
browser_click(session_id=session_id, selector="#link")
browser_get_state(session_id=session_id)  # Check changes
browser_close(session_id=session_id)
```

### Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| `PlaywrightContextManager object has no attribute 'chromium'` | Using `async with async_playwright()` context manager | Use `pw = await async_playwright().start()` |
| `Target page, context or browser has been closed` | Session expired or never created | Check `_validate_session()` and session expiry |
| Click timeout on generic selectors | Element not visible or wrong selector | Use specific selectors, add visibility wait with fallback |
| `networkidle` hangs | Third-party resources never settle | Use `domcontentloaded` |

## File Structure

```
browseTheNet.py
├── Imports
├── Session Registry (_sessions, _sessions_lock, _SESSION_TIMEOUT)
├── Core Session Functions
│   ├── _cleanup_expired_sessions()
│   ├── _destroy_session()
│   └── _validate_session()
├── Stateless Tools
│   ├── basic_page_fetch()
│   ├── fetch_page_sections()
│   ├── fetch_page_progressive()
│   └── fetch_page_section_by_id()
├── Stateful Tools
│   ├── browser_open()
│   ├── browser_navigate()
│   ├── browser_click()
│   ├── browser_fill()
│   ├── browser_get_state()
│   ├── browser_go_back()
│   ├── browser_go_forward()
│   ├── browser_screenshot()
│   └── browser_close()
├── Internal Helpers
│   ├── _extract_sections()
│   └── _classify()
└── if __name__ == "__main__": mcp.run()
```

## Dependencies

```
fastmcp
playwright
httpx
beautifulsoup4
```

Install Playwright browsers:
```bash
playwright install chromium
```

## Running

```bash
python browseTheNet.py
```

Or import as an MCP server in your application.