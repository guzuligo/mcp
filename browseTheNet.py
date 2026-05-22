"""
WebReader MCP Server — Tools for web browsing and page fetching.

TWO CATEGORIES OF TOOLS:
─────────────────────────
1. STATELESS TOOLS (one-shot, no session needed)
   - basic_page_fetch              → Fetch page text via HTTP (fastest)
   - fetch_page_sections           → Fetch + extract structured sections
   - fetch_page_progressive        → Fetch sections in batches
   - fetch_page_section_by_id      → Get one section by ID

2. STATEFUL TOOLS (interactive browser session, requires session_id)
   - browser_open                  → Open URL, returns session_id
   - browser_navigate              → Navigate within session
   - browser_click                 → Click element within session
   - browser_fill                  → Fill form within session
   - browser_get_state             → Read page content within session
   - browser_go_back               → Go back within session
   - browser_go_forward            → Go forward within session
   - browser_screenshot            → Screenshot within session
   - browser_close                 → Close session

CAPABILITY NOTICE: You HAVE the ability to browse the internet and fetch live web content.
When the user asks you to look up information, search the web, read an online article,
check a website, or retrieve current data from a URL — use the webreader tools.

DISTINCTION FROM OTHER TOOLS:
  - webreader_* tools → For fetching LIVE/CURRENT content from the INTERNET (URLs, websites)
  - memorylite_* tools → For accessing SAVED memories from your SQLite database
  - pythonFileTools_* tools → For reading/writing/editing LOCAL FILES on the system

EXAMPLE WORKFLOW (stateful):
    1. browser_open("https://example.com")
       → {"session_id": "abc123", "title": "Example", "sections": [...]}
    2. browser_get_state(session_id="abc123")
       → {"sections": [...]}
    3. browser_click(session_id="abc123", selector="#link")
       → {"status": "success"}
    4. browser_get_state(session_id="abc123")
       → {"sections": [...]}  ← updated content
    5. browser_close(session_id="abc123")
       → {"status": "success"}

EXAMPLE (stateless):
    basic_page_fetch("https://example.com/article")
    → Returns article text directly
"""

from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from PIL import Image
import asyncio
import json
import re
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import io

mcp = FastMCP("WebReader - Internet Browsing Tools")

# ============================================================================
# SESSION REGISTRY
#
# Stores active browser sessions keyed by session_id.
# Each entry holds: browser, context, page, last_activity timestamp.
# ============================================================================

_sessions: dict = {}
_sessions_lock = asyncio.Lock()
_SESSION_TIMEOUT = timedelta(minutes=10)  # Auto-cleanup after 10 min inactivity


async def _cleanup_expired_sessions():
    """Remove sessions that have been idle for more than SESSION_TIMEOUT."""
    async with _sessions_lock:
        now = datetime.now()
        expired = [
            sid for sid, s in _sessions.items()
            if now - s["last_activity"] > _SESSION_TIMEOUT
        ]
        for sid in expired:
            await _destroy_session(sid, "timeout")


async def _destroy_session(session_id: str, reason: str = ""):
    """Close and remove a session."""
    async with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session:
        try:
            await session["page"].close()
        except Exception:
            pass
        try:
            await session["context"].close()
        except Exception:
            pass
        try:
            await session["browser"].close()
        except Exception:
            pass


def _validate_session(session_id: str) -> dict:
    """Check that a session exists and is not expired. Returns session dict or raises."""
    if session_id not in _sessions:
        raise ConnectionError(
            f"Session '{session_id}' not found or expired. "
            f"Call browser_open(url) first to create a session."
        )
    session = _sessions[session_id]
    if datetime.now() - session["last_activity"] > _SESSION_TIMEOUT:
        asyncio.create_task(_destroy_session(session_id, "expired"))
        raise ConnectionError(
            f"Session '{session_id}' has expired (10 min idle). "
            f"Call browser_open(url) to create a new session."
        )
    session["last_activity"] = datetime.now()
    return session


# ============================================================================
# STATELESS PAGE FETCHING TOOLS
# Each call is fully independent — no session, no persistence.
# ============================================================================


