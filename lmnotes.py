"""
lmnotes - File-Based LLM Notebook System (FastMCP)

A pure file-based notebook management system using hierarchical index.md files
for navigation and search. All notes are stored as human-readable markdown files.

Folder Structure:
    ~/.lmnotes/
    ├── index.md                     # Root catalog
    ├── procedures/                  # Skill memory & learned procedures
    │   ├── index.md
    │   └── 260729165500_git_rebase_fix.md
    ├── reports/                     # Completed action reports
    ├── individuals/                 # People & intelligent beings info
    ├── conversations/               # Conversation summaries
    ├── knowledge/                   # Facts & learned information
    ├── system/                      # System prompts & behavioral rules
    └── references/                  # User-shared files with descriptions

File Naming: {YYYYMMDDHHmmss}_{slug}.md
- ID = timestamp portion (used by all tools)
- Slug = human-readable descriptor
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

try:
    from fastmcp import FastMCP
except ImportError:
    FastMCP = None

# Debug mode: when True, exposes internal paths for development
DEBUG = True

# Initialization flag - set to True only after init_notebook is called
_initialized = False

# Global MCP instance (created lazily)
_mcp_instance = None

# Valid folder categories
VALID_FOLDERS = ["procedures", "reports", "individuals", "conversations", "knowledge", "system", "references"]

# Selection store: selection_id -> selection data
_selection_store: Dict[str, dict] = {}
_selection_counter = 0

# Global notebook folder - set by init_notebook, used by all tools
_notebook_folder: Optional[str] = None


def _get_mcp() -> FastMCP:
    """Get or create the MCP instance."""
    global _mcp_instance
    if _mcp_instance is None:
        _mcp_instance = FastMCP("lmnotes")
    return _mcp_instance


def _require_initialized() -> Optional[dict]:
    """Return an error dict if the notebook has not been initialized, else None."""
    global _initialized
    if not _initialized:
        return {"status": "error", "message": "Notebook not initialized. Call lmnotes_init_notebook first."}
    return None


def _tool_run(func, *args, **kwargs) -> str:
    """Run a Notebook method and return JSON. Handles None (not initialized)."""
    result = func(*args, **kwargs)
    if result is None:
        err = _require_initialized()
        return json.dumps(err if err else {"status": "error", "message": "Not initialized"}, indent=2)
    return json.dumps(result, indent=2)


# ============================================================================
# Notebook Class - Core Note Management Logic
# ============================================================================

class Notebook:
    """File-based LLM notebook management system."""

    def __init__(self, folder: str = None):
        self.folder = self._resolve_folder(folder)

    @staticmethod
    def _resolve_folder(folder: str = None) -> Path:
        """Resolve the notebook folder path from argument or default."""
        if folder is not None:
            p = Path(folder).expanduser()
            # If ends with /, append .lmnotes
            if str(folder).rstrip().endswith('/') or str(folder).rstrip().endswith('\\'):
                return p.parent / (p.name + "/.lmnotes") if p.name else p
            return p
        # Default: ~/.lmnotes/
        home = Path.home()
        return home / ".lmnotes"

    def _ensure_ready(self, subfolder: str = None) -> None:
        """Ensure the notebook root and optionally a subfolder exist.
        
        Creates only what's needed on demand (lazy creation).
        Root folder is created if missing; subfolder only when writing to it.
        """
        root = Path(self.folder)
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)
        if subfolder:
            (root / subfolder).mkdir(parents=True, exist_ok=True)

    def _ensure_initialized(self) -> None:
        """Ensure only the notebook root exists (lazy subfolder creation)."""
        self._ensure_ready()

    def _write_root_index(self, root: Path) -> None:
        """Write or update the root index.md."""
        index_path = root / "index.md"
        content = "# LMNotes - Root Index\n\n## Categories\n\n"
        for folder in VALID_FOLDERS:
            folder_path = root / folder
            if folder_path.exists():
                count = len(list(folder_path.glob("*.md"))) - 1  # Exclude index.md
                content += f"- **{folder}** ({count} notes)\n"
        content += "\n---\n\n*Use `search_notes` to find specific content.*\n"
        index_path.write_text(content, encoding="utf-8")

    @staticmethod
    def _generate_id(timestamp: datetime = None) -> str:
        """Generate a timestamp-based unique ID."""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        return timestamp.strftime("%y%m%d%H%M%S")

    @staticmethod
    def _make_slug(title: str, note_id: str = "") -> str:
        """Create a human-readable slug from a title."""
        # Remove special characters and convert to lowercase
        slug = re.sub(r'[^\w\s-]', '', title.lower())
        slug = re.sub(r'\s+', '_', slug.strip())
        if not slug:
            return "untitled"
        return slug

    @staticmethod
    def _parse_frontmatter(content: str) -> Tuple[Dict, str]:
        """Parse YAML-like front-matter from markdown content.
        
        Returns: (frontmatter_dict, body_content)
        """
        if not content or not content.startswith("---"):
            return {}, content
        
        # Find closing ---
        end_idx = content.find("\n---", 3)
        if end_idx == -1:
            return {}, content
        
        fm_text = content[4:end_idx].strip()
        body = content[end_idx + 4:].strip()
        
        frontmatter = {}
        for line in fm_text.split("\n"):
            line = line.strip()
            if not line or ':' not in line:
                continue
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip()
            
            # Handle list values like [tag1, tag2]
            if value.startswith('[') and value.endswith(']'):
                try:
                    frontmatter[key] = json.loads(value)
                    continue
                except json.JSONDecodeError:
                    pass
            
            frontmatter[key] = value
        
        return frontmatter, body

    @staticmethod
    def _build_frontmatter(data: Dict[str, Any]) -> str:
        """Build YAML-like front-matter from a dict."""
        lines = ["---"]
        for key in ["id", "title", "folder", "parent_id"]:
            if key in data and data[key]:
                lines.append(f"{key}: {data[key]}")
        
        # Handle tags specially
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

    def _find_note_file(self, note_id: str) -> Optional[Path]:
        """Find a note file by its ID across all folders.
        
        Uses exact filename matching to avoid collisions when two notes share
        the same timestamp prefix (e.g., created within the same second).
        Returns None if multiple files match or none found.
        """
        root = Path(self.folder)
        for folder_name in VALID_FOLDERS + ["", "."]:
            search_path = root / folder_name if folder_name else root
            if not search_path.exists():
                continue
            # Use exact filename pattern: {note_id}_{slug}.md
            matches = list(search_path.glob(f"{note_id}_*.md"))
            # Filter out index.md and non-note files
            valid_matches = [f for f in matches if "_" in f.name and f.name != "index.md"]
            if len(valid_matches) == 1:
                return valid_matches[0]
        return None

    def _read_note_file(self, filepath: Path) -> Optional[Dict]:
        """Read a note file and parse its front-matter."""
        if not filepath.exists():
            return None
        content = filepath.read_text(encoding="utf-8")
        fm, body = self._parse_frontmatter(content)
        fm["filepath"] = str(filepath)
        fm["raw_content"] = content
        fm["body"] = body
        return fm

    def _update_index(self, folder: str) -> None:
        """Update the index.md for a given folder."""
        root = Path(self.folder)
        folder_path = root / folder
        if not folder_path.exists():
            return
        
        # Collect all note files (excluding index.md)
        notes = []
        for f in sorted(folder_path.glob("*.md")):
            if f.name == "index.md":
                continue
            fm, _ = self._parse_frontmatter(f.read_text(encoding="utf-8"))
            if fm.get("id"):
                tags_str = ", ".join(fm.get("tags", [])) if isinstance(fm.get("tags"), list) else str(fm.get("tags", ""))
                updated = fm.get("updated", fm.get("created", ""))[:10] if fm.get("updated") or fm.get("created") else ""
                children_count = self._count_children(fm["id"])
                notes.append({
                    "id": fm["id"],
                    "title": fm.get("title", "Untitled"),
                    "tags": tags_str,
                    "updated": updated,
                    "children_count": children_count
                })
        
        # Build index content
        lines = [f"# {folder.capitalize()}", ""]
        if notes:
            lines.append("| ID | Title | Tags | Last Updated | Children |")
            lines.append("|----|-------|------|--------------|----------|")
            for n in notes:
                lines.append(f"| {n['id']} | {n['title']} | {n['tags']} | {n['updated']} | {n['children_count']} |")
        else:
            lines.append("*No notes yet.*")
        
        lines.extend([
            "",
            "---",
            f"*Search hint: use `search_notes` with folder=\"{folder}\" to find by keyword*",
            ""
        ])
        
        index_path = folder_path / "index.md"
        index_path.write_text("\n".join(lines), encoding="utf-8")

    def _update_root_index(self) -> None:
        """Update the root index.md."""
        self._write_root_index(Path(self.folder))

    # ============================================================================
    # Core Operations
    # ============================================================================

    def init_notebook(self, folder: str = None) -> dict:
        """Initialize or reconfigure the notebook folder location.
        
        Sets a global reference so all subsequent tool calls use this folder.
        Only creates the root directory if it doesn't exist. Subfolders are 
        created lazily on first write.
        
        Args:
            folder: Path to use as notebook root. If ends with '/', appends .lmnotes/.
            
        Returns:
            Dict with status and resolved path.
        """
        global _notebook_folder
        
        if folder:
            self.folder = self._resolve_folder(folder)
        
        # Persist the resolved folder globally so all tools use it
        _notebook_folder = str(self.folder)
        
        # Create root only if missing (lazy subfolder creation)
        self._ensure_ready()
        
        # Write root index so the notebook is navigable from the start
        self._write_root_index(Path(self.folder))
        
        self._git_init()
        
        global _initialized
        _initialized = True
        
        root = Path(self.folder)
        result = {
            "status": "success",
            "message": "Notebook ready."
        }
        if DEBUG:
            result["notebook_folder"] = str(root)
        return result
    
    def _git_init(self) -> None:
        """Initialize git repo in notebook folder. Safe to call repeatedly."""
        root = Path(self.folder)
        git_dir = root / ".git"
        if git_dir.exists():
            # Still ensure user config exists (may have been set before)
            self._ensure_git_user_config(str(root))
            return
        try:
            subprocess.run(
                ["git", "init", "--initial-branch=main"],
                cwd=str(root), capture_output=True, check=True
            )
            # Write .gitignore to exclude Python cache files
            gi = root / ".gitignore"
            if not gi.exists():
                gi.write_text("*.pyc\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
            # Configure git user so commits work even without global config
            self._ensure_git_user_config(str(root))
            # Initial commit of index.md if it exists
            idx = root / "index.md"
            if idx.exists():
                subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "Initial notebook structure"],
                    cwd=str(root), capture_output=True, check=True
                )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass  # git not available or failed — continue without version control

    @staticmethod
    def _ensure_git_user_config(repo_path: str) -> None:
        """Ensure git user.email and user.name are configured locally."""
        try:
            subprocess.run(
                ["git", "config", "user.email", "lmnotes@local"],
                cwd=repo_path, capture_output=True, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "LMNotes"],
                cwd=repo_path, capture_output=True, check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    
    def _git_commit(self, message: str) -> dict:
        """Stage and commit all changes in the notebook folder.
        
        Returns:
            Dict with status ('ok', 'skipped', or 'error') and optional details.
        """
        root = Path(self.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"status": "skipped", "message": "Git not initialized"}
        try:
            # Check for staged changes before committing
            check = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(root), capture_output=True
            )
            if check.returncode == 0:
                return {"status": "skipped", "message": "No changes to commit"}
            subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True, check=True)
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(root), capture_output=True, text=True, check=True
            )
            # Get the new commit hash
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root), capture_output=True, text=True
            )
            return {
                "status": "ok",
                "commit_hash": hash_result.stdout.strip()
            }
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.strip() if e.stderr else str(e)
            return {"status": "error", "message": f"Git commit failed: {stderr}"}
        except FileNotFoundError:
            return {"status": "error", "message": "Git not found. Is git installed?"}

    def _count_children(self, parent_id: str) -> int:
        """Count how many notes reference the given ID as their parent."""
        root = Path(self.folder)
        count = 0
        for folder_name in VALID_FOLDERS:
            search_path = root / folder_name
            if not search_path.exists():
                continue
            for note_file in sorted(search_path.glob("*.md")):
                if note_file.name == "index.md":
                    continue
                fm, _ = self._parse_frontmatter(note_file.read_text(encoding="utf-8"))
                if fm.get("parent_id") == parent_id:
                    count += 1
        return count

    def create_note(self, title: str, content: str, folder: str, 
                    tags: List[str] = None, note_id: str = None,
                    parent_id: str = None) -> dict:
        """Create a new note file and update the parent index.
        
        Args:
            title: Note title
            content: Markdown content
            folder: Folder category (procedures, reports, etc.)
            tags: List of tags for searchability
            note_id: Custom ID (auto-generated if omitted)
            parent_id: Optional ID of a parent note to link this note to
            
        Returns:
            Dict with status, id, filepath, and note data.
        """
        err = _require_initialized()
        if err:
            return err
        if folder not in VALID_FOLDERS:
            return {"status": "error", "message": f"Invalid folder '{folder}'. Must be one of: {VALID_FOLDERS}"}
        
        # Validate parent_id exists if provided
        if parent_id:
            found = self._find_note_file(parent_id)
            if not found:
                return {"status": "error", "message": f"Parent note with ID '{parent_id}' not found"}
        
        # Ensure root and target subfolder exist (lazy creation)
        self._ensure_initialized()
        self._ensure_ready(subfolder=folder)
        
        now = datetime.now(timezone.utc)
        ts_id = note_id if note_id else self._generate_id(now)
        slug = self._make_slug(title, ts_id)
        filename = f"{ts_id}_{slug}.md"
        
        data = {
            "id": ts_id,
            "title": title,
            "folder": folder,
            "tags": tags or [],
            "created": now.isoformat(),
            "updated": now.isoformat()
        }
        if parent_id:
            data["parent_id"] = parent_id
        
        frontmatter = self._build_frontmatter(data)
        full_content = f"{frontmatter}\n\n{content}" if content else frontmatter
        
        root = Path(self.folder)
        filepath = root / folder / filename
        filepath.write_text(full_content, encoding="utf-8")
        
        # Update indexes
        self._update_index(folder)
        self._update_root_index()
        
        self._git_commit(f"Add note: {title} ({ts_id})")
        
        result = {
            "status": "success",
            "id": ts_id,
            "title": title,
            "folder": folder,
            "tags": tags or [],
            "created": data["created"],
            "updated": data["updated"]
        }
        if DEBUG:
            result["filepath"] = str(filepath)
        return result

    def read_note(self, note_id: str, detail_level: int = 1) -> dict:
        """Read a note by its ID.
        
        Args:
            note_id: The timestamp-based unique identifier
            detail_level: 0=minimal (id+title), 1=one matching line, 
                         2=matching paragraph (~5 lines), 3=full content
            
        Returns:
            Dict with status and note data. Includes parent info if present.
        """
        err = _require_initialized()
        if err:
            return err
        filepath = self._find_note_file(note_id)
        if not filepath:
            # Try to find by exact filename match (handles same-second collisions)
            root = Path(self.folder)
            for folder_name in VALID_FOLDERS + ["", "."]:
                search_path = root / folder_name if folder_name else root
                if not search_path.exists():
                    continue
                matches = list(search_path.glob(f"{note_id}_*.md"))
                valid_matches = [f for f in matches if "_" in f.name and f.name != "index.md"]
                if len(valid_matches) >= 1:
                    filepath = valid_matches[0]
                    break
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = self._read_note_file(filepath)
        if not note:
            return {"status": "error", "message": f"Could not read note file: {filepath}"}
        
        result = {
            "status": "success",
            "id": note["id"],
            "title": note.get("title", "Untitled"),
            "folder": note.get("folder", ""),
            "tags": note.get("tags", []),
            "created": note.get("created", ""),
            "updated": note.get("updated", "")
        }
        
        # Parent info is always visible (functional data, not internal path)
        if note.get("parent_id"):
            result["parent_id"] = note["parent_id"]
            parent_file = self._find_note_file(note["parent_id"])
            if parent_file:
                parent_fm, _ = self._parse_frontmatter(parent_file.read_text(encoding="utf-8"))
                result["parent_title"] = parent_fm.get("title", "Untitled")
        
        # Children count is always visible (functional data)
        result["children_count"] = self._count_children(note_id)
        
        if detail_level == 0:
            result["content"] = None
        elif detail_level == 1:
            # Return first non-empty line as preview
            lines = [l for l in note.get("body", "").split("\n") if l.strip()]
            result["preview_line"] = lines[0] if lines else ""
            result["content"] = None
        elif detail_level == 2:
            # Return first few paragraphs (~5 lines)
            body = note.get("body", "")
            paragraphs = re.split(r'\n\n+', body)
            preview = "\n\n".join(paragraphs[:3]) if paragraphs else ""
            result["preview"] = preview[:1000]  # Cap at 1000 chars
            result["content"] = None
        elif detail_level == 3:
            result["content"] = note.get("body", "")
        
        return result

    def search_notes(self, keywords: List[str], folder: str = None,
                     detail_level: int = 1, max_results: int = 20,
                     max_tokens: int = 4096) -> dict:
        """Search notes by keywords with ranking based on match count.
        
        Args:
            keywords: List of keywords to search for
            folder: Limit search to a specific folder (optional)
            detail_level: 0=minimal, 1=one line, 2=paragraph, 3=full content
            max_results: Maximum number of results
            max_tokens: Token budget safeguard
            
        Returns:
            Dict with ranked groups by match count.
        """
        err = _require_initialized()
        if err:
            return err
        root = Path(self.folder)
        if not root.exists():
            return {"status": "error", "message": "Notebook not initialized. Call init_notebook first."}
        
        search_paths = []
        folders_to_search = [folder] if folder else VALID_FOLDERS
        
        for f in folders_to_search:
            p = root / f if f else root
            if p.exists():
                search_paths.append(p)
        
        # Search all notes
        results = []  # List of (match_count, matched_kw, note_data)
        
        for path in search_paths:
            for note_file in path.glob("*.md"):
                if note_file.name == "index.md":
                    continue
                
                note = self._read_note_file(note_file)
                if not note:
                    continue
                
                text_to_search = f"{note.get('title', '')} {note.get('body', '')} {' '.join(str(t) for t in note.get('tags', []))}"
                text_lower = text_to_search.lower()
                
                matched = [kw for kw in keywords if kw.lower() in text_lower]
                if matched:
                    results.append((len(matched), matched, note))
        
        # Group by match count
        groups: Dict[int, List[dict]] = {}
        for match_count, matched_kw, note in results:
            if match_count not in groups:
                groups[match_count] = []
            groups[match_count].append({
                "id": note["id"],
                "title": note.get("title", "Untitled"),
                "folder": note.get("folder", ""),
                "matched_keywords": matched_kw,
                "total_keywords": len(keywords),
                "note_data": note
            })
        
        # Sort groups by match count (descending)
        sorted_groups = sorted(groups.items(), key=lambda x: x[0], reverse=True)
        
        # Apply max_results limit and detail_level
        final_result = []
        current_tokens = 0
        
        for match_count, items in sorted_groups:
            if len(final_result) >= max_results:
                break
            
            group_items = []
            for item in items:
                if len(final_result) + len(group_items) >= max_results:
                    break
                
                note_data = item["note_data"]
                
                result_item = {
                    "id": item["id"],
                    "title": item["title"],
                    "folder": item["folder"],
                    "matched_keywords": item["matched_keywords"],
                    "match_count": match_count,
                    "total_keywords": item["total_keywords"]
                }
                
                if detail_level == 0:
                    pass  # Minimal - no content
                elif detail_level == 1:
                    lines = [l for l in note_data.get("body", "").split("\n") if l.strip()]
                    result_item["preview_line"] = lines[0] if lines else ""
                elif detail_level == 2:
                    paragraphs = re.split(r'\n\n+', note_data.get("body", ""))
                    preview = "\n\n".join(paragraphs[:3]) if paragraphs else ""
                    result_item["preview"] = preview[:1000]
                elif detail_level == 3:
                    content = note_data.get("body", "")
                    # Estimate tokens (~4 chars per token)
                    estimated_tokens = len(content) // 4
                    if current_tokens + estimated_tokens > max_tokens and final_result:
                        result_item["_excluded"] = True
                        continue
                    result_item["content"] = content
                    current_tokens += estimated_tokens
                
                group_items.append(result_item)
            
            final_result.extend(group_items)
        
        # Build output with grouping
        ranked_output = {}
        for item in final_result:
            mc = item.pop("match_count")
            if mc not in ranked_output:
                ranked_output[mc] = []
            ranked_output[mc].append(item)
        
        excluded = len(results) - len(final_result)
        
        return {
            "status": "success",
            "search_keywords": keywords,
            "total_matches": len(results),
            "results_returned": len(final_result),
            "excluded_too_many_results": excluded,
            "groups": ranked_output
        }

    def list_folder(self, folder: str = None) -> dict:
        """List contents of a folder as structured rows (database-style).
        
        Args:
            folder: Folder name (optional, defaults to root).
            
        Returns:
            Dict with folder name and list of note rows.
        """
        err = _require_initialized()
        if err:
            return err
        self._ensure_initialized()
        root = Path(self.folder)
        
        if not folder or folder == "":
            # Root index - return category summary as table rows
            notes = []
            for f_name in VALID_FOLDERS:
                folder_path = root / f_name
                if folder_path.exists():
                    count = len(list(folder_path.glob("*.md"))) - 1
                    notes.append({
                        "id": "",
                        "title": f_name,
                        "type": "folder",
                        "notes_count": count,
                        "tags": [],
                        "updated": ""
                    })
            return {
                "status": "success",
                "folder": "root",
                "notes": notes,
                "total_categories": len(notes)
            }
        else:
            folder_path = root / folder
            if not folder_path.exists():
                return {"status": "error", "message": f"Folder '{folder}' not found"}
            
            notes = []
            for f in sorted(folder_path.glob("*.md")):
                if f.name == "index.md":
                    continue
                fm, _ = self._parse_frontmatter(f.read_text(encoding="utf-8"))
                note_row = {
                    "id": fm.get("id", ""),
                    "title": fm.get("title", "Untitled"),
                    "tags": fm.get("tags", []),
                    "created": fm.get("created", "")[:10] if fm.get("created") else "",
                    "updated": fm.get("updated", "")[:10] if fm.get("updated") else ""
                }
                if DEBUG:
                    note_row["filepath"] = str(f)
                notes.append(note_row)
            
            return {
                "status": "success",
                "folder": folder,
                "notes": notes,
                "total": len(notes)
            }

    def list_children(self, note_id: str) -> dict:
        """List all notes that reference the given ID as their parent.
        
        Args:
            note_id: The ID of the parent note
            
        Returns:
            Dict with status and list of child note rows.
        """
        err = _require_initialized()
        if err:
            return err
        
        # Verify parent exists — try exact match first, then fall back to glob
        parent_file = self._find_note_file(note_id)
        if not parent_file:
            root = Path(self.folder)
            for folder_name in VALID_FOLDERS + ["", "."]:
                search_path = root / folder_name if folder_name else root
                if not search_path.exists():
                    continue
                matches = list(search_path.glob(f"{note_id}_*.md"))
                valid_matches = [f for f in matches if "_" in f.name and f.name != "index.md"]
                if len(valid_matches) >= 1:
                    parent_file = valid_matches[0]
                    break
        if not parent_file:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        fm, _ = self._parse_frontmatter(parent_file.read_text(encoding="utf-8"))
        children = []
        root = Path(self.folder)
        
        for folder_name in VALID_FOLDERS:
            search_path = root / folder_name
            if not search_path.exists():
                continue
            for note_file in sorted(search_path.glob("*.md")):
                if note_file.name == "index.md":
                    continue
                child_fm, _ = self._parse_frontmatter(note_file.read_text(encoding="utf-8"))
                if child_fm.get("parent_id") == note_id:
                    children.append({
                        "id": child_fm["id"],
                        "title": child_fm.get("title", "Untitled"),
                        "folder": folder_name,
                        "tags": ", ".join(child_fm.get("tags", [])) if isinstance(child_fm.get("tags"), list) else str(child_fm.get("tags", "")),
                        "updated": child_fm.get("updated", "")[:10] if child_fm.get("updated") else "",
                        "parent_id": child_fm["parent_id"]
                    })
        
        return {
            "status": "success",
            "note_id": note_id,
            "parent_title": fm.get("title", "Untitled"),
            "children_count": len(children),
            "children": children
        }

    def list_notes(self, folder: str = None, 
                   detail_level: int = 1, max_results: int = 50) -> dict:
        """List all notes as structured rows (database-style table view).
        
        This is the primary navigation tool for the LLM. Returns a clean
        tabular representation without any internal file paths.
        
        Args:
            folder: Limit to a specific folder (optional, defaults to all)
            detail_level: 0=minimal (id+title+folder only), 1=with tags+updated (default)
            max_results: Maximum notes to return
            
        Returns:
            Dict with status and list of note rows.
        """
        err = _require_initialized()
        if err:
            return err
        self._ensure_initialized()
        root = Path(self.folder)
        
        # First pass: collect all note IDs for parent lookup
        all_ids = set()
        folders_to_list = [folder] if folder else VALID_FOLDERS
        for f_name in folders_to_list:
            folder_path = root / f_name
            if not folder_path.exists():
                continue
            for note_file in sorted(folder_path.glob("*.md")):
                if note_file.name == "index.md":
                    continue
                fm, _ = self._parse_frontmatter(note_file.read_text(encoding="utf-8"))
                if fm.get("id"):
                    all_ids.add(fm["id"])
        
        all_notes = []
        for f_name in folders_to_list:
            folder_path = root / f_name
            if not folder_path.exists():
                continue
            for note_file in sorted(folder_path.glob("*.md")):
                if note_file.name == "index.md":
                    continue
                fm, body = self._parse_frontmatter(note_file.read_text(encoding="utf-8"))
                if not fm.get("id"):
                    continue
                
                tags_str = ", ".join(fm.get("tags", [])) if isinstance(fm.get("tags"), list) else str(fm.get("tags", ""))
                updated = fm.get("updated", fm.get("created", ""))[:10] if fm.get("updated") or fm.get("created") else ""
                
                note_row = {
                    "id": fm["id"],
                    "title": fm.get("title", "Untitled"),
                    "folder": f_name,
                    "tags": tags_str,
                    "updated": updated,
                    "children_count": self._count_children(fm["id"])
                }
                
                # Parent ID is always visible (functional data)
                if fm.get("parent_id"):
                    note_row["parent_id"] = fm["parent_id"]
                    parent_file = self._find_note_file(fm["parent_id"])
                    if parent_file:
                        parent_fm, _ = self._parse_frontmatter(parent_file.read_text(encoding="utf-8"))
                        note_row["parent_title"] = parent_fm.get("title", "Untitled")
                
                if detail_level >= 1 and body:
                    first_line = next((l.strip() for l in body.split("\n") if l.strip()), "")
                    note_row["preview"] = first_line[:200] if first_line else ""
                
                all_notes.append(note_row)
                
                if len(all_notes) >= max_results:
                    break
            
            if len(all_notes) >= max_results:
                break
        
        return {
            "status": "success",
            "total_returned": len(all_notes),
            "notes": all_notes
        }

    def read_index(self, folder: str = None) -> dict:
        """Read an index.md file for navigation.
        
        Args:
            folder: Folder to read (optional, defaults to root).
            
        Returns:
            Dict with raw content and parsed entries.
        """
        err = _require_initialized()
        if err:
            return err
        self._ensure_initialized()
        root = Path(self.folder)
        
        if not folder or folder == "":
            index_path = root / "index.md"
        else:
            index_path = root / folder / "index.md"
        
        if not index_path.exists():
            return {"status": "error", "message": f"Index not found for '{folder or 'root'}'"}
        
        content = index_path.read_text(encoding="utf-8")
        
        # Parse table entries if present
        entries = []
        in_table = False
        for line in content.split("\n"):
            if line.startswith("| ID |"):
                in_table = True
                continue
            if in_table and line.startswith("|---"):
                continue
            if in_table and line.startswith("|"):
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 3:
                    entries.append({"id": parts[0], "title": parts[1], "tags": parts[2]})
            elif in_table and not line.startswith("|"):
                in_table = False
        
        return {
            "status": "success",
            "folder": folder or "root",
            "content": content,
            "entries": entries
        }

    def _git_diff_file(self, filepath: Path, from_rev: str = "HEAD", to_rev: str = None) -> str:
        """Get diff for a file between two revisions.
        
        Defaults to comparing working tree against HEAD (last committed state),
        so uncommitted edits show immediately even before a successful commit.
        
        Args:
            filepath: Path to the note file
            from_rev: Starting revision (default: HEAD)
            to_rev: Ending revision (default: None = working tree)
            
        Returns:
            Diff text or empty string.
        """
        root = Path(self.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return ""
        try:
            rel_path = filepath.relative_to(root)
            if to_rev is None:
                # Working tree vs from_rev (shows uncommitted changes immediately)
                result = subprocess.run(
                    ["git", "diff", from_rev, "--", str(rel_path)],
                    cwd=str(root), capture_output=True, text=True
                )
            else:
                result = subprocess.run(
                    ["git", "diff", f"{from_rev}", to_rev, "--", str(rel_path)],
                    cwd=str(root), capture_output=True, text=True
                )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""
    
    def update_note(self, note_id: str, title: str = None, tags: List[str] = None,
                    content: str = None) -> dict:
        """Update an existing note's fields. Content is replaced entirely.
        
        Args:
            note_id: The unique identifier of the note
            title: New title (optional)
            tags: New tags list (optional)
            content: New full content (optional)
            
        Returns:
            Dict with status and updated data.
        """
        err = _require_initialized()
        if err:
            return err
        filepath = self._find_note_file(note_id)
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = self._read_note_file(filepath)
        if not note:
            return {"status": "error", "message": f"Could not read note: {filepath}"}
        
        # Update fields
        new_title = title if title is not None else note.get("title", "")
        new_tags = tags if tags is not None else note.get("tags", [])
        new_content = content if content is not None else note.get("body", "")
        
        now = datetime.now(timezone.utc)
        data = {
            "id": note_id,
            "title": new_title,
            "folder": note.get("folder", ""),
            "tags": new_tags,
            "created": note.get("created", now.isoformat()),
            "updated": now.isoformat()
        }
        
        frontmatter = self._build_frontmatter(data)
        full_content = f"{frontmatter}\n\n{new_content}" if new_content else frontmatter
        
        filepath.write_text(full_content, encoding="utf-8")
        self._update_index(note.get("folder", ""))
        self._update_root_index()
        
        self._git_commit(f"Update note {note_id}: {'title' if title else 'tags/content'} changed")
        
        return {
            "status": "success",
            "id": note_id,
            "title": new_title,
            "tags": new_tags,
            "updated": now.isoformat()
        }

    def git_log(self, note_id: str) -> dict:
        """Return commit history for a specific note file.
        
        Args:
            note_id: The ID of the note to get history for
            
        Returns:
            Dict with status and list of commits (hash, date, message).
        """
        err = _require_initialized()
        if err:
            return err
        filepath = self._find_note_file(note_id)
        if not filepath:
            # Also check references folder (non-note files like CSVs)
            root = Path(self.folder)
            for f in root.rglob(f"*{note_id}*.md"):
                if "_" in f.name and f.name != "index.md":
                    filepath = f
                    break
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        root = Path(self.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"status": "success", "note_id": note_id, "commits": [], "message": "Git not initialized in this notebook."}
        
        try:
            rel_path = filepath.relative_to(root)
            result = subprocess.run(
                ["git", "log", "--pretty=format:%H|%ai|%s", "--", str(rel_path)],
                cwd=str(root), capture_output=True, text=True
            )
            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) == 3:
                    commits.append({
                        "hash": parts[0],
                        "date": parts[1][:10],
                        "message": parts[2]
                    })
            return {
                "status": "success",
                "note_id": note_id,
                "filepath": str(filepath),
                "commits": commits
            }
        except (subprocess.CalledProcessError, FileNotFoundError):
            return {"status": "error", "message": "Failed to read git log"}
    
    def git_diff(self, note_id: str, from_rev: str = "", to_rev: str = "") -> dict:
        """Show diff between two revisions of a note.
        
        Args:
            note_id: The ID of the note
            from_rev: Starting revision (default: HEAD~1)
            to_rev: Ending revision (default: HEAD)
            
        Returns:
            Dict with status and diff text.
        """
        err = _require_initialized()
        if err:
            return err
        filepath = self._find_note_file(note_id)
        if not filepath:
            root = Path(self.folder)
            for f in root.rglob(f"*{note_id}*.md"):
                if "_" in f.name and f.name != "index.md":
                    filepath = f
                    break
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        root = Path(self.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"status": "success", "note_id": note_id, "diff": "", "message": "Git not initialized."}
        
        from_r = from_rev if from_rev else "HEAD~1"
        to_r = to_rev if to_rev else "HEAD"
        
        try:
            rel_path = filepath.relative_to(root)
            result = subprocess.run(
                ["git", "diff", from_r, to_r, "--", str(rel_path)],
                cwd=str(root), capture_output=True, text=True
            )
            return {
                "status": "success",
                "note_id": note_id,
                "from_rev": from_r,
                "to_rev": to_r,
                "diff": result.stdout.strip() or "(no changes)"
            }
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return {"status": "error", "message": f"Failed to read git diff: {e}"}
    
    def git_checkout(self, note_id: str, revision: str) -> dict:
        """Restore a note to a previous revision.
        
        Args:
            note_id: The ID of the note
            revision: Git commit hash or reference (e.g., HEAD~1, abc1234)
            
        Returns:
            Dict with status and result info.
        """
        err = _require_initialized()
        if err:
            return err
        filepath = self._find_note_file(note_id)
        if not filepath:
            root = Path(self.folder)
            for f in root.rglob(f"*{note_id}*.md"):
                if "_" in f.name and f.name != "index.md":
                    filepath = f
                    break
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        root = Path(self.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"status": "error", "message": "Git not initialized in this notebook."}
        
        try:
            rel_path = filepath.relative_to(root)
            # Checkout the file from the specified revision (don't change working tree for other files)
            subprocess.run(
                ["git", "checkout", revision, "--", str(rel_path)],
                cwd=str(root), capture_output=True, check=True
            )
            self._update_index(filepath.parent.name if filepath.parent != root else "")
            self._update_root_index()
            self._git_commit(f"Restore note {note_id} to {revision}")
            return {
                "status": "success",
                "note_id": note_id,
                "restored_to": revision,
                "filepath": str(filepath) if DEBUG else None
            }
        except subprocess.CalledProcessError as e:
            return {"status": "error", "message": f"Git checkout failed: {e.stderr.strip() or str(e)}"}
        except FileNotFoundError:
            return {"status": "error", "message": "Git not found. Is git installed?"}
    
    def append_to_note(self, note_id: str, addition: str, 
                       separator: str = "\n\n---\n\n") -> dict:
        """Append text to an existing note's content.
        
        Args:
            note_id: The unique identifier of the note
            addition: Text to append
            separator: Visual separator (default: "\\n\\n---\\n\\n")
            
        Returns:
            Dict with status and length info.
        """
        err = _require_initialized()
        if err:
            return err
        filepath = self._find_note_file(note_id)
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = self._read_note_file(filepath)
        if not note:
            return {"status": "error", "message": f"Could not read note: {filepath}"}
        
        original_content = note.get("body", "")
        new_content = original_content + separator + addition
        
        # Rebuild with updated content
        data = {
            "id": note_id,
            "title": note.get("title", ""),
            "folder": note.get("folder", ""),
            "tags": note.get("tags", []),
            "created": note.get("created", ""),
            "updated": datetime.now(timezone.utc).isoformat()
        }
        
        frontmatter = self._build_frontmatter(data)
        full_content = f"{frontmatter}\n\n{new_content}"
        
        filepath.write_text(full_content, encoding="utf-8")
        self._update_index(note.get("folder", ""))
        self._update_root_index()
        
        self._git_commit(f"Append to note {note_id}: +{len(addition)} chars")
        
        return {
            "status": "success",
            "id": note_id,
            "original_length": len(original_content),
            "new_length": len(new_content),
            "separator_used": separator
        }

    def delete_note(self, note_id: str) -> dict:
        """Delete a note file and update the index.
        
        Args:
            note_id: The unique identifier of the note
            
        Returns:
            Dict with status.
        """
        err = _require_initialized()
        if err:
            return err
        filepath = self._find_note_file(note_id)
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = self._read_note_file(filepath)
        folder = note.get("folder", "") if note else ""
        
        filepath.unlink()
        
        if folder:
            self._update_index(folder)
        self._update_root_index()
        
        self._git_commit(f"Delete note {note_id}")
        
        result = {
            "status": "success",
            "id": note_id
        }
        if DEBUG:
            result["deleted_file"] = str(filepath)
        return result

    def get_stats(self) -> dict:
        """Return notebook statistics."""
        err = _require_initialized()
        if err:
            return err
        self._ensure_initialized()
        root = Path(self.folder)
        
        stats = {"total_notes": 0, "folders": {}}
        
        for folder in VALID_FOLDERS:
            folder_path = root / folder
            if folder_path.exists():
                count = len(list(folder_path.glob("*.md"))) - 1  # Exclude index.md
                stats["folders"][folder] = count
                stats["total_notes"] += count
        
        result = {
            "status": "success",
            "total_notes": stats["total_notes"],
            "folders": stats["folders"]
        }
        if DEBUG:
            result["notebook_folder"] = str(root)
        return result

    def read_system_prompt(self) -> dict:
        """Read system folder prompts.
        
        Returns:
            Dict with core prompt and additional notes.
        """
        err = _require_initialized()
        if err:
            return err
        self._ensure_initialized()
        root = Path(self.folder)
        system_path = root / "system"
        
        result = {
            "status": "success",
            "core_prompt": None,
            "notes": []
        }
        
        if not system_path.exists():
            return result
        
        # Read core prompt (000000000000_core_prompt.md)
        core_file = system_path / "000000000000_core_prompt.md"
        if core_file.exists():
            result["core_prompt"] = core_file.read_text(encoding="utf-8")
        
        # Read other system notes
        for f in sorted(system_path.glob("*.md")):
            if f.name == "index.md" or f.name == "000000000000_core_prompt.md":
                continue
            note = self._read_note_file(f)
            if note:
                result["notes"].append({
                    "id": note.get("id", ""),
                    "title": note.get("title", f.name),
                    "content": note.get("body", "")
                })
        
        return result

    def copy_to_references(self, source_path: str, description: str = "", 
                            note_id: str = None) -> dict:
        """Copy a file to the references folder and optionally create a description.
        
        Args:
            source_path: Full path to the file to copy
            description: Markdown description for the companion .md file
            note_id: Custom ID (auto-generated if omitted)
            
        Returns:
            Dict with status, destination path, and note data.
        """
        err = _require_initialized()
        if err:
            return err
        self._ensure_initialized()
        
        src = Path(source_path).expanduser()
        if not src.exists():
            return {"status": "error", "message": f"Source file not found: {source_path}"}
        
        dest_folder = Path(self.folder) / "references"
        dest_folder.mkdir(parents=True, exist_ok=True)
        
        # Generate ID and filename
        now = datetime.now(timezone.utc)
        ts_id = note_id if note_id else self._generate_id(now)
        original_name = src.name
        base_name = src.stem.replace(".", "_")
        
        # Avoid name collision
        dest_file = dest_folder / f"{ts_id}_{base_name}{src.suffix}"
        counter = 1
        while dest_file.exists():
            dest_file = dest_folder / f"{ts_id}_{base_name}_{counter}{src.suffix}"
            counter += 1
        
        # Copy file
        shutil.copy2(src, dest_file)
        
        # Create description .md if description provided
        md_note_id = ts_id
        note_data = {
            "id": md_note_id,
            "title": f"Reference: {original_name}",
            "folder": "references",
            "tags": ["reference"],
            "created": now.isoformat(),
            "updated": now.isoformat()
        }
        
        if description:
            # Write the description note
            frontmatter = self._build_frontmatter(note_data)
            md_content = f"{frontmatter}\n\n{description}"
            
            # Use a separate ID for the .md file to avoid collision
            md_id = ts_id
            md_file = dest_folder / f"{md_id}_{base_name}.md"
            counter = 1
            while md_file.exists():
                md_file = dest_folder / f"{md_id}_{base_name}_{counter}.md"
                counter += 1
            
            md_file.write_text(md_content, encoding="utf-8")
        
        self._update_index("references")
        self._update_root_index()
        
        result = {
            "status": "success",
            "id": md_note_id,
            "filename": original_name,
            "note_created": bool(description)
        }
        if DEBUG:
            result["source_path"] = str(src)
            result["destination_path"] = str(dest_file)
        return result

    # ============================================================================
    # Select and Edit Workflow (from memorylite pattern)
    # ============================================================================

    def select_note(self, note_id: str, pattern: str = None, 
                    mode: str = "exact", start_line: int = 1, 
                    end_line: int = -1) -> dict:
        """Select/search text within a note for editing.
        
        Args:
            note_id: The unique identifier of the note
            pattern: Text to search for (required for exact/regex modes)
            mode: "exact" (default), "regex", or "lines"
            start_line: Start line number (for "lines" mode, 1-based)
            end_line: End line number (for "lines" mode, 1-based, -1=last)
            
        Returns:
            Dict with selection_id, occurrences, matched text preview.
        """
        err = _require_initialized()
        if err:
            return err
        filepath = self._find_note_file(note_id)
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = self._read_note_file(filepath)
        if not note:
            return {"status": "error", "message": f"Could not read note: {filepath}"}
        
        global _selection_counter
        
        valid_modes = ["exact", "regex", "lines"]
        if mode not in valid_modes:
            return {"status": "error", "message": f"Invalid mode '{mode}'. Must be one of: {valid_modes}"}
        
        body = note.get("body", "")
        occurrences = 0
        matched_text = ""
        match_positions = []
        
        try:
            if mode == "exact":
                if not pattern:
                    return {"status": "error", "message": "Pattern is required for exact mode"}
                # Find all occurrences
                pos = 0
                while True:
                    idx = body.find(pattern, pos)
                    if idx == -1:
                        break
                    match_positions.append({"start": idx, "end": idx + len(pattern)})
                    occurrences += 1
                    pos = idx + 1
                
                # Build preview (first match + truncated context)
                if match_positions:
                    first = match_positions[0]
                    start = max(0, first["start"] - 200)
                    end = min(len(body), first["end"] + 200)
                    matched_text = body[start:end]
                    
            elif mode == "regex":
                if not pattern:
                    return {"status": "error", "message": "Pattern is required for regex mode"}
                matches = list(re.finditer(pattern, body))
                occurrences = len(matches)
                for m in matches:
                    match_positions.append({"start": m.start(), "end": m.end()})
                
                if matches:
                    m = matches[0]
                    start = max(0, m.start() - 200)
                    end = min(len(body), m.end() + 200)
                    matched_text = body[start:end]
                    
            elif mode == "lines":
                lines = body.split("\n")
                if end_line == -1:
                    end_line = len(lines)
                
                selected_lines = lines[start_line - 1:end_line]
                occurrences = len(selected_lines)
                matched_text = "\n".join(selected_lines)
                
        except re.error as e:
            return {"status": "error", "message": f"Invalid regex pattern: {e}"}
        
        # Create selection ID
        _selection_counter += 1
        selection_id = f"sel_{note_id}_{_selection_counter}"
        
        # Store selection for later editing
        _selection_store[selection_id] = {
            "note_id": note_id,
            "filepath": str(filepath),
            "mode": mode,
            "pattern": pattern,
            "start_line": start_line,
            "end_line": end_line,
            "occurrences": occurrences,
            "match_positions": match_positions,
            "body_snapshot": body
        }
        
        # Truncate matched_text if too long
        truncated = False
        if len(matched_text) > 500:
            half = 200
            matched_text = matched_text[:half] + "\n...<truncated>... " + matched_text[-half:]
            truncated = True
        
        return {
            "status": "success",
            "note_id": note_id,
            "mode": mode,
            "occurrences": occurrences,
            "matched_text": matched_text,
            "truncated": truncated,
            "selection_id": selection_id,
            "match_positions": match_positions[:5]  # Return first 5 positions
        }

    def _apply_selection_edit(self, selection_id: str, replacement: str = None,
                               addition: str = None, occurrence: int = 0) -> dict:
        """Apply an edit to a previously selected note. Internal helper."""
        if selection_id not in _selection_store:
            return {"status": "error", "message": "This selection has already been used to edit the note. Call select_note again to make further edits."}
        
        sel = _selection_store.pop(selection_id)  # Nullify after use
        filepath = Path(sel["filepath"])
        
        # Capture before state for diff
        before_content = sel["body_snapshot"]
        
        body = sel["body_snapshot"]
        mode = sel["mode"]
        
        if mode == "lines" and addition is not None:
            # For lines mode with append, we need to work with the selected text
            pass
        
        try:
            if replacement is not None:
                # Replace matches
                if mode == "exact" and sel["pattern"]:
                    pattern = sel["pattern"]
                    positions = sel["match_positions"]
                    
                    if occurrence == 0:
                        # Replace all
                        new_body = body.replace(pattern, replacement)
                        changes_made = len(positions)
                    else:
                        # Replace specific occurrence (1-based)
                        if occurrence <= len(positions):
                            pos = positions[occurrence - 1]
                            new_body = body[:pos["start"]] + replacement + body[pos["end"]:]
                            changes_made = 1
                        else:
                            return {"status": "error", "message": f"Occurrence {occurrence} not found (only {len(positions)} matches)"}
                        
                elif mode == "regex" and sel["pattern"]:
                    pattern = sel["pattern"]
                    
                    if occurrence == 0:
                        new_body = re.sub(pattern, replacement, body)
                        changes_made = len(re.findall(pattern, body))
                    else:
                        # Replace specific occurrence
                        matches = list(re.finditer(pattern, body))
                        if occurrence <= len(matches):
                            m = matches[occurrence - 1]
                            new_body = body[:m.start()] + replacement + body[m.end():]
                            changes_made = 1
                        else:
                            return {"status": "error", "message": f"Occurrence {occurrence} not found (only {len(matches)} matches)"}
                else:
                    new_body = body
                    
            elif addition is not None:
                # Append after matches
                if mode == "exact" and sel["pattern"]:
                    pattern = sel["pattern"]
                    positions = sel["match_positions"]
                    
                    if occurrence == 0:
                        # Append after all occurrences (process from end to preserve positions)
                        new_body = body
                        for pos in reversed(positions):
                            new_body = new_body[:pos["end"]] + addition + new_body[pos["end"]:]
                        changes_made = len(positions)
                    else:
                        if occurrence <= len(positions):
                            pos = positions[occurrence - 1]
                            new_body = body[:pos["end"]] + addition + body[pos["end"]:]
                            changes_made = 1
                        else:
                            return {"status": "error", "message": f"Occurrence {occurrence} not found"}
                elif mode == "regex" and sel["pattern"]:
                    pattern = sel["pattern"]
                    
                    if occurrence == 0:
                        new_body = re.sub(pattern, lambda m: m.group(0) + addition, body)
                        changes_made = len(re.findall(pattern, body))
                    else:
                        matches = list(re.finditer(pattern, body))
                        if occurrence <= len(matches):
                            m = matches[occurrence - 1]
                            new_body = body[:m.end()] + addition + body[m.end():]
                            changes_made = 1
                        else:
                            return {"status": "error", "message": f"Occurrence {occurrence} not found"}
                else:
                    new_body = body + addition
            else:
                # Delete mode - no replacement, no addition
                if mode == "exact" and sel["pattern"]:
                    pattern = sel["pattern"]
                    positions = sel["match_positions"]
                    
                    if occurrence == 0:
                        new_body = body.replace(pattern, "")
                        changes_made = len(positions)
                    else:
                        if occurrence <= len(positions):
                            pos = positions[occurrence - 1]
                            new_body = body[:pos["start"]] + body[pos["end"]:]
                            changes_made = 1
                        else:
                            return {"status": "error", "message": f"Occurrence {occurrence} not found"}
                elif mode == "regex" and sel["pattern"]:
                    pattern = sel["pattern"]
                    
                    if occurrence == 0:
                        new_body = re.sub(pattern, "", body)
                        changes_made = len(re.findall(pattern, body))
                    else:
                        matches = list(re.finditer(pattern, body))
                        if occurrence <= len(matches):
                            m = matches[occurrence - 1]
                            new_body = body[:m.start()] + body[m.end():]
                            changes_made = 1
                        else:
                            return {"status": "error", "message": f"Occurrence {occurrence} not found"}
                else:
                    new_body = body
                    
        except Exception as e:
            return {"status": "error", "message": f"Edit failed: {e}"}
        
        # Rebuild the note file with updated content
        note_id = sel["note_id"]
        note = self._read_note_file(filepath)
        if not note:
            return {"status": "error", "message": f"Could not read note for editing"}
        
        data = {
            "id": note_id,
            "title": note.get("title", ""),
            "folder": note.get("folder", ""),
            "tags": note.get("tags", []),
            "created": note.get("created", ""),
            "updated": datetime.now(timezone.utc).isoformat()
        }
        
        frontmatter = self._build_frontmatter(data)
        full_content = f"{frontmatter}\n\n{new_body}"
        filepath.write_text(full_content, encoding="utf-8")
        
        folder = note.get("folder", "")
        self._update_index(folder)
        self._update_root_index()
        
        # Capture diff after writing and commit status
        commit_result = self._git_commit(f"Edit note {note_id}: {'replace' if replacement is not None else 'delete'}")
        diff_text = self._git_diff_file(filepath) if (Path(self.folder) / ".git").exists() else ""
        
        return {
            "status": "success",
            "note_id": note_id,
            "changes_made": changes_made,
            "selection_nullified": True,
            "message": f"Selection '{selection_id}' has been used. Call select_note again for further edits.",
            "diff": diff_text or "(no git diff available)",
            "commit": commit_result
        }

    def edit_selection(self, selection_id: str, replacement: str = "", 
                       occurrence: int = 0) -> dict:
        """Edit text based on a previous selection. Selection is nullified after editing.
        
        The change is auto-committed to git. A diff preview is included in the response.
        
        Args:
            selection_id: ID returned from select_note
            replacement: New text to replace with
            occurrence: 0=ALL, 1=first, 2=second, etc.
            
        Returns:
            Dict with status, edit summary, and diff preview. Selection is nullified after editing.
        """
        return self._apply_selection_edit(selection_id, replacement=replacement, occurrence=occurrence)

    def delete_selection(self, selection_id: str, occurrence: int = 0) -> dict:
        """Delete text based on a previous selection. Selection is nullified after editing.
        
        The change is auto-committed to git. A diff preview is included in the response.
        
        Args:
            selection_id: ID returned from select_note
            occurrence: 0=ALL, 1=first, 2=second, etc.
            
        Returns:
            Dict with status, edit summary, and diff preview. Selection is nullified after editing.
        """
        return self._apply_selection_edit(selection_id, addition=None, occurrence=occurrence)

    def append_selection(self, selection_id: str, addition: str = "", 
                         occurrence: int = 0) -> dict:
        """Append text after previously selected matches. Selection is nullified.
        
        The change is auto-committed to git. A diff preview is included in the response.
        
        Args:
            selection_id: ID returned from select_note
            addition: Text to append after each match
            occurrence: 0=ALL, 1=first, 2=second, etc.
            
        Returns:
            Dict with status, edit summary, and diff preview. Selection is nullified after appending.
        """
        return self._apply_selection_edit(selection_id, addition=addition, occurrence=occurrence)

    def manual(self, tool_name: str = "") -> dict:
        """Return documentation for a specific tool or general usage guide.
        
        Args:
            tool_name: Name of the tool (e.g., 'edit_selection', 'create_note').
                      If empty, returns general usage guide.
            
        Returns:
            Dict with status and content string.
        """
        err = _require_initialized()
        if err:
            return err
        
        # Map tool names to their docstrings
        tool_docs = {
            "init_notebook": self.init_notebook.__doc__,
            "create_note": self.create_note.__doc__,
            "read_note": self.read_note.__doc__,
            "search_notes": self.search_notes.__doc__,
            "list_notes": self.list_notes.__doc__,
            "list_children": self.list_children.__doc__,
            "list_folder": self.list_folder.__doc__,
            "read_index": self.read_index.__doc__,
            "update_note": self.update_note.__doc__,
            "append_to_note": self.append_to_note.__doc__,
            "select_note": self.select_note.__doc__,
            "edit_selection": self.edit_selection.__doc__,
            "delete_selection": self.delete_selection.__doc__,
            "append_selection": self.append_selection.__doc__,
            "delete_note": self.delete_note.__doc__,
            "get_stats": self.get_stats.__doc__,
            "read_system_prompt": self.read_system_prompt.__doc__,
            "copy_to_references": self.copy_to_references.__doc__,
            "git_log": self.git_log.__doc__,
            "git_diff": self.git_diff.__doc__,
            "git_checkout": self.git_checkout.__doc__,
            "manual": self.manual.__doc__,
        }
        
        if tool_name and tool_name in tool_docs:
            return {
                "status": "success",
                "tool": tool_name,
                "content": tool_docs[tool_name]
            }
        
        # Return general guide when no tool specified
        guide = """## LMNotes Quick Guide

