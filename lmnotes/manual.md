# LMNotes Quick-Start Guide

## Initialization
1. Call `lmnotes_init_notebook(folder="~/my_notes/")` to set up your notebook directory.
   If omitted, defaults to ~/.lmnotes/.
2. The folder is created automatically (root only; subfolders are lazy-created on first write).

## Core Workflow: Create → Read → Search → Edit

### Creating Notes
- `lmnotes_create_note(title, content, folder, tags="")` — creates a new note file with frontmatter.
  - folder must be one of: procedures, reports, individuals, conversations, knowledge, system, references
  - parent_id optional to link child notes
  - Auto-commits to git if initialized

### Reading Notes
- `lmnotes_read_note(note_id, detail_level=1)` — reads a note by its timestamp-based ID.
  - detail_level: 0=minimal (id+title only), 1=one line preview (default), 2=paragraph (~5 lines), 3=full content

### Searching Notes
- `lmnotes_search_notes("git rebase conflict", folder="procedures")` — searches across all notes.
  - Results are ranked by how many keywords match (5/5 > 3/5 > 1/5)

### Listing Notes
- `lmnotes_list_notes()` — returns a clean table view of ALL notes
- `lmnotes_list_folder("procedures")` — lists one folder's contents
- `lmnotes_list_children(note_id)` — finds child notes under a parent

### Editing Notes (Two-Step Workflow)
1. **Select** text: `lmnotes_select_note(note_id, pattern="target", mode="exact")`
2. **Edit** using selection_id:
   - `lmnotes_edit_selection(selection_id, replacement="new text", occurrence=0)` — replace all
   - `lmnotes_delete_selection(selection_id, occurrence=0)` — delete matched
   - `lmnotes_append_selection(selection_id, addition="more text")` — append after matches
   - occurrence: 0=all, 1=first, 2=second, etc.
   - Selection is **nullified** after use

### Other Operations
- `lmnotes_update_note(note_id, title="new", tags="a,b")` — replace content
- `lmnotes_append_to_note(note_id, addition)` — append with separator
- `lmnotes_delete_note(note_id)` — delete note and update index
- `lmnotes_copy_to_references(source_path, description)` — copy external file

### Git Integration (Automatic)
Every write operation auto-commits. Additional tools:
- `lmnotes_git_log(note_id)` — view commit history
- `lmnotes_git_diff(note_id, from_rev="HEAD")` — see changes vs last commit
- `lmnotes_git_checkout(note_id, revision)` — restore to previous version

### System Prompts
- `lmnotes_read_system_prompt()` — load behavioral rules

## File Structure
    ~/.lmnotes/
    ├── index.md              # Root catalog
    ├── procedures/index.md   # Each subfolder has its own index
    ├── reports/index.md
    └── ... (other folders)

Each note: {timestamp}_{slug}.md with YAML frontmatter (id, title, folder, tags, created, updated).

## Error Handling
- All operations return JSON with "status": "success" or "error"
- Not-initialized errors state the need to call init_notebook first
- Selection IDs are single-use — re-select for further edits


## Tool: lmnotes_init_notebook
Initialize or reconfigure the notebook folder location.
**Args:**
- `folder` (str): Path to use as notebook root. If ends with '/', appends .lmnotes/. Defaults to ~/.lmnotes/.
**Returns:**
- JSON with `status` and resolved path.

## Tool: lmnotes_create_note
Create a new note file with the given title, content, and metadata.
**Args:**
- `title` (str): Note title (used in frontmatter and filename slug)
- `content` (str): Markdown body content
- `folder` (str): Target folder (procedures, reports, individuals, conversations, knowledge, system, references)
- `tags` (str): Comma-separated list of tags
- `note_id` (str): Optional custom timestamp ID
- `parent_id` (str): Optional parent note ID
**Returns:**
- JSON with status, note ID, title, folder, tags, and timestamps.

## Tool: lmnotes_read_note
Read a note by its ID.
**Args:**
- `note_id` (str): The timestamp-based ID of the note
- `detail_level` (int): 0=minimal, 1=preview (default), 2=paragraph, 3=full
**Returns:**
- JSON with note metadata and content.

## Tool: lmnotes_search_notes
Search notes by keywords with ranking.
**Args:**
- `keywords` (str): Space-separated keywords
- `folder` (str): Optional folder to limit search
- `detail_level` (int): 0-3 (default 1)
- `max_results` (int): Default 20
- `max_tokens` (int): Default 4096
**Returns:**
- JSON with grouped results ranked by match count.

## Tool: lmnotes_list_notes
List all notes as structured rows (database-style table view).
**Args:**
- `folder` (str): Optional folder to limit to
- `detail_level` (int): 0=minimal, 1=with preview (default)
- `max_results` (int): Default 50
**Returns:**
- JSON with status and list of note rows.

## Tool: lmnotes_list_children
List all child notes under a parent note.
**Args:**
- `note_id` (str): The parent note ID
**Returns:**
- JSON with parent title and list of child notes.

