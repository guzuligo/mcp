"""
lmnotes - File-Based LLM Notebook System (FastMCP)

A pure file-based notebook management system using hierarchical index.md files
for navigation and search. All notes are stored as human-readable markdown files.

Package Structure:
    lmnotes/
    ├── __init__.py          # Package entry, re-exports
    ├── notebook.py          # Notebook class with service composition
    ├── utils.py             # Pure utility functions (no circular deps)
    ├── operations.py        # CRUD operations service
    ├── edits.py             # Edit workflow service
    └── versioning.py        # Git integration service

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

# Re-export everything from notebook.py so tests can access globals via lmnotes._initialized etc.
from .notebook import (  # noqa: F401,F403
    Notebook,
    create_notebook,
    DEBUG,
    _initialized,
    _notebook_folder,
    _selection_store,
    _selection_counter,
)

# Re-export VALID_FOLDERS from utils for backwards compatibility
from .utils import VALID_FOLDERS  # noqa: F401

__all__ = [
    "Notebook",
    "create_notebook",
    "VALID_FOLDERS",
    "DEBUG",
    "_initialized",
    "_notebook_folder",
    "_selection_store",
    "_selection_counter",
]