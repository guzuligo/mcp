"""
WebReader MCP Server — simple, intuitive web tools.

MENTAL MODEL (just a few verbs):
─────────────────────────────────
  webreader_read(url)                → Read a page and get clean markdown. THE default tool.
                                       Just works: auto browser, auto cookie-banner handling.
  webreader_search(query)            → Search the web, get top results (best-effort).
  webreader_open(url)                → Start an INTERACTIVE session, returns a session_id.
  webreader_act(session_id, action)  → Do ONE thing in a session:
                                       click | fill | navigate | scroll | read | screenshot | go_back | go_forward
  webreader_close(session_id)        → End the session.

For ~90% of tasks you only need webreader_read(url).
Use webreader_open/webreader_act/webreader_close only when you must click, fill forms, or step through a site.

DESIGN NOTES:
  - The tool does the hard work (rendering, cookie dismissal, article extraction).
    The LLM only states *what* it wants, never *how* to scrape.
  - Extraction uses trafilatura (a battle-tested article extractor) with a
    BeautifulSoup fallback. Nav, footers, cookie text and CSS are excluded.
"""

import asyncio
import base64
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from playwright.async_api import async_playwright
import io
from PIL import Image

# Optional article extractor — fall back to BeautifulSoup if unavailable.
try:
    import trafilatura
    _HAS_TRAFILATURA = True
except Exception:  # pragma: no cover - defensive
    trafilatura = None
    _HAS_TRAFILATURA = False

mcp = FastMCP("WebReader - Simple Web Tools")

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_TIMEOUT_MS = 30_000
_DEFAULT_MAX_CHARS = 20_000
_MAX_INLINE_BYTES = 1_500_000   # hard cap on the inline screenshot payload (~1.5 MB)

# Known cookie/consent banner buttons, tried in order (most specific first).
_COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",                      # OneTrust
    "#onetrust-reject-all-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAll", # Cookiebot
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept cookies')",
    "button:has-text('Accept')",
    "button:has-text('Agree')",
    "button:has-text('Allow')",
    "button:has-text('Got it')",
    "button:has-text('OK')",
    "button:has-text('Ok')",
]

# ============================================================================
# SESSION REGISTRY  (interactive browser sessions keyed by session_id)
# ============================================================================
_sessions: dict = {}
_sessions_lock = asyncio.Lock()
_SESSION_TIMEOUT = timedelta(minutes=15)


# ============================================================================
# SHARED HELPERS
# ============================================================================
def _extract_article(html: str):
    """Extract the main article text (markdown) + title from raw HTML.

    Uses trafilatura when available; falls back to a BeautifulSoup block
    extractor. Returns (content:str, title:str|None).
    """
    content = ""
    if _HAS_TRAFILATURA:
        try:
            content = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                output_format="markdown",
            ) or ""
        except Exception:
            content = ""
    if not content or len(content.strip()) < 40:
        content = _bs4_extract(html)

    title = None
    try:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.get_text(strip=True):
            title = soup.title.get_text(strip=True)
        elif soup.find("h1"):
            title = soup.find("h1").get_text(strip=True)
    except Exception:
        pass
    return content.strip(), title


