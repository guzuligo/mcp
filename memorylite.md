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
- **Reduced I/O**: `include_summary=False` default reduces data transfer cost
- **Scalability**: SQLite handles large datasets better than in-memory JSON

### Database Schema

The database is stored in a single file (`~/.swordmemory/memory.db`) with the following structure:

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,                    -- YYMMDDhhmmss format timestamp-based ID
    keyword TEXT NOT NULL UNIQUE,           -- Unique keyword/ID for this memory
    title TEXT NOT NULL,                     -- Short descriptive title
    summary TEXT NOT NULL,                   -- Detailed description with specific details
    memory_types TEXT NOT NULL DEFAULT '[]', -- JSON array string: '["personal","technical"]'
    related_ids TEXT NOT NULL DEFAULT '[]', -- JSON array string: '["id1","id2"]'
    related_items TEXT NOT NULL DEFAULT '[]', -- JSON array string for batch updates
    keywords TEXT NOT NULL DEFAULT '[]',     -- Semantic keywords for searching
    created_at TEXT NOT NULL,              -- ISO format timestamp of creation
    updated_at TEXT NOT NULL                -- ISO format timestamp of last update
);

-- Indexes for fast lookups
CREATE INDEX idx_memories_keyword ON memories(keyword);
CREATE INDEX idx_memories_id ON memories(id);
CREATE INDEX idx_memories_created_at ON memories(created_at);
```

**IMPORTANT NOTES FOR LLMs:**
- `memory_types`, `related_ids`, `related_items`, and `keywords` are stored as **JSON array strings** (e.g., `'["personal","technical"]'`)
- Use `json_each()` for SQL-based array membership checks:
  ```sql
  SELECT * FROM memories, json_each(memory_types) WHERE json_each.value = 'personal'
  ```
- All queries should specify columns explicitly (avoid `SELECT *`):
  - Default: exclude the `summary` field to reduce I/O cost
  - Use `include_summary=True` parameter when full details are needed

### Memory Record Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string (YYMMDDhhmmss) | Timestamp-based unique identifier (e.g., 260506193000 for 2026-05-06 19:30:00) |
| `keyword` | string | Unique identifier/ID that also acts as a title reference |
| `title` | string | Short descriptive title - should be expressive enough to help determine if reading the summary is needed. Think of it as a "should I read more?" indicator. Make it descriptive but concise. |
| `summary` | string | Detailed description of the experience/knowledge. **IMPORTANT**: Include specific details like dates, numbers, links, names, and any non-general information that shouldn't get lost. The summary should summarize what was learned from the conversation. |
| `memory_types` | array of strings (stored as JSON) | Category tags for grouping related memories across users |
| `related_ids` | array of strings (stored as JSON) | List of other memory IDs this is related to |
| `related_items` | array of strings (stored as JSON) | GROUP of related memory IDs that should be updated together as a batch when one is modified |
| `keywords` | array of strings (stored as JSON) | SEMANTIC KEYWORDS/PHRASES useful for SEARCHING and MATCHING. These are terms the user might remember and search by later. Include product names, technical terms, key concepts, or phrases that capture the essence of this memory. This field is PRIMARY for semantic recall. |
| `created_at` | string (ISO) | Timestamp when the memory was created |
| `updated_at` | string (ISO) | Timestamp of last update |

### Memory Type Tags

Each memory item can have **multiple type tags** for flexible grouping. This ensures that even if multiple users use the system, their memories won't get mixed up.

| Tag | Description | Use Case |
|-----|-----------|----------|
| `personal` | User's name, age, facts about the user | When user talked about themselves |
| `document` / `reference` | Memories from provided documents or referenced pages | When a document was provided or a page was discussed |
| `chat` / `chitchat` | General conversation (lower priority) | Casual conversation without important content |
| `technical` | Technical details, code snippets, configurations | Programming-related discussions |

**Example**: A memory about fixing a bug while discussing a document could have tags: `["personal", "document", "technical"]`.

## Database Initialization and Error Handling

### Automatic DB Initialization

Each tool function automatically initializes the database if it doesn't exist. The system handles two cases for the database path:
- If `DB_FILE` ends with `/` or `\`, it's treated as a directory and `memory.db` is appended to form the file path
- Otherwise, `DB_FILE` is used as the full file path directly

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
| `ensure_db_initialized()` | Ensures the database file exists and is initialized. Creates the DB file if it doesn't exist, then initializes the schema. Raises FileNotFoundError if the parent directory can't be created. |
| `_check_db_exists()` | Returns True if the DB file exists or can be created, False otherwise |
| `_get_connection_safe()` | Gets a SQLite connection with proper settings and error handling. Returns (conn, None) on success or (None, error_message) if the DB file doesn't exist yet. |

## API Reference

### Core Methods (MemoryLite Class)

#### `save_memory(keyword, title, summary, types, related_ids, related_items, important_keywords)`
Saves a new memory item to the database. Uses YYMMDDhhmmss format for memory IDs with sequence counter for same-second creations.

```sql
INSERT INTO memories (id, keyword, title, summary, memory_types, related_ids, related_items, keywords, created_at, updated_at)
VALUES (?, ?, ?, ?, '["personal"]', '[]', '[]', '[]', ?, ?)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| keyword | str | Unique identifier/ID for this memory item |
| title | str | Descriptive title (acts as "should I read more?" indicator) |
| summary | str | Detailed description with specific details (dates, numbers, links, names). Should contain specifics that won't be obvious from reading just the title. |
| types | list[str] | Category tags: ["personal", "document", "reference", "chat", "chitchat", "technical"] |
| related_ids | list[str] | List of other memory IDs this is related to |
| related_items | list[str] | GROUP of related memory IDs that should be updated together as a batch when one is modified |
| important_keywords | list[str] | SEMANTIC KEYWORDS/PHRASES useful for SEARCHING and MATCHING. Include product names, technical terms, key concepts, or phrases that capture the essence of this memory. This field is PRIMARY for semantic recall - populate it with words/phrases a user would naturally use when remembering or searching for this memory later. |

