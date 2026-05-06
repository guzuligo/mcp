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
    id TEXT PRIMARY KEY,                    -- UUID4 string, unique identifier
    keyword TEXT NOT NULL UNIQUE,           -- Unique keyword/ID for this memory
    title TEXT NOT NULL,                     -- Short descriptive title
    summary TEXT NOT NULL,                   -- Detailed description with specific details
    memory_types TEXT NOT NULL DEFAULT '[]', -- JSON array string: '["personal","technical"]'
    related_ids TEXT NOT NULL DEFAULT '[]', -- JSON array string: '["id1","id2"]'
    important_keywords_related TEXT NOT NULL DEFAULT '[]', -- JSON array string: '["kw1","kw2"]'
    created_at TEXT NOT NULL,              -- ISO format timestamp of creation
    updated_at TEXT NOT NULL                -- ISO format timestamp of last update
);

-- Indexes for fast lookups
CREATE INDEX idx_memories_keyword ON memories(keyword);
CREATE INDEX idx_memories_id ON memories(id);
CREATE INDEX idx_memories_created_at ON memories(created_at);
```

**IMPORTANT NOTES FOR LLMs:**
- `memory_types`, `related_ids`, and `important_keywords_related` are stored as **JSON array strings** (e.g., `'["personal","technical"]'`)
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
| `id` | string (UUID4) | Auto-generated unique identifier for each memory item |
| `keyword` | string | Unique identifier/ID that also acts as a title reference |
| `title` | string | Short descriptive title - should be expressive enough to help determine if reading the summary is needed. Think of it as a "should I read more?" indicator. Make it descriptive but concise. |
| `summary` | string | Detailed description of the experience/knowledge. **IMPORTANT**: Include specific details like dates, numbers, links, names, and any non-general information that shouldn't get lost. The summary should summarize what was learned from the conversation. |
| `memory_types` | array of strings (stored as JSON) | Category tags for grouping related memories across users |
| `related_ids` | array of strings (stored as JSON) | List of other memory IDs this is related to |
| `important_keywords_related` | array of strings (stored as JSON) | Keywords for lookup/searching |
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

## API Reference

### Core Methods (MemoryLite Class)

#### `save_memory(keyword, title, summary, types, related_ids, important_keywords)`
Saves a new memory item to the database.

```sql
INSERT INTO memories (id, keyword, title, summary, memory_types, related_ids, important_keywords_related, created_at, updated_at)
VALUES (?, ?, ?, ?, '["personal"]', '[]', '[]', ?, ?)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| keyword | str | Unique identifier/ID for this memory item |
| title | str | Descriptive title (acts as "should I read more?" indicator) |
| summary | str | Detailed description with specific details (dates, numbers, links, names) |
| types | list[str] | Category tags: ["personal", "document", "reference", "chat", "chitchat", "technical"] |
| related_ids | list[str] | List of other memory IDs this is related to |
| important_keywords | list[str] | Keywords for lookup/searching |

#### `get_memory_by_id(memory_id)`
Retrieves a specific memory by its unique ID. Returns the full memory record or None.

```sql
SELECT * FROM memories WHERE id = ?
-- Or without summary (default):
SELECT id, keyword, title, memory_types, related_ids, important_keywords_related, created_at, updated_at FROM memories WHERE id = ?
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

#### `search(pattern=None, types=None, keyword=None, include_summary=False)`
Searches memories across ALL text fields in a SINGLE query:

```sql
-- Search by pattern (matches any field)
SELECT id, keyword, title, ... FROM memories 
WHERE title LIKE '%pattern%' OR summary LIKE '%pattern%' OR keyword LIKE '%pattern%'

-- Filter by type tags using json_each
SELECT id, keyword, title, ... FROM memories, json_each(memory_types) 
WHERE json_each.value = 'personal'

-- Exact keyword match
SELECT * FROM memories WHERE keyword = ?
```

Returns list of matching memory records.

#### `get_all_memories(include_summary=False)`
Gets all stored memories. By default, the `summary` field is excluded to reduce I/O cost.

```sql
-- Default (without summary):
SELECT id, keyword, title, memory_types, related_ids, important_keywords_related, created_at, updated_at FROM memories ORDER BY created_at DESC

-- With summary:
SELECT id, keyword, title, summary, memory_types, related_ids, important_keywords_related, created_at, updated_at FROM memories ORDER BY created_at DESC
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
SELECT COUNT(DISTINCT id) FROM memories, json_each(memory_types) WHERE json_each.value = 'personal'
```

#### `get_all_types()` [NEW]
Gets all unique type tags used in the database with their counts.

```sql
SELECT json_each.value as type_name, COUNT(DISTINCT id) as count 
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

### FastMCP Tools

All tools return JSON strings for MCP compatibility.

| Tool Name | Description | Key Parameters |
|-----------|-------------|----------------|
| `save_memory` | Save a new memory item | keyword, title, summary, types (JSON array string), related_ids, important_keywords |
| `get_memory_by_id` | Retrieve specific memory by ID | memory_id |
| `get_memories_by_ids` | Retrieve multiple memories by IDs | Comma-separated string of IDs |
| `get_memory_by_keyword` | Retrieve specific memory by keyword | keyword |
| `search` | Search memories across all fields in single query | pattern (text to search), types (JSON array string), keyword |
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

### Related IDs Usage
- Link related memories by their IDs to create memory graphs
- Helps traverse connected knowledge across sessions

## Performance Optimizations

### include_summary=False Default
All retrieval methods default to `include_summary=False`, which means:
- Queries explicitly list columns instead of using `SELECT *`
- The `summary` field is excluded unless requested
- Reduces I/O cost significantly for large datasets

### Single Query Search
The `search()` method uses a single SQL query with OR conditions across all searchable fields:
```sql
SELECT id, keyword, title, ... FROM memories 
WHERE title LIKE ? OR summary LIKE ? OR keyword LIKE ? OR memory_types LIKE ? OR important_keywords_related LIKE ?
```

This is more efficient than the old approach of multiple passes through the data.

### json_each for Type Filtering
Type filtering uses SQLite's `json_each()` function:
```sql
SELECT id FROM memories, json_each(memory_types) WHERE json_each.value = 'personal'
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
from memorylite import MemoryLite, create_memory

# Create instance
mem = MemoryLite()

# Save a new memory
result = mem.save_memory(
    keyword="debugging_tip",
    title="Debugging: Check Logs Before Making Changes",
    summary="When debugging, always check the application logs first. Specific detail: The error was in /var/log/app.log line 42.",
    types=["technical", "personal"],
    related_ids=[],
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

### Database Not Found
The database is automatically created at `~/.swordmemory/memory.db` on first use. If it doesn't exist, the `_init_db()` method creates the schema.

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