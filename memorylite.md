# memorylite - Lightweight LLM Memory Database System (SQLite Backend)

## Overview

`memorylite` is an LLM memory management system built on FastMCP that uses **SQLite** as its database backend. This replaces the JSON file approach with proper SQL queries for better performance, transactional safety, and efficient searching.

## Architecture

### Design Philosophy

The system follows a **database-style approach** where all memories are stored in an SQLite database with proper indexes for fast lookups. Each memory item has a unique ID and can be related to other memories by their IDs.

#### Why SQLite?
- **Better performance**: SQL queries vs regex/string matching
- **ACID transactions**: Data integrity guaranteed
- **Efficient searching**: Single SELECT across all fields instead of multiple passes
- **Reduced I/O**: `details_level` parameter controls output detail
- **Scalability**: SQLite handles large datasets better than in-memory JSON

### Database Schema

The database is stored in a single file (`~/.swordmemory/memory.db`) with the following structure:

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,                    -- YYMMDDhhmmss format timestamp-based ID
    title TEXT NOT NULL,                     -- Short descriptive title
    summary TEXT NOT NULL,                   -- Detailed description with specific details
    memory_type INTEGER NOT NULL DEFAULT 0,  -- Integer type code (see MEMORY TYPE CODES below)
    related_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array string: '["id1","id2"]'
    keywords TEXT NOT NULL DEFAULT '[]',     -- Semantic keywords for searching
    created_at TEXT NOT NULL,              -- ISO format timestamp of creation
    updated_at TEXT NOT NULL                -- ISO format timestamp of last update
);

-- Indexes for fast lookups
CREATE INDEX idx_memories_id ON memories(id);
CREATE INDEX idx_memories_created_at ON memories(created_at);
```

**IMPORTANT NOTES FOR LLMs:**
- `related_ids` and `keywords` are stored as **JSON array strings** (e.g., `'["personal","technical"]'`)
- Use `json_each()` for SQL-based array membership checks:
  ```sql
  SELECT * FROM memories, json_each(keywords) WHERE json_each.value = 'debugging'
  ```
- All queries should specify columns explicitly (avoid `SELECT *`)
- Use `details_level` parameter to control output detail

### Memory Record Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string (YYMMDDhhmmss) | Timestamp-based unique identifier (e.g., 260506193000 for 2026-05-06 19:30:00) |
| `title` | string | Short descriptive title - should be expressive enough to help determine if reading the summary is needed. Think of it as a "should I read more?" indicator. |
| `summary` | string | Detailed description of the experience/knowledge. **IMPORTANT**: Include specific details like dates, numbers, links, names, and any non-general information. |
| `memory_type` | integer | Type code: 0=Unspecified, 1=Personal, 2=Document, 3=Reference, 4=Chat, 5=Chitchat, 6=Technical, 7-99=Reserved, 100+=User-defined |
| `related_ids` | array of strings (stored as JSON) | List of other memory IDs this is related to |
| `keywords` | array of strings (stored as JSON) | SEMANTIC KEYWORDS/PHRASES useful for SEARCHING and MATCHING |
| `created_at` | string (ISO) | Timestamp when the memory was created |
| `updated_at` | string (ISO) | Timestamp of last update |

### Memory Type Codes

Each memory has a **single integer type code** for categorization:

| Code | Name | Description |
|------|------|-------------|
| **0** | Unspecified | Default type when no specific category applies |
| **1** | Personal | Related to the user: their life, feelings, experiences, relationships, personal goals |
| **2** | Document | Summary or information extracted from a specific document provided to the LLM |
| **3** | Reference | General knowledge reference: internet search results, pasted content from external sources |
| **4** | Chat | General conversation without a specific topic or purpose |
| **5** | Chitchat | Casual conversation, not significant, nothing new was learned |
| **6** | Technical | Coding sessions, git repos, programming languages, math, science, new procedures |
| **7-99** | Reserved | Reserved for future built-in use |
| **100** | Custom Index | Use this memory's keywords to define your custom type meanings (e.g., `["101=Health", "102=Finance"]`) |
| **101+** | User-Defined | Custom types defined by the user (meanings defined via type 100 memories) |

**Usage Examples:**
- A personal diary entry: `memory_type=1`
- A summary of a research paper: `memory_type=2`
- An internet search result about cooking: `memory_type=3`
- A casual chat with no specific topic: `memory_type=4`
- Small talk that isn't significant: `memory_type=5`
- A coding tutorial or technical guide: `memory_type=6`

## Database Initialization and Error Handling

### Automatic DB Initialization

Each tool function automatically initializes the database if it doesn't exist. The system also automatically repairs any malformed data in existing records.

### Error Handling Pattern

Each tool function catches `sqlite3.OperationalError` and checks for "unable to open database file" errors:

```python
except sqlite3.OperationalError as e:
    if "unable to open database file" in str(e):
        return json.dumps({
            "status": "error",
            "message": "Database not yet initiated. Save a memory first."
        }, indent=2)
