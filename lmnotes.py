"""
lmnotes - File-Based LLM Notebook System (FastMCP)

A pure file-based notebook management system using hierarchical index.md files
for navigation and search. All notes are stored as human-readable markdown files.

This is the main entry point for the MCP server. The core Notebook class and
business logic are in the lmnotes/ package (modular architecture).

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
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    from fastmcp import FastMCP
except ImportError:
    FastMCP = None

# ============================================================================
# Import from modular package — GLOBALS ARE REFERENCES, NOT COPIES
# ============================================================================
# All global state lives in notebook.py. We import by reference so there is
# exactly ONE copy of each variable. When init_session sets a session in
# notebook.py, lmnotes.py sees the same change automatically.

from lmnotes import notebook as _nb  # noqa: E402
from lmnotes.notebook import (
    Notebook,
    create_notebook,
    init_session,
    reinit_session,
    get_session,
    list_sessions,
    close_session,
    DEBUG,
    VALID_FOLDERS,
    MAX_SESSIONS,
)

# LIVE REFERENCES to notebook.py's module variables (functions, not values)
_initialized = _nb._initialized  # lambda function
_notebook_folder = _nb._notebook_folder  # lambda function
_selection_store = _nb._selection_store
_selection_counter = _nb._selection_counter

_mcp_instance = None


def _get_mcp() -> FastMCP:
    """Get or create the MCP instance."""
    global _mcp_instance
    if _mcp_instance is None:
        _mcp_instance = FastMCP("lmnotes")
    return _mcp_instance


def _tool_run(func, *args, **kwargs) -> str:
    """Run a Notebook method and return JSON. Handles None (not initialized)."""
    try:
        result = func(*args, **kwargs)
    except AttributeError as e:
        # create_notebook() returns None when not initialized,
        # and .method() on None raises AttributeError
        if not _initialized:
            return json.dumps(
                {"status": "error", "message": "Notebook not initialized. Call lmnotes_init_notebook first."},
                indent=2
            )
        return json.dumps(
            {"status": "error", "message": f"Tool error: {e}"},
            indent=2
        )
    if result is None:
        if not _initialized:
            return json.dumps(
                {"status": "error", "message": "Notebook not initialized. Call lmnotes_init_notebook first."},
                indent=2
            )
        return json.dumps({"status": "error", "message": "Not initialized"}, indent=2)
    return json.dumps(result, indent=2)


# ============================================================================
# MCP Server Setup
# ============================================================================

mcp = _get_mcp()


# ============================================================================
# MCP Tools (27 tools with full docstrings)
# ============================================================================

@mcp.tool
def lmnotes_init_notebook(folder: str = "") -> str:
    """Initialize a new session with the given notebook folder.

    Creates a new session (001, 002, etc.) with its own notebook folder.
    All subsequent tool calls will use this session's folder until
    another session is created or the session is reinitialized.
    
    Session IDs are 3-digit strings (001-999). Max 999 concurrent sessions.
    Sessions are ephemeral (lost when Python process restarts).

    Args:
        folder: Path to use as notebook root. If ends with '/', appends .lmnotes/.
                If empty or omitted, uses default ~/.lmnotes/

    Returns:
        JSON with status, session_id, and resolved path.
    """
    result = init_session(folder if folder else None)
    return json.dumps(result, indent=2)


@mcp.tool
def lmnotes_reinit_session(session_id: str, folder: str = "") -> str:
    """Reinitialize an existing session with a new folder.

    Changes the notebook folder for the specified session and switches
    to that session. The session_id remains the same.

    Args:
        session_id: The session to reinitialize (e.g., "001", "042")
        folder: New notebook folder path. If empty, uses default ~/.lmnotes/

    Returns:
        JSON with status, session_id, and new folder path.
    """
    result = reinit_session(session_id, folder if folder else None)
    return json.dumps(result, indent=2)


@mcp.tool
def lmnotes_get_session() -> str:
    """Return the current session information.

    Returns the active session ID and its associated notebook folder.

    Returns:
        JSON with session_id, folder, and status.
    """
    result = get_session()
    return json.dumps(result, indent=2)


@mcp.tool
def lmnotes_list_sessions() -> str:
    """List all active sessions with their folders.

    Returns a list of all sessions (001-999) with their notebook folders
    and indicates which one is currently active.

    Returns:
        JSON with sessions list, current session, and counts.
    """
    result = list_sessions()
    return json.dumps(result, indent=2)


@mcp.tool
def lmnotes_close_session(session_id: str) -> str:
    """Close a session and free its slot.

    Removes the specified session. If the closed session was the active one,
    the current session is reset to "000" (no session).

    Args:
        session_id: The session to close (e.g., "001", "042")

    Returns:
        JSON with status and message.
    """
    result = close_session(session_id)
    return json.dumps(result, indent=2)


@mcp.tool
def lmnotes_create_note(title: str, content: str, folder: str, tags: str = "",
                        note_id: str = "", parent_id: str = "") -> str:
    """Create a new note file with the given title, content, and metadata.

    Creates a markdown file with YAML-like front-matter in the specified folder.
    Automatically commits to git if initialized.

    Args:
        title: Note title (used in frontmatter and filename slug)
        content: Markdown body content
        folder: Target folder (procedures, reports, individuals, conversations,
                knowledge, system, references)
        tags: Comma-separated list of tags (e.g., "git, rebase, conflict")
        note_id: Optional custom timestamp ID (auto-generated if empty)
        parent_id: Optional parent note ID for hierarchical links

    Returns:
        JSON with status, note ID, title, folder, tags, and timestamps.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    return _tool_run(
        lambda: create_notebook().create_note(title, content, folder, tag_list,
                                               note_id if note_id else None,
                                               parent_id if parent_id else None)
    )


