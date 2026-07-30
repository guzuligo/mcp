"""
utils.py — Pure utility functions for lmnotes.

These functions have no dependencies on the Notebook class,
making them safe to import without circular import issues.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from lmnotes.notebook import Notebook


# Valid folder categories
VALID_FOLDERS = ["procedures", "reports", "individuals", "conversations", "knowledge", "system", "references"]


def generate_id(timestamp: datetime = None) -> str:
    """Generate a timestamp-based unique ID."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    return timestamp.strftime("%y%m%d%H%M%S")


def make_slug(title: str, note_id: str = "") -> str:
    """Create a human-readable slug from a title."""
    slug = re.sub(r'[^\w\s-]', '', title.lower())
    slug = re.sub(r'\s+', '_', slug.strip())
    if not slug:
        return "untitled"
    return slug


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML-like front-matter from markdown content."""
    if not content or not content.startswith("---"):
        return {}, content
    
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return {}, content
    
    fm_text = content[4:end_idx].strip()
    body = content[end_idx + 4:].strip()
    
    frontmatter: Dict[str, Any] = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip()
        value = value.strip()
        
        if value.startswith('[') and value.endswith(']'):
            try:
                import json
                frontmatter[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        
        frontmatter[key] = value
    
    return frontmatter, body


def build_frontmatter(data: Dict[str, Any]) -> str:
    """Build YAML-like front-matter from a dict."""
    lines = ["---"]
    for key in ["id", "title", "folder", "parent_id"]:
        if key in data and data[key]:
            lines.append(f"{key}: {data[key]}")
    
    if "tags" in data:
        tags = data["tags"]
        if isinstance(tags, list):
            lines.append(f"tags: [{', '.join(str(t) for t in tags)}]")
        elif isinstance(tags, str):
            lines.append(f"tags: {tags}")
    
    for key in ["created", "updated"]:
        if key in data and data[key]:
            lines.append(f"{key}: {data[key]}")
    
    lines.append("---")
    return "\n".join(lines)


def resolve_folder(folder: str = None) -> Path:
    """Resolve the notebook folder path from argument or default."""
    if folder is not None:
        p = Path(folder).expanduser()
        if str(folder).rstrip().endswith('/') or str(folder).rstrip().endswith('\\'):
            return p.parent / (p.name + "/.lmnotes") if p.name else p
        return p
    return Path.home() / ".lmnotes"


def ensure_ready(folder: Path, subfolder: str = None) -> None:
    """Ensure the notebook root and optionally a subfolder exist."""
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
    if subfolder:
        (folder / subfolder).mkdir(parents=True, exist_ok=True)


def find_note_file(notebook: "Notebook", note_id: str, folder: str) -> Optional[Path]:
    """Find a note file by its ID across all folders or within a specific folder."""
    root = Path(notebook.folder)
    
    # If folder is specified, search only there
    if folder:
        search_path = root / folder
        if not search_path.exists():
            return None
        matches = list(search_path.glob(f"{note_id}_*.md"))
        valid_matches = [f for f in matches if "_" in f.name and f.name != "index.md"]
        if len(valid_matches) == 1:
            return valid_matches[0]
        return valid_matches[0] if valid_matches else None
    
    # Search all folders
    for folder_name in VALID_FOLDERS + ["", "."]:
        search_path = root / folder_name if folder_name else root
        if not search_path.exists():
            continue
        matches = list(search_path.glob(f"{note_id}_*.md"))
        valid_matches = [f for f in matches if "_" in f.name and f.name != "index.md"]
        if len(valid_matches) == 1:
            return valid_matches[0]
    return None


def read_note_file(filepath: Path) -> Optional[Dict[str, Any]]:
    """Read a note file and parse its front-matter."""
    if not filepath.exists():
        return None
    content = filepath.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(content)
    fm["filepath"] = str(filepath)
    fm["raw_content"] = content
    fm["body"] = body
    return fm