```

This provides clear, actionable feedback to the LLM instead of a generic SQLite error that would cause unnecessary retries.

### Key Methods for DB Management

| Method | Description |
|--------|-------------|
| `ensure_db_initialized()` | Ensures the database file exists, initializes schema, and repairs any malformed data |
| `_check_db_exists()` | Returns True if the DB file exists or can be created, False otherwise |
| `_get_connection_safe()` | Gets a SQLite connection with proper settings and error handling |
| `repair_database()` | Scans all records and fixes malformed JSON fields automatically |

## API Reference

### Core Methods (MemoryLite Class)

#### `save_memory(title, summary, memory_type, related_ids, important_keywords)`
Saves a new memory item to the database. Uses YYMMDDhhmmss format for memory IDs.

```sql
INSERT INTO memories (id, title, summary, memory_type, related_ids, keywords, created_at, updated_at)
VALUES (?, ?, ?, 0, '[]', '[]', ?, ?)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| title | str | Descriptive title (acts as "should I read more?" indicator) |
| summary | str | Detailed description with specific details (dates, numbers, links, names). Should contain specifics that won't be obvious from reading just the title. |
| memory_type | int | Integer type code (0-99 reserved, 100+ user-defined). See Memory Type Codes section above. |
| related_ids | list[str] | List of other memory IDs this is related to |
| important_keywords | list[str] | SEMANTIC KEYWORDS/PHRASES useful for SEARCHING and MATCHING |

#### `get_memory_by_id(memory_id, details_level=2)`
Retrieves a specific memory by its unique ID.

```sql
SELECT id, title, summary, memory_type, related_ids, keywords, created_at, updated_at FROM memories WHERE id = ?
```

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_id | str | The unique identifier of the memory |
| details_level | int | 0=minimal (title+keywords only), 1=excludes summary (default: 2=full) |

#### `get_memories_by_ids(memory_ids, details_level=1)`
Retrieves multiple memories by their IDs.

```sql
SELECT id, title, memory_type, related_ids, keywords, created_at, updated_at FROM memories WHERE id IN (?, ?, ?)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_ids | list[str] | List of unique identifiers to retrieve |
| details_level | int | 0=minimal, 1=excludes summary (default), 2=full |

#### `search(pattern=None, memory_type=None, details_level=1, wordJoin="OR")`
Searches memories across ALL text fields in a SINGLE query:

```sql
-- Search by pattern (matches any field) - OR mode (default): Each word searched independently
SELECT id, title, ... FROM memories 
WHERE title LIKE '%pattern%' OR summary LIKE '%pattern%' OR keywords LIKE '%pattern%'

-- Filter by memory type
SELECT * FROM memories WHERE memory_type = 1

-- AND mode: Each word must appear in each field
title LIKE '%word1%' AND summary LIKE '%word2%' AND keywords LIKE '%word3%'
```

| Parameter | Type | Description |
|-----------|------|-------------|
| pattern | str | Text to search for (each space-separated token becomes a separate LIKE condition) |
| memory_type | int | Filter by memory type code (0-6 for built-in types) |
| details_level | int | 0=minimal, 1=excludes summary (default), 2=full |
| wordJoin | str | How to combine multi-word patterns - "OR" (any word matches, default) or "AND" (all words must match) |

#### `get_all_memories(details_level=1)`
Gets all stored memories. Use `details_level` to control output detail.

```sql
-- Default (details_level=1, without summary):
SELECT id, title, memory_type, related_ids, keywords, created_at, updated_at FROM memories ORDER BY created_at DESC