@mcp.tool
def lmnotes_read_note(note_id: str, detail_level: int = 1) -> str:
    """Read a note by its ID.

    Args:
        note_id: The timestamp-based ID of the note (e.g., "260729165500")
        detail_level: 0=minimal (id+title only), 1=one line preview (default),
                     2=paragraph (~5 lines), 3=full content

    Returns:
        JSON with note metadata and content (depending on detail_level).
    """
    return _tool_run(lambda: create_notebook().read_note(note_id, detail_level))


@mcp.tool
def lmnotes_search_notes(keywords: str, folder: str = "", detail_level: int = 1,
                         max_results: int = 20, max_tokens: int = 4096) -> str:
    """Search notes by keywords with ranking based on match count.

    Searches across all notes (or a specific folder) and ranks results
    by how many keywords match. Even poor keyword choices return results
    with clear match labels.

    Args:
        keywords: Space-separated keywords to search for
        folder: Optional folder to limit search to (empty = all folders)
        detail_level: 0=minimal, 1=preview (default), 2=paragraph, 3=full
        max_results: Maximum number of results to return (default: 20)
        max_tokens: Maximum token budget for detail_level=3 results

    Returns:
        JSON with grouped results ranked by match count.
    """
    kw_list = [k.strip() for k in keywords.split() if k.strip()]
    return _tool_run(
        lambda: create_notebook().search_notes(kw_list, folder if folder else None,
                                                detail_level, max_results, max_tokens)
    )


@mcp.tool
def lmnotes_list_notes(folder: str = "", detail_level: int = 1, max_results: int = 50) -> str:
    """List all notes as structured rows (database-style table view).

    Returns a clean table view of all notes without internal file paths.
    Shows ID, title, folder, tags, last updated, and children count.

    Args:
        folder: Optional folder to limit to (empty = all folders)
        detail_level: 0=no preview, 1=show preview line
        max_results: Maximum number of notes to return

    Returns:
        JSON with structured note rows.
    """
    return _tool_run(lambda: create_notebook().list_notes(folder if folder else None,
                                                           detail_level, max_results))


@mcp.tool
def lmnotes_list_children(note_id: str) -> str:
    """List all child notes under a parent note.

    Finds all notes that reference the given note_id as their parent_id.
    Useful for exploring hierarchical note structures.

    Args:
        note_id: The parent note ID

    Returns:
        JSON with parent title and list of child notes.
    """
    return _tool_run(lambda: create_notebook().list_children(note_id))


@mcp.tool
def lmnotes_list_folder(folder: str = "") -> str:
    """List contents of a specific folder as structured rows.

    Args:
        folder: Folder name (procedures, reports, individuals, conversations,
                knowledge, system, references). If empty, lists root categories.

    Returns:
        JSON with folder contents or root category summary.
    """
    return _tool_run(lambda: create_notebook().list_folder(folder if folder else None))


@mcp.tool
def lmnotes_read_index(folder: str = "") -> str:
    """Read an index.md file for navigation.

    Returns the contents and parsed entries of an index file.

    Args:
        folder: Folder name (empty = root index)

    Returns:
        JSON with index content and parsed entries.
    """
    return _tool_run(lambda: create_notebook().read_index(folder if folder else None))


