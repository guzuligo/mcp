"""
lmnotespy - File-Based LLM Notebook System (FastMCP)

A pure file-based notebook management system using hierarchical index.md files
for navigation and search. All notes are stored as human-readable markdown files.

Package Structure:
    lmnotespy/
    ├── __init__.py          # Package entry, re-exports
    ├── sessions.py          # Persistent SQLite session store
    ├── notebook.py          # Notebook class with service composition
    ├── utils.py             # Pure utility functions (no circular deps)
    ├── operations.py        # CRUD operations service
    ├── edits.py             # Edit workflow service
    └── versioning.py        # Git integration service
"""

from .notebook import (
    Notebook,
    create_notebook,
    init_session,
    get_session,
    list_sessions,
    close_session,
    DEBUG,
)

# Re-export VALID_FOLDERS from utils for backwards compatibility
from .utils import VALID_FOLDERS  # noqa: F401

__all__ = [
    "Notebook",
    "create_notebook",
    "init_session",
    "get_session",
    "list_sessions",
    "close_session",
    "VALID_FOLDERS",
    "DEBUG",
]