-- Full (details_level=2):
SELECT id, title, summary, memory_type, related_ids, keywords, created_at, updated_at FROM memories ORDER BY created_at DESC
```

| Parameter | Type | Description |
|-----------|------|-------------|
| details_level | int | 0=minimal (title+keywords only), 1=excludes summary (default), 2=full |

#### `replace_memory(memory_id, updates)`
Replaces fields in an existing memory record. Returns True if replacement was successful.

**Note:** This method REPLACES field values entirely - it does NOT add to existing content. To ADD content without losing existing data, use the dedicated append methods:
- `append_to_summary()` - to append text to summary
- `append_to_keywords()` - to add keywords
- `append_to_related_ids()` - to add related IDs

```sql
UPDATE memories SET title = ?, summary = ?, ... , updated_at = ? WHERE id = ?
```

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_id | str | The unique identifier of the memory to update |
| updates | dict | Dictionary of fields to update (e.g., `{"title": "New Title", "memory_type": 2}`) |

Valid keys for updates: `title`, `summary`, `memory_type`, `related_ids`, `keywords`

#### `append_to_summary(memory_id, summary_addition, separator)`
Appends text to an existing memory's summary field with a configurable separator.

Use this to accumulate additional information about a memory over time instead of replacing the entire summary.

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_id | str | The unique identifier of the memory to append to |
| summary_addition | str | Text to append to the summary |
| separator | str | Custom separator between old and new content (default: `"\\n\\n---\\n\\n"`) |

Returns: Dict with `status`, `original_summary_length`, `new_summary_length`, and `separator_used`.

#### `append_to_keywords(memory_id, new_keywords)`
Appends new keywords to an existing memory's keywords list. Duplicates are automatically filtered out.

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_id | str | The unique identifier of the memory to update |
| new_keywords | list[str] | List of keyword strings to add |

Returns: Dict with `status`, `added_keywords`, `added_count`, `removed_duplicates`, and `total_count`.

#### `append_to_related_ids(memory_id, new_related_ids)`
Appends new related IDs to an existing memory's related_ids list. Duplicates are automatically filtered out.

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_id | str | The unique identifier of the memory to update |
| new_related_ids | list[str] | List of memory ID strings to add |

Returns: Dict with `status`, `added_related_ids`, `added_count`, `removed_duplicates`, and `total_count`.

#### `select_memory(memory_id, pattern, mode, start_line, end_line)`
Selects/searches text within a memory's summary field.

**Search Modes:**
- **`"exact"`** (default): Exact string matching (case-sensitive)
- **`"regex"`**: Regular expression pattern matching
- **`"lines"`**: Line range selection using `start_line` and `end_line` (1-based)

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_id | str | The unique identifier of the memory |
| pattern | str | Search pattern (required for exact/regex modes) |
| mode | str | Search mode: `"exact"`, `"regex"`, or `"lines"` |
| start_line | int | Start line number (for "lines" mode, 1-based) |
| end_line | int | End line number (for "lines" mode, 1-based, inclusive) |

Returns: Dict with `status`, `occurrences`, `matched_text`, `truncated`, `selection_id`, and `match_positions`.

**Truncation:** If matched text exceeds 500 characters, it is truncated to: `first 200 chars \n...<truncated>... last 200 chars`

#### `edit_selection(selection_id, replacement, occurrence)`
Edits text based on a previous selection. The selection is **nullified** after editing.

| Parameter | Type | Description |
|-----------|------|-------------|
| selection_id | str | The selection ID returned from `select_memory` |
| replacement | str | Text to replace matched content with |
| occurrence | int | Which occurrence to edit: `1`=first, `2`=second, `0`=all |

Returns: Dict with `status`, `changes_made`, `edits`, and `selection_nullified` flag.

**Important:** After editing, the selection is nullified. You must call `select_memory` again to select new content.

#### `delete_selection(selection_id, occurrence)`
Deletes text based on a previous selection. This is a convenience method that removes selected text without replacement.

| Parameter | Type | Description |
|-----------|------|-------------|
| selection_id | str | The selection ID returned from `select_memory` |
| occurrence | int | Which occurrence to delete: `1`=first, `2`=second, `0`=all |

Returns: Dict with `status`, `changes_made`, `edits`, and `selection_nullified` flag.

**Use this tool** when you want to remove text without replacing it with new content.

#### `append_selection(selection_id, addition, occurrence)`
Appends text after each selected match. Unlike `edit_selection` (which replaces), this preserves the original text and inserts new content after it.

| Parameter | Type | Description |
|-----------|------|-------------|
| selection_id | str | The selection ID returned from `select_memory` |
| addition | str | Text to append after each matched occurrence |
| occurrence | int | Which occurrence(s) to append to: `1`=first, `2`=second, `0`=all |

Returns: Dict with `status`, `changes_made`, `appends`, and `selection_nullified` flag.

**Use this tool** for:
- Continuing incomplete code (e.g., append closing braces, function bodies)
- Adding fix comments after bug markers
- Extending partial sentences or thoughts

#### `delete_memory(memory_id)`
Deletes a specific memory by ID. Returns True if deleted successfully.

```sql
DELETE FROM memories WHERE id = ?
```

#### `get_memory_stats()`
Returns statistics about the database including counts per type code.

```sql
SELECT COUNT(*) FROM memories
SELECT COUNT(*) FROM memories WHERE memory_type = 1
```

#### `get_all_types()`
Gets all unique memory type codes used in the database with their counts.

```sql
SELECT memory_type, COUNT(*) as count FROM memories GROUP BY memory_type ORDER BY memory_type
```

#### `get_all_keywords(pattern=None)`
Lists all keywords and titles, optionally filtered by pattern.

```sql
SELECT title, keywords FROM memories WHERE title LIKE ? GROUP BY id ORDER BY created_at DESC
```

#### `get_all_words(pattern=None)`
Extracts all words from every text field in the database. Scans title, summary, and keywords fields. Returns a breakdown of which words appear in which fields.

**NOTE**: This is an EXPENSIVE operation as it must fetch all records with their full text. Use only when a deep word-level search is required.

### FastMCP Tools

All tools return JSON strings for MCP compatibility. Each tool function catches `sqlite3.OperationalError` and checks for "unable to open database file" errors.

| Tool Name | Description | Key Parameters |
|-----------|-------------|----------------|
| `save_memory` | Save a new memory item | title, summary, memory_type (int or name string), related_ids, keywords |
| `get_memory_by_id` | Retrieve specific memory by ID | memory_id, details_level (default: 2) |
| `get_memories_by_ids` | Retrieve multiple memories by IDs | memory_ids_str (comma-separated), details_level (default: 1) |
| `search` | Search memories across all fields | pattern, memory_type, details_level (default: 1), wordJoin |
| `get_all_memories` | Get all stored memories | details_level (default: 1) |
| `get_all_types` | Show available type codes with counts | None |
| `get_all_keywords` | List all keywords and titles | Optional pattern filter |
| `get_memory_stats` | View memory statistics | None |
| `delete_memory` | Delete a memory item | memory_id |
| `replace_memory` | Replace fields in an existing memory | memory_id, updates (JSON string or dict) |
| `append_to_summary` | Append text to summary with configurable separator | memory_id, summary_addition, separator (default: `"\\n\\n---\\n\\n"`) |
| `append_to_keywords` | Add keywords without losing existing ones | memory_id, keywords (JSON string or list) |
| `append_to_related_ids` | Add related IDs without losing existing links | memory_id, related_ids (JSON string or list) |
| `select_memory` | Select/search text within a memory's summary | memory_id, pattern, mode (exact/regex/lines), start_line, end_line |
| `edit_selection` | Edit previously selected text | selection_id, replacement, occurrence (1=first, 2=second, 0=all) |
| `delete_selection` | Delete previously selected text | selection_id, occurrence (1=first, 2=second, 0=all) |
| `append_selection` | Append text after previously selected text | selection_id, addition, occurrence (1=first, 2=second, 0=all) |

## Best Practices for LLM Usage

### Title Guidelines
- **Be expressive but concise**: The title should help you determine if reading the full summary is necessary
- **Think of it as a "should I read more?" indicator**: If the title suggests depth, read the summary; otherwise skip it
- **Include key context**: Mention the subject matter and any relevant identifiers in the title itself

### Summary Guidelines
- **Include specific details**: Dates, numbers, links, names, URLs, file paths - anything that wouldn't be obvious from a general reading
- **Summarize what was learned**: The summary should capture the essence of the conversation or document
- **Be thorough but organized**: Use paragraphs or bullet points for complex topics
- **Use append_to_summary**: When accumulating information over time, use the append method instead of update to avoid losing context

### Memory Type Selection
Use the appropriate type code for each memory:
- **0** = Unspecified - when no specific category applies
- **1** = Personal - user's life, feelings, experiences, relationships
- **2** = Document - summaries/info from specific documents provided
- **3** = Reference - general knowledge from internet searches or external sources
- **4** = Chat - general conversation without specific topic
- **5** = Chitchat - casual, not significant, nothing new learned
- **6** = Technical - coding, git repos, programming, math, science
- **100+** = User-defined - define custom types using type 100 memories

### Keywords Guidelines
The `keywords` field is PRIMARY for semantic recall. Populate it with the exact words/phrases you'd use if you remembered this memory later but couldn't remember its title or summary. Include:
- Product names, model numbers, technical specifications
- Key concepts, frameworks, or methodologies mentioned
- Names of people, organizations, or locations relevant to this memory
- Specific dates, version numbers, or identifiers that could be searched later
- Phrases that capture the essence of what this memory is about

Do NOT include:
- Words already covered by 'title' or 'summary' (avoid simple duplication)
- Generic stop words (the, a, an, for, etc.)
- Anything too broad to be useful as a search term

### Related IDs Usage
- Link related memories by their IDs to create memory graphs
- Helps traverse connected knowledge across sessions
- Use `append_to_related_ids` to add new connections without losing existing ones

## Select and Edit Workflow

The `select_memory` and `edit_selection` tools provide a two-step workflow for precise text editing within memories.

### Step 1: Select Memory
```
select_memory(memory_id="260620174500", pattern="bug", mode="exact")
```

Returns:
```json
{
  "status": "success",
  "memory_id": "260620174500",
  "mode": "exact",
  "occurrences": 3,
  "matched_text": "The bug was found...\n...<truncated>...bug in production",
  "truncated": true,
  "selection_id": "sel_260620174500_1",
  "match_positions": [{"start": 42, "end": 45}, ...]
}
```

### Step 2: Edit Selection
```
edit_selection(selection_id="sel_260620174500_1", replacement="error", occurrence=1)
```

- `occurrence=1`: Replace only the first "bug"
- `occurrence=2`: Replace only the second "bug"
- `occurrence=0`: Replace ALL occurrences

**Important:** After editing, the selection is nullified. You must call `select_memory` again for further edits.

## Appending vs Updating Memories

### When to Use `append_*` Methods

The append methods are designed for **accumulating information** over time without losing existing data:

```python
# Instead of replace_memory (which replaces):
# replace_memory(memory_id, {"keywords": ["new_keyword"]})  # Loses old keywords!