### Initialization
Call `lmnotes_init_notebook` first to set up your notebook folder. All other tools require this.

### Creating Notes
Use `lmnotes_create_note` with title, content, and folder. Optional: tags, note_id, parent_id (to link to another note).

### Reading & Searching
- `lmnotes_read_note(id, detail_level)` — read a single note (0=minimal, 1=preview, 2=paragraph, 3=full)
- `lmnotes_search_notes(keywords)` — search across all notes with ranking
- `lmnotes_list_notes()` — browse all notes as a clean table

### Navigation
- `lmnotes_list_children(note_id)` — find all notes that reference this one as parent
- `lmnotes_list_folder(folder)` — list contents of a specific folder
- `lmnotes_read_index(folder)` — read the raw index.md for navigation

### Editing Notes
Two-step workflow: select first, then edit/delete/append.
1. `lmnotes_select_note(note_id, pattern, mode="exact")` → gets selection_id
2. `lmnotes_edit_selection(selection_id, replacement)` / `delete_selection` / `append_selection`
   - Each edit auto-commits to git with a diff preview in the response
   - Selection is nullified after use — call select_note again for further edits

### Version History (Git)
- `lmnotes_git_log(note_id)` — see commit history for a note
- `lmnotes_git_diff(note_id, from_rev, to_rev)` — show changes between revisions
- `lmnotes_git_checkout(note_id, revision)` — restore a previous version

