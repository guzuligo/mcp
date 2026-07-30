# lmnotes - File-Based LLM Notebook System (FastMCP)

## Overview

`lmnotes` is an LLM notebook management system built on FastMCP that uses a **pure file-based approach** with hierarchical `index.md` files for navigation and search. All notes are stored as human-readable markdown files, making them portable, version-controllable, and editable outside the tool.

### ⚠️ Initialization Required

**All operations require calling `init_notebook()` first.** Every other tool will return an error like "Notebook not initialized. Call lmnotes_init_notebook first." if called before init. The default folder is `~/.lmnotes/` after init is called with no arguments.

### Architecture

`lmnotes` uses a modular package structure:

```
lmnotes/
├── __init__.py          # Package entry, re-exports
├── notebook.py          # Notebook class + global state (single source of truth)
├── utils.py             # Pure utility functions (frontmatter parsing, ID generation)
├── operations.py        # CRUD operations service (init, create, read, search, list)
├── edits.py             # Edit workflow service (update, append, select/edit)
└── versioning.py        # Git integration service (commit, log, diff, checkout)

lmnotes.py               # FastMCP entry point — all 21 tool definitions
```

Global state (`_initialized`, `_notebook_folder`, `_selection_store`, `_selection_counter`) lives in `notebook.py` and is imported by reference in `lmnotes.py` — there is exactly one copy of each variable.

### Design Philosophy

- **Files as source of truth**: All notes are stored as human-readable markdown files
- **Hierarchical indexes**: Each folder contains an `index.md` that acts as a catalog
- **ID-based operations**: All tools reference notes by their timestamp ID, not filename
- **Git integration**: Every write operation auto-commits to git with descriptive messages
- **No external dependencies**: Only Python standard library + FastMCP

### Why File-Based?

- Human-readable and editable with any text editor
- Version-controllable with Git
- Portable — copy the folder anywhere
- No database corruption concerns
- Natural organization via subfolders

---

## Architecture

### Folder Structure

```
~/.lmnotes/                          # Root notebook directory (default)
├── index.md                         # Root catalog of all categories
├── procedures/                      # Skill memory & learned procedures
│   ├── index.md                     # Catalog of procedure notes
│   └── 260729165500_git_rebase_fix.md
├── reports/                         # Completed action reports
│   ├── index.md
│   └── 260729165501_deploy_staging.md
├── individuals/                     # People & intelligent beings info
│   ├── index.md
│   └── 260729165502_ahmad_preferences.md
├── conversations/                   # Conversation summaries
│   ├── index.md
│   └── 260729165503_project_kickoff.md
├── knowledge/                       # Facts & learned information
│   ├── index.md
│   └── 260729165504_python_gil_details.md
├── system/                          # System prompts & behavioral rules
│   ├── index.md
│   ├── 000000000000_core_prompt.md  # Always-read system prompt
│   └── 000000000001_mistake_log.md  # Past mistakes to avoid
└── references/                      # User-shared files with descriptions
    ├── index.md
    ├── 260729165500_budget_report.md     # Description of the CSV
    ├── budget_report.csv                 # The actual referenced file
    ├── 260729165501_team_photo.md        # Description of the image
    └── team_photo.jpg                    # The actual image
```

### File Naming Convention

- **Format**: `{YYYYMMDDHHmmss}_{slug}.md`
- **ID**: The `{YYYYMMDDHHmmss}` timestamp portion is the **unique identifier** — all tool operations use this ID
- **Slug**: Human-readable descriptor (e.g., `git_rebase_fix`)
- **System files**: Use `000000000000` prefix with sequential numbers for persistent system files

### Content File Format

Each note is a Markdown file with YAML-like front-matter:

```markdown
---
id: 260729165500
title: Git Rebase Conflict Resolution
tags: [git, procedures, conflict-resolution]
folder: procedures
created: 2026-07-29T16:55:00
updated: 2026-07-29T16:55:00
---

# Git Rebase Conflict Resolution

Step 1: When conflicts appear during rebase, first check which branch has the changes...
```

### Index File Format

Each `index.md` serves as a directory catalog:

```markdown
# Procedures

| ID | Title | Tags | Last Updated |
|----|-------|------|--------------|
| 260729165500 | Git Rebase Conflict Resolution | git, procedures | 2026-07-29 |

---
*Search hint: use `search_notes` with folder="procedures" to find by keyword*
```

### References Folder

The `references/` folder stores files shared with the LLM. Each file may have a companion `.md` description:

- The `.md` file describes the content so the LLM doesn't need to re-read the original
- For images, the `.md` describes what's in the image
- The user places files here manually or via the `copy_to_references` tool
- Reading actual referenced files (CSVs, images) is handled by other tools outside this system

---

## CLI Usage

```bash
# Start with default notebook folder (~/.lmnotes/)
python lmnotes.py

# Start with custom notebook folder
python lmnotes.py --folder ~/my_notes/

# Start with a path ending in / (treated as directory)
python lmnotes.py --folder /path/to/notebooks/
```

If `--folder` is not provided, the default is `~/.lmnotes/`. The folder can also be changed at runtime via the `init_notebook` tool.

---

## API Reference

### Core Methods (Notebook Class)

#### `init_notebook(folder: str = None) -> dict`
Initializes or reconfigures the notebook folder location. Sets a **global reference** so all subsequent tool calls use this folder automatically. Only creates the root directory if it doesn't exist — subfolders are created lazily on first write.

| Parameter | Type | Description |
|-----------|------|-------------|
| `folder` | str | Path to use as notebook root. If ends with `/`, appends `.lmnotes/`. Defaults to `~/.lmnotes/`. |

Returns: dict with `status`, and a message confirming the folder is set. The internal path is hidden by default — visible only when `DEBUG = True` at the top of `lmnotes.py`.

**Important:** This MUST be called before any other operation. Without it, all tools return "Notebook not initialized." Once called, this folder persists for all subsequent tool calls in the session.

#### `create_note(title: str, content: str, folder: str, tags: list = None, note_id: str = None, parent_id: str = None) -> dict`
Creates a new note file with optional parent reference and updates the parent folder's `index.md`. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `title` | str | Note title (used in front-matter) |
| `content` | str | Markdown content of the note |
| `folder` | str | Folder category: `procedures`, `reports`, `individuals`, `conversations`, `knowledge`, `system`, or `references` |
| `tags` | list[str] | Tags for searchability |
| `note_id` | str (optional) | Custom ID. If not provided, auto-generated from timestamp |
| `parent_id` | str (optional) | ID of a parent note to link this note to. Must exist. |

Returns: dict with `status`, `id`, and note data. The internal filepath is hidden by default — visible only when `DEBUG = True`.

#### `read_note(note_id: str, detail_level: int = 1) -> dict`
Reads a note by its ID. Searches all folders recursively. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `note_id` | str | The timestamp-based unique identifier |
| `detail_level` | int (0-3, default: 1) | How much content to return |

Returns: dict with `status`, `id`, and note data. Or error if not initialized.

#### `search_notes(keywords: list[str], folder: str = None, detail_level: int = 1, max_results: int = 20, max_tokens: int = 4096) -> dict`
Searches notes by keywords with ranking based on match count. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `keywords` | list[str] | Keywords to search for in front-matter and content |
| `folder` | str (optional) | Limit search to a specific folder |
| `detail_level` | int (0-3, default: 1) | Content detail returned per result |
| `max_results` | int (default: 20) | Maximum number of results |
| `max_tokens` | int (default: 4096) | Token budget safeguard for full-content results |

Returns: dict with ranked groups by match count, each containing matching notes. Or error if not initialized.

#### `list_notes(folder: str = None, detail_level: int = 1, max_results: int = 50) -> dict`
Lists all notes as structured rows (database-style table view). **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `folder` | str (optional) | Limit to a specific folder. Defaults to all folders. |
| `detail_level` | int (0-1, default: 1) | 0=minimal (id+title+folder), 1=with tags+updated+preview |
| `max_results` | int (default: 50) | Maximum notes to return |

Returns: dict with status and list of note rows. Or error if not initialized.

#### `list_folder(folder: str = None) -> dict`
Lists contents of a folder as structured rows. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `folder` | str (optional) | Folder name. If omitted, lists root categories. |