def _bs4_extract(html: str) -> str:
    """Lightweight fallback extractor: pull semantic blocks from the main area."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=re.compile("content|article|post|entry|body-text", re.I))
        or soup.body
        or soup
    )
    parts = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre"]):
        txt = " ".join(el.get_text(" ", strip=True).split())
        min_len = 3 if el.name in ("h1", "h2", "h3", "h4") else 25
        if len(txt) >= min_len:
            parts.append(txt)
    return "\n\n".join(parts)


def _ok(title, url, content, method, **extra) -> str:
    d = {"ok": True, "title": title, "url": url, "content": content, "method": method}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


def _err(url=None, reason="", tried=None, **extra) -> str:
    d = {"ok": False, "reason": reason}
    if url:
        d["url"] = url
    if tried is not None:
        d["tried"] = tried
    if extra:
        d.update(extra)
    d["hint"] = "Try webreader_search to find an alternate source, or use webreader_open + webreader_act to interact."
    return json.dumps(d, ensure_ascii=False)


def _as_selector(selector: str) -> str:
    """Accept either a CSS selector or a plain visible-text label."""
    s = (selector or "").strip()
    if not s:
        raise ValueError("selector is required for this action")
    # If it already looks like a selector, use it as-is.
    if any(ch in s for ch in "#.:[>~") or s.startswith(
        ("a", "button", "div", "span", "input", "select", "textarea", "label", "img")
    ):
        return s
    return f"text={s}"


async def _robust_click(page, sel: str) -> str:
    """Click a locator, tolerating hidden/dropdown elements.

    Tries, in order: (1) normal click, (2) force click (skips actionability
    checks), (3) a raw JS el.click(). Returns the strategy that worked.
    Raises ValueError with an actionable hint if none work.
    """
    loc = page.locator(sel).first
    try:
        await loc.click(timeout=8000)
        return "click"
    except Exception:
        pass
    try:
        await loc.click(force=True, timeout=8000)
        return "force-click"
    except Exception:
        pass
    try:
        await loc.evaluate("el => el.click()")
        return "js-click"
    except Exception:
        pass
    raise ValueError(
        f"could not click '{sel}' (not visible / not in DOM). "
        "It may only appear on hover — use action='navigate' with the direct URL."
    )


async def _compress_screenshot(png_bytes: bytes, max_dim: int = 768, quality: int = 85,
                                fmt: str = "png") -> tuple:
    """Process a PNG screenshot, optionally compressing to a smaller format.

    By default, returns lossless PNG with maximum compression (compress_level=9).
    If fmt="jpeg" or fmt="webp", converts to lossy format with given quality.

    max_dim (clamped 100–4096) caps the longest side; quality is for jpeg/webp only.
    Returns (bytes, mime_type).
    """
    max_dim = max(100, min(4096, int(max_dim)))
    quality = max(1, min(100, int(quality)))
    fmt = fmt.lower().strip()
    if fmt not in ("png", "jpeg", "webp"):
        fmt = "png"
    
    try:
        img = Image.open(io.BytesIO(png_bytes))
        
        # Composite onto white background and convert to RGB (no alpha channel)
        # This prevents transparent pixels from appearing as black borders in LM Studio
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        
        # Resize if needed
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_w = max(320, int(w * scale))
            new_h = max(200, int(h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        
        # Re-encode to desired format
        if fmt == "png":
            output_buf = io.BytesIO()
            img.save(output_buf, format="PNG", compress_level=9)
            screenshot_bytes = output_buf.getvalue()
            return screenshot_bytes, "image/png"
        elif fmt == "jpeg":
            if img.mode != "RGB":
                img = img.convert("RGB")
            output_buf = io.BytesIO()
            img.save(output_buf, format="JPEG", quality=quality, optimize=True)
            screenshot_bytes = output_buf.getvalue()
            return screenshot_bytes, "image/jpeg"
        elif fmt == "webp":
            if img.mode != "RGB":
                img = img.convert("RGB")
            output_buf = io.BytesIO()
            img.save(output_buf, format="WEBP", quality=quality, optimize=True)
            screenshot_bytes = output_buf.getvalue()
            return screenshot_bytes, "image/webp"
    except Exception:
        return png_bytes, "image/png"
    
    return png_bytes, "image/png"


async def _do_screenshot(page, path: str | None, full_page: bool, sid: str,
                         max_dim: int = 768, quality: int = 85,
                         fmt: str = "png") -> "str | list":
    """Capture a screenshot. Returns content blocks (ImageContent + TextContent) 
    so the MCP host can project the image into the model's vision context.

    By default, returns lossless PNG. Can optionally return JPEG or WebP for
    smaller file sizes.

    Smart capture: detects if the page is an image and screenshots the appropriate
    element to avoid browser chrome borders.

    The image is saved to a file (a temp path by default) AND returned inline
    as an ImageContent block with raw base64-encoded data. A TextContent block with
    metadata (path, size, mime type) is also included.

    Args:
        fmt: Output format - "png" (default, lossless), "jpeg" (lossy, ~15-50KB),
             or "webp" (lossy, good compression).
        quality: JPEG/WebP quality 1-100 (default 85, only used for jpeg/webp).

    Returns:
        A list of content blocks: [ImageContent, TextContent]
        - ImageContent: raw base64-encoded image with mimeType
        - TextContent: JSON metadata with path, size, mime info
    """
    # Detect if the current page is an image (for direct image viewing)
    page_url = page.url.lower()
    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico')
    
    # Check if URL points to an image file
    is_image_url = any(page_url.endswith(ext) for ext in image_extensions)
    # Also check if URL contains image content type (for dynamic image endpoints)
    is_image_content = 'image/' in page_url
    
    # Smart capture: screenshot the appropriate element
    if is_image_url or is_image_content:
        # Page is an image - screenshot the img element to avoid browser chrome
        try:
            img_element = page.locator("img").first
            if await img_element.is_visible():
                screenshot_bytes = await img_element.screenshot(type="png")
            else:
                # Fallback to body screenshot
                screenshot_bytes = await page.locator("body").screenshot(type="png")
        except Exception:
            # Fallback to body screenshot
            screenshot_bytes = await page.locator("body").screenshot(type="png")
    else:
        # Regular web page - screenshot the body element to avoid browser chrome
        try:
            screenshot_bytes = await page.locator("body").screenshot(type="png")
        except Exception:
            # Final fallback to full page screenshot
            screenshot_bytes = await page.screenshot(full_page=full_page, type="png")
    
    # Process with Pillow (resize, compress, format conversion)
    img = Image.open(io.BytesIO(screenshot_bytes))
    
    # Keep RGBA mode to preserve original colors and alpha channel
    # Only convert to RGB for JPEG/WebP formats which don't support alpha
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    
    # Resize if needed
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_w = max(320, int(w * scale))
        new_h = max(200, int(h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
    
    # Re-encode to desired format
    if fmt == "png":
        output_buf = io.BytesIO()
        # Keep RGBA for PNG to preserve original colors and alpha
        img.save(output_buf, format="PNG")
        screenshot_bytes = output_buf.getvalue()
        mime = "image/png"
    elif fmt == "jpeg":
        # JPEG doesn't support alpha - composite onto white background
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        output_buf = io.BytesIO()
        img.save(output_buf, format="JPEG", quality=quality, optimize=True)
        screenshot_bytes = output_buf.getvalue()
        mime = "image/jpeg"
    elif fmt == "webp":
        output_buf = io.BytesIO()
        img.save(output_buf, format="WEBP", quality=quality, optimize=True)
        screenshot_bytes = output_buf.getvalue()
        mime = "image/webp"
    
    # Save to file with correct extension
    ext_map = {"png": "png", "jpeg": "jpg", "webp": "webp"}
    file_ext = ext_map.get(fmt, "png")
    dest = path or os.path.join(tempfile.gettempdir(), f"webreader_{sid}.{file_ext}")
    with open(dest, "wb") as f:
        f.write(screenshot_bytes)
    
    # Encode to base64 for inline return (raw base64, NOT data URI)
    b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
    
    # Build metadata JSON
    meta = {
        "ok": True,
        "action": "screenshot",
        "path": dest,
        "mime": mime,
        "size_bytes": len(screenshot_bytes),
        "message": "Screenshot captured. Image is attached above as ImageContent."
    }
    
    # Return content blocks: ImageContent for the image, TextContent for metadata
    # Use raw base64 data (NOT data URI) - mimeType field tells the decoder the format
    return [
        ImageContent(type="image", data=b64, mimeType=mime),
        TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
    ]


async def _fetch_http(url: str):
    """GET a URL. Returns (status:int, html:str). Raises on network error."""
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        resp = await client.get(url, headers=headers)
        return resp.status_code, resp.text


async def _dismiss_cookie_banner(page) -> bool:
    """Best-effort dismissal of cookie/consent banners. Returns True if clicked one."""
    for sel in _COOKIE_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click(timeout=2000)
                await page.wait_for_timeout(300)
                return True
        except Exception:
            continue
    # Last resort: force-click the well-known OneTrust button via JS.
    try:
        await page.evaluate(
            "() => { const b = document.querySelector('#onetrust-accept-btn-handler'); if (b) b.click(); }"
        )
        await page.wait_for_timeout(300)
        return True
    except Exception:
        return False


async def _render_playwright(url: str):
    """Render a URL in a short-lived headless browser. Returns (html, title)."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 900})
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            await _dismiss_cookie_banner(page)
            await page.wait_for_timeout(500)
            title = await page.title()
            html = await page.content()
            return html, title
        finally:
            await browser.close()