#### `get_memory_by_id(memory_id)`
Retrieves a specific memory by its unique ID. Returns the full memory record or None.

```sql
SELECT * FROM memories WHERE id = ?
-- Or without summary (default):
SELECT id, keyword, title, memory_types, related_ids, related_items, keywords, created_at, updated_at FROM memories WHERE id = ?
```

#### `get_memories_by_ids(memory_ids)`
Retrieves multiple memories by their IDs. Accepts a list of UUID4 strings.

```sql
SELECT id, keyword, title, ... FROM memories WHERE id IN (?, ?, ?)
```

#### `get_memory_by_keyword(keyword)`
Retrieves a specific memory by its keyword/ID. Returns the full memory record or None.

```sql
SELECT * FROM memories WHERE keyword = ?
```

#### `search(pattern=None, types=None, keyword=None, include_summary=False, wordJoin="OR")`
Searches memories across ALL text fields in a SINGLE query:

```sql
-- Search by pattern (matches any field) - OR mode (default): Each word searched independently
SELECT id, keyword, title, ... FROM memories 
WHERE title LIKE '%pattern%' OR summary LIKE '%pattern%' OR keyword LIKE '%pattern%'

-- Filter by type tags using json_each
SELECT id, keyword, title, ... FROM memories, json_each(memory_types) 
WHERE json_each.value = 'personal'

-- Exact keyword match
SELECT * FROM memories WHERE keyword = ?

-- AND mode: Each word must appear in each field
title LIKE '%word1%' AND summary LIKE '%word2%' AND keywords LIKE '%word3%'
```

| Parameter | Type | Description |
|-----------|------|-------------|
| pattern | str | Text to search for (each space-separated token becomes a separate LIKE condition) |
| types | list[str] | Filter by type tags (e.g., ["personal"]) |
| keyword | str | Exact keyword match |
| include_summary | bool | Whether to include the summary field (default: False) |
| wordJoin | str | How to combine multi-word patterns - "OR" (any word matches, default) or "AND" (all words must match) |