Returns: dict with folder name and list of note rows. Or error if not initialized.

#### `read_index(folder: str = None) -> dict`
Reads an `index.md` file for navigation purposes. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `folder` | str (optional) | Folder to read index from. Defaults to root. |

Returns: dict with raw index content and parsed entries. Or error if not initialized.

#### `update_note(note_id: str, title: str = None, tags: list = None, content: str = None) -> dict`
Updates an existing note's fields. Content is replaced entirely (use `append_to_note` to append). **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `note_id` | str | The unique identifier of the note |
| `title` | str (optional) | New title |
| `tags` | list[str] (optional) | New tags list |
| `content` | str (optional) | New full content |

Returns: dict with `status` and updated data. Or error if not initialized.

#### `append_to_note(note_id: str, addition: str, separator: str = "\n\n---\n\n") -> dict`
Appends text to an existing note's content. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `note_id` | str | The unique identifier of the note |
| `addition` | str | Text to append |
| `separator` | str (default: "\n\n---\n\n") | Visual separator between old and new content |

Returns: dict with status, original length, new length. Or error if not initialized.

#### `select_note(note_id: str, pattern: str = None, mode: str = "exact", start_line: int = 1, end_line: int = -1) -> dict`
Selects/searches text within a note for editing. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `note_id` | str | The unique identifier of the note |
| `pattern` | str (required for exact/regex) | Search pattern |
| `mode` | str | `"exact"`, `"regex"`, or `"lines"` |
| `start_line` | int (1-based, default: 1) | Start line (for "lines" mode) |
| `end_line` | int (1-based, inclusive, -1=last) | End line (for "lines" mode) |

Returns: dict with `selection_id`, `occurrences`, matched text preview, and match positions. Or error if not initialized.

#### `edit_selection(selection_id: str, replacement: str, occurrence: int = 0) -> dict`
Edits text based on a previous selection. Selection is **nullified** after editing. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `selection_id` | str | ID returned from `select_note` |
| `replacement` | str | New text to replace with |
| `occurrence` | int | `1`=first, `2`=second, `0`=all occurrences |

Returns: dict with status and edit summary. Or error if not initialized.

#### `delete_selection(selection_id: str, occurrence: int = 0) -> dict`
Deletes text based on a previous selection. **Requires init to be called first.**

Same parameters as `edit_selection`. Returns status and edit summary.

#### `append_selection(selection_id: str, addition: str, occurrence: int = 0) -> dict`
Appends text after each selected match without replacing the original. **Requires init to be called first.**

Same parameters. Returns status and append summary.

#### `delete_note(note_id: str) -> dict`
Deletes a note file and updates the parent folder's `index.md`. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `note_id` | str | The unique identifier of the note |

Returns: dict with status. Or error if not initialized.

#### `get_stats() -> dict`
Returns notebook statistics: counts per folder, total notes. **Requires init to be called first.**

Returns: dict with status and stats. Or error if not initialized.

#### `read_system_prompt() -> dict`
Reads system folder prompts. Returns the core prompt (`000000000000_core_prompt.md`) and any additional system notes. **Requires init to be called first.**

Returns: dict with core_prompt string (if exists) and list of additional notes. Or error if not initialized.

#### `copy_to_references(source_path: str, description: str = "", note_id: str = None) -> dict`
Copies a file from anywhere on the filesystem into the references folder. **Requires init to be called first.**

| Parameter | Type | Description |
|-----------|------|-------------|
| `source_path` | str | Full path to the file to copy |
| `description` | str (optional) | Markdown description to write as companion `.md` |
| `note_id` | str (optional) | Custom ID. Auto-generated if omitted. |

Returns: dict with status, filename, and note data. Internal paths hidden by default — visible only when `DEBUG = True`.

---

## Parent/Children Reference System

Notes can reference a parent note via the `parent_id` frontmatter field, creating a tree structure for related notes (e.g., incidents about a person linked to their profile).

**Creating a child note:**
```json
create_note(
  title="Ahmad - Work Incident July 2026",
  content="# ...",
  folder="individuals",
  parent_id="260729165502"   // references "Ahmad Preferences"
)
```

