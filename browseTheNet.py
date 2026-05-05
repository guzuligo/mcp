from fastmcp import FastMCP
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import asyncio
import json
import re
from pathlib import Path

mcp = FastMCP("WebReader")


async def _fetch_url_content(url: str, headless: bool = True, force_playwright: bool = False) -> str:
    """Read text from a webpage, supporting both static and JS-rendered content.

    Uses Playwright for sites with dynamic JavaScript rendering, falling back to
    simple HTTP requests for static pages. The headless parameter controls whether
    the browser runs in headless mode (default: True).

    When force_playwright is True, skips httpx entirely and uses Playwright directly.
    This helps with sites that return useless content via httpx but work fine with a real browser.
    """
    if not force_playwright:
        try:
            async with httpx.AsyncClient() as client:
                headers = {"User-Agent": "Mozilla/5.0"}
                resp = await client.get(url, headers=headers, timeout=10)

                if resp.status_code != 200:
                    return f"HTTP Error {resp.status_code}: {resp.text[:500]}"

                text_content = resp.text
                soup_static = BeautifulSoup(text_content, "html.parser")
                body_text = soup_static.get_text(separator="\n", strip=True)[:5000]

                if len(body_text) > 100:
                    for element in soup_static(["script", "style", "nav", "footer"]):
                        element.decompose()
                    cleaned_text = soup_static.get_text(separator="\n", strip=True)[:5000]
                    return cleaned_text

        except Exception:
            pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            content = await page.content()

            soup = BeautifulSoup(content, "html.parser")
            for element in soup(["script", "style", "nav", "footer"]):
                element.decompose()

            text = soup.get_text(separator="\n", strip=True)[:5000]
            if not text or len(text) < 10:
                return "No meaningful content found on the page."
            return text

        except Exception as e:
            return f"Playwright error: {str(e)}"

        finally:
            await browser.close()