# Use append_to_keywords (which adds):
append_to_keywords(memory_id, ["new_keyword"])  # Keeps old keywords + adds new
```

### Configurable Separator for Summary

The `append_to_summary` method uses a configurable separator (default: `"\\n\\n---\\n\\n"`):

```python
# Default separator (horizontal rule):
append_to_summary(memory_id, "New information here")
# Result: "Original summary\n\n---\n\nNew information here"

# Custom separator for dated entries:
append_to_summary(memory_id, "2026-06-20: Updated info", separator="\n\n## [2026-06-20]:\n\n")

# Custom separator for bullet points:
append_to_summary(memory_id, "- New point to remember", separator="\n- ")
```

## Details Level System

The `details_level` parameter controls what fields are returned:

| Level | Fields Included | Use Case |
|-------|----------------|----------|
| **0** | id, title, keywords | Minimal info for quick listing |
| **1** | id, title, memory_type, related_ids, keywords, created_at, updated_at | Summary excluded (default for bulk operations) |
| **2** | id, title, summary, memory_type, related_ids, keywords, created_at, updated_at | Full details including summary |

**Performance**: Level 0 and 1 reduce I/O by excluding the summary field, which is typically the largest field.

## Extending the System

### User-Defined Memory Types (100+)

To create custom memory types, save a type 100 memory with keywords defining your custom codes:

```
save_memory(
    title="Custom Type Definitions",
    summary="Define custom memory type codes for my specific use cases",
    memory_type=100,
    keywords=['["101=Health", "102=Finance", "103=Education", "104=Recipes"]']
)
```

Then use codes 101, 102, 103, 104 for your custom categories.

### Adding Repair Functions

The system automatically repairs malformed data on initialization. No manual intervention needed.

## File Structure

```
~/.swordmemory/
  memory.db          # SQLite database file (replaces memory.json)