async def _read_live_page(page):
    """Extract article content + title from an already-open live page."""
    html = await page.content()
    content, title = _extract_article(html)
    return content, title


async def _get_session(session_id: str) -> dict:
    async with _sessions_lock:
        s = _sessions.get(session_id)
        if not s:
            raise ValueError(f"unknown session_id: {session_id} (was it closed?)")
        # Expire stale sessions.
        if datetime.now() - s["last"] > _SESSION_TIMEOUT:
            _sessions.pop(session_id, None)
            raise ValueError(f"session {session_id} expired (idle >15 min)")
        s["last"] = datetime.now()
    return s


async def _destroy_session(sid: str):
    async with _sessions_lock:
        s = _sessions.pop(sid, None)
    if not s:
        return
    try:
        await s["browser"].close()
    except Exception:
        pass
    try:
        await s["pw"].stop()
    except Exception:
        pass


# ============================================================================
# TOOLS
# ============================================================================
@mcp.tool()
async def webreader_read(url: str, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """Read a webpage and return its main content as clean markdown.

    THIS IS THE DEFAULT TOOL FOR READING ANY PAGE. One call, no setup.
    It handles the hard parts automatically:
      - fast HTTP fetch first; if that yields little/no article, it re-tries
        in a real headless browser (JavaScript-heavy / Webflow-style sites);
      - dismisses cookie / consent banners (OneTrust, Cookiebot, ...);
      - strips navigation, footers, ads, CSS and cookie text so you get the
        actual article body.

    Args:
        url: The page to read (must start with http:// or https://).
        max_chars: Max characters of content to return (default 20000).

    Returns:
        JSON: {"ok": true, "title", "url", "content" (markdown), "method"}.
        On failure: {"ok": false, "reason", "tried": [...], "hint"}.

    Example:
        webreader_read("https://ltx.io/blog/ltx-2-5-prompt-guide")
    """
    tried = []
    # 1) Fast HTTP path.
    try:
        status, html = await _fetch_http(url)
        tried.append(f"http:{status}")
        if status == 200:
            content, title = _extract_article(html)
            if content and len(content) >= 40:
                return _ok(title, url, content[:max_chars], "http")
    except Exception:
        tried.append("http:error")
    # 2) Escalate to a real browser (JS rendering + cookie dismissal).
    try:
        html, title = await _render_playwright(url)
        tried.append("playwright")
        content, t2 = _extract_article(html)
        if content and len(content) >= 40:
            return _ok(title or t2, url, content[:max_chars], "playwright")
    except Exception as e:
        tried.append(f"playwright:{type(e).__name__}")
    return _err(url, "could not extract readable article content", tried)


@mcp.tool()
async def webreader_search(query: str, max_results: int = 5) -> str:
    """Search the web and return top results (title, url, snippet).

    Best-effort: uses DuckDuckGo's HTML endpoint (no API key required). If the
    endpoint is unavailable or changes layout, this returns ok:false with a
    reason rather than crashing. Use webreader_read on any result URL to read it.

    Args:
        query: Search terms.
        max_results: Max results to return (default 5).

    Returns:
        JSON: {"ok": true, "query", "results": [{title, url, snippet}, ...]}
    """
    try:
        headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            r = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers=headers,
            )
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for res in soup.select("div.result, article"):
            a = res.select_one("a.result__a, h2 a")
            if not a:
                continue
            snip_el = res.select_one(".result__snippet, .result__snippet p")
            href = a.get("href", "")
            if "uddg=" in href:
                q = parse_qs(urlparse(href).query)
                if q.get("uddg"):
                    href = unquote(q["uddg"][0])
            results.append({
                "title": a.get_text(strip=True),
                "url": href,
                "snippet": snip_el.get_text(strip=True) if snip_el else "",
            })
            if len(results) >= max_results:
                break
        if results:
            return json.dumps({"ok": True, "query": query, "results": results}, ensure_ascii=False)
        # NOTE: DuckDuckGo layout/anti-bot may block parsing — best-effort by design.
        return json.dumps({
            "ok": False, "query": query, "results": [],
            "reason": "no results parsed (endpoint may be blocked or layout changed)",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "ok": False, "query": query, "results": [],
            "reason": f"webreader_search failed: {type(e).__name__}: {e}",
        }, ensure_ascii=False)


