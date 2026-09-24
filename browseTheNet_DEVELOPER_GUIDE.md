# WebReader MCP Server — Developer Guide

> **Testing environment (use this venv for tests):** `/home/user2/Documents/workspace/code/venv/bin/python`
>
> ```bash
> # Run the test suite
> /home/user2/Documents/workspace/code/venv/bin/python -m pytest test_webread.py -v
> ```

## Overview

This MCP server provides **five simple, intuitive web tools**. The design principle:
*the tool does the hard work (fetching, rendering, cookie dismissal, article
extraction); the LLM only states what it wants.*

| Tool | Purpose |
|------|---------|
| `webreader_read(url)` | Read a page → clean markdown. **The default tool for reading anything.** |
| `webreader_search(query)` | Search the web → top results (best-effort). |
| `webreader_open(url)` | Start an interactive session → returns a `session_id`. |
| `webreader_act(session_id, action, ...)` | Do one thing in a session (click / fill / navigate / scroll / read / screenshot / go_back / go_forward). |
| `webreader_close(session_id)` | End a session. |

For ~90% of tasks you only need `webreader_read(url)`. Use `webreader_open` / `webreader_act` /
`webreader_close` only when you must click, fill forms, or step through a site.

## Content Extraction Strategy

`webreader_read` (and `webreader_act(action="read")`) extract the real article body:

1. **trafilatura** (a battle-tested article extractor) does the primary job —
   it strips navigation, footers, cookie text, ads and `<style>` noise, and
   returns clean markdown of the main content.
2. If trafilatura returns little, a **BeautifulSoup fallback** (`_bs4_extract`)
   pulls the semantic blocks (`h1–h4`, `p`, `li`, `blockquote`, `pre`) from the
   main container (`article` / `main` / common content classes).

This is the robust version of the `curl | grep '<p>'` trick that works on
Webflow / JS-heavy sites — the content is extracted from the semantic markup,
never from raw `textContent` (which is how navigation and CSS used to leak in).

## Fetching / Escalation

`webreader_read` uses an **auto-escalation pipeline** — the LLM never has to pick a
mode:

```
1. HTTP GET (httpx, real browser User-Agent)
   └─ if it yields a real article → done (method: "http")
2. otherwise → headless Playwright render:
       goto → wait for networkidle → dismiss cookie/consent banner → extract
     (method: "playwright")
3. if both fail → {"ok": false, "reason", "tried": [...], "hint"}
```

### Cookie / consent banner handling

`_dismiss_cookie_banner` best-effort clicks the first visible button among the
known vendors (OneTrust, Cookiebot, generic "Accept"/"Agree"/"OK"), with a
JavaScript force-click fallback on the OneTrust handler. This is why cookie
popups no longer block extraction.

## Session Architecture

Interactive sessions live in an in-process registry:

```python
_sessions: dict = {}                 # session_id -> {pw, browser, ctx, page, last}
_sessions_lock = asyncio.Lock()      # protects the registry
_SESSION_TIMEOUT = timedelta(minutes=15)
```

- `webreader_open` creates one Playwright instance + browser + context + page and
  stores it under a fresh `session_id`.
- `webreader_act` looks the session up (`_get_session`), refreshes `last`, and runs
  the action. Unknown/expired sessions raise a clear `ValueError`.
- `webreader_close` (`_destroy_session`) pops the entry and closes browser +
  Playwright. Closing an unknown id is a safe no-op.
- Sessions expire after 15 min idle.

### Playwright lifecycle rule

Each session owns its **own** Playwright instance (`pw = await
async_playwright().start()`). Do **not** use `async with async_playwright() as
p:` around session tools — that calls `p.stop()` on exit and kills the browser.

## Tool Reference

### `webreader_read(url, max_chars=20000)`

One-shot read. Returns JSON:
`{"ok": true, "title", "url", "content" (markdown), "method"}` on success, or
`{"ok": false, "reason", "tried": [...], "hint"}` on failure.

### `webreader_search(query, max_results=5)`

Best-effort DuckDuckGo HTML search (no API key). Returns
`{"ok": true, "query", "results": [{title, url, snippet}]}`. If the endpoint is
blocked or its layout changes it returns `ok: false` with a reason rather than
crashing. **Lowest-priority tool** — by design it degrades gracefully.

