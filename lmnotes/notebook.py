"""
notebook.py — Core Notebook class with service composition.

This is the thin entry point that composes services from:
- utils.py: pure utilities
- operations.py: CRUD operations
- edits.py: edit workflow
- versioning.py: git integration

No circular imports — services are instantiated at runtime with `self`.
"""

from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from lmnotes.operations import OperationsService
    from lmnotes.edits import EditService
    from lmnotes.versioning import GitService


class Notebook:
    """File-based LLM notebook management system with modular services."""

    def __init__(self, folder: str = None):
        self.folder = self._resolve_folder(folder)
        # Services are created with `self` — no import-time binding
        # Import here to avoid circular dependency at class definition time
        from lmnotes.operations import OperationsService  # pylint: disable=import-outside-toplevel
        from lmnotes.edits import EditService  # pylint: disable=import-outside-toplevel
        from lmnotes.versioning import GitService  # pylint: disable=import-outside-toplevel
        
        self._operations = OperationsService(self)
        self._edits = EditService(self)
        self._versioning = GitService(self)

    @staticmethod
    def _resolve_folder(folder: str = None) -> Path:
        """Resolve the notebook folder path from argument or default."""
        # pylint: disable=import-outside-toplevel
        from lmnotes.utils import resolve_folder
        return resolve_folder(folder)

    def _ensure_ready(self, subfolder: str = None) -> None:
        """Ensure the notebook root and optionally a subfolder exist."""
        from lmnotes.utils import ensure_ready  # pylint: disable=import-outside-toplevel
        ensure_ready(Path(self.folder), subfolder)

    def _ensure_initialized(self) -> None:
        """Ensure only the notebook root exists."""
        self._ensure_ready()

    # ------------------------------------------------------------------
    # Thin delegation to services
    # ------------------------------------------------------------------

    # -- Git helpers (internal) --

    def _git_init(self) -> bool:
        """Initialize git repo. Safe to call repeatedly.
        
        Returns:
            True if git was initialized, False if it already existed or failed.
        """
        return self._versioning.git_init()

    def _git_commit(self, message: str) -> dict:
        """Stage and commit all changes."""
        return self._versioning.git_commit(message)

    def _git_diff_file(self, filepath: Path, from_rev: str = "HEAD", to_rev: str = None) -> str:
        """Get diff for a file between two revisions."""
        return self._versioning.git_diff_file(filepath, from_rev, to_rev)

    # -- Index management --

    def _write_root_index(self) -> None:
        """Write or update the root index.md."""
        self._operations.write_root_index()

    def _update_root_index(self) -> None:
        """Alias for _write_root_index — used by services for consistency."""
        self._write_root_index()

    def _update_index(self, folder: str) -> None:
        """Update the index.md for a given folder."""
        self._operations.update_index(folder)

    def _count_children(self, parent_id: str) -> int:
        """Count how many notes reference the given ID as their parent."""
        return self._operations._count_children(parent_id)

    # -- Public CRUD operations --

    def init_notebook(self, folder: str = None) -> dict:
        """Initialize or reconfigure the notebook folder location.
        
        This now uses the session-based system. Creates a new session
        (001, 002, etc.) with the given folder.
        """
        return init_session(folder)

    def create_note(self, title: str, content: str, folder: str, 
                    tags: list = None, note_id: str = None,
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
        return self._operations.create_note(title, content, folder, tags, note_id, parent_id)

    def read_note(self, note_id: str, detail_level: int = 1) -> dict:
        """Read a note by its ID."""
        return self._operations.read_note(note_id, detail_level)

    def search_notes(self, keywords: list, folder: str = None,
                     detail_level: int = 1, max_results: int = 20,
                     max_tokens: int = 4096) -> dict:
        """Search notes by keywords with ranking."""
        return self._operations.search_notes(keywords, folder, detail_level, max_results, max_tokens)

    def list_folder(self, folder: str = None) -> dict:
        """List contents of a folder as structured rows."""
        return self._operations.list_folder(folder)

    def list_children(self, note_id: str) -> dict:
        """List all notes that reference the given ID as their parent."""
        return self._operations.list_children(note_id)

    def list_notes(self, folder: str = None, 
                   detail_level: int = 1, max_results: int = 50) -> dict:
        """List all notes as structured rows."""
        return self._operations.list_notes(folder, detail_level, max_results)

    def read_index(self, folder: str = None) -> dict:
        """Read an index.md file for navigation."""
        return self._operations.read_index(folder)

    def get_stats(self) -> dict:
        """Return notebook statistics."""
        return self._operations.get_stats()

    def read_system_prompt(self) -> dict:
        """Read system folder prompts."""
        return self._operations.read_system_prompt()

    # -- Edit operations --

    def update_note(self, note_id: str, title: str = None, tags: list = None,
                    content: str = None) -> dict:
        """Update an existing note's fields."""
        return self._edits.update_note(note_id, title, tags, content)

    def append_to_note(self, note_id: str, addition: str, 
                       separator: str = "\n\n---\n\n") -> dict:
        """Append text to an existing note's content."""
        return self._edits.append_to_note(note_id, addition, separator)

    def select_note(self, note_id: str, pattern: str = None, 
                    mode: str = "exact", start_line: int = 1, 
                    end_line: int = -1) -> dict:
        """Select/search text within a note for editing."""
        return self._edits.select_note(note_id, pattern, mode, start_line, end_line)

    def edit_selection(self, selection_id: str, replacement: str = "", 
                       occurrence: int = 0) -> dict:
        """Edit text based on a previous selection."""
        return self._edits.edit_selection(selection_id, replacement, occurrence)

    def delete_selection(self, selection_id: str, occurrence: int = 0) -> dict:
        """Delete text based on a previous selection."""
        return self._edits.delete_selection(selection_id, occurrence)

    def append_selection(self, selection_id: str, addition: str = "", 
                         occurrence: int = 0) -> dict:
        """Append text after previously selected matches."""
        return self._edits.append_selection(selection_id, addition, occurrence)

    def delete_note(self, note_id: str) -> dict:
        """Delete a note file and update the index."""
        return self._edits.delete_note(note_id)

    def copy_to_references(self, source_path: str, description: str = "", 
                           note_id: str = None) -> dict:
        """Copy a file to the references folder."""
        return self._edits.copy_to_references(source_path, description, note_id)

    # -- Git public tools --

    def git_log(self, note_id: str) -> dict:
        """Return commit history for a specific note file."""
        return self._versioning.git_log(note_id)

    def git_diff(self, note_id: str, from_rev: str = "HEAD", to_rev: str = "") -> dict:
        """Show diff between two revisions of a note."""
        return self._versioning.git_diff(note_id, from_rev, to_rev)

    def git_checkout(self, note_id: str, revision: str) -> dict:
        """Restore a note to a previous git revision."""
        return self._versioning.git_checkout(note_id, revision)

    # -- Manual / Help --

    def manual(self, tool_name: str = "") -> dict:
        """Return documentation for a specific tool or general usage guide."""
        content = self._load_manual_content()
        
        if not tool_name:
            # Return everything before the first ## Tool: section (quick-start guide)
            tool_idx = content.find("## Tool:")
            if tool_idx > 0:
                guide = content[:tool_idx].strip()
            else:
                guide = content.strip()
            return {"status": "success", "tool": None, "content": guide}
        
        # Extract specific tool section
        section = self._extract_tool_section(content, tool_name)
        if section:
            return {"status": "success", "tool": tool_name, "content": section}
        
        # Tool not found — return quick-start guide
        tool_idx = content.find("## Tool:")
        if tool_idx > 0:
            guide = content[:tool_idx].strip()
        else:
            guide = content.strip()
        return {"status": "success", "tool": None, "message": f"Tool '{tool_name}' not found", "content": guide}

    def _load_manual_content(self) -> str:
        """Load manual.md from the lmnotes package directory."""
        try:
            import importlib.resources as pkg_resources  # Python 3.9+
            content = pkg_resources.read_text("lmnotes", "manual.md")
            return content
        except (ImportError, AttributeError):
            # Fallback: read from filesystem relative to this module
            pass
        
        # Fallback: try direct file path
        try:
            manual_path = Path(__file__).parent / "manual.md"
            return manual_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return "# LMNotes\n\nNo manual.md found."

    def _extract_tool_section(self, content: str, tool_name: str) -> str:
        """Extract a single tool section from manual.md content.
        
        Looks for '## Tool: lmnotes_{tool_name}' and returns that section
        up to (but not including) the next '## Tool:' or EOF.
        """
        marker = f"## Tool: lmnotes_{tool_name}\n"
        idx = content.find(marker)
        if idx == -1:
            return ""
        
        # Find the start of this section
        section_start = idx + len(marker)
        
        # Find the next '## Tool:' section (or EOF)
        next_tool = content.find("\n## Tool:", section_start)
        if next_tool == -1:
            section = content[section_start:].strip()
        else:
            section = content[section_start:next_tool].strip()
        
        return section


# ============================================================================
# Global State & Factory
# ============================================================================

# Session management (ephemeral, in-memory only)
# Sessions are 3-digit strings: "000" = no session, "001"-"999" = active sessions
_current_session: str = "000"
_session_counter: int = 0
_sessions: dict = {}  # session_id -> {"folder": str}
MAX_SESSIONS: int = 999

# Backwards compatibility aliases (for existing code that references these)
_initialized = lambda: _current_session != "000"  # Function, not value
_notebook_folder = lambda: _sessions.get(_current_session, {}).get("folder")  # Function

_selection_store: dict = {}
_selection_counter = 0
DEBUG = True
VALID_FOLDERS = ["procedures", "reports", "individuals", "conversations", "knowledge", "system", "references"]


def _generate_session_id() -> str:
    """Generate next 3-digit session ID (001, 002, ..., 999)."""
    global _session_counter  # pylint: disable=global-statement
    _session_counter += 1
    return f"{_session_counter:03d}"


def init_session(folder: str = None) -> dict:
    """Create a new session with the given notebook folder.
    
    Creates a new session (001, 002, etc.) with its own notebook folder.
    Creates the root directory and index.md if they don't exist.
    When max sessions (999) is reached, returns an error.
    Session IDs are 3-digit strings: "001", "002", etc.
    
    Args:
        folder: Notebook folder path. If None, uses ~/.lmnotes/
        
    Returns:
        dict with session_id, folder path, and status
    """
    global _current_session, _sessions  # pylint: disable=global-statement
    
    if _session_counter >= MAX_SESSIONS:
        return {"status": "error", "message": f"Maximum sessions ({MAX_SESSIONS}) reached. Close some sessions first."}
    
    session_id = _generate_session_id()
    # Import resolve_folder from utils
    from lmnotes.utils import resolve_folder, ensure_ready  # pylint: disable=import-outside-toplevel
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    
    resolved_folder = str(resolve_folder(folder))
    root = Path(resolved_folder)
    
    # Create root directory and index.md if they don't exist
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    
    # Create root index.md if it doesn't exist
    index_path = root / "index.md"
    if not index_path.exists():
        from lmnotes.utils import VALID_FOLDERS  # pylint: disable=import-outside-toplevel
        content = "# LMNotes - Root Index\n\n## Categories\n\n"
        for fldr in VALID_FOLDERS:
            folder_path = root / fldr
            if folder_path.exists():
                count = len(list(folder_path.glob("*.md"))) - 1
                content += f"- **{fldr}** ({count} notes)\n"
        content += "\n---\n\n*Use `search_notes` to find specific content.*\n"
        index_path.write_text(content, encoding="utf-8")
    
    _sessions[session_id] = {"folder": resolved_folder}
    _current_session = session_id
    
    return {
        "status": "success",
        "session_id": session_id,
        "folder": resolved_folder,
        "message": f"Session {session_id} created"
    }


def reinit_session(session_id: str, folder: str = None) -> dict:
    """Reinitialize an existing session with a new folder.
    
    Changes the notebook folder for the specified session and switches
    to that session. The session_id remains the same.
    
    Args:
        session_id: The session to reinitialize (e.g., "001")
        folder: New notebook folder path. If None, uses ~/.lmnotes/
        
    Returns:
        dict with session_id, folder path, and status
    """
    global _current_session, _sessions  # pylint: disable=global-statement
    
    if session_id not in _sessions:
        return {"status": "error", "message": f"Session {session_id} not found."}
    
    from lmnotes.utils import resolve_folder  # pylint: disable=import-outside-toplevel
    resolved_folder = str(resolve_folder(folder))
    _sessions[session_id]["folder"] = resolved_folder
    _current_session = session_id
    
    return {
        "status": "success",
        "session_id": session_id,
        "folder": resolved_folder,
        "message": f"Session {session_id} reinitialized to {resolved_folder}"
    }


def get_session() -> dict:
    """Return the current session information.
    
    Returns:
        dict with session_id, folder, and status
    """
    global _current_session, _sessions  # pylint: disable=global-statement
    
    if _current_session == "000" or _current_session not in _sessions:
        return {"status": "no_session", "session_id": "000", "message": "No active session"}
    
    return {
        "status": "success",
        "session_id": _current_session,
        "folder": _sessions[_current_session]["folder"]
    }


def list_sessions() -> dict:
    """List all active sessions with their folders.
    
    Returns:
        dict with sessions list and status
    """
    global _sessions  # pylint: disable=global-statement
    
    sessions_list = []
    for sid, data in sorted(_sessions.items()):
        sessions_list.append({
            "session_id": sid,
            "folder": data["folder"],
            "is_current": sid == _current_session
        })
    
    return {
        "status": "success",
        "current_session": _current_session,
        "total_sessions": len(_sessions),
        "max_sessions": MAX_SESSIONS,
        "sessions": sessions_list
    }


def close_session(session_id: str) -> dict:
    """Close a session and free its slot.
    
    Args:
        session_id: The session to close (e.g., "001")
        
    Returns:
        dict with status and message
    """
    global _current_session, _sessions  # pylint: disable=global-statement
    
    if session_id not in _sessions:
        return {"status": "error", "message": f"Session {session_id} not found."}
    
    del _sessions[session_id]
    
    # If we're closing the current session, reset to 000
    if _current_session == session_id:
        _current_session = "000"
    
    return {
        "status": "success",
        "session_id": session_id,
        "message": f"Session {session_id} closed"
    }


def create_notebook(folder: str = None) -> Optional["Notebook"]:
    """Create a Notebook instance from the current session's folder.
    
    Only returns an instance if a session is active.
    Returns None if no session is active.
    
    A fresh Notebook instance is created each time, but it uses the
    folder path stored in the current session. This is ephemeral —
    if the Python process restarts, all session state is lost.
    """
    global _current_session, _sessions  # pylint: disable=global-statement
    
    # Check if a session is active
    if _current_session == "000" or _current_session not in _sessions:
        return None
    
    # Use the session's folder, or the provided folder override
    if folder is None:
        folder = _sessions[_current_session]["folder"]
    
    return Notebook(folder)