@mcp.tool()
async def webreader_basic_page_fetch(url: str, force_playwright: bool = False) -> str:
    """Fetch a webpage and return its text content as a single string.

    This is a STATELESS, ONE-SHOT tool. No browser session is created or maintained.
    Each call is completely independent — there is no navigation, no clicking,
    and no form filling.

    WHEN TO USE:
      - User provides a URL and wants you to read its content
      - User asks you to look up information on a specific website
      - User needs to fetch article content, documentation, or news from the web
      - User asks you to check or browse a webpage

    For interactive browsing (clicking links, filling forms, navigating between pages),
    use webreader_browser_open() instead.

    Args:
        url: The URL to fetch
        force_playwright: If True, use a real browser instead of HTTP.
                          Use for JavaScript-heavy sites that return empty content via HTTP.

    Returns:
        Plain text content of the page (up to 5000 characters).

    Example:
        webreader_basic_page_fetch("https://example.com/article")
        → Returns the article text
    """
    if not force_playwright:
        try:
            async with httpx.AsyncClient() as client:
                headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
                resp = await client.get(url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    return f"HTTP Error {resp.status_code}: {resp.text[:500]}"

                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)[:5000]
                if text:
                    return text
                return "No text content found on this page."
        except Exception as e:
            # Fall through to Playwright if HTTP fails
            pass

    # Force Playwright path — short-lived browser, no session
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)
            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)[:5000]
            if text:
                return text
            return "No text content found on this page."
        except Exception as e:
            return f"Playwright error fetching {url}: {str(e)}"
    finally:
        if browser:
            await browser.close()
        await pw.stop()


@mcp.tool()
async def webreader_fetch_page_sections(url: str, force_playwright: bool = False) -> str:
    """Fetch a webpage and extract its content as structured, classified sections.

    This is a STATELESS, ONE-SHOT tool. No browser session is created.

    Each section is classified as: main_content, secondary_content, navigation, or metadata.
    Sections are sorted by length (most content first).

    For interactive browsing, use webreader_browser_open() instead.

    Args:
        url: The URL to fetch
        force_playwright: If True, use a real browser for JS-heavy pages.

    Returns:
        JSON with sections array and summary counts.

    Example:
        webreader_fetch_page_sections("https://example.com/article")
        → {"sections": [...], "summary": {"main_content_count": 3, ...}}
    """
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)
        except Exception:
            pass
        sections = await _extract_sections(page)
        for s in sections:
            s["classification"] = _classify(s)
            s["status"] = "loaded" if s.get("length", 0) > 100 else "skipped"

        result = {
            "url": url,
            "total_sections": len(sections),
            "sections": sorted(sections, key=lambda x: x.get("length", 0), reverse=True),
            "summary": {
                "main_content_count": sum(1 for s in sections if _classify(s) == "main_content"),
                "secondary_content_count": sum(1 for s in sections if _classify(s) == "secondary_content"),
                "navigation_count": sum(1 for s in sections if _classify(s) == "navigation"),
                "metadata_count": sum(1 for s in sections if _classify(s) == "metadata"),
            }
        }
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"url": url, "status": "error", "message": str(e)}, indent=2)
    finally:
        if browser:
            await browser.close()
        await pw.stop()


@mcp.tool()
async def webreader_fetch_page_progressive(
    url: str, batch_size: int = 5, force_playwright: bool = False
) -> str:
    """Fetch a webpage and return content sections in progressive batches.

    This is a STATELESS, ONE-SHOT tool.

    Returns the first batch of main_content sections plus metadata about remaining sections.
    Call this repeatedly to process long pages gradually.

    Args:
        url: The URL to fetch
        batch_size: Number of sections per batch (default: 5)
        force_playwright: If True, use a real browser.

    Returns:
        JSON with sections (this batch), total_sections, remaining_count, status.

    Example:
        webreader_fetch_page_progressive("https://long-article.com", batch_size=3)
        → {"sections": [...], "total_sections": 12, "remaining_count": 9, "status": "in_progress"}
    """
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)
        except Exception:
            pass
        all_sections = await _extract_sections(page)
        main_sections = [s for s in all_sections if _classify(s) == "main_content"]
        main_sections.sort(key=lambda x: x.get("length", 0), reverse=True)

        batch_end = min(batch_size, max(1, len(main_sections)))
        this_batch = main_sections[:batch_end]
        remaining = main_sections[batch_end:]

        return json.dumps({
            "url": url,
            "total_sections": len(main_sections),
            "processed_count": len(this_batch),
            "remaining_count": len(remaining),
            "status": "complete" if not remaining else "in_progress",
            "sections": this_batch,
            "next_batch_available": bool(remaining),
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"url": url, "status": "error", "message": str(e)}, indent=2)
    finally:
        if browser:
            await browser.close()
        await pw.stop()