Returns list of matching memory records.

#### `get_all_memories(include_summary=False)`
Gets all stored memories. By default, the `summary` field is excluded to reduce I/O cost.

```sql
-- Default (without summary):
SELECT id, keyword, title, memory_types, related_ids, related_items, keywords, created_at, updated_at FROM memories ORDER BY created_at DESC

-- With summary:
SELECT id, keyword, title, summary, memory_types, related_ids, related_items, keywords, created_at, updated_at FROM memories ORDER BY created_at DESC
```

#### `update_memory(memory_id, updates)`
Updates an existing memory record. Returns True if updated successfully.

```sql
UPDATE memories SET keyword = ?, title = ?, ... , updated_at = ? WHERE id = ?
```

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_id | str | The unique identifier of the memory to update |
| updates | dict | Dictionary of fields to update (e.g., {"title": "New Title"}) |

Valid keys for updates: `keyword`, `title`, `summary`, `memory_types`, `related_ids`, `related_items`, `keywords`

#### `delete_memory(memory_id)`
Deletes a specific memory by ID. Returns True if deleted successfully.

```sql
DELETE FROM memories WHERE id = ?
```

#### `get_memory_stats()`
Returns statistics about the database including counts per type tag.

```sql
SELECT COUNT(*) FROM memories
-- Per-type counts using json_each:
SELECT COUNT(DISTINCT id FROM memories, json_each(memory_types) WHERE json_each.value = 'personal'
```

#### `get_all_types()` [NEW]
Gets all unique type tags used in the database with their counts.

```sql
SELECT json_each.value as type_name, COUNT(DISTINCT id as count 
FROM memories, json_each(memory_types) 
GROUP BY type_name
```

#### `get_all_keywords(pattern=None)` [NEW]
Lists all keywords and titles, optionally filtered by pattern.

```sql
SELECT keyword, title FROM memories GROUP BY keyword ORDER BY keyword
-- With filter:
SELECT keyword, title FROM memories WHERE keyword LIKE ? OR title LIKE ? GROUP BY keyword ORDER BY keyword
```

#### `get_all_words(pattern=None)` [NEW]
Extracts all words from every text field in the database. Scans title, summary, keyword, memory_types, related_ids, related_items, and keywords fields. Returns a breakdown of which words appear in which fields.

**NOTE**: This is an EXPENSIVE operation as it must fetch all records with their full text. Use only when a deep word-level search is required. For most use cases, `get_all_keywords()` or `search()` should be preferred for better performance.

### FastMCP Tools

All tools return JSON strings for MCP compatibility. Each tool function catches `sqlite3.OperationalError` and checks for "unable to open database file" errors, returning a descriptive message ("Database not yet initiated. Save a memory first.") instead of letting the generic SQLite error propagate.

| Tool Name | Description | Key Parameters |
|-----------|-------------|----------------|
| `save_memory` | Save a new memory item | keyword, title, summary, types (JSON array string), related_ids, related_items, keywords |
| `get_memory_by_id` | Retrieve specific memory by ID | memory_id |
| `get_memories_by_ids` | Retrieve multiple memories by IDs | Comma-separated string of IDs |
| `get_memory_by_keyword` | Retrieve specific memory by keyword | keyword |
| `search` | Search memories across all fields in single query | pattern (text to search), types (JSON array string), keyword, wordJoin ("OR"/"AND") |
| `get_all_memories` | Get all stored memories (without summary) | None |
| `get_all_types` | Show available type tags with counts | None |
| `get_all_keywords` | List all keywords and titles | Optional pattern filter |
| `get_memory_stats` | View memory statistics | None |
| `delete_memory` | Delete a memory item | memory_id |
| `update_memory` | Update an existing memory | memory_id, updates (JSON string) |

## Best Practices for LLM Usage