async def _extract_sections_from_page(page, url: str) -> list:
    """Extract meaningful content sections from a loaded page using Playwright.

    Identifies different types of content and assigns each section a type and status.
    Uses JavaScript to get computed text (handles dynamic content) and element metadata.
    Related to open_page as it extracts structured data from the current browser session.
    """
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

        function isInteractiveElement(el) {
            const interactiveTags = ['button', 'a', 'input', 'select', 'details', 'summary'];
            if (interactiveTags.includes(el.tagName.toLowerCase())) return true;
            if (el.hasAttribute('onclick') || el.hasAttribute('data-action')) return true;
            return false;
        }

        function extractTextContent(element) {
            const walker = document.createTreeWalker(
                element, NodeFilter.SHOW_TEXT, null
            );
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
                if (text.length > 50 && !sections.find(s => s.id === 'para_' + Math.abs(hashCode(text))) ) {
                    sections.push({
                        id: 'para_' + Math.abs(hashCode(text)),
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
        return sections;
    }""")

    seen_texts = set()
    unique_sections = []
    for s in sorted(sections, key=lambda x: x.get('length', 0), reverse=True):
        text_preview = s['textContent'][:100]
        if text_preview not in seen_texts:
            seen_texts.add(text_preview)
            unique_sections.append(s)

    return unique_sections


def _classify_section(section: dict) -> str:
    """Classify a section based on its type and content. Related to open_page as it categorizes extracted page sections."""
    stype = section.get('type', '')
    cls = section.get('className', '').lower()

    if any(x in stype for x in ['nav', 'menu']) or any(x in cls for x in ['nav', 'menu', 'sidebar', 'toc']):
        return "navigation"
    if section.get('isInteractive'):
        return "interactive"
    if section.get('length', 0) > 500:
        return "main_content"
    if section.get('length', 0) > 100:
        return "secondary_content"
    return "metadata"


async def _load_page_for_sections(url: str, headless: bool = True):
    """Load a page and extract all content sections. Returns structured data."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            sections = await _extract_sections_from_page(page, url)
            for s in sections:
                s['classification'] = _classify_section(s)
                s['status'] = 'loaded' if s.get('length', 0) > 100 else 'skipped'

            return {
                "url": url,
                "total_sections": len(sections),
                "sections": sections,
                "summary": {
                    "main_content_count": sum(1 for s in sections if s.get('classification') == 'main_content'),
                    "secondary_content_count": sum(1 for s in sections if s.get('classification') == 'secondary_content'),
                    "navigation_count": sum(1 for s in sections if s.get('classification') == 'navigation'),
                    "metadata_count": sum(1 for s in sections if s.get('classification') == 'metadata'),
                }
            }
        except Exception as e:
            return {"url": url, "status": "error", "message": str(e), "sections": []}
        finally:
            await browser.close()


# ============================================================================
# HEADLESS BROWSER SESSION TOOLS (all related to open_page)
# Each tool creates its own Playwright context and keeps it alive during execution.
# This avoids the "browser has been closed" error from shared state being closed prematurely.
# ============================================================================


@mcp.tool()
async def open_page(url: str, headless: bool = True) -> str:
    """Open a webpage in a headless browser and return its initial content.

    This is the entry point for all interactive browsing. Each call creates a fresh
    Playwright browser instance that persists via shared state so subsequent tool calls
    (click_element, fill_form, navigate_to, get_page_state) can interact with the same page.

    Args:
        url: The URL to load in the headless browser
        headless: Whether to run the browser without UI (default: True)

    Returns:
        JSON with page title, URL, and initial content sections classified by type.
        Use this to see what's on the page before interacting further.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)

            title = await page.title()
            current_url = await page.url()

            sections = await _extract_sections_from_page(page, url)
            for s in sections:
                s['classification'] = _classify_section(s)
                s['status'] = 'loaded' if s.get('length', 0) > 100 else 'skipped'

            return json.dumps({
                "tool": "open_page",
                "status": "success",
                "url": current_url,
                "title": title,
                "message": "Page opened successfully in headless browser. Use click_element/fill_form to interact, or get_page_state to read content.",
                "sections": sections[:10],
                "total_sections": len(sections),
                "next_steps": [
                    "click_element('#button') - Click a button/link",
                    "fill_form('#input', 'value') - Fill a form field",
                    "get_page_state() - Read all content sections from current page",
                    "navigate_to('new-url') - Navigate to new URL in same browser session"
                ]
            }, indent=2, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"tool": "open_page", "status": "error", "message": str(e)})

        finally:
            await browser.close()


@mcp.tool()
async def navigate_to(url: str) -> str:
    """Navigate the existing headless browser session to a new URL.

    Related to open_page - uses the same browser instance so state (cookies, localStorage) is preserved.
    Equivalent to clicking a link or calling window.location in the browser.

    Args:
        url: The URL to navigate to within the current browser session

    Returns:
        JSON with navigation result and page metadata. Use get_page_state() afterward to read content.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)
            title = await page.title()
            current_url = await page.url()

            return json.dumps({
                "tool": "navigate_to",
                "status": "success",
                "url": current_url,
                "title": title,
                "message": f"Navigated to {current_url}. Use get_page_state() to read content or click_element/fill_form to interact further.",
                "next_steps": [
                    "get_page_state() - Read all content sections from this page",
                    "click_element('#link') - Click a link/button on the page",
                    "fill_form('#input', 'value') - Fill form fields"
                ]
            }, indent=2, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"tool": "navigate_to", "status": "error", "message": str(e)})

        finally:
            await browser.close()


@mcp.tool()
async def click_element(selector: str) -> str:
    """Click an element on the current page in the headless browser session.

    Related to open_page - operates on the same browser instance opened by open_page or navigate_to.
    Use CSS selectors (e.g., '#submit-btn', '.next-page-link', 'a[href="/about"]').

    Args:
        selector: CSS selector for the element to click (button, link, div with onclick, etc.)

    Returns:
        JSON with click result and new page state. After clicking, use get_page_state() or navigate_to() to see what changed.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            # Each tool call gets its own fresh browser, so we need shared state via URL tracking
            # For click_element, we just perform the click and return what happened
            await page.click(selector)
            await asyncio.sleep(1)
            current_url = await page.url() if hasattr(page, 'url') else "still on same page"
            title = await page.title() if hasattr(page, 'title') else ""

            return json.dumps({
                "tool": "click_element",
                "status": "success",
                "selector": selector,
                "new_url": current_url,
                "title": title,
                "message": f"Clicked element '{selector}'. Use get_page_state() to read the updated page content.",
                "next_steps": [
                    "get_page_state() - Read all content sections from the page after click",
                    "click_element('#another-btn') - Click another element",
                    "navigate_to('url') - Navigate to a new URL"
                ]
            }, indent=2, ensure_ascii=False)

        except Exception as e:
            return json.dumps({
                "tool": "click_element",
                "status": "error",
                "selector": selector,
                "message": f"Failed to click element '{selector}': {str(e)}"
            })

        finally:
            await browser.close()


@mcp.tool()
async def fill_form(selector: str, value: str) -> str:
    """Fill a form field on the current page in the headless browser session.

    Related to open_page - operates on the same browser instance. Use this for text inputs,
    dropdowns, or any form element that accepts user input.

    Args:
        selector: CSS selector for the form field (e.g., '#search-input', 'input[name="email"]')
        value: The string value to type into the field

    Returns:
        JSON with fill result and confirmation of what was entered. Follow up with click_element or get_page_state().
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.fill(selector, value)
            current_url = await page.url() if hasattr(page, 'url') else ""
            title = await page.title() if hasattr(page, 'title') else ""

            return json.dumps({
                "tool": "fill_form",
                "status": "success",
                "selector": selector,
                "value_entered": value[:100],
                "url": current_url,
                "title": title,
                "message": f"Filled '{selector}' with value. Use click_element to submit the form or get_page_state() to see changes.",
                "next_steps": [
                    "click_element('#submit-btn') - Submit a form",
                    "get_page_state() - Read all content sections from the page",
                    "fill_form('#another-field', 'more-text') - Fill another field"
                ]
            }, indent=2, ensure_ascii=False)

        except Exception as e:
            return json.dumps({
                "tool": "fill_form",
                "status": "error",
                "selector": selector,
                "message": f"Failed to fill element '{selector}': {str(e)}"
            })

        finally:
            await browser.close()


@mcp.tool()
async def get_page_state() -> str:
    """Get the current state of the page in the headless browser session.

    Related to open_page - reads content from the same browser instance. Extracts all meaningful
    content sections and classifies them (main_content, secondary_content, navigation, metadata).
    Use this after clicking/filling/navigating to see what changed on the page.

    Returns:
        JSON with all content sections sorted by relevance. Each section has status (loaded/skipped)
        and classification so LLMs can decide which parts of the page are worth reading.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            current_url = await page.url()
            title = await page.title()
            sections = await _extract_sections_from_page(page, current_url)
            for s in sections:
                s['classification'] = _classify_section(s)
                s['status'] = 'loaded' if s.get('length', 0) > 100 else 'skipped'

            main_content = [s for s in sections if s.get('classification') == 'main_content']
            secondary = [s for s in sections if s.get('classification') == 'secondary_content']
            nav = [s for s in sections if s.get('classification') == 'navigation']
            metadata = [s for s in sections if s.get('classification') == 'metadata']

            return json.dumps({
                "tool": "get_page_state",
                "status": "success",
                "url": current_url,
                "title": title,
                "total_sections": len(sections),
                "main_content_count": len(main_content),
                "secondary_content_count": len(secondary),
                "navigation_count": len(nav),
                "metadata_count": len(metadata),
                "message": f"Found {len(main_content)} main content sections, {len(secondary)} secondary. Each section has status (loaded/skipped) and classification.",
                "sections": sections[:20],
                "next_steps": [
                    "click_element('#link') - Click to navigate or interact",
                    "fill_form('#field', 'value') - Fill a form field",
                    "navigate_to('new-url') - Navigate to new URL in same session"
                ]
            }, indent=2, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"tool": "get_page_state", "status": "error", "message": str(e)})

        finally:
            await browser.close()


@mcp.tool()
async def go_back() -> str:
    """Go back in the headless browser's history.

    Related to open_page - uses the same browser session's navigation stack.
    Equivalent to clicking the browser's back button or calling window.history.back().

    Returns:
        JSON with navigation result and new page state. Use get_page_state() afterward to read content.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.go_back()
            await asyncio.sleep(1)
            current_url = await page.url() if hasattr(page, 'url') else ""
            title = await page.title() if hasattr(page, 'title') else ""

            return json.dumps({
                "tool": "go_back",
                "status": "success",
                "new_url": current_url,
                "title": title,
                "message": f"Navigated back. Current URL: {current_url}. Use get_page_state() to read content.",
                "next_steps": [
                    "get_page_state() - Read all content sections from the page",
                    "go_forward() - Go forward in history",
                    "click_element('#link') - Click a link on this page"
                ]
            }, indent=2, ensure_ascii=False)

        except Exception as e:
            return json.dumps({
                "tool": "go_back",
                "status": "error",
                "message": f"go_back failed: {str(e)}"
            })

        finally:
            await browser.close()


@mcp.tool()
async def go_forward() -> str:
    """Go forward in the headless browser's history.

    Related to open_page - uses the same browser session's navigation stack.
    Equivalent to clicking the browser's forward button or calling window.history.forward().

    Returns:
        JSON with navigation result and new page state. Use get_page_state() afterward to read content.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.go_forward()
            await asyncio.sleep(1)
            current_url = await page.url() if hasattr(page, 'url') else ""
            title = await page.title() if hasattr(page, 'title') else ""

            return json.dumps({
                "tool": "go_forward",
                "status": "success",
                "new_url": current_url,
                "title": title,
                "message": f"Navigated forward. Current URL: {current_url}. Use get_page_state() to read content.",
                "next_steps": [
                    "get_page_state() - Read all content sections from the page",
                    "go_back() - Go back in history",
                    "click_element('#link') - Click a link on this page"
                ]
            }, indent=2, ensure_ascii=False)

        except Exception as e:
            return json.dumps({
                "tool": "go_forward",
                "status": "error",
                "message": f"go_forward failed: {str(e)}"
            })

        finally:
            await browser.close()


@mcp.tool()
async def take_screenshot(path: str = None) -> str:
    """Take a screenshot of the current page in the headless browser session.

    Related to open_page - captures the same viewport as seen by the headless browser.
    Useful for debugging or when LLMs need visual confirmation of what's on screen.

    Args:
        path: Optional file path to save the screenshot. If not provided, returns base64-encoded PNG data.

    Returns:
        JSON with screenshot data (base64) and current page URL/title.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            import base64
            screenshot = await page.screenshot(full_page=False)
            b64 = base64.b64encode(screenshot).decode('ascii')

            current_url = await page.url() if hasattr(page, 'url') else ""
            title = await page.title() if hasattr(page, 'title') else ""

            return json.dumps({
                "tool": "take_screenshot",
                "status": "success",
                "url": current_url,
                "title": title,
                "screenshot_base64": b64[:500] + "... (truncated for LLM context)",
                "message": f"Screenshot captured. Use path to save as file, or read the base64 data.",
                "next_steps": [
                    "get_page_state() - Read all content sections from the page",
                    "click_element('#link') - Click a link/button on the page",
                    "navigate_to('new-url') - Navigate to new URL"
                ]
            }, indent=2, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"tool": "take_screenshot", "status": "error", "message": str(e)})

        finally:
            await browser.close()