## Tool: lmnotes_list_folder
List contents of a specific folder as structured rows.
**Args:**
- `folder` (str): Folder name (empty = root categories)
**Returns:**
- JSON with folder contents or root category summary.

## Tool: lmnotes_read_index
Read an index.md file for navigation.
**Args:**
- `folder` (str): Folder name (empty = root index)
**Returns:**
- JSON with index content and parsed entries.

## Tool: lmnotes_update_note
Update an existing note's fields. Content is replaced entirely.
**Args:**
- `note_id` (str): The ID of the note to update
- `title` (str): New title (empty = keep current)
- `tags` (str): Comma-separated tags (empty = keep current)
- `content` (str): New content (empty = keep current)
**Returns:**
- JSON with updated note metadata.

## Tool: lmnotes_append_to_note
Append text to an existing note's content.
**Args:**
- `note_id` (str): The ID of the note to append to
- `addition` (str): Text to append
- `separator` (str): Default "\n\n---\n\n"
**Returns:**
- JSON with original and new content lengths.

## Tool: lmnotes_select_note
Select/search text within a note for editing.
**Args:**
- `note_id` (str): The ID of the note to search
- `pattern` (str): Text pattern to find (required for exact/regex)
- `mode` (str): "exact", "regex", or "lines"
- `start_line` (int): Starting line number (for lines mode)
- `end_line` (int): Ending line number, -1 = last line
**Returns:**
- JSON with selection_id, matched text preview, and occurrence count.

## Tool: lmnotes_edit_selection
Edit text based on a previous selection. Selection is nullified after editing.
**Args:**
- `selection_id` (str): The selection_id returned by select_note
- `replacement` (str): Text to replace matched occurrences with
- `occurrence` (int): 0=all occurrences, 1=first, 2=second, etc.
**Returns:**
- JSON with changes_made count and git diff.

## Tool: lmnotes_delete_selection
Delete text based on a previous selection. Selection is nullified after editing.
**Args:**
- `selection_id` (str): The selection_id returned by select_note
- `occurrence` (int): 0=all occurrences, 1=first, 2=second, etc.
**Returns:**
- JSON with changes_made count and git diff.

## Tool: lmnotes_append_selection
Append text after previously selected matches. Selection is nullified.
**Args:**
- `selection_id` (str): The selection_id returned by select_note
- `addition` (str): Text to append after each match
- `occurrence` (int): 0=all occurrences, 1=first, 2=second, etc.
**Returns:**
- JSON with changes_made count and git diff.

## Tool: lmnotes_delete_note
Delete a note file and update the parent index.
**Args:**
- `note_id` (str): The unique identifier of the note
**Returns:**
- JSON with status.

## Tool: lmnotes_get_stats
Return notebook statistics (counts per folder, total notes).
**Args:**
- None
**Returns:**
- JSON with status and stats.

## Tool: lmnotes_read_system_prompt
Read system folder prompts. Returns the core prompt and any additional notes.
**Args:**
- None
**Returns:**
- JSON with core_prompt and notes list.

## Tool: lmnotes_copy_to_references
Copy a file from anywhere on the filesystem into the references folder.
**Args:**
- `source_path` (str): Full path to the file to copy
- `description` (str): Markdown description of the file content
- `note_id` (str): Custom ID for the reference note
**Returns:**
- JSON with status, destination path, and note data.

## Tool: lmnotes_git_log
Return commit history for a specific note.
**Args:**
- `note_id` (str): The ID of the note
**Returns:**
- JSON with status, note_id, and list of commits (hash, date, message).

## Tool: lmnotes_git_diff
Show diff between two revisions of a note. Use from_rev="HEAD~1" to see what changed in the last commit.
**Args:**
- `note_id` (str): The ID of the note
- `from_rev` (str): Starting revision. Use "HEAD~1" to compare the last commit to HEAD. Default: HEAD
- `to_rev` (str): Ending revision. Default: working tree (current uncommitted changes)
**Returns:**
- JSON with status, revisions, and diff text.

**Usage Examples:**
- See changes in last commit: `git_diff(note_id="260729165500", from_rev="HEAD~1")`
- See working tree changes: `git_diff(note_id="260729165500")`
- Compare two commits: `git_diff(note_id="260729165500", from_rev="abc1234", to_rev="HEAD")`

## Tool: lmnotes_git_checkout
Restore a note to a previous git revision. This is the undo tool for notes.
**Args:**
- `note_id` (str): The ID of the note
- `revision` (str): Git commit hash or reference (e.g., HEAD~1, abc1234)
**Returns:**
- JSON with status and result info.

**Usage Examples:**
- Undo last edit: `git_checkout(note_id="260729165500", revision="HEAD~1")`
- Undo specific commit: `git_checkout(note_id="260729165500", revision="abc1234")`

## Tool: lmnotes_manual
Return documentation for a specific tool or general usage guide.
**Args:**
- `tool_name` (str): Name of the tool (e.g., 'edit_selection', 'create_note'). If empty, returns the general usage guide.
**Returns:**
- JSON with status and content string.