**What the LLM sees (always visible, not hidden by DEBUG):**
- `parent_id` — ID of the parent note
- `parent_title` — resolved title of the parent note
- `children_count` — number of child notes referencing this note

Example from `list_notes`:
```json
{"id": "260730120001", "title": "Ahmad Incident July 2026", "folder": "individuals", "parent_id": "260729165502", "parent_title": "Ahmad Preferences", "children_count": 0}
```

**Traversing children:**
Use `lmnotes_list_children(note_id)` to get all notes under a parent:
```json
{
  "status": "success",
  "note_id": "260729165502",
  "parent_title": "Ahmad Preferences",
  "children_count": 3,
  "children": [
    {"id": "...", "title": "Ahmad - Work Incident July 2026", "folder": "individuals", ...},
    {"id": "...", "title": "Ahmad - Birthday Party", "folder": "individuals", ...}
  ]
}
```

**Index tables now include a Children column:**
```markdown
| ID | Title | Tags | Last Updated | Children |
|----|-------|------|--------------|----------|
| 260729165502 | Ahmad Preferences | ahmad, preferences | 2026-07-29 | 3 |
```

---

## Debug Mode

At the top of `lmnotes.py`, there is a `DEBUG` flag:

```python
# Debug mode: when True, exposes internal paths for development
DEBUG = True
```

When `DEBUG = True`:
- Tool results include `filepath`, `notebook_folder`, `deleted_file`, `source_path`, `destination_path` fields
- Useful during development and testing

When `DEBUG = False`:
- All internal filepaths are hidden from the LLM-visible output
- Results only contain IDs, titles, tags, timestamps — no paths
- The underlying folder structure remains completely transparent to the LLM

---

## Search Ranking System

When searching for multiple keywords, results are **grouped by match count**:

```
=== 5/5 Keywords Matched ===
  [260729165500] Git Rebase Conflict Resolution  (matched: git, rebase, conflict, resolve, fix)

=== 3/5 Keywords Matched ===
  [260729165501] Git Branch Management  (matched: git, branch, resolve)
  [260729165502] Mercurial vs Git  (matched: git, conflict, fix)

=== 1/5 Keywords Matched ===
  [260729165503] Version Control Basics  (matched: git)
```

Even if keyword choices are poor, results with fewer matches are still returned with clear labels indicating how many matched.

---

## Detail Levels

| Level | What's Returned | Use Case |
|-------|----------------|----------|
| **0** | ID + title only | Quick listing, minimal context |
| **1** | ID + title + one matching line of content | Default — balance of info and brevity |
| **2** | ID + title + matching paragraph (~5 lines around match) | Need more context without full read |
| **3** | ID + title + full note content | Deep dive; use with `max_tokens` safeguard |

---

## Token Safeguard

When `detail_level=3`, if the total output would exceed `max_tokens`, results are trimmed from the bottom until under budget. A notice is included:

```
[Trimmed: 2 results excluded to stay within token budget of 4096 tokens]
```

---

## Select and Edit Workflow

The two-step workflow saves context tokens by avoiding full file reads during editing.

### Step 1: Select Note Content
```json
select_note(note_id="260729165500", pattern="bug", mode="exact")
```

Returns:
```json
{
  "status": "success",
  "note_id": "260729165500",
  "mode": "exact",
  "occurrences": 3,
  "matched_text": "...bug was found...<truncated>...bug in production...",
  "selection_id": "sel_260729165500_1",
  "match_positions": [{"start": 42, "end": 45}, ...]
}
```

### Step 2: Edit the Selection
```json
edit_selection(selection_id="sel_260729165500_1", replacement="error", occurrence=1)
```

- `occurrence=1`: Replace only the first match
- `occurrence=2`: Replace only the second match
- `occurrence=0`: Replace ALL occurrences

**Important:** After editing, the selection is nullified. Call `select_note` again for further edits.

---

## Appending vs Updating Notes

### When to Use `append_to_note`

Use append when accumulating information over time:

```json
// Instead of update_note (which replaces content):
update_note(note_id="260729165500", content="New content")  // Loses old content!

// Use append_to_note (which adds):
append_to_note(note_id="260729165500", addition="Additional finding: the bug was a race condition.")
```