@mcp.tool
def lmnotes_update_note(note_id: str, title: str = "", tags: str = "",
                        content: str = "") -> str:
    """Update an existing note's fields. Content is replaced entirely.

    Args:
        note_id: The ID of the note to update
        title: New title (empty = keep current)
        tags: Comma-separated tags (empty = keep current)
        content: New content (empty = keep current)

    Returns:
        JSON with updated note metadata.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    return _tool_run(
        lambda: create_notebook().update_note(note_id,
                                               title if title else None,
                                               tag_list,
                                               content if content else None)
    )


@mcp.tool
def lmnotes_append_to_note(note_id: str, addition: str,
                           separator: str = "\n\n---\n\n") -> str:
    """Append text to an existing note's content.

    Args:
        note_id: The ID of the note to append to
        addition: Text to append
        separator: Separator between original and new content (default: blank line + horizontal rule)

    Returns:
        JSON with original and new content lengths.
    """
    return _tool_run(lambda: create_notebook().append_to_note(note_id, addition, separator))


@mcp.tool
def lmnotes_select_note(note_id: str, pattern: str = "", mode: str = "exact",
                        start_line: int = 1, end_line: int = -1) -> str:
    """Select/search text within a note for editing.

    Returns a selection_id that can be used with edit_selection,
    delete_selection, or append_selection. Selection is single-use.

    Args:
        note_id: The ID of the note to search
        pattern: Text pattern to find (required for exact/regex modes)
        mode: "exact" (literal), "regex", or "lines" (start_line/end_line)
        start_line: Starting line number (for lines mode)
        end_line: Ending line number, -1 = last line (for lines mode)

    Returns:
        JSON with selection_id, matched text preview, and occurrence count.
    """
    return _tool_run(
        lambda: create_notebook().select_note(note_id, pattern if pattern else None,
                                               mode, start_line, end_line)
    )


@mcp.tool
def lmnotes_edit_selection(selection_id: str, replacement: str = "",
                           occurrence: int = 0) -> str:
    """Edit text based on a previous selection. Selection is nullified after editing.

    Args:
        selection_id: The selection_id returned by select_note
        replacement: Text to replace matched occurrences with
        occurrence: 0=all occurrences, 1=first, 2=second, etc.

    Returns:
        JSON with changes_made count and git diff.
    """
    return _tool_run(
        lambda: create_notebook().edit_selection(selection_id, replacement if replacement else "",
                                                  occurrence)
    )


@mcp.tool
def lmnotes_delete_selection(selection_id: str, occurrence: int = 0) -> str:
    """Delete text based on a previous selection. Selection is nullified after editing.

    Args:
        selection_id: The selection_id returned by select_note
        occurrence: 0=all occurrences, 1=first, 2=second, etc.

    Returns:
        JSON with changes_made count and git diff.
    """
    return _tool_run(lambda: create_notebook().delete_selection(selection_id, occurrence))


@mcp.tool
def lmnotes_append_selection(selection_id: str, addition: str = "",
                             occurrence: int = 0) -> str:
    """Append text after previously selected matches. Selection is nullified.

    Args:
        selection_id: The selection_id returned by select_note
        addition: Text to append after each match
        occurrence: 0=all occurrences, 1=first, 2=second, etc.

    Returns:
        JSON with changes_made count and git diff.
    """
    return _tool_run(
        lambda: create_notebook().append_selection(selection_id, addition if addition else "",
                                                    occurrence)
    )


@mcp.tool
def lmnotes_delete_note(note_id: str) -> str:
    """Delete a note file and update the parent index.

    Args:
        note_id: The unique identifier of the note

    Returns:
        JSON with status.
    """
    return _tool_run(lambda: create_notebook().delete_note(note_id))


@mcp.tool
def lmnotes_get_stats() -> str:
    """Return notebook statistics (counts per folder, total notes).

    Returns:
        JSON with status and stats.
    """
    return _tool_run(lambda: create_notebook().get_stats())


@mcp.tool
def lmnotes_read_system_prompt() -> str:
    """Read system folder prompts. Returns the core prompt and any additional notes.

    This should be called at the start of a conversation to load behavioral rules.

    Returns:
        JSON with core_prompt and notes list.
    """
    return _tool_run(lambda: create_notebook().read_system_prompt())


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
    return _tool_run(
        lambda: create_notebook().copy_to_references(
            source_path, description if description else "", note_id if note_id else None)
    )


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
    return _tool_run(lambda: create_notebook().git_log(note_id))


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
    return _tool_run(
        lambda: create_notebook().git_diff(note_id,
                                            from_rev if from_rev else "",
                                            to_rev if to_rev else "")
    )


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
    return _tool_run(lambda: create_notebook().git_checkout(note_id, revision))


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
    return _tool_run(lambda: create_notebook().manual(tool_name if tool_name else ""))


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
    args = _parse_args()
    if args.folder:
        nb = Notebook(args.folder)
        # This sets notebook.py's _notebook_folder (we imported by reference)
        global _notebook_folder
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
    print("Session Management:")
    print("  lmnotes_init_notebook      - Initialize a new session (001-999)")
    print("  lmnotes_reinit_session     - Reinitialize session with new folder")
    print("  lmnotes_get_session        - Get current session info")
    print("  lmnotes_list_sessions      - List all active sessions")
    print("  lmnotes_close_session      - Close a session")
    print()
    print("Note Operations:")
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
    print("  lmnotes_git_log            - Show commit history for a note")
    print("  lmnotes_git_diff           - Diff between two revisions of a note")
    print("  lmnotes_git_checkout       - Restore a note to a previous revision")
    print("  lmnotes_manual             - Documentation (usage guide or tool help)")
    print()

    # Run the MCP server
    mcp.run()