### `webreader_open(url)`

Opens a page in a session, dismisses banners, returns
`{"ok": true, "session_id", "title", "url", "preview", "hint"}`.

### `webreader_act(session_id, action, selector=None, value=None, url=None, path=None, direction=None, pixels=800, full_page=False, max_dim=768, quality=80)`

| action | params | result |
|--------|--------|--------|
| `click` | `selector` (CSS **or** visible text) | `{ok, action, url, strategy}` — hidden/dropdown-tolerant (see below) |
| `fill` | `selector`, `value` | `{ok, action, url}` |
| `navigate` | `url` | `{ok, action, url, title, preview}` |
| `scroll` | `direction` = up\|down\|top\|bottom, `pixels` (default 800) | `{ok, action, direction}` |
| `read` | — | `{ok, title, url, content, method}` |
| `screenshot` | `path` / `full_page` / `max_dim` (default 768) / `quality` (default 80) all optional | content blocks `[Image, Text]` (see below) |
| `go_back` / `go_forward` | — | `{ok, action, url}` |

`_as_selector` transparently accepts a plain visible-text label (wrapped as
`text=...`) or a raw CSS selector.

**Click is hidden/dropdown-tolerant** (`_robust_click`). It tries, in order:
1. normal click (element must be visible),
2. **force** click (element is in the DOM but hidden),
3. raw JS `el.click()` (bypasses actionability checks).