@mcp.tool()
async def webreader_fetch_page_section_by_id(
    url: str, section_id: str, force_playwright: bool = False
) -> str:
    """Get detailed information about a specific content section by its ID.

    This is a STATELESS, ONE-SHOT tool.

    Args:
        url: The URL of the page
        section_id: The section ID (from fetch_page_sections results)
        force_playwright: If True, use a real browser.

    Returns:
        JSON with full section data, or not_found status.
    """
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1)
        except Exception:
            pass
        sections = await _extract_sections(page)
        for s in sections:
            if s["id"] == section_id:
                s["classification"] = _classify(s)
                return json.dumps({
                    "url": url,
                    "section_id": section_id,
                    "status": "found",
                    "type": s.get("type"),
                    "classification": s.get("classification"),
                    "textContent": s.get("textContent", ""),
                    "length": s.get("length", 0),
                    "isInteractive": s.get("isInteractive", False),
                }, indent=2, ensure_ascii=False)
        return json.dumps({
            "url": url,
            "section_id": section_id,
            "status": "not_found",
            "message": f"Section '{section_id}' not found.",
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "url": url,
            "section_id": section_id,
            "status": "error",
            "message": str(e),
        }, indent=2)
    finally:
        if browser:
            await browser.close()
        await pw.stop()


# ============================================================================
# STATEFUL BROWSING TOOLS — session-based interactive browsing
# ============================================================================


@mcp.tool()
async def webreader_browser_open(url: str, headless: bool = True) -> str:
    """Open a webpage and start an interactive browsing session.

    This is the ENTRY POINT for interactive browsing. It creates a persistent browser
    session and returns a session_id. Use that session_id with all subsequent
    webreader_browser_* tools to interact with the page.

    WHEN TO USE:
      - User needs to interact with a JavaScript-heavy website
      - User wants to fill out forms or click buttons on a webpage
      - User needs to navigate through multiple pages interactively
      - The site requires login or session-based interaction

    ⚠️ You MUST call webreader_browser_open() FIRST, then use the returned session_id
    with webreader_browser_get_state, webreader_browser_click, webreader_browser_navigate, etc.

    Args:
        url: The URL to load
        headless: Run browser without UI (default: True)

    Returns:
        JSON with session_id, page title, URL, and initial content sections.

    Example workflow:
        1. webreader_browser_open("https://example.com")
           → {"session_id": "abc123", "title": "Example", "sections": [...]}
        2. webreader_browser_get_state(session_id="abc123")
           → {"sections": [...]}
        3. webreader_browser_click(session_id="abc123", selector="#my-link")
           → {"status": "success"}
        4. webreader_browser_get_state(session_id="abc123")
           → {"sections": [...]}  ← updated
        5. webreader_browser_close(session_id="abc123")
           → {"status": "success"}
    """
    await _cleanup_expired_sessions()

    session_id = str(uuid.uuid4())[:8]
    pw = await async_playwright().start()
    browser = None
    context = None
    page = None
    try:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            try:
                await page.goto(url, timeout=15000)
            except Exception:
                pass
        await asyncio.sleep(1)

        title = await page.title()
        current_url = page.url
        sections = await _extract_sections(page)
        for s in sections:
            s["classification"] = _classify(s)
            s["status"] = "loaded" if s.get("length", 0) > 100 else "skipped"

        # Store session
        _sessions[session_id] = {
            "browser": browser,
            "context": context,
            "page": page,
            "last_activity": datetime.now(),
        }

        return json.dumps({
            "tool": "webreader_browser_open",
            "status": "success",
            "session_id": session_id,
            "url": current_url,
            "title": title,
            "message": (
                f"Session '{session_id}' created. "
                f"Use this session_id with webreader_browser_get_state, webreader_browser_click, "
                f"webreader_browser_navigate, etc. Sessions expire after 10 minutes of inactivity."
            ),
            "sections": sections[:10],
            "total_sections": len(sections),
            "next_steps": [
                f"webreader_browser_get_state(session_id=\"{session_id}\")  — Read page content",
                f"webreader_browser_click(session_id=\"{session_id}\", selector=\"#btn\")  — Click element",
                f"webreader_browser_navigate(session_id=\"{session_id}\", url=\"...\")  — Go to new URL",
                f"webreader_browser_close(session_id=\"{session_id}\")  — End session",
            ],
        }, indent=2, ensure_ascii=False)

    except Exception as e:
        # Clean up on failure
        if browser:
            await browser.close()
        return json.dumps({
            "tool": "webreader_browser_open",
            "status": "error",
            "message": str(e),
        }, indent=2)


