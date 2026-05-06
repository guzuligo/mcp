# memorydb - LLM Memory Database System

## Overview

`memorydb` is an LLM memory management system built on FastMCP that uses a **single JSON file** as its database backend. This approach was chosen for being cross-platform friendly, requiring no additional installation, and making version control and backups straightforward.

## Architecture

### Design Philosophy

The system follows a **database-style approach** where all memories are stored in one central JSON file with in-memory indexes for fast lookups. Each memory item has a unique ID and can be related to other memories by their IDs rather than filenames.

#### Why JSON?
- **Cross-platform friendly**: Works on any OS without platform-specific dependencies
- **No installation required**: Just Python's built-in `json` module
- **Human readable**: Easy to inspect, edit, and version control
- **Easy backups**: Single file vs directory tree simplifies backup processes

### Database Schema

The database is stored in a single JSON file (`~/.swordmemory/memory.json`) with the following structure:

```json
{
  "version": "1.0",
  "last_updated": "ISO timestamp or null",
  "memories": [
    {
      "id": "uuid4-unique-identifier",
      "keyword": "unique-keyword-id",
      "title": "Descriptive title for quick scanning",
      "summary": "Detailed description of the experience/knowledge",
      "memory_types": ["personal", "document"],
      "related_ids": ["id1", "id2"],
      "important_keywords_related": ["keyword1", "keyword2"],
      "created_at": "ISO timestamp",
      "updated_at": "ISO timestamp"
    }
  ],
  "indexes": {
    "by_id": {"uuid4-unique-id": "keyword"},
    "by_keyword": {"keyword": "uuid4-unique-id"},
    "by_types": {
      "personal": ["id1", "id2"],
      "document": ["id3"]
    }
  }
}
```

### Memory Record Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string (UUID4) | Auto-generated unique identifier for each memory item |
| `keyword` | string | Unique identifier/ID that also acts as a title reference |
| `title` | string | Short descriptive title - should be expressive enough to help determine if reading the summary is needed. Think of it as a "should I read more?" indicator. Make it descriptive but concise. |
| `summary` | string | Detailed description of the experience/knowledge. **IMPORTANT**: Include specific details like dates, numbers, links, names, and any non-general information that shouldn't get lost. The summary should summarize what was learned from the conversation. |
| `memory_types` | array of strings | Category tags for grouping related memories across users |
| `related_ids` | array of strings | List of other memory IDs this is related to (replaces old related_files) |
| `important_keywords_related` | array of strings | Keywords for lookup/searching |
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

### Core Methods (MemoryDB Class)

#### `save_memory(keyword, title, summary, types, related_ids, important_keywords)`
Saves a new memory item to the database.

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

#### `get_memory_by_keyword(keyword)`
Retrieves a specific memory by its keyword/ID. Returns the full memory record or None.

#### `search(pattern=None, types=None, keyword=None)`
Searches memories with three modes:
- By regex pattern across titles, summaries, and keywords
- Filter by type tags (returns all matching)
- Exact keyword match

Returns list of matching memory records.

#### `get_all_memories()`
Returns all stored memories as a list.

#### `update_memory(memory_id, updates)`
Updates an existing memory record. Returns True if updated successfully.

| Parameter | Type | Description |
|-----------|------|-------------|
| memory_id | str | The unique identifier of the memory to update |
| updates | dict | Dictionary of fields to update (e.g., {"title": "New Title"}) |

#### `delete_memory(memory_id)`
Deletes a specific memory by ID. Returns True if deleted successfully.

#### `get_memory_stats()`
Returns statistics about the database including counts per type tag.

### FastMCP Tools

All tools return JSON strings for MCP compatibility.

| Tool Name | Description | Key Parameters |
|-----------|-------------|----------------|
| `save_memory` | Save a new memory item | keyword, title, summary, types (JSON array string), related_ids, important_keywords |
| `get_memory_by_id` | Retrieve specific memory by ID | memory_id |
| `get_memory_by_keyword` | Retrieve specific memory by keyword | keyword |
| `search` | Search memories by pattern or type filter | pattern (regex), types (JSON array string), keyword |
| `get_all_memories` | Get all stored memories | None |
| `get_memory_stats` | View memory statistics | None |
| `delete_memory` | Delete a memory item | memory_id |
| `update_memory` | Update an existing memory | memory_id, updates (JSON string) |
| `get_all_memory_types` | Show available type tags | None |

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
- This replaces the old file-based relationship system
- Helps traverse connected knowledge across sessions

## Extending the System

### Adding New Type Tags
Modify the `__valid_types()` method in the `MemoryDB` class:
```python
def __valid_types(self) -> List[str]:
    return ["personal", "document", "reference", "chat", "chitchat", "technical", "new_tag"]
```

### Custom Indexes
Add new index types in `_update_indexes()` and `_remove_from_indexes()` methods.

### Export/Import
The database is a single JSON file, so it can be:
- Copied directly for backup
- Merged with other databases by combining `memories` arrays
- Converted to CSV or other formats using standard Python libraries

## File Structure

```
~/.swordmemory/
  memory.json          # Single database file containing all memories and indexes
```

## Quick Start

```python
from memorydb import MemoryDB, create_memory

# Create instance
mem = create_memory()

# Save a new memory
result = mem.save_memory(
    keyword="debugging_tip",
    title="Debugging: Check Logs Before Making Changes",
    summary="When debugging, always check the application logs first. Specific detail: The error was in /var/log/app.log line 42.",
    types=["technical", "personal"],
    related_ids=[],
    important_keywords=["debugging", "logs", "best-practice"]
)

# Retrieve by keyword
memory = mem.get_memory_by_keyword("debugging_tip")
```

## Running as MCP Server

```bash
python memorydb.py
```

This starts the FastMCP server with all tools available.