### Documentation
- `lmnotes_manual()` — this guide
- `lmnotes_manual("tool_name")` — detailed docstring for a specific tool

### Folder Categories
procedures | reports | individuals | conversations | knowledge | system | references"""
        
        return {
            "status": "success",
            "tool": None,
            "content": guide
        }


# ============================================================================
# Factory function
# ============================================================================

def create_notebook(folder: str = None) -> Optional[Notebook]:
    """Create a Notebook instance with the given folder or global default.
    
    Only returns an instance if init_notebook has been called (setting _notebook_folder).
    Returns None if not initialized, so the MCP tool wrapper can return the error message.
    Falls back to ~/.lmnotes/ only when _initialized is True and no explicit folder was given.
    """
    global _initialized, _notebook_folder
    if not _initialized:
        return None
    if folder is None and _notebook_folder is not None:
        folder = _notebook_folder
    elif folder is None and not _initialized:
        # Not initialized at all - let the caller handle it
        pass
    return Notebook(folder)


# ============================================================================
# FastMCP Tools
# ============================================================================

if FastMCP is not None:
    mcp = _get_mcp()

    @mcp.tool
    def lmnotes_init_notebook(folder: str = "") -> str:
        """Initialize or reconfigure the notebook folder location.
        
        Call this at the start of a session to set up your notebook directory.
        If not called, defaults to ~/.lmnotes/.
        
        Args:
            folder: Path to use as notebook root. If ends with '/', appends .lmnotes/.
                   Example: '~/my_notes/' or '/data/notebooks/'
            
        Returns:
            JSON with status and created directories.
        """
        return _tool_run(Notebook(folder if folder else None).init_notebook, folder if folder else None)

    @mcp.tool
    def lmnotes_create_note(title: str, content: str, folder: str, 
                            tags: str = "", note_id: str = "", parent_id: str = "") -> str:
        """Create a new note file and update the parent index.
        
        Args:
            title: Note title
            content: Markdown content of the note
            folder: Folder category (procedures, reports, individuals, conversations, 
                    knowledge, system, references)
            tags: Comma-separated list of tags (e.g., "git, procedures, bug-fix")
            note_id: Custom ID. If empty, auto-generated from timestamp.
            parent_id: Optional ID of a parent note to link this note to.
            
        Returns:
            JSON with status, id, filepath, and note data.
        """
        nb = create_notebook()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        return _tool_run(nb.create_note, title, content, folder, tag_list, 
                        note_id if note_id else None, parent_id if parent_id else None)

    @mcp.tool
    def lmnotes_read_note(note_id: str, detail_level: int = 1) -> str:
        """Read a note by its ID.
        
        Args:
            note_id: The timestamp-based unique identifier (e.g., "260729165500")
            detail_level: 0=minimal (id+title only), 1=one matching line (default),
                         2=matching paragraph (~5 lines), 3=full content
            
        Returns:
            JSON with status and note data.
        """
        return _tool_run(create_notebook().read_note, note_id, detail_level)

    @mcp.tool
    def lmnotes_search_notes(keywords: str, folder: str = "", 
                              detail_level: int = 1, max_results: int = 20,
                              max_tokens: int = 4096) -> str:
        """Search notes by keywords with ranking based on match count.
        
        Results are grouped by number of keywords matched (5/5 > 3/5 > 1/5).
        Even poor keyword choices return results with clear match labels.
        
        Args:
            keywords: Space-separated keywords to search for (e.g., "git rebase conflict")
            folder: Limit search to a specific folder (optional)
            detail_level: 0=minimal, 1=one line (default), 2=paragraph, 3=full content
            max_results: Maximum number of results (default: 20)
            max_tokens: Token budget safeguard for full-content results (default: 4096)
            
        Returns:
            JSON with ranked groups by match count.
        """
        nb = create_notebook()
        kw_list = [k.strip().lower() for k in keywords.split() if k.strip()]
        folder_val = folder if folder else None
        return _tool_run(nb.search_notes, kw_list, folder_val, detail_level, max_results, max_tokens)

    @mcp.tool
    def lmnotes_list_folder(folder: str = "") -> str:
        """List contents of a folder by reading its index.md.
        
        Args:
            folder: Folder name (optional, defaults to root).
            
        Returns:
            JSON with folder name and list of notes.
        """
        return _tool_run(create_notebook().list_folder, folder if folder else None)

    @mcp.tool
    def lmnotes_list_notes(folder: str = "", 
                            detail_level: int = 1, max_results: int = 50) -> str:
        """List all notes as a clean table view (database-style).
        
        This is the primary navigation tool. Returns structured rows without
        exposing internal file paths. Use this to browse the notebook catalog.
        
        Args:
            folder: Limit to a specific folder (optional, defaults to all)
            detail_level: 0=minimal (id+title+folder), 1=with tags+updated+preview (default)
            max_results: Maximum notes to return (default: 50)
            
        Returns:
            JSON with status and list of note rows.
        """
        nb = create_notebook()
        folder_val = folder if folder else None
        return _tool_run(nb.list_notes, folder_val, detail_level, max_results)

    @mcp.tool
    def lmnotes_list_children(note_id: str) -> str:
        """List all notes that reference the given note as their parent.
        
        Use this to traverse down from a parent note (e.g., find all incidents
        related to a person). The LLM sees parent_id and children_count on every
        note — use list_children to get the full details of each child.
        
        Args:
            note_id: The ID of the parent note
            
        Returns:
            JSON with status, parent_title, children_count, and list of children.
        """
        return _tool_run(create_notebook().list_children, note_id)

    @mcp.tool
    def lmnotes_read_index(folder: str = "") -> str:
        """Read an index.md file for navigation.
        
        Args:
            folder: Folder to read (optional, defaults to root).
            
        Returns:
            JSON with raw content and parsed entries.
        """
        return _tool_run(create_notebook().read_index, folder if folder else None)

    @mcp.tool
    def lmnotes_update_note(note_id: str, title: str = "", tags: str = "", 
                            content: str = "") -> str:
        """Update an existing note's fields. Content is replaced entirely.
        
        To APPEND instead of replace, use lmnotes_append_to_note().
        
        Args:
            note_id: The unique identifier of the note
            title: New title (optional)
            tags: Comma-separated new tags (optional)
            content: New full content (optional)
            
        Returns:
            JSON with status and updated data.
        """
        nb = create_notebook()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        return _tool_run(nb.update_note, note_id, title if title else None, tag_list, content if content else None)

    @mcp.tool
    def lmnotes_append_to_note(note_id: str, addition: str, 
                                separator: str = "\n\n---\n\n") -> str:
        """Append text to an existing note's content.
        
        Args:
            note_id: The unique identifier of the note
            addition: Text to append
            separator: Visual separator (default: "\\n\\n---\\n\\n")
            
        Returns:
            JSON with status and length info.
        """
        return _tool_run(create_notebook().append_to_note, note_id, addition, separator)

    @mcp.tool
    def lmnotes_select_note(note_id: str, pattern: str = "", 
                             mode: str = "exact", start_line: int = 1,
                             end_line: int = -1) -> str:
        """Select/search text within a note for editing.
        
        THIS IS THE FIRST STEP IN THE EDIT WORKFLOW. Call this first to get a
        selection_id, then use edit_selection/delete_selection/append_selection.
        
        Args:
            note_id: The unique identifier of the note
            pattern: Text to search for (required for exact/regex modes)
            mode: "exact" (default), "regex", or "lines"
            start_line: Start line number (for "lines" mode, 1-based)
            end_line: End line number (for "lines" mode, 1-based, -1=last)
            
        Returns:
            JSON with selection_id, occurrences, matched text preview.
        """
        nb = create_notebook()
        return _tool_run(nb.select_note, note_id, pattern if pattern else None, mode, start_line, end_line)

    @mcp.tool
    def lmnotes_edit_selection(selection_id: str, replacement: str = "", 
                                 occurrence: int = 0) -> str:
        """Edit text based on a previous selection. Selection is nullified after use.
        
        COMPLETE EDIT WORKFLOW:
          Step 1: select_note(note_id="...", pattern="...") -> gets selection_id
          Step 2: edit_selection(selection_id=selection_id, replacement="new text", occurrence=N)
          
        Args:
            selection_id: ID returned from lmnotes_select_note (required!)
            replacement: New text to replace matched content with
            occurrence: 0=ALL occurrences, 1=first, 2=second, etc.
            
        Returns:
            JSON with status and edit summary. Selection is nullified after editing.
        """
        return _tool_run(create_notebook().edit_selection, selection_id, replacement if replacement else "", occurrence)

    @mcp.tool
    def lmnotes_delete_selection(selection_id: str, occurrence: int = 0) -> str:
        """Delete text based on a previous selection. Selection is nullified after use.
        
        Args:
            selection_id: ID returned from lmnotes_select_note (required!)
            occurrence: 0=ALL occurrences, 1=first, 2=second, etc.
            
        Returns:
            JSON with status and edit summary. Selection is nullified after editing.
        """
        return _tool_run(create_notebook().delete_selection, selection_id, occurrence)

    @mcp.tool
    def lmnotes_append_selection(selection_id: str, addition: str = "", 
                                   occurrence: int = 0) -> str:
        """Append text after previously selected matches. Selection is nullified after use.
        
        Args:
            selection_id: ID returned from lmnotes_select_note (required!)
            addition: Text to append after each matched occurrence
            occurrence: 0=ALL occurrences, 1=first, 2=second, etc.
            
        Returns:
            JSON with status and edit summary. Selection is nullified after appending.
        """
        return _tool_run(create_notebook().append_selection, selection_id, addition if addition else "", occurrence)

    @mcp.tool
    def lmnotes_delete_note(note_id: str) -> str:
        """Delete a note file and update the parent index.
        
        Args:
            note_id: The unique identifier of the note
            
        Returns:
            JSON with status.
        """
        return _tool_run(create_notebook().delete_note, note_id)

    @mcp.tool
    def lmnotes_get_stats() -> str:
        """Return notebook statistics (counts per folder, total notes).
        
        Returns:
            JSON with status and stats.
        """
        return _tool_run(create_notebook().get_stats)

    @mcp.tool
    def lmnotes_read_system_prompt() -> str:
        """Read system folder prompts. Returns the core prompt and any additional notes.
        
        This should be called at the start of a conversation to load behavioral rules.
        
        Returns:
            JSON with core_prompt and notes list.
        """
        return _tool_run(create_notebook().read_system_prompt)

    @mcp.tool
    def lmnotes_copy_to_references(source_path: str, description: str = "", 
                                     note_id: str = "") -> str:
        """Copy a file from anywhere on the filesystem into the references folder.
        
        Use this when the user asks to reference a file. Creates a companion .md
        description if description is provided.
        
        Args:
            source_path: Full path to the file to copy (e.g., "/home/user/docs/report.pdf")
            description: Markdown description of the file content (optional)
            note_id: Custom ID for the reference note (auto-generated if empty)
            
        Returns:
            JSON with status, destination path, and note data.
        """
        nb = create_notebook()
        return _tool_run(nb.copy_to_references, source_path, description if description else "", note_id if note_id else None)

    @mcp.tool
    def lmnotes_git_log(note_id: str) -> str:
        """Return commit history for a specific note.
        
        Shows all git commits that touched this note's file — useful for
        understanding the evolution of a note over time.
        
        Args:
            note_id: The ID of the note
            
        Returns:
            JSON with status, note_id, and list of commits (hash, date, message).
        """
        return _tool_run(create_notebook().git_log, note_id)

    @mcp.tool
    def lmnotes_git_diff(note_id: str, from_rev: str = "", to_rev: str = "") -> str:
        """Show diff between two revisions of a note.
        
        Args:
            note_id: The ID of the note
            from_rev: Starting revision (default: HEAD~1)
            to_rev: Ending revision (default: HEAD)
            
        Returns:
            JSON with status, revisions, and diff text.
        """
        return _tool_run(create_notebook().git_diff, note_id, from_rev if from_rev else "", to_rev if to_rev else "")

    @mcp.tool
    def lmnotes_git_checkout(note_id: str, revision: str) -> str:
        """Restore a note to a previous git revision.
        
        WARNING: This overwrites the current file content with the specified
        revision's version. A new commit is created after restoring.
        
        Args:
            note_id: The ID of the note
            revision: Git commit hash or reference (e.g., HEAD~1, abc1234)
            
        Returns:
            JSON with status and result info.
        """
        return _tool_run(create_notebook().git_checkout, note_id, revision)

    @mcp.tool
    def lmnotes_manual(tool_name: str = "") -> str:
        """Return documentation for a specific tool or general usage guide.
        
        Call with no args for the full quick-start guide.
        Call with a tool name to get its detailed docstring.
        
        Args:
            tool_name: Name of the tool (e.g., 'edit_selection', 'create_note').
                      If empty, returns the general usage guide.
            
        Returns:
            JSON with status and content string.
        """
        return _tool_run(create_notebook().manual, tool_name if tool_name else "")


# ============================================================================
# CLI Argument Parsing and Main Entry Point
# ============================================================================

def _parse_args():
    """Parse command-line arguments for notebook folder configuration."""
    parser = argparse.ArgumentParser(
        description="lmnotes - File-Based LLM Notebook System (FastMCP)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python lmnotes.py                        # Use ~/.lmnotes/
  python lmnotes.py --folder ~/my_notes/   # Use custom path
  python lmnotes.py --folder /data/notebooks/  # Directory-style path

Folder Categories:
  procedures     - Learned procedures, how-to steps, skills acquired
  reports        - Summaries of completed actions or tasks
  individuals    - Information about people or intelligent beings
  conversations  - Summaries of conversations with the user
  knowledge      - Facts and learned information not tied to a conversation
  system         - Behavioral rules, system prompts, mistake logs
  references     - Files shared by the user that need persistent access
        """
    )
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Path to use as notebook root. If ends with '/', appends .lmnotes/. Default: ~/.lmnotes/"
    )
    return parser.parse_args()