@mcp.tool()
async def webreader_open(url: str) -> str:
    """Open a webpage in an INTERACTIVE browser session and return a session_id.

    Use this when you need to click, fill forms, or step through a site.
    (For simply reading a page, prefer webreader_read — no session needed.)
    Cookie/consent banners are dismissed automatically on open.

    Args:
        url: The page to open.

    Returns:
        JSON: {"ok": true, "session_id", "title", "url", "preview", "hint"}.
    """
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(400)
        content, title = await _read_live_page(page)
        sid = uuid.uuid4().hex[:12]
        _sessions[sid] = {
            "pw": pw, "browser": browser, "ctx": ctx, "page": page,
            "last": datetime.now(),
        }
        return json.dumps({
            "ok": True,
            "session_id": sid,
            "title": title,
            "url": page.url,
            "preview": content[:400],
            "hint": "Use webreader_act(session_id, action, ...) to interact; action='read' for full text; webreader_close(session_id) when done.",
        }, ensure_ascii=False)
    except Exception as e:
        for b in (browser,):
            if b:
                try:
                    await b.close()
                except Exception:
                    pass
        try:
            await pw.stop()
        except Exception:
            pass
        return _err(url, f"failed to open page: {type(e).__name__}: {e}")


@mcp.tool()
async def webreader_act(
    session_id: str,
    action: str,
    selector: str | None = None,
    value: str | None = None,
    url: str | None = None,
    path: str | None = None,
    direction: str | None = None,
    pixels: int = 800,
    full_page: bool = False,
    max_dim: int = 768,
    quality: int = 85,
    fmt: str = "png",
) -> "str | list":
    """Perform ONE action inside an open session (see webreader_open).

    Actions:
      click     → click an element (hidden/dropdown-tolerant). selector = CSS OR visible text
      fill      → set a field's value. selector = the input; value = text to type
      navigate  → go to a new url.    url = target
      scroll    → scroll the page.    direction = up|down|top|bottom; pixels = amount (default 800)
      read      → extract the current page's article content (same engine as webreader_read)
      screenshot→ capture the page. Returns inline base64 image + saved path.
                  path / full_page / max_dim (default 768) / quality (default 80) optional.
      go_back   → browser back
      go_forward→ browser forward

    Args:
        session_id: id returned by webreader_open.
        action: one of the actions above.
        selector: for click/fill — a CSS selector or the element's visible text.
        value: for fill — the text to enter.
        url: for navigate — the URL to load.
        path: for screenshot — optional file path to save to (a temp file is always saved too).
        direction: for scroll — up, down, top, or bottom (default down).
        pixels: for scroll — how far to scroll (default 800).
        full_page: for screenshot — capture the whole page, not just the viewport.
        max_dim: for screenshot — max pixels on the longest side (default 768, clamped 100–4096).
        quality: for screenshot — JPEG/WebP quality 1–100 (default 85, only used for jpeg/webp).
        fmt: for screenshot — output format: "png" (default, lossless), "jpeg" (lossy, ~15-50KB),
             or "webp" (lossy, good compression).

    Returns:
        A JSON string describing the result (updated url, ok, etc.).
        screenshot returns content blocks: [ImageContent, TextContent]
        - ImageContent: base64-encoded image with mimeType for vision display
        - TextContent: JSON with {"ok", "path", "mime", "size_bytes", "message"}
    """
    s = await _get_session(session_id)
    page = s["page"]
    action = (action or "").strip().lower()

    if action == "click":
        sel = _as_selector(selector)
        strategy = await _robust_click(page, sel)
        await page.wait_for_timeout(400)
        await _dismiss_cookie_banner(page)
        return json.dumps({"ok": True, "action": "click", "url": page.url, "strategy": strategy}, ensure_ascii=False)

    if action == "fill":
        sel = _as_selector(selector)
        await page.locator(sel).first.fill(value or "", timeout=_TIMEOUT_MS)
        await page.wait_for_timeout(200)
        return json.dumps({"ok": True, "action": "fill", "url": page.url}, ensure_ascii=False)

    if action == "navigate":
        if not url:
            raise ValueError("navigate requires a url")
        await page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        await _dismiss_cookie_banner(page)
        content, title = await _read_live_page(page)
        return json.dumps({
            "ok": True, "action": "navigate", "url": page.url,
            "title": title, "preview": content[:300],
        }, ensure_ascii=False)

    if action == "scroll":
        d = (direction or "down").strip().lower()
        if d not in ("up", "down", "top", "bottom"):
            raise ValueError(f"scroll direction must be up|down|top|bottom (got '{direction}')")
        if d == "top":
            await page.evaluate("window.scrollTo(0, 0)")
        elif d == "bottom":
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            delta = -int(pixels) if d == "up" else int(pixels)
            await page.evaluate(f"window.scrollBy(0, {delta})")
        await page.wait_for_timeout(200)
        return json.dumps({"ok": True, "action": "scroll", "direction": d}, ensure_ascii=False)

    if action == "read":
        content, title = await _read_live_page(page)
        return _ok(title, page.url, content, "session-read")

    if action == "screenshot":
        return await _do_screenshot(page, path, full_page, session_id,
                                    max_dim=max_dim, quality=quality, fmt=fmt)

    if action == "go_back":
        await page.go_back(wait_until="domcontentloaded")
        await page.wait_for_timeout(400)
        return json.dumps({"ok": True, "action": "go_back", "url": page.url}, ensure_ascii=False)

    if action == "go_forward":
        await page.go_forward(wait_until="domcontentloaded")
        await page.wait_for_timeout(400)
        return json.dumps({"ok": True, "action": "go_forward", "url": page.url}, ensure_ascii=False)

    return json.dumps({
        "ok": False,
        "reason": f"unknown action '{action}'",
        "valid_actions": ["click", "fill", "navigate", "scroll", "read", "screenshot", "go_back", "go_forward"],
    }, ensure_ascii=False)


@mcp.tool()
async def webreader_close(session_id: str) -> str:
    """End an interactive session and free its browser (see webreader_open).

    Args:
        session_id: id returned by webreader_open.

    Returns:
        JSON: {"ok": true, "action": "close"}.
    """
    await _destroy_session(session_id)
    return json.dumps({"ok": True, "action": "close", "session_id": session_id}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
