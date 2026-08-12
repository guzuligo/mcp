"""
operations.py — CRUD operations service for lmnotes.

Handles init, create, read, search, list operations.
Imports Notebook only for TYPE_CHECKING to avoid circular imports.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, TYPE_CHECKING
import re

if TYPE_CHECKING:
    from lmnotes.notebook import Notebook


class OperationsService:
    """Service for CRUD operations on the notebook."""

    def __init__(self, notebook: "Notebook"):
        self.nb = notebook

    def require_initialized(self) -> Optional[dict]:
        """Return an error dict if the notebook has not been initialized, else None."""
        # pylint: disable=import-outside-toplevel
        from lmnotes.notebook import _current_session, _sessions
        
        # Check if a session is active
        if _current_session == "000" or _current_session not in _sessions:
            return {"status": "error", "message": "Notebook not initialized. Call lmnotes_init_notebook first."}
        
        return None

    def write_root_index(self) -> None:
        """Write or update the root index.md."""
        root = Path(self.nb.folder)
        from lmnotes.utils import VALID_FOLDERS  # pylint: disable=import-outside-toplevel
        content = "# LMNotes - Root Index\n\n## Categories\n\n"
        for folder in VALID_FOLDERS:
            folder_path = root / folder
            if folder_path.exists():
                count = len(list(folder_path.glob("*.md"))) - 1
                content += f"- **{folder}** ({count} notes)\n"
        content += "\n---\n\n*Use `search_notes` to find specific content.*\n"
        (root / "index.md").write_text(content, encoding="utf-8")

    def update_index(self, folder: str) -> None:
        """Update the index.md for a given folder."""
        root = Path(self.nb.folder)
        folder_path = root / folder
        if not folder_path.exists():
            return
        
        from lmnotes.utils import parse_frontmatter  # pylint: disable=import-outside-toplevel
        notes = []
        for f in sorted(folder_path.glob("*.md")):
            if f.name == "index.md":
                continue
            fm, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
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
        
        (folder_path / "index.md").write_text("\n".join(lines), encoding="utf-8")

    def _count_children(self, parent_id: str) -> int:
        """Count how many notes reference the given ID as their parent."""
        root = Path(self.nb.folder)
        from lmnotes.utils import parse_frontmatter, VALID_FOLDERS  # pylint: disable=import-outside-toplevel
        count = 0
        for folder_name in VALID_FOLDERS:
            search_path = root / folder_name
            if not search_path.exists():
                continue
            for note_file in sorted(search_path.glob("*.md")):
                if note_file.name == "index.md":
                    continue
                fm, _ = parse_frontmatter(note_file.read_text(encoding="utf-8"))
                if fm.get("parent_id") == parent_id:
                    count += 1
        return count

    # Note: init_notebook is now handled by init_session() in notebook.py
    # This old method is kept for backwards compatibility but is not used.

    def create_note(self, title: str, content: str, folder: str, 
                    tags: List[str] = None, note_id: str = None,
                    parent_id: str = None) -> dict:
        """Create a new note file with the given title, content, and metadata.
        
        Args:
            title: Note title (used in frontmatter and filename slug)
            content: Markdown body content
            folder: Target folder (procedures, reports, etc.)
            tags: Optional list of tag strings
            note_id: Optional custom timestamp ID
            parent_id: Optional parent note ID for hierarchical links
        """
        # pylint: disable=import-outside-toplevel,global-statement
        import lmnotes as _lmn
        from lmnotes.utils import (  # noqa: F401
            generate_id, make_slug, build_frontmatter, find_note_file,
            ensure_ready, VALID_FOLDERS
        )
        
        err = self.require_initialized()
        if err:
            return err
        if folder not in VALID_FOLDERS:
            return {"status": "error", "message": f"Invalid folder '{folder}'. Must be one of: {VALID_FOLDERS}"}
        
        if parent_id:
            found = find_note_file(self.nb, parent_id, "")
            if not found:
                return {"status": "error", "message": f"Parent note with ID '{parent_id}' not found"}
        
        # Note: self.nb.folder is already set from create_notebook() which uses the session's folder
        # No need to check _notebook_folder lambda anymore
        ensure_ready(Path(self.nb.folder), subfolder=folder)
        
        now = datetime.now(timezone.utc)
        ts_id = note_id if note_id else generate_id(now)
        slug = make_slug(title, ts_id)
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
        
        frontmatter = build_frontmatter(data)
        full_content = f"{frontmatter}\n\n{content}" if content else frontmatter
        
        root = Path(self.nb.folder)
        filepath = root / folder / filename
        filepath.write_text(full_content, encoding="utf-8")
        
        self.update_index(folder)
        self.write_root_index()
        # Ensure git is initialized before committing
        self.nb._git_init()
        self.nb._git_commit(f"Add note: {title} ({ts_id})")
        
        result = {
            "status": "success",
            "id": ts_id,
            "title": title,
            "folder": folder,
            "tags": tags or [],
            "created": data["created"],
            "updated": data["updated"]
        }
        if _lmn.DEBUG:
            result["filepath"] = str(filepath)
        return result

    def read_note(self, note_id: str, detail_level: int = 1) -> dict:
        """Read a note by its ID."""
        # pylint: disable=import-outside-toplevel
        from lmnotes.utils import parse_frontmatter, find_note_file, read_note_file  # noqa: F401
        
        err = self.require_initialized()
        if err:
            return err
        filepath = find_note_file(self.nb, note_id, "")
        if not filepath:
            root = Path(self.nb.folder)
            from lmnotes.utils import VALID_FOLDERS  # noqa: F401
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
        
        note = read_note_file(filepath)
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
        
        if note.get("parent_id"):
            result["parent_id"] = note["parent_id"]
            parent_file = find_note_file(self.nb, note["parent_id"], "")
            if parent_file:
                parent_fm, _ = parse_frontmatter(parent_file.read_text(encoding="utf-8"))
                result["parent_title"] = parent_fm.get("title", "Untitled")
        
        result["children_count"] = self._count_children(note_id)
        
        if detail_level == 0:
            result["content"] = None
        elif detail_level == 1:
            lines = [l for l in note.get("body", "").split("\n") if l.strip()]
            result["preview_line"] = lines[0] if lines else ""
            result["content"] = None
        elif detail_level == 2:
            body = note.get("body", "")
            paragraphs = re.split(r'\n\n+', body)
            preview = "\n\n".join(paragraphs[:3]) if paragraphs else ""
            result["preview"] = preview[:1000]
            result["content"] = None
        elif detail_level == 3:
            result["content"] = note.get("body", "")
        
        return result

    def search_notes(self, keywords: List[str], folder: str = None,
                     detail_level: int = 1, max_results: int = 20,
                     max_tokens: int = 4096) -> dict:
        """Search notes by keywords with ranking based on match count."""
        # pylint: disable=import-outside-toplevel
        from lmnotes.utils import parse_frontmatter, read_note_file, VALID_FOLDERS  # noqa: F401
        
        err = self.require_initialized()
        if err:
            return err
        root = Path(self.nb.folder)
        if not root.exists():
            return {"status": "error", "message": "Notebook not initialized. Call init_notebook first."}
        
        search_paths = []
        folders_to_search = [folder] if folder else VALID_FOLDERS
        
        for f in folders_to_search:
            p = root / f if f else root
            if p.exists():
                search_paths.append(p)
        
        results = []
        
        for path in search_paths:
            for note_file in path.glob("*.md"):
                if note_file.name == "index.md":
                    continue
                
                note = read_note_file(note_file)
                if not note:
                    continue
                
                text_to_search = f"{note.get('title', '')} {note.get('body', '')} {' '.join(str(t) for t in note.get('tags', []))}"
                text_lower = text_to_search.lower()
                
                matched = [kw for kw in keywords if kw.lower() in text_lower]
                if matched:
                    results.append((len(matched), matched, note))
        
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
        
        sorted_groups = sorted(groups.items(), key=lambda x: x[0], reverse=True)
        
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
                    pass
                elif detail_level == 1:
                    lines = [l for l in note_data.get("body", "").split("\n") if l.strip()]
                    result_item["preview_line"] = lines[0] if lines else ""
                elif detail_level == 2:
                    paragraphs = re.split(r'\n\n+', note_data.get("body", ""))
                    preview = "\n\n".join(paragraphs[:3]) if paragraphs else ""
                    result_item["preview"] = preview[:1000]
                elif detail_level == 3:
                    content = note_data.get("body", "")
                    estimated_tokens = len(content) // 4
                    if current_tokens + estimated_tokens > max_tokens and final_result:
                        result_item["_excluded"] = True
                        continue
                    result_item["content"] = content
                    current_tokens += estimated_tokens
                
                group_items.append(result_item)
            
            final_result.extend(group_items)
        
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
        """List contents of a folder as structured rows."""
        # pylint: disable=import-outside-toplevel
        import lmnotes as _lmn
        from lmnotes.utils import parse_frontmatter, VALID_FOLDERS  # noqa: F401
        
        err = self.require_initialized()
        if err:
            return err
        root = Path(self.nb.folder)
        
        if not folder or folder == "":
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
                fm, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
                note_row = {
                    "id": fm.get("id", ""),
                    "title": fm.get("title", "Untitled"),
                    "tags": fm.get("tags", []),
                    "created": fm.get("created", "")[:10] if fm.get("created") else "",
                    "updated": fm.get("updated", "")[:10] if fm.get("updated") else ""
                }
                if _lmn.DEBUG:
                    note_row["filepath"] = str(f)
                notes.append(note_row)
            
            return {
                "status": "success",
                "folder": folder,
                "notes": notes,
                "total": len(notes)
            }

    def list_children(self, note_id: str) -> dict:
        """List all notes that reference the given ID as their parent."""
        # pylint: disable=import-outside-toplevel
        from lmnotes.utils import parse_frontmatter, find_note_file, VALID_FOLDERS  # noqa: F401
        
        err = self.require_initialized()
        if err:
            return err
        
        parent_file = find_note_file(self.nb, note_id, "")
        if not parent_file:
            root = Path(self.nb.folder)
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
        
        fm, _ = parse_frontmatter(parent_file.read_text(encoding="utf-8"))
        children = []
        root = Path(self.nb.folder)
        
        for folder_name in VALID_FOLDERS:
            search_path = root / folder_name
            if not search_path.exists():
                continue
            for note_file in sorted(search_path.glob("*.md")):
                if note_file.name == "index.md":
                    continue
                child_fm, _ = parse_frontmatter(note_file.read_text(encoding="utf-8"))
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
        """List all notes as structured rows (database-style table view)."""
        # pylint: disable=import-outside-toplevel
        from lmnotes.utils import parse_frontmatter, find_note_file, VALID_FOLDERS  # noqa: F401
        
        err = self.require_initialized()
        if err:
            return err
        root = Path(self.nb.folder)
        
        folders_to_list = [folder] if folder else VALID_FOLDERS
        
        all_notes = []
        for f_name in folders_to_list:
            folder_path = root / f_name
            if not folder_path.exists():
                continue
            for note_file in sorted(folder_path.glob("*.md")):
                if note_file.name == "index.md":
                    continue
                fm, body = parse_frontmatter(note_file.read_text(encoding="utf-8"))
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
                
                if fm.get("parent_id"):
                    note_row["parent_id"] = fm["parent_id"]
                    parent_file = find_note_file(self.nb, fm["parent_id"], "")
                    if parent_file:
                        parent_fm, _ = parse_frontmatter(parent_file.read_text(encoding="utf-8"))
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
        """Read an index.md file for navigation."""
        err = self.require_initialized()
        if err:
            return err
        root = Path(self.nb.folder)
        
        if not folder or folder == "":
            index_path = root / "index.md"
        else:
            index_path = root / folder / "index.md"
        
        if not index_path.exists():
            return {"status": "error", "message": f"Index not found for '{folder or 'root'}'"}
        
        content = index_path.read_text(encoding="utf-8")
        
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

    def get_stats(self) -> dict:
        """Return notebook statistics."""
        # pylint: disable=import-outside-toplevel
        import lmnotes as _lmn
        from lmnotes.utils import VALID_FOLDERS  # noqa: F401
        
        err = self.require_initialized()
        if err:
            return err
        root = Path(self.nb.folder)
        
        stats = {"total_notes": 0, "folders": {}}
        
        for folder in VALID_FOLDERS:
            folder_path = root / folder
            if folder_path.exists():
                count = len(list(folder_path.glob("*.md"))) - 1
                stats["folders"][folder] = count
                stats["total_notes"] += count
        
        result = {"status": "success", "total_notes": stats["total_notes"], "folders": stats["folders"]}
        if _lmn.DEBUG:
            result["notebook_folder"] = str(root)
        return result

    def read_system_prompt(self) -> dict:
        """Read system folder prompts."""
        err = self.require_initialized()
        if err:
            return err
        root = Path(self.nb.folder)
        system_path = root / "system"
        
        result = {"status": "success", "core_prompt": None, "notes": []}
        
        if not system_path.exists():
            return result
        
        from lmnotes.utils import read_note_file  # pylint: disable=import-outside-toplevel
        core_file = system_path / "000000000000_core_prompt.md"
        if core_file.exists():
            result["core_prompt"] = core_file.read_text(encoding="utf-8")
        
        from lmnotes.utils import VALID_FOLDERS  # pylint: disable=import-outside-toplevel
        for f in sorted(system_path.glob("*.md")):
            if f.name == "index.md" or f.name == "000000000000_core_prompt.md":
                continue
            note = read_note_file(f)
            if note:
                result["notes"].append({
                    "id": note.get("id", ""),
                    "title": note.get("title", f.name),
                    "content": note.get("body", "")
                })
        
        return result