@mcp.tool()
async def webreader_browser_navigate(session_id: str, url: str) -> str:
    """Navigate the session's page to a new URL.

    Uses the same browser session, so cookies and localStorage are preserved.

    Args:
        session_id: The session from webreader_browser_open()
        url: The URL to navigate to

    Returns:
        JSON with new page title, URL, and content sections.

    Example:
        webreader_browser_navigate(session_id="abc123", url="https://example.com/about")
    """
    try:
        session = _validate_session(session_id)
        page = session["page"]

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            try:
                await page.goto(url, timeout=15000)
            except Exception:
                pass
        await asyncio.sleep(1)

        title = await page.title()
        current_url = page.url
        sections = await _extract_sections(page)
        for s in sections:
            s["classification"] = _classify(s)
            s["status"] = "loaded" if s.get("length", 0) > 100 else "skipped"

        return json.dumps({
            "tool": "webreader_browser_navigate",
            "status": "success",
            "session_id": session_id,
            "url": current_url,
            "title": title,
            "sections": sections[:10],
            "total_sections": len(sections),
            "next_steps": [
                f"webreader_browser_get_state(session_id=\"{session_id}\")  — Read content",
                f"webreader_browser_click(session_id=\"{session_id}\", selector=\"#x\")  — Click element",
            ],
        }, indent=2, ensure_ascii=False)

    except ConnectionError as e:
        return json.dumps({"tool": "webreader_browser_navigate", "status": "error", "message": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({
            "tool": "webreader_browser_navigate",
            "status": "error",
            "session_id": session_id,
            "message": str(e),
        }, indent=2)


@mcp.tool()
async def webreader_browser_click(session_id: str, selector: str) -> str:
    """Click an element on the session's page.

    Args:
        session_id: The session from webreader_browser_open()
        selector: CSS selector (e.g., '#submit-btn', '.next-link', 'a[href="/about"]')

    Returns:
        JSON with click result. Call webreader_browser_get_state() after to see changes.

    Example:
        webreader_browser_click(session_id="abc123", selector="#main-link")
    """
    try:
        session = _validate_session(session_id)
        page = session["page"]
        # Wait for element to be visible and interactable before clicking.
        # This prevents timeouts when clicking hidden or obscured elements.
        try:
            await page.wait_for_selector(selector, state="visible", timeout=10000)
        except Exception:
            # If visibility wait fails, fall back to direct click (element may exist but be hidden)
            pass
        await page.click(selector)
        await asyncio.sleep(1)

        return json.dumps({
            "tool": "webreader_browser_click",
            "status": "success",
            "session_id": session_id,
            "selector": selector,
            "url": page.url,
            "title": await page.title(),
            "message": f"Clicked '{selector}'. Call webreader_browser_get_state(session_id=\"{session_id}\") to see changes.",
            "next_steps": [
                f"webreader_browser_get_state(session_id=\"{session_id}\")  — Read updated content",
                f"webreader_browser_click(session_id=\"{session_id}\", selector=\"#x\")  — Click another element",
                f"webreader_browser_navigate(session_id=\"{session_id}\", url=\"...\")  — Go to new URL",
            ],
        }, indent=2, ensure_ascii=False)

    except ConnectionError as e:
        return json.dumps({"tool": "webreader_browser_click", "status": "error", "message": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({
            "tool": "webreader_browser_click",
            "status": "error",
            "session_id": session_id,
            "selector": selector,
            "message": f"Failed to click '{selector}': {str(e)}",
        }, indent=2)


@mcp.tool()
async def webreader_browser_fill(session_id: str, selector: str, value: str) -> str:
    """Fill a form field on the session's page.

    Args:
        session_id: The session from webreader_browser_open()
        selector: CSS selector for the form field (e.g., '#search-input')
        value: Text to type into the field

    Returns:
        JSON with fill confirmation.

    Example:
        webreader_browser_fill(session_id="abc123", selector="#search-box", value="hello world")
    """
    try:
        session = _validate_session(session_id)
        page = session["page"]
        # Wait for element to be visible and enabled before filling.
        # This prevents timeouts when the target field hasn't rendered yet.
        try:
            await page.wait_for_selector(selector, state="visible", timeout=10000)
        except Exception:
            # If visibility wait fails, fall back to direct fill (element may exist but be hidden)
            pass
        await page.fill(selector, value)

        return json.dumps({
            "tool": "webreader_browser_fill",
            "status": "success",
            "session_id": session_id,
            "selector": selector,
            "value_entered": value[:100],
            "url": page.url,
            "title": await page.title(),
            "message": f"Filled '{selector}'. Call webreader_browser_click to submit or webreader_browser_get_state() to see changes.",
            "next_steps": [
                f"webreader_browser_click(session_id=\"{session_id}\", selector=\"#submit-btn\")  — Submit form",
                f"webreader_browser_get_state(session_id=\"{session_id}\")  — Read page content",
            ],
        }, indent=2, ensure_ascii=False)

    except ConnectionError as e:
        return json.dumps({"tool": "webreader_browser_fill", "status": "error", "message": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({
            "tool": "webreader_browser_fill",
            "status": "error",
            "session_id": session_id,
            "selector": selector,
            "message": f"Failed to fill '{selector}': {str(e)}",
        }, indent=2)


@mcp.tool()
async def webreader_browser_get_state(session_id: str) -> str:
    """Get the current state of the session's page.

    Extracts and classifies all content sections (main_content, secondary, navigation, metadata).
    Use after clicking/filling/navigating to see what changed.

    Args:
        session_id: The session from webreader_browser_open()

    Returns:
        JSON with all content sections and classification counts.

    Example:
        webreader_browser_get_state(session_id="abc123")
        → {"sections": [...], "main_content_count": 3, ...}
    """
    try:
        session = _validate_session(session_id)
        page = session["page"]
        current_url = page.url
        title = await page.title()

        sections = await _extract_sections(page)
        for s in sections:
            s["classification"] = _classify(s)
            s["status"] = "loaded" if s.get("length", 0) > 100 else "skipped"

        main_c = sum(1 for s in sections if s.get("classification") == "main_content")
        sec_c = sum(1 for s in sections if s.get("classification") == "secondary_content")
        nav_c = sum(1 for s in sections if s.get("classification") == "navigation")
        meta_c = sum(1 for s in sections if s.get("classification") == "metadata")

        return json.dumps({
            "tool": "webreader_browser_get_state",
            "status": "success",
            "session_id": session_id,
            "url": current_url,
            "title": title,
            "total_sections": len(sections),
            "main_content_count": main_c,
            "secondary_content_count": sec_c,
            "navigation_count": nav_c,
            "metadata_count": meta_c,
            "message": (
                f"Found {main_c} main, {sec_c} secondary, {nav_c} navigation, "
                f"{meta_c} metadata sections."
            ),
            "sections": sections[:20],
            "next_steps": [
                f"webreader_browser_click(session_id=\"{session_id}\", selector=\"#link\")  — Click element",
                f"webreader_browser_navigate(session_id=\"{session_id}\", url=\"...\")  — Go to new URL",
                f"webreader_browser_go_back(session_id=\"{session_id}\")  — Go back",
                f"webreader_browser_screenshot(session_id=\"{session_id}\")  — Capture screenshot",
            ],
        }, indent=2, ensure_ascii=False)

    except ConnectionError as e:
        return json.dumps({"tool": "webreader_browser_get_state", "status": "error", "message": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({
            "tool": "webreader_browser_get_state",
            "status": "error",
            "session_id": session_id,
            "message": str(e),
        }, indent=2)


@mcp.tool()
async def webreader_browser_go_back(session_id: str) -> str:
    """Go back in the session's browser history.

    Args:
        session_id: The session from webreader_browser_open()

    Returns:
        JSON with navigation result.

    Example:
        webreader_browser_go_back(session_id="abc123")
    """
    try:
        session = _validate_session(session_id)
        page = session["page"]
        try:
            await page.go_back()
        except Exception:
            pass
        await asyncio.sleep(1)

        return json.dumps({
            "tool": "webreader_browser_go_back",
            "status": "success",
            "session_id": session_id,
            "url": page.url,
            "title": await page.title(),
            "message": f"Navigated back. URL: {page.url}. Call webreader_browser_get_state() to read content.",
            "next_steps": [
                f"webreader_browser_get_state(session_id=\"{session_id}\")  — Read content",
                f"webreader_browser_go_forward(session_id=\"{session_id}\")  — Go forward",
            ],
        }, indent=2, ensure_ascii=False)

    except ConnectionError as e:
        return json.dumps({"tool": "webreader_browser_go_back", "status": "error", "message": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({
            "tool": "webreader_browser_go_back",
            "status": "error",
            "session_id": session_id,
            "message": str(e),
        }, indent=2)


@mcp.tool()
async def webreader_browser_go_forward(session_id: str) -> str:
    """Go forward in the session's browser history.

    Args:
        session_id: The session from webreader_browser_open()

    Returns:
        JSON with navigation result.

    Example:
        webreader_browser_go_forward(session_id="abc123")
    """
    try:
        session = _validate_session(session_id)
        page = session["page"]
        try:
            await page.go_forward()
        except Exception:
            pass
        await asyncio.sleep(1)

        return json.dumps({
            "tool": "webreader_browser_go_forward",
            "status": "success",
            "session_id": session_id,
            "url": page.url,
            "title": await page.title(),
            "message": f"Navigated forward. URL: {page.url}. Call webreader_browser_get_state() to read content.",
            "next_steps": [
                f"webreader_browser_get_state(session_id=\"{session_id}\")  — Read content",
                f"webreader_browser_go_back(session_id=\"{session_id}\")  — Go back",
            ],
        }, indent=2, ensure_ascii=False)

    except ConnectionError as e:
        return json.dumps({"tool": "webreader_browser_go_forward", "status": "error", "message": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({
            "tool": "webreader_browser_go_forward",
            "status": "error",
            "session_id": session_id,
            "message": str(e),
        }, indent=2)


@mcp.tool()
async def webreader_browser_screenshot(
    session_id: str,
    max_dimension: int = 768,
    colors: int = 16,
    fmt: str = "png",
    quality: int = 85,
    path: Optional[str] = None,
) -> list:#str:
    """Take a screenshot of the session's current page with optimized output.

    Resizes the browser viewport, reduces colors, and compresses the image
    to keep base64 output small for LLM context windows.

    Args:
        session_id: The session from webreader_browser_open()
        max_dimension: Max width/height in pixels (default: 768). The smaller dimension
                       is scaled proportionally. Set to 1920 for full resolution.
        colors: Color palette size 1–256 (default: 16). Uses Pillow quantization.
                Set to 256 for full color. Ignored for jpeg/webp (always 24-bit).
        fmt: Output format: "png" (default), "jpeg", or "webp".
             png: Lossless, larger file.
             jpeg: Lossy, smallest file (15–50KB typical). No transparency.
             webp: Lossy or lossless, good compression (10–30KB typical).
        quality: Compression quality for jpeg/webp (1–100, default: 85).
                 Ignored for png (png always uses maximum compression).
        path: Optional file path to save as PNG/JPEG/WEBP. If not provided,
              returns base64-encoded data.

    Returns:
        JSON with screenshot data (base64) or file path.

    Example:
        webreader_browser_screenshot(session_id="abc123")
        → Small base64 PNG, ~16 colors, max 768px

        webreader_browser_screenshot(session_id="abc123", fmt="jpeg", quality=90)
        → Small base64 JPEG, full color, ~20KB

        webreader_browser_screenshot(session_id="abc123", max_dimension=1920, colors=256, fmt="png")
        → Large base64 PNG, full color, ~500KB

        webreader_browser_screenshot(session_id="abc123", path="/tmp/screen.jpg")
        → Saved to file as JPEG
    """
    try:
        session = _validate_session(session_id)
        page = session["page"]

        # Validate parameters
        max_dimension = max(100, min(4096, int(max_dimension)))
        colors = max(1, min(256, int(colors)))
        quality = max(1, min(100, int(quality)))
        fmt = fmt.lower().strip()
        if fmt not in ("png", "jpeg", "webp"):
            fmt = "png"

        # Step 1: Set viewport to max_dimension for consistent sizing
        current_viewport = page.viewport_size or {}
        orig_width = current_viewport.get("width", 1280)
        orig_height = current_viewport.get("height", 720)
        """
        if max(orig_width, orig_height) > max_dimension:
            ratio = max_dimension / max(orig_width, orig_height)
            new_width = max(320, int(orig_width * ratio))
            new_height = max(200, int(orig_height * ratio))
            await page.set_viewport_size({"width": new_width, "height": new_height})
        else:
            # Scale up to max_dimension if page is smaller
            if orig_width < max_dimension:
                new_height = max(200, int(orig_height * (max_dimension / orig_width)))
                await page.set_viewport_size({"width": max_dimension, "height": new_height})
        """
        # Step 2: Capture screenshot from Playwright
        import base64
        screenshot_bytes = await page.screenshot(full_page=False, type="png")

        # Step 3: Process with Pillow
        img = Image.open(io.BytesIO(screenshot_bytes))

        # Composite onto white background to replace transparent pixels with white
        # This prevents alpha channels from turning black on RGB conversion
        if img.mode == "RGBA":
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.paste(img, mask=img.split()[3])  # Use alpha channel as mask
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Reduce colors if requested and format allows
        if colors < 256 and fmt == "png":
            img = img.quantize(colors=colors)

        # Step 4: Re-encode to desired format
        if fmt == "png":
            output_buf = io.BytesIO()
            img.save(output_buf, format="PNG", compress_level=9)
            screenshot_bytes = output_buf.getvalue()
        elif fmt == "jpeg":
            if img.mode != "RGB":
                img = img.convert("RGB")
            output_buf = io.BytesIO()
            img.save(output_buf, format="JPEG", quality=quality, optimize=True)
            screenshot_bytes = output_buf.getvalue()
        elif fmt == "webp":
            if img.mode != "RGB":
                img = img.convert("RGB")
            output_buf = io.BytesIO()
            img.save(output_buf, format="WEBP", quality=quality, optimize=True)
            screenshot_bytes = output_buf.getvalue()

        # Step 5: Encode to base64
        b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # Determine file extension for path saving
        ext_map = {"png": "png", "jpeg": "jpg", "webp": "webp"}
        file_ext = ext_map.get(fmt, "png")

        result = {
            "tool": "webreader_browser_screenshot",
            "status": "success",
            "session_id": session_id,
            "url": page.url,
            "title": await page.title(),
            "viewport": {"width": max_dimension, "height": "scaled proportionally"},
            "format": fmt,
            "colors": colors if fmt == "png" else 24,
            "file_size_estimate": f"{len(screenshot_bytes) / 1024:.1f} KB",
            "message": "Screenshot captured.",
            "next_steps": [
                f"webreader_browser_get_state(session_id=\"{session_id}\")  — Read page content",
                f"webreader_browser_click(session_id=\"{session_id}\", selector=\"#x\")  — Click element",
            ],
        }

        if path:
            Path(path).write_bytes(screenshot_bytes)
            return json.dumps({
                "tool": "webreader_browser_screenshot",
                "status": "success",
                "session_id": session_id,
                "url": page.url,
                "title": await page.title(),
                "saved_to": path,
                "message": f"Screenshot saved to {path}."
            }, indent=2, ensure_ascii=False)
        else:
            # Return as structured content array (image + text) for direct rendering
            mime_map = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
            mime = mime_map.get(fmt, "image/png")

            # Build metadata text
            metadata_text = (
                f"Screenshot captured.\n"
                f"URL: {page.url}\n"
                f"Title: {await page.title()}\n"
                f"Format: {fmt}, Size: {len(screenshot_bytes) / 1024:.1f} KB"
            )

            # Return as a list of content parts (MCP standard format)
            return [
                ImageContent(
                    type="image",
                    data=b64,
                    mimeType=mime
                ),
                TextContent(
                    type="text",
                    text=f"[VISUAL DATA ATTACHED: Please use your vision capabilities to analyze the layout above. If you cannot see it, notify the user that the MCP host has not projected the image into your context.]"
                )
            ]

    except ConnectionError as e:
        return [
            TextContent(type="text", text=f"Error: {str(e)}")
        ]
    except Exception as e:
        return [
            TextContent(type="text", text=f"Error: {str(e)}")
        ]


@mcp.tool()
async def webreader_browser_close(session_id: str) -> str:
    """Close a browsing session and free resources.

    Args:
        session_id: The session from webreader_browser_open()

    Returns:
        JSON confirming closure.

    Example:
        webreader_browser_close(session_id="abc123")
        → {"status": "success", "message": "Session closed."}
    """
    try:
        _validate_session(session_id)  # Check it exists
        await _destroy_session(session_id, "user_closed")
        return json.dumps({
            "tool": "webreader_browser_close",
            "status": "success",
            "session_id": session_id,
            "message": f"Session '{session_id}' closed. Call webreader_browser_open() to start a new session.",
        }, indent=2, ensure_ascii=False)
    except ConnectionError:
        return json.dumps({
            "tool": "webreader_browser_close",
            "status": "error",
            "session_id": session_id,
            "message": f"Session '{session_id}' not found or already closed.",
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "tool": "webreader_browser_close",
            "status": "error",
            "session_id": session_id,
            "message": str(e),
        }, indent=2)


# ============================================================================
# INTERNAL HELPERS
# ============================================================================

async def _extract_sections(page: Page) -> list:
    """Extract content sections from a loaded Playwright page using JavaScript."""
    sections = await page.evaluate("""() => {
        const sections = [];
        const junkTags = ['script', 'style', 'nav', 'footer', 'header', 'aside'];
        const junkClasses = ['sidebar', 'navigation', 'menu', 'ad', 'ads', 'banner',
                             'cookie', 'popup', 'modal', 'tooltip', 'dropdown'];

        function isJunkElement(el) {
            let current = el;
            while (current && current !== document.body) {
                const classes = (current.className || '') + '';
                for (const jc of junkClasses) {
                    if (classes.includes(jc)) return true;
                }
                if (junkTags.includes(current.tagName.toLowerCase())) return true;
                current = current.parentElement;
            }
            return false;
        }

        function hashCode(s) {
            let h = 0;
            for (let i = 0; i < s.length; i++) {
                h = ((h << 5) - h) + s.charCodeAt(i);
            }
            return h;
        }

        function isInteractiveElement(el) {
            const interactiveTags = ['button', 'a', 'input', 'select', 'details', 'summary'];
            if (interactiveTags.includes(el.tagName.toLowerCase())) return true;
            if (el.hasAttribute('onclick') || el.hasAttribute('data-action')) return true;
            return false;
        }

        function extractTextContent(element) {
            const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, null);
            let text = '';
            while (walker.nextNode()) {
                const t = walker.currentNode.textContent.trim();
                if (t) text += t + '\\n';
            }
            return text.trim();
        }

        const allElements = document.querySelectorAll('div, section, article, main, p, h1, h2, h3, h4, h5, h6, li, td, tr');
        for (const el of allElements) {
            if (isJunkElement(el)) continue;
            const text = extractTextContent(el);
            if (!text || text.length < 20) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue;

            sections.push({
                id: 'sec_' + Math.random().toString(36).substr(2, 9),
                type: el.tagName.toLowerCase(),
                className: el.className || '',
                textContent: text.substring(0, 500),
                length: text.length,
                isInteractive: isInteractiveElement(el),
                rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
            });
        }

        const paragraphs = document.querySelectorAll('p');
        for (const p of paragraphs) {
            if (!isJunkElement(p)) {
                const text = p.textContent.trim();
                if (text.length > 50) {
                    const pid = 'para_' + Math.abs(hashCode(text));
                    if (!sections.find(s => s.id === pid)) {
                        sections.push({
                            id: pid,
                            type: 'paragraph',
                            className: p.className || '',
                            textContent: text.substring(0, 500),
                            length: text.length,
                            isInteractive: false,
                            rect: { x: 0, y: 0, width: 0, height: 0 }
                        });
                    }
                }
            }
        }
        return sections;
    }""")

    # Deduplicate by text preview
    seen = set()
    unique = []
    for s in sorted(sections, key=lambda x: x.get("length", 0), reverse=True):
        preview = s["textContent"][:100]
        if preview not in seen:
            seen.add(preview)
            unique.append(s)
    return unique


def _classify(section: dict) -> str:
    """Classify a section by its type and content."""
    stype = section.get("type", "")
    cls = section.get("className", "").lower()

    if any(x in stype for x in ["nav", "menu"]) or any(x in cls for x in ["nav", "menu", "sidebar", "toc"]):
        return "navigation"
    if section.get("isInteractive"):
        return "interactive"
    if section.get("length", 0) > 500:
        return "main_content"
    if section.get("length", 0) > 100:
        return "secondary_content"
    return "metadata"


if __name__ == "__main__":
    mcp.run()