### Title Guidelines
- **Be expressive but concise**: The title should help you determine if reading the full summary is necessary
- **Think of it as a "should I read more?" indicator**: If the title suggests depth, read the summary; otherwise skip it
- **Include key context**: Mention the subject matter and any relevant identifiers in the title itself

### Summary Guidelines
- **Include specific details**: Dates, numbers, links, names, URLs, file paths - anything that wouldn't be obvious from a general reading
- **Summarize what was learned**: The summary should capture the essence of the conversation or document
- **Be thorough but organized**: Use paragraphs or bullet points for complex topics

### Type Tag Selection
- Choose all relevant tags for each memory item
- Use `personal` when discussing user-specific information (name, age, preferences)
- Use `document`/`reference` when a document was provided or referenced
- Use `chat`/`chitchat` for casual conversation with low importance
- Use `technical` for code, configurations, and technical discussions

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
- Use `related_items` for GROUP of related memory IDs that should be updated together as a batch when one is modified

## Performance Optimizations

### include_summary=False Default
All retrieval methods default to `include_summary=False`, which means:
- Queries explicitly list columns instead of using `SELECT *`
- The `summary` field is excluded unless requested
- Reduces I/O cost significantly for large datasets

### Single Query Search
The `search()` method uses a single SQL query with OR conditions across all searchable fields. This is more efficient than the old approach of multiple passes through the data.

### json_each for Type Filtering
Type filtering uses SQLite's `json_each()` function:
```sql
SELECT id FROM memories, json_each(memory_types WHERE json_each.value = 'personal'
```

## Extending the System

### Adding New Type Tags
Modify the `VALID_TYPES` list in the `MemoryLite` class:
```python
VALID_TYPES = ["personal", "document", "reference", "chat", "chitchat", "technical", "new_tag"]
```

### Custom Queries
All SQL queries are defined within each method. To add new search modes, create new methods following the existing patterns.

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

# Save a new memory
result = mem.save_memory(
    keyword="debugging_tip",
    title="Debugging: Check Logs Before Making Changes",
    summary="When debugging, always check the application logs first. Specific detail: The error was in /var/log/app.log line 42.",
    types=["technical", "personal"],
    related_ids=[],
    related_items=[],
    important_keywords=["debugging", "logs", "best-practice"]
)

# Retrieve by keyword (without summary by default)
memory = mem.get_memory_by_keyword("debugging_tip")

# Search across all fields in single query
results = mem.search(pattern="application logs")

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
| Indexes | In-memory dicts (`by_id`, `by_keyword`, `by_types`) | Database-level B-tree indexes |
| Transactions | Manual file read/write | ACID transactions via SQLite |
| I/O Cost | Always includes full records | `include_summary=False` by default |
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

This provides clear, actionable feedback to the LLM instead of a generic SQLite error that would cause unnecessary retries. The `save_memory` tool calls `ensure_db_initialized()` which creates the directory and initializes the schema if needed.

### Database Not Found
The database is automatically created at `~/.swordmemory/memory.db`) on first use. If it doesn't exist, the `_init_db()` method creates the schema.

### Type Filtering Issues
Remember that `memory_types` is stored as a JSON array string. To check if a type exists:
```sql
-- Correct way using json_each:
SELECT * FROM memories, json_each(memory_types) WHERE json_each.value = 'personal'

-- NOT this (won't work for arrays):
WHERE memory_types LIKE '%personal%'  -- May match partial strings
```

### Summary Field Not Included
By default, all retrieval methods exclude the `summary` field. To include it:
- Use `get_memory_by_id()` which includes full records by default
- Or pass `include_summary=True` to methods that support this parameter

## Error Handling Details

Each tool function implements consistent error handling:

1. **Primary exception handler**: Catches `sqlite3.OperationalError` for "unable to open database file" errors
2. **Secondary exception handler**: Catches all other exceptions and returns them as JSON strings
3. **Return format**: All tools return JSON strings with status, message, and data fields

Example error handling pattern:
```python
try:
    # Database operation
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