```

## Quick Start

```python
from memorylite import MemoryLite

# Create instance
mem = MemoryLite()

# Save a new memory (memory_type=6 for technical)
result = mem.save_memory(
    title="Debugging: Check Logs Before Making Changes",
    summary="When debugging, always check the application logs first. Specific detail: The error was in /var/log/app.log line 42.",
    memory_type=6,  # Technical
    related_ids=[],
    important_keywords=["debugging", "logs", "best-practice"]
)

# Retrieve by ID (full details)
memory = mem.get_memory_by_id(result["id"], details_level=2)

# Search across all fields
results = mem.search(pattern="application logs", details_level=1)

# Append to summary with new information
mem.append_to_summary(result["id"], "Additional finding: The bug was caused by a race condition.")

# Add new keywords without losing old ones
mem.append_to_keywords(result["id"], ["race-condition", "bug-fix"])

# Link to another related memory
mem.append_to_related_ids(result["id"], [another_memory_id])

# Get all types with counts
all_types = mem.get_all_types()
```

## Running as MCP Server

```bash
python memorylite.py
```

This starts the FastMCP server with all tools available.

### CLI Arguments

| Argument | Description |
|----------|-------------|
| `--path` | Database path: if ends with '/' treats as directory (appends memory.db), otherwise uses as full file path |

Examples:
```bash
python memorylite.py --path /home/user/memory/     # Use directory as base for memory.db
python memorylite.py --path /home/user/mydb.db      # Use full file path directly
python memorylite.py                                # Use default ~/.swordmemory/memory.db
```

## Migration from memorydb

| Aspect | memorydb (JSON) | memorylite (SQLite) |
|--------|-----------------|---------------------|
| Storage | Single JSON file (`memory.json`) | SQLite database (`memory.db`) |
| Search | Regex + string matching in Python | SQL SELECT with WHERE/LIKE/IN clauses |
| Indexes | In-memory dicts (`by_id`, `by_keyword`) | Database-level B-tree indexes |
| Transactions | Manual file read/write | ACID transactions via SQLite |
| Type System | JSON array of strings | Single integer type code |
| Related Items | Separate field | Removed (use related_ids) |
| Detail Control | include_summary (boolean) | details_level (0/1/2) |
| Query Style | Multiple passes over data | Single query across all fields |

## Troubleshooting

### Unable to Open Database File Error

When the database doesn't exist yet, each tool function catches the `sqlite3.OperationalError` with "unable to open database file" and returns:
```json
{
  "status": "error",
  "message": "Database not yet initiated. Save a memory first."
}
```

This provides clear, actionable feedback to the LLM instead of a generic SQLite error.

### Malformed Data Handling

The system automatically repairs malformed JSON data in `related_ids` and `keywords` fields on each initialization. Common issues fixed:
- Single quotes instead of double quotes
- `None` instead of `null`
- Empty strings
- Non-list values

### Summary Field Not Included

By default, `get_all_memories` uses `details_level=1` which excludes the summary. To include it:
- Use `details_level=2` for full details
- Use `get_memory_by_id()` which defaults to `details_level=2`

## Error Handling Details

Each tool function implements consistent error handling:

1. **Primary exception handler**: Catches `sqlite3.OperationalError` for "unable to open database file" errors
2. **Secondary exception handler**: Catches all other exceptions and returns them as JSON strings
3. **Return format**: All tools return JSON strings with status, message, and data fields

Example error handling pattern:
```python
try:
    result = mem.get_memory_by_id(memory_id)
except sqlite3.OperationalError as e:
    if "unable to open database file" in str(e):
        return json.dumps({
            "status": "error",
            "message": "Database not yet initiated. Save a memory first."
        }, indent=2)
    return json.dumps({"status": "error", "message": str(e)})
except Exception as e:
    return json.dumps({"status": "error", "message": str(e)})
```

This ensures that the LLM always receives clear, actionable feedback about database state.