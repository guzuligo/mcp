from fastmcp import FastMCP
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import asyncio
import json
import re

mcp = FastMCP("WebReader")


async def _fetch_url_content(url: str, headless: bool = True) -> str:
    """Read text from a webpage, supporting both static and JS-rendered content.
    
    Uses Playwright for sites with dynamic JavaScript rendering, falling back to 
    simple HTTP requests for static pages. The headless parameter controls whether
    the browser runs in headless mode (default: True).
    """
    # First try the fast httpx approach for static pages
    try:
        async with httpx.AsyncClient() as client:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = await client.get(url, headers=headers, timeout=10)
            
            if resp.status_code != 200:
                return f"HTTP Error {resp.status_code}: {resp.text[:500]}"
            
            # Check if the page has minimal JS content (quick heuristic)
            text_content = resp.text
            
            # If the page is very short or clearly static, just use it directly
            soup_static = BeautifulSoup(text_content, "html.parser")
            body_text = soup_static.get_text(separator="\n", strip=True)[:5000]
            
            # If we got reasonable content without needing JS, return it
            if len(body_text) > 100:
                # Clean up junk from static page too
                for element in soup_static(["script", "style", "nav", "footer"]):
                    element.decompose()
                cleaned_text = soup_static.get_text(separator="\n", strip=True)[:5000]
                return cleaned_text
            
    except Exception:
        # If httpx fails, fall through to playwright
        pass
    
    # Use Playwright for JS-rendered content or if httpx failed
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            
            # Wait for any dynamic content to load
            await asyncio.sleep(2)
            
            # Get the fully rendered content
            content = await page.content()
            
            soup = BeautifulSoup(content, "html.parser")
            
            # Remove junk
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
    """
    # Use JS to extract structured data about all meaningful elements
    sections = await page.evaluate("""() => {
        const sections = [];
        
        // Remove script/style/footer/nav/header from consideration for main content
        const junkTags = ['script', 'style', 'nav', 'footer', 'header', 'aside'];
        const junkClasses = ['sidebar', 'navigation', 'menu', 'ad', 'ads', 'banner', 
                             'cookie', 'popup', 'modal', 'tooltip', 'dropdown'];
        
        function isJunkElement(el) {
            // Check if element or any ancestor has a junk class
            let current = el;
            while (current && current !== document.body) {
                const classes = (current.className || '') + '';
                for (const jc of junkClasses) {
                    if (classes.includes(jc)) return true;
                }
                // Check tag name
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
            // Get all text nodes, trimmed and joined
            const walker = document.createTreeWalker(
                element,
                NodeFilter.SHOW_TEXT,
                null
            );
            let text = '';
            while (walker.nextNode()) {
                const t = walker.currentNode.textContent.trim();
                if (t) text += t + '\\n';
            }
            return text.trim();
        }
        
        // Find all block-level elements that might contain content
        const allElements = document.querySelectorAll('div, section, article, main, p, h1, h2, h3, h4, h5, h6, li, td, tr');
        
        for (const el of allElements) {
            if (isJunkElement(el)) continue;
            
            const text = extractTextContent(el);
            if (!text || text.length < 20) continue; // Skip very short content
            
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue; // Hidden elements
            
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
        
        // Also check for specific content patterns
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
    
    # Sort by length (longer = more likely to be meaningful content) and deduplicate
    seen_texts = set()
    unique_sections = []
    for s in sorted(sections, key=lambda x: x.get('length', 0), reverse=True):
        text_preview = s['textContent'][:100]
        if text_preview not in seen_texts:
            seen_texts.add(text_preview)
            unique_sections.append(s)
    
    return unique_sections


def _classify_section(section: dict) -> str:
    """Classify a section based on its type and content."""
    stype = section.get('type', '')
    cls = section.get('className', '').lower()
    text = section.get('textContent', '').lower()
    
    # Navigation elements
    if any(x in stype for x in ['nav', 'menu']) or any(x in cls for x in ['nav', 'menu', 'sidebar', 'toc']):
        return "navigation"
    
    # Interactive elements (buttons, links) - skip for content extraction
    if section.get('isInteractive'):
        return "interactive"
    
    # Large text blocks are likely main content
    if section.get('length', 0) > 500:
        return "main_content"
    
    # Medium blocks could be secondary content
    if section.get('length', 0) > 100:
        return "secondary_content"
    
    # Small blocks are likely decorative or metadata
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
            
            # Classify each section
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
            return {
                "url": url,
                "status": "error",
                "message": str(e),
                "sections": []
            }
        
        finally:
            await browser.close()


@mcp.tool()
async def fetch_page(url: str) -> str:
    """Read text from a webpage, supporting both static and JS-rendered content.
    
    Automatically detects whether to use simple HTTP or headless browser based on 
    page complexity. For JS-heavy sites, uses Playwright with Chromium.
    """
    return await _fetch_url_content(url)


@mcp.tool()
async def fetch_page_sections(url: str) -> str:
    """Fetch a webpage and extract its content as structured sections with progress tracking.
    
    Each section is classified by type (main_content, secondary_content, navigation, metadata)
    and has an individual status (loaded, skipped, error). This allows LLMs to process only
    the relevant content sections while skipping navigation, ads, etc.
    
    Returns JSON with all extracted sections sorted by relevance (longest/most meaningful first).
    """
    result = await _load_page_for_sections(url)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def fetch_page_progressive(url: str, batch_size: int = 5) -> str:
    """Fetch a webpage and extract content sections in progressive batches.
    
    Returns the first batch of sections along with progress metadata showing how many
    total sections exist and what's remaining. LLMs can call this multiple times to get
    all sections gradually, processing each batch before requesting the next.
    
    Args:
        url: The URL to fetch
        batch_size: Number of sections per batch (default: 5)
    
    Returns JSON with:
    - total_sections: Total number of content sections found
    - processed_count: How many sections are in this response
    - status: 'in_progress' or 'complete'
    - sections: The actual section data for this batch
    """
    result = await _load_page_for_sections(url)
    
    all_sections = [s for s in result.get('sections', []) if s.get('classification') == 'main_content']
    
    # Sort by length (most content first)
    all_sections.sort(key=lambda x: x.get('length', 0), reverse=True)
    
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
async def fetch_page_section_by_id(url: str, section_id: str) -> str:
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