The `strategy` field reports which one worked. If all fail (the element only
exists after hover, i.e. it isn't in the DOM yet), it raises a `ValueError`
telling you to `navigate` directly to the URL.

**Screenshots return content blocks** so the LLM actually *sees* the image
(the same `ImageContent`-block design the old `webreader_browser_screenshot`
used — a base64 string inside JSON is NOT visible to the LLM):

```
[ ImageContent(type="image", data="<base64>", mimeType="image/jpeg"),
  TextContent(type="text", text='{"ok": true, "action": "screenshot", "path": "/tmp/webreader_<sid>.jpg", "mime": "image/jpeg", "size_bytes": 87000}') ]
```

The image is **also always saved to a file** (temp path by default, or `path=`)
as a fallback. **Sizing is bounded by design:**
- `max_dim` (default **768**, clamped 100–4096) caps the longest side.
- `quality` (default **80**) is the JPEG quality.
- `_MAX_INLINE_BYTES` (**1.5 MB**) is a hard cap on the inline payload. If the
  first compression pass exceeds it, a second, more aggressive pass runs
  (half the max_dim, quality 50). If it *still* exceeds the cap, the tool
  falls back to a JSON **string** with the saved `path` + a `hint` — instead of
  embedding a broken giant image. The saved file is always written.

Typical viewport shot: **~50–150 KB** (768px, JPEG q80). A 4K source or a tall
`full_page` shot both get scaled down to ≤768px before encoding.
`full_page=True` captures the whole scrollable page. (Verified against
fastmcp 3.3.1: a mixed tool may return content blocks for `screenshot` and a
JSON string for every other action.)

### `webreader_close(session_id)`

Ends the session. `{ok, action, "close", session_id}`.

## Response Contract

All tools return JSON with an `ok` boolean. Success carries the payload;
failure carries a human-readable `reason` (and a `hint` for `webreader_read`) so an
LLM can react instead of guessing. This replaces the old free-form error strings.

## Adding a New Tool

```python
@mcp.tool()
async def my_tool(url: str) -> str:
    """One line: what it does and when to use it. Keep the surface minimal."""
    # Reuse the shared helpers:
    #   _fetch_http(url)          -> (status, html)
    #   _render_playwright(url)   -> (html, title)
    #   _extract_article(html)    -> (content, title)
    #   _dismiss_cookie_banner(page)
    #   _sessions / _get_session / _destroy_session  (for interactive tools)
    ...
    return json.dumps({"ok": True, ...}, ensure_ascii=False)
```

**Prefer extending an existing tool over adding a new one.** Every extra tool
adds surface area an LLM can pick wrongly. If two tools differ only by a flag,
merge them and let the tool decide internally.

## Best Practices

- **Auto-escalate, don't ask the LLM to.** A `force_playwright`-style flag is a
  smell — it pushes implementation choice onto the caller. Decide internally.
- **Extract from semantic markup**, never raw `textContent` (avoids CSS/nav).
- **Return a uniform `{ok, ...}` contract** with actionable `reason`/`hint`.
- **Dismiss cookie banners** before extracting on interactive/open flows.
- **Each session owns its Playwright instance**; always close browser + pw.

## Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| CSS / nav text in output | extracting from `textContent` | use `_extract_article` (trafilatura/bs4 semantic extraction) |
| Cookie banner blocks content | banner not dismissed | `_dismiss_cookie_banner` runs in `_render_playwright` / `webreader_open` |
| JS-heavy page empty over HTTP | content rendered client-side | auto-escalation to Playwright in `webreader_read` |
| `unknown session_id` | session closed/expired | call `webreader_open` again to get a fresh id |
| `PlaywrightContextManager ... 'chromium'` | used `async with async_playwright()` in a session tool | use `pw = await async_playwright().start()` |
| `click` times out on a hidden/dropdown element | element not visible / not in DOM | `_robust_click` already falls back (force → JS); if it still fails, `navigate` to the direct URL |
| `screenshot` LLM can't "see" the image | returning a base64 data-URI inside a JSON string | return an `ImageContent` content block (LLM renders it); base64-in-JSON text is invisible to the LLM |
| `screenshot` returns a JSON string (no image block) | image exceeded the 1.5 MB inline cap | open the saved `path`, or retry with a smaller `max_dim` (e.g. 512) |

## File Structure

```
browseTheNet.py
├── Imports (+ optional trafilatura)
├── Constants (_UA, _TIMEOUT_MS, _COOKIE_SELECTORS)
├── Session Registry (_sessions, _sessions_lock, _SESSION_TIMEOUT)
├── Shared Helpers
│   ├── _extract_article()   # trafilatura → bs4 fallback
│   ├── _bs4_extract()
│   ├── _ok() / _err()       # uniform JSON response builders
│   ├── _as_selector()
│   ├── _robust_click()      # click fallback: normal → force → JS
│   ├── _compress_screenshot()  # PNG → JPEG (max_dim clamp 100–4096, quality clamp 1–100)
│   ├── _do_screenshot()     # [ImageContent, TextContent] + saved file (1.5 MB cap → JSON-string fallback)
│   ├── _MAX_INLINE_BYTES    # 1,500,000 — hard cap on inline screenshot payload
│   ├── _fetch_http()
│   ├── _dismiss_cookie_banner()
│   ├── _render_playwright()
│   ├── _read_live_page()
│   ├── _get_session()
│   └── _destroy_session()
├── Tools
│   ├── webreader_read()
│   ├── webreader_search()
│   ├── webreader_open()
│   ├── webreader_act()
│   └── webreader_close()
└── if __name__ == "__main__": mcp.run()
```

## Dependencies

```
fastmcp
playwright
httpx
beautifulsoup4
trafilatura
```

Install Playwright browsers:
```bash
/home/user2/Documents/workspace/code/venv/bin/python -m playwright install chromium
```

## Running

```bash
/home/user2/Documents/workspace/code/venv/bin/python browseTheNet.py
```

Or import as an MCP server in your application.

## Manual Testing Workflow

```python
# 1. Read a page (the default path)
webreader_read("https://ltx.io/blog/ltx-2-5-prompt-guide")

# 2. Search (best-effort)
webreader_search("ltx-2.5 prompting guide")

# 3. Interactive session
res = webreader_open("https://example.com")
sid = json.loads(res)["session_id"]
webreader_act(sid, "click", selector="Sign in")      # visible text or CSS selector
webreader_act(sid, "fill", selector="#email", value="a@b.com")
webreader_act(sid, "read")                            # extract current page
webreader_act(sid, "screenshot")                      # image content block + saved temp file
webreader_act(sid, "screenshot", path="shot.png")     # also save to a specific path
webreader_act(sid, "screenshot", max_dim=512)         # smaller image (default 768)
webreader_act(sid, "screenshot", full_page=True)      # whole scrollable page
webreader_act(sid, "scroll", direction="down")        # scroll the page
webreader_close(sid)
```