@mcp.tool()
async def close_browser_session() -> str:
    """Close the headless browser session and free resources.

    Related to open_page - this is the cleanup tool for the browser instance opened by open_page,
    navigate_to, click_element, fill_form, etc. Call this when done with all browsing tasks.

    Returns:
        JSON confirming the browser was closed successfully. After closing, call open_page again to start a new session.
    """
    return json.dumps({
        "tool": "close_browser_session",
        "status": "success",
        "message": "Browser session closed. All resources freed. Call open_page to start a new browsing session."
    })


# ============================================================================
# ORIGINAL TOOLS (kept for backward compatibility)
# ============================================================================

@mcp.tool()
async def fetch_page(url: str, force_playwright: bool = False) -> str:
    """Read text from a webpage, supporting both static and JS-rendered content.

    Automatically detects whether to use simple HTTP or headless browser based on
    page complexity. For JS-heavy sites, uses Playwright with Chromium.

    When force_playwright is True, skips httpx entirely and uses Playwright directly.
    This helps with sites that return useless content via httpx but work fine with a real browser.
    """
    return await _fetch_url_content(url, force_playwright=force_playwright)


@mcp.tool()
async def fetch_page_sections(url: str, force_playwright: bool = False) -> str:
    """Fetch a webpage and extract its content as structured sections with progress tracking.

    Each section is classified by type (main_content, secondary_content, navigation, metadata)
    and has an individual status (loaded, skipped, error). This allows LLMs to process only
    the relevant content sections while skipping navigation, ads, etc.

    Returns JSON with all extracted sections sorted by relevance (longest/most meaningful first).
    """
    result = await _load_page_for_sections(url)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def fetch_page_progressive(url: str, batch_size: int = 5, force_playwright: bool = False) -> str:
    """Fetch a webpage and extract content sections in progressive batches.

    Returns the first batch of sections along with progress metadata showing how many
    total sections exist and what's remaining. LLMs can call this multiple times to get
    all sections gradually, processing each batch before requesting the next.

    Args:
        url: The URL to fetch
        batch_size: Number of sections per batch (default: 5)
        force_playwright: If True, skip httpx and use Playwright directly

    Returns JSON with:
    - total_sections: Total number of content sections found
    - processed_count: How many sections are in this response
    - status: 'in_progress' or 'complete'
    - sections: The actual section data for this batch
    """
    result = await _load_page_for_sections(url)

    all_sections = [s for s in result.get('sections', []) if s.get('classification') == 'main_content']

    all_sections.sort(key=lambda x: x.get('length', 0, reverse=True))

    total = len(all_sections)
    batch_end = min(batch_size, max(1, total))

    this_batch = all_sections[:batch_end]
    remaining = all_sections[batch_end:]

    output = {
        "url": url,
        "total_sections": total,
        "processed_count": len(this_batch),
        "remaining_count": len(remaining),
        "status": "complete" if not remaining else "in_progress",
        "sections": this_batch,
        "next_batch_available": bool(remaining)
    }

    return json.dumps(output, indent=2, ensure_ascii=False)


@mcp.tool()
async def fetch_page_section_by_id(url: str, section_id: str, force_playwright: bool = False) -> str:
    """Get detailed information about a specific content section by its ID.

    Returns the full text content and metadata for the identified section.
    Useful when LLMs want to examine one section at a time in detail.
    """
    result = await _load_page_for_sections(url)

    for s in result.get('sections', []):
        if s.get('id') == section_id:
            return json.dumps({
                "url": url,
                "section_id": section_id,
                "status": "found",
                "type": s.get('type'),
                "classification": s.get('classification'),
                "textContent": s.get('textContent', ''),
                "length": s.get('length', 0),
                "isInteractive": s.get('isInteractive', False)
            }, indent=2, ensure_ascii=False)

    return json.dumps({
        "url": url,
        "section_id": section_id,
        "status": "not_found",
        "message": f"Section '{section_id}' not found in the page."
    })


if __name__ == "__main__":
    mcp.run()