from fastmcp import FastMCP
import httpx
from bs4 import BeautifulSoup

mcp = FastMCP("WebReader")

@mcp.tool()
async def fetch_page(url: str) -> str:
    """Read text from a standard webpage."""
    async with httpx.AsyncClient() as client:
        # We use a User-Agent to avoid being blocked by simple bots
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = await client.get(url, headers=headers)
        
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove junk
        for element in soup(["script", "style", "nav", "footer"]):
            element.decompose()
            
        return soup.get_text(separator="\n", strip=True)[:5000]

if __name__ == "__main__":
    mcp.run()