### Configurable Separator

```json
// Default separator:
append_to_note("id", "New info")
// Result: "Original content\n\n---\n\nNew info"

// Custom dated separator:
append_to_note("id", "2026-07-29: Updated", separator="\n\n## [2026-07-29]:\n\n")

// Bullet point style:
append_to_note("id", "- New point to remember", separator="\n- ")
```

---

## Best Practices for LLM Usage

### Title Guidelines
- Be expressive but concise — the title should help you decide if reading the summary is necessary
- Include key context in the title itself (subject matter, identifiers)

### Content Guidelines
- Include specific details: dates, numbers, links, names, URLs, file paths
- Use front-matter tags for searchability
- Organize complex topics with headers and bullet points

### Folder Selection Guide
| Folder | When to Use |
|--------|-------------|
| `procedures` | Learned procedures, how-to steps, skills acquired |
| `reports` | Summaries of completed actions or tasks |
| `individuals` | Information about people or intelligent beings (real or fictional) |
| `conversations` | Summaries of conversations with the user |
| `knowledge` | Facts and learned information not tied to a specific conversation |
| `system` | Behavioral rules, system prompts, mistake logs for self-improvement |
| `references` | Files shared by the user that need persistent access |

### Keywords Guidelines
Use keywords as your primary search hook. Include:
- Product names, model numbers, technical specifications
- Key concepts, frameworks, or methodologies mentioned
- Names of people, organizations, locations
- Specific dates, version numbers, identifiers
- Phrases capturing the essence of what the note is about

Do NOT include generic stop words (the, a, an, for) or anything too broad.

---

## File Structure Summary

```
~/.lmnotes/
├── index.md                  # Root catalog
├── procedures/
│   ├── index.md
│   └── {timestamp}_{slug}.md
├── reports/
│   ├── index.md
│   └── {timestamp}_{slug}.md
├── individuals/
│   ├── index.md
│   └── {timestamp}_{slug}.md
├── conversations/
│   ├── index.md
│   └── {timestamp}_{slug}.md
├── knowledge/
│   ├── index.md
│   └── {timestamp}_{slug}.md
├── system/
│   ├── index.md
│   ├── 000000000000_core_prompt.md
│   └── 000000000001_mistake_log.md
└── references/
    ├── index.md
    ├── {timestamp}_{slug}.md      # Description file
    └── <referenced_file>          # The actual referenced file
```

---

## Git Integration

Every write operation (create, update, append, delete, edit) automatically commits to git with a descriptive message. This provides version history and the ability to roll back changes.

### Auto-Commit Behavior

| Operation | Commit Message Format |
|-----------|----------------------|
| `create_note` | `Add note: {title} ({id})` |
| `update_note` | `Update note {id}: {field} changed` |
| `append_to_note` | `Append to note {id}: +{chars} chars` |
| `edit_selection` | `Edit note {id}: replace/delete` |
| `delete_note` | `Delete note {id}` |

### Git Tools

#### `lmnotes_git_log(note_id)`
Shows commit history for a specific note file.

```json
{
  "status": "success",
  "note_id": "260729165500",
  "commits": [
    {"hash": "abc1234", "date": "2026-07-30T08:50:00+00:00", "message": "Add note: Git Rebase Conflict Resolution (260729165500)"},
    {"hash": "def5678", "date": "2026-07-30T08:51:00+00:00", "message": "Edit note 260729165500: replace"}
  ]
}
```

#### `lmnotes_git_diff(note_id, from_rev="HEAD", to_rev="")`
Shows diff between two revisions. Default: HEAD to working tree.

```json
{
  "status": "success",
  "from_rev": "HEAD",
  "to_rev": "working tree",
  "diff": "@@ -2,7 +2,7 @@\n-updated: 2026-07-30T08:50:06\n+updated: 2026-07-30T08:50:11\n ..."
}
```

#### `lmnotes_git_checkout(note_id, revision)`
Restores a note to a previous git revision. Creates a new commit after restoring.

