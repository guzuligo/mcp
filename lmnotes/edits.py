"""
edits.py — Edit workflow service for lmnotes.

Handles update, append, select/edit workflow operations.
Imports Notebook only for TYPE_CHECKING to avoid circular imports.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, TYPE_CHECKING
import re

if TYPE_CHECKING:
    from lmnotes.notebook import Notebook


class EditService:
    """Service for edit workflow operations."""

    def __init__(self, notebook: "Notebook"):
        self.nb = notebook

    def require_initialized(self) -> Optional[dict]:
        """Return an error dict if the notebook has not been initialized, else None."""
        # pylint: disable=import-outside-toplevel
        import lmnotes as _lmn
        
        if not _lmn._initialized:
            return {"status": "error", "message": "Notebook not initialized. Call lmnotes_init_notebook first."}
        
        if not getattr(_lmn, '_initialized', False):
            return {"status": "error", "message": "Notebook not initialized. Call lmnotes_init_notebook first."}
        
        return None

    def update_note(self, note_id: str, title: str = None, tags: List[str] = None,
                    content: str = None) -> dict:
        """Update an existing note's fields. Content is replaced entirely."""
        # pylint: disable=import-outside-toplevel
        import lmnotes as _lmn
        from lmnotes.utils import (  # noqa: F401
            parse_frontmatter, build_frontmatter, find_note_file, read_note_file
        )
        
        err = self.require_initialized()
        if err:
            return err
        filepath = find_note_file(self.nb, note_id, "")
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = read_note_file(filepath)
        if not note:
            return {"status": "error", "message": f"Could not read note: {filepath}"}
        
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
        
        frontmatter = build_frontmatter(data)
        full_content = f"{frontmatter}\n\n{new_content}" if new_content else frontmatter
        
        filepath.write_text(full_content, encoding="utf-8")
        self.nb._update_index(note.get("folder", ""))
        self.nb._update_root_index()
        
        self.nb._git_commit(f"Update note {note_id}: {'title' if title else 'tags/content'} changed")
        
        return {
            "status": "success",
            "id": note_id,
            "title": new_title,
            "tags": new_tags,
            "updated": now.isoformat()
        }

    def append_to_note(self, note_id: str, addition: str, 
                       separator: str = "\n\n---\n\n") -> dict:
        """Append text to an existing note's content."""
        # pylint: disable=import-outside-toplevel
        import lmnotes as _lmn
        from lmnotes.utils import (  # noqa: F401
            build_frontmatter, find_note_file, read_note_file
        )
        
        err = self.require_initialized()
        if err:
            return err
        filepath = find_note_file(self.nb, note_id, "")
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = read_note_file(filepath)
        if not note:
            return {"status": "error", "message": f"Could not read note: {filepath}"}
        
        original_content = note.get("body", "")
        new_content = original_content + separator + addition
        
        data = {
            "id": note_id,
            "title": note.get("title", ""),
            "folder": note.get("folder", ""),
            "tags": note.get("tags", []),
            "created": note.get("created", ""),
            "updated": datetime.now(timezone.utc).isoformat()
        }
        
        frontmatter = build_frontmatter(data)
        full_content = f"{frontmatter}\n\n{new_content}"
        
        filepath.write_text(full_content, encoding="utf-8")
        self.nb._update_index(note.get("folder", ""))
        self.nb._update_root_index()
        
        self.nb._git_commit(f"Append to note {note_id}: +{len(addition)} chars")
        
        return {
            "status": "success",
            "id": note_id,
            "original_length": len(original_content),
            "new_length": len(new_content),
            "separator_used": separator
        }

    def select_note(self, note_id: str, pattern: str = None, 
                    mode: str = "exact", start_line: int = 1, 
                    end_line: int = -1) -> dict:
        """Select/search text within a note for editing."""
        # pylint: disable=import-outside-toplevel
        from lmnotes.utils import find_note_file, read_note_file  # noqa: F401
        
        err = self.require_initialized()
        if err:
            return err
        filepath = find_note_file(self.nb, note_id, "")
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = read_note_file(filepath)
        if not note:
            return {"status": "error", "message": f"Could not read note: {filepath}"}
        
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
                pos = 0
                while True:
                    idx = body.find(pattern, pos)
                    if idx == -1:
                        break
                    match_positions.append({"start": idx, "end": idx + len(pattern)})
                    occurrences += 1
                    pos = idx + 1
                
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
        
        # pylint: disable=import-outside-toplevel,global-statement
        import lmnotes as _lmn
        _lmn._selection_counter += 1
        selection_id = f"sel_{note_id}_{_lmn._selection_counter}"
        
        _lmn._selection_store[selection_id] = {
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
            "match_positions": match_positions[:5]
        }

    def _apply_selection_edit(self, selection_id: str, replacement: str = None,
                              addition=None, occurrence: int = 0) -> dict:
        """Apply an edit to a previously selected note. Internal helper."""
        # pylint: disable=import-outside-toplevel,global-statement
        import lmnotes as _lmn
        from lmnotes.utils import (  # noqa: F401
            parse_frontmatter, build_frontmatter, find_note_file,
            read_note_file
        )
        
        if selection_id not in _lmn._selection_store:
            return {"status": "error", "message": "This selection has already been used to edit the note. Call select_note again to make further edits."}
        
        sel = _lmn._selection_store.pop(selection_id)
        filepath = Path(sel["filepath"])
        
        body = sel["body_snapshot"]
        mode = sel["mode"]
        pattern = sel.get("pattern")
        
        try:
            changes_made = 0
            if replacement is not None:
                if mode == "exact" and pattern:
                    positions = sel["match_positions"]
                    
                    if occurrence == 0:
                        new_body = body.replace(pattern, replacement)
                        changes_made = len(positions)
                    else:
                        if occurrence <= len(positions):
                            pos = positions[occurrence - 1]
                            new_body = body[:pos["start"]] + replacement + body[pos["end"]:]
                            changes_made = 1
                        else:
                            return {"status": "error", "message": f"Occurrence {occurrence} not found (only {len(positions)} matches)"}
                            
                elif mode == "regex" and pattern:
                    if occurrence == 0:
                        new_body = re.sub(pattern, replacement, body)
                        changes_made = len(re.findall(pattern, body))
                    else:
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
                if mode == "exact" and pattern:
                    positions = sel["match_positions"]
                    
                    if occurrence == 0:
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
                elif mode == "regex" and pattern:
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
                # Deletion
                if mode == "exact" and pattern:
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
                elif mode == "regex" and pattern:
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
        
        note_id = sel["note_id"]
        note = find_note_file(self.nb, note_id, "")
        if note:
            note_data = read_note_file(note)
        else:
            note_data = None
        
        if not note_data:
            # Try reading directly
            if filepath.exists():
                note_data = read_note_file(filepath)
        
        if not note_data:
            return {"status": "error", "message": f"Could not read note for editing"}
        
        data = {
            "id": note_id,
            "title": note_data.get("title", ""),
            "folder": note_data.get("folder", ""),
            "tags": note_data.get("tags", []),
            "created": note_data.get("created", ""),
            "updated": datetime.now(timezone.utc).isoformat()
        }
        
        frontmatter = build_frontmatter(data)
        full_content = f"{frontmatter}\n\n{new_body}"
        filepath.write_text(full_content, encoding="utf-8")
        
        folder = note_data.get("folder", "")
        self.nb._update_index(folder)
        self.nb._update_root_index()
        
        commit_result = self.nb._git_commit(f"Edit note {note_id}: {'replace' if replacement is not None else 'delete'}")
        
        # Get diff using the versioning service's git_diff method
        # This handles note ID lookup and returns {"status", "from_rev", "to_rev", "diff"}
        diff_text = ""
        try:
            commit_hash = commit_result.get("commit_hash")
            if commit_hash:
                diff_result = self.nb._versioning.git_diff(note_id, from_rev=commit_hash)
                if diff_result.get("status") == "success":
                    diff_text = diff_result.get("diff", "") or ""
        except Exception:
            diff_text = ""
        
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
        """Edit text based on a previous selection. Selection is nullified after editing."""
        return self._apply_selection_edit(selection_id, replacement=replacement if replacement != "" else None, occurrence=occurrence)

    def delete_selection(self, selection_id: str, occurrence: int = 0) -> dict:
        """Delete text based on a previous selection. Selection is nullified after editing."""
        return self._apply_selection_edit(selection_id, addition=None, occurrence=occurrence)

    def append_selection(self, selection_id: str, addition: str = "", 
                         occurrence: int = 0) -> dict:
        """Append text after previously selected matches. Selection is nullified."""
        return self._apply_selection_edit(selection_id, addition=addition if addition != "" else "", occurrence=occurrence)

    def delete_note(self, note_id: str) -> dict:
        """Delete a note file and update the index."""
        # pylint: disable=import-outside-toplevel
        import lmnotes as _lmn
        from lmnotes.utils import find_note_file, read_note_file  # noqa: F401
        
        err = self.require_initialized()
        if err:
            return err
        filepath = find_note_file(self.nb, note_id, "")
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}
        
        note = read_note_file(filepath)
        folder = note.get("folder", "") if note else ""
        
        filepath.unlink()
        
        if folder:
            self.nb._update_index(folder)
        self.nb._update_root_index()
        
        self.nb._git_commit(f"Delete note {note_id}")
        
        result = {"status": "success", "id": note_id}
        if _lmn.DEBUG:
            result["deleted_file"] = str(filepath)
        return result

    def copy_to_references(self, source_path: str, description: str = "", 
                           note_id: str = None) -> dict:
        """Copy a file to the references folder and optionally create a description."""
        # pylint: disable=import-outside-toplevel
        import lmnotes as _lmn
        import shutil
        from lmnotes.utils import (  # noqa: F401
            generate_id, make_slug, build_frontmatter, read_note_file
        )
        
        err = self.require_initialized()
        if err:
            return err
        
        root = Path(self.nb.folder)
        dest_folder = root / "references"
        dest_folder.mkdir(parents=True, exist_ok=True)
        
        now = datetime.now(timezone.utc)
        ts_id = note_id if note_id else generate_id(now)
        src = Path(source_path).expanduser()
        
        if not src.exists():
            return {"status": "error", "message": f"Source file not found: {source_path}"}
        
        original_name = src.name
        base_name = src.stem.replace(".", "_")
        
        dest_file = dest_folder / f"{ts_id}_{base_name}{src.suffix}"
        counter = 1
        while dest_file.exists():
            dest_file = dest_folder / f"{ts_id}_{base_name}_{counter}{src.suffix}"
            counter += 1
        
        shutil.copy2(src, dest_file)
        
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
            frontmatter = build_frontmatter(note_data)
            md_content = f"{frontmatter}\n\n{description}"
            
            md_id = ts_id
            md_file = dest_folder / f"{md_id}_{base_name}.md"
            counter = 1
            while md_file.exists():
                md_file = dest_folder / f"{md_id}_{base_name}_{counter}.md"
                counter += 1
            
            md_file.write_text(md_content, encoding="utf-8")
        
        self.nb._update_index("references")
        self.nb._update_root_index()
        
        result = {
            "status": "success",
            "id": md_note_id,
            "filename": original_name,
            "note_created": bool(description)
        }
        if _lmn.DEBUG:
            result["source_path"] = str(src)
            result["destination_path"] = str(dest_file)
        return result