def _setup_folder_from_cli():
    """Set up the notebook folder from CLI arguments and set the global."""
    global _notebook_folder
    args = _parse_args()
    if args.folder:
        nb = Notebook(args.folder)
        _notebook_folder = str(nb.folder)
        return _notebook_folder
    return None


# ============================================================================
# Main entry point
# ============================================================================

if __name__ == "__main__":
    folder_override = _setup_folder_from_cli()
    
    if folder_override:
        print(f"lmnotes - LLM Notebook System")
        print(f"Notebook folder: {folder_override}")
    else:
        print("lmnotes - LLM Notebook System")
        print(f"Default notebook folder: ~/.lmnotes/")
    
    print("=" * 50)
    print("Available tools:")
    print("  lmnotes_init_notebook      - Initialize or reconfigure notebook folder")
    print("  lmnotes_create_note        - Create a new note")
    print("  lmnotes_read_note          - Read a note by ID")
    print("  lmnotes_search_notes       - Search notes with keyword ranking")
    print("  lmnotes_list_notes         - List all notes (database-style table view)")
    print("  lmnotes_list_children      - List children of a parent note")
    print("  lmnotes_list_folder        - List a specific folder's notes")
    print("  lmnotes_read_index         - Read an index.md for navigation")
    print("  lmnotes_update_note        - Update a note's fields")
    print("  lmnotes_append_to_note     - Append content to a note")
    print("  lmnotes_select_note        - Select text within a note (for editing)")
    print("  lmnotes_edit_selection     - Edit previously selected text")
    print("  lmnotes_delete_selection   - Delete previously selected text")
    print("  lmnotes_append_selection   - Append after selected text")
    print("  lmnotes_delete_note        - Delete a note")
    print("  lmnotes_get_stats          - View notebook statistics")
    print("  lmnotes_read_system_prompt - Read system prompts")
    print("  lmnotes_copy_to_references - Copy external file to references")
    print("  lmnotes_git_log          - Show commit history for a note")
    print("  lmnotes_git_diff         - Diff between two revisions of a note")
    print("  lmnotes_git_checkout     - Restore a note to a previous revision")
    print("  lmnotes_manual           - Documentation (usage guide or tool help)")
    print()
    
    # Run the MCP server
    mcp.run()