```json
{
  "status": "ok",
  "note_id": "260729165500",
  "restored_to": "abc1234",
  "message": "Note restored to revision abc1234"
}
```

### Edit Response with Commit Info

Edit operations now include both diff and commit information:

```json
{
  "status": "success",
  "note_id": "260729165500",
  "changes_made": 1,
  "selection_nullified": true,
  "diff": "@@ -2,7 +2,7 @@\n-MODIFIED LINE TWO\n+UPDATED LINE TWO\n ...",
  "commit": {
    "status": "ok",
    "commit_hash": "aa99f2f8faabc05875fccfd9bba840273a632479"
  }
}
```

---

## Quick Start

```python
from lmnotes import Notebook, create_notebook

# Create instance (defaults to ~/.lmnotes/)
nb = create_notebook()

# ⚠️ REQUIRED: Initialize the notebook folder before any operations
init_result = nb.init_notebook("~/my_notes/")  # or omit for default
print(init_result)  # {"status": "success", "message": "Notebook ready."}

# Create a note
result = nb.create_note(
    title="Git Rebase Conflict Resolution",
    content="Step 1: When conflicts appear...\n...",
    folder="procedures",
    tags=["git", "procedures", "conflict-resolution"]
)

# Search notes
results = nb.search_notes(keywords=["git", "rebase"], detail_level=1)

# Read a note by ID
note = nb.read_note(result["id"], detail_level=2)

# Append to existing note
nb.append_to_note(result["id"], "\n\n## Update:\n\nFixed the edge case with detached HEAD.")

# Select and edit within a note
sel = nb.select_note(result["id"], pattern="edge case", mode="exact")
nb.edit_selection(sel["selection_id"], replacement="detached HEAD scenario", occurrence=1)
```

---

## Running as MCP Server

```bash
python lmnotes.py
```

This starts the FastMCP server with all tools available.

### CLI Arguments

| Argument | Description |
|----------|-------------|
| `--folder` | Path to use as notebook root. If ends with `/`, appends `.lmnotes/`. Default: `~/.lmnotes/` |

Examples:
```bash
python lmnotes.py                        # Use ~/.lmnotes/
python lmnotes.py --folder ~/my_notes/   # Use custom path
python lmnotes.py --folder /data/notebooks/  # Directory-style path

---

## How to Go Further

### Adding a New Tool
1. **Define the tool** in `lmnotes.py` — add a `@mcp.tool` decorated function with full docstring
2. **Add business logic** to the appropriate service module:
   - CRUD operations → `lmnotes/operations.py`
   - Edit workflow → `lmnotes/edits.py`
   - Git integration → `lmnotes/versioning.py`
3. **Create delegation method** in `notebook.py` — thin wrapper that calls `self._service.method()`
4. **Update `manual.md`** — add a new `## Tool: lmnotes_your_tool` section with Args and Returns

### Modifying Business Logic
- **CRUD**: Edit `lmnotes/operations.py` — `OperationsService` class
- **Edits**: Edit `lmnotes/edits.py` — `EditService` class
- **Git**: Edit `lmnotes/versioning.py` — `GitService` class
- **Global state**: Edit `lmnotes/notebook.py` — globals live here

### Running Tests
```bash
# Run all tests
pytest test_lmnotes.py -v

# Run specific test class
pytest test_lmnotes.py::TestManual -v

# Run with verbose output
pytest test_lmnotes.py -v --tb=long
```
Current: **77 tests pass**

### Starting MCP Server
```bash
# Default folder (~/.lmnotes/)
python lmnotes.py

# Custom folder
python lmnotes.py --folder ~/my_notes/
```

### Editing Documentation
- **Tool docs**: Edit `lmnotes/manual.md` — compact format, one section per tool
- **General docs**: Edit `lmnotes.md` — this file
- **Quick-start guide**: In `lmnotes/manual.md`, before the first `## Tool:` section

### Key Architecture Notes
- Global state (`_initialized`, `_notebook_folder`, etc.) lives in `notebook.py`
- `lmnotes.py` imports globals by reference: `from lmnotes import notebook as _nb`
- Services are composed at runtime — no import-time binding
- `manual.md` is loaded at runtime via `_load_manual_content()`
