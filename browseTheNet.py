from fastmcp import FastMCP
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import asyncio

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


@mcp.tool()
async def fetch_page(url: str) -> str:
    """Read text from a webpage, supporting both static and JS-rendered content.
    
    Automatically detects whether to use simple HTTP or headless browser based on 
    page complexity. For JS-heavy sites, uses Playwright with Chromium.
    """
    return await _fetch_url_content(url)


if __name__ == "__main__":
    mcp.run()