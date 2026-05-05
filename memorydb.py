"""
memorydb - LLM Memory Database System (FastMCP Tool)

A FastMCP-based tool to help LLM manage memory using a single JSON file as database.

Memory types are now multi-tag based for flexible grouping:
  - personal: User's name, age, facts about the user
  - document/reference: Memories from provided documents or referenced pages
  - chat/chitchat: General conversation (lower priority)
  - technical: Technical details, code snippets, configurations

Each memory item can have multiple tags like ["personal", "document"] for flexible grouping.
This ensures that even if more users use the system, they won't get mixed up.

DATABASE SCHEMA:
Each memory record contains:
  - id: Auto-generated unique identifier (UUID)
  - keyword: Unique identifier/ID for this memory item (acts as title)
  - title: Short descriptive title - should be expressive enough to help determine if reading the summary is needed
           Think of it as a "should I read more?" indicator. Make it descriptive but concise.
  - summary: Detailed description of the experience/knowledge
             IMPORTANT: Include specific details like dates, numbers, links, names, and any non-general information
             that shouldn't get lost. The summary should summarize what was learned from the conversation.
  - memory_types: List of category tags (e.g., ["personal", "document"])
  - related_ids: List of other memory IDs this is related to
  - important_keywords_related: List of keywords for lookup/searching
  - created_at: ISO format timestamp of creation
  - updated_at: ISO format timestamp of last update

DATABASE FILE:
All memories are stored in a single JSON file (the "database") with indexes for fast lookups.
"""

import json
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Set
from fastmcp import FastMCP

# Thread lock for thread-safe database operations
_db_lock = threading.Lock()

mcp = FastMCP("memorydb")


class MemoryDB:
    """LLM Memory Management System using a single JSON file as database."""

    # Configuration - can be modified at the top level
    DB_FILE = None  # Will default to ~/Documents/.swordmemory/memory.json if not set

    def __valid_types(self) -> List[str]:
        """Get list of valid memory type tags."""
        return ["personal", "document", "reference", "chat", "chitchat", "technical"]

    def _get_db_path(self) -> Path:
        """Get the path to the database file."""
        if self.DB_FILE is not None:
            return Path(self.DB_FILE)
        else:
            home_docs = Path.home().joinpath("Documents")
            db_dir = home_docs.joinpath(".swordmemory")
            db_dir.mkdir(parents=True, exist_ok=True)
            return db_dir / "memory.json"

    def _load_db(self) -> dict:
        """Load the database from file."""
        db_path = self._get_db_path()
        if not db_path.exists():
            return {
                "version": "1.0",
                "last_updated": None,
                "memories": [],
                "indexes": {
                    "by_id": {},
                    "by_keyword": {},
                    "by_types": {}
                }
            }
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Validate and fix JSON - handle common corruption issues
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            # Try to fix common corruption: trailing commas, single quotes, etc.
            import re
            fixed = content
            # Replace trailing commas before closing braces/brackets
            fixed = re.sub(r',(\s*[}\]])', r'\1', fixed)
            # Replace single quotes with double quotes for string values
            fixed = re.sub(r"'([^']*)'", r'"\1"', fixed)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError as e2:
                raise json.JSONDecodeError(
                    f"Database file is corrupted and could not be auto-fixed. "
                    f"Original error: {e}; Fix attempt error: {e2}",
                    e.doc, e.pos
                )

    def _save_db(self, data: dict) -> None:
        """Save the database to file (atomic write via temp file)."""
        db_path = self._get_db_path()
        # Write to temp file first, then rename for atomicity
        import tempfile
        import os
        db_dir = str(Path(db_path).parent)
        fd, tmp_path = tempfile.mkstemp(dir=db_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            # Atomic rename
            if os.path.exists(tmp_path):
                os.replace(tmp_path, db_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def _save_db_locked(self, data: dict) -> None:
        """Save the database with thread lock for safety."""
        with _db_lock:
            self._save_db(data)

    def _generate_id(self) -> str:
        """Generate a unique ID for a new memory."""
        return str(uuid.uuid4())

    def _get_timestamp(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    def _update_indexes(self, db: dict, memory: dict) -> None:
        """Update the database indexes with a new or updated memory record."""
        by_id = db["indexes"]["by_id"]
        by_keyword = db["indexes"]["by_keyword"]
        by_types = db["indexes"]["by_types"]

        # Update by_id index
        by_id[memory["id"]] = memory["keyword"]

        # Update by_keyword index
        if memory["keyword"]:
            by_keyword[memory["keyword"]] = memory["id"]

        # Update by_types index (each type tag)
        for t in memory.get("memory_types", []):
            if t not in by_types:
                by_types[t] = []
            if memory["id"] not in by_types[t]:
                by_types[t].append(memory["id"])

    def _remove_from_indexes(self, db: dict, memory_id: str) -> None:
        """Remove a memory from all indexes."""
        # Find the memory to get its types and keyword
        memory = None
        for m in db["memories"]:
            if m["id"] == memory_id:
                memory = m
                break

        if not memory:
            return

        by_id = db["indexes"]["by_id"]
        by_keyword = db["indexes"]["by_keyword"]
        by_types = db["indexes"]["by_types"]

        # Remove from by_id
        if memory_id in by_id:
            del by_id[memory_id]

        # Remove from by_keyword
        for kw, mid in list(by_keyword.items()):
            if mid == memory_id:
                del by_keyword[kw]
                break

        # Remove from by_types
        for t in memory.get("memory_types", []):
            if t in by_types and memory_id in by_types[t]:
                by_types[t].remove(memory_id)

    def save_memory(
        self,
        keyword: str,
        title: str,
        summary: str,
        types: List[str] = None,
        related_ids: List[str] = None,
        important_keywords: List[str] = None
    ) -> dict:
        """Save a new memory item to the database.

        All fields are required except related_ids and important_keywords which default to empty lists.
        The summary should contain specific details (dates, numbers, links, names) that won't be obvious from reading just the title.
        """
        valid_types = self.__valid_types()
        if not types or any(t not in valid_types for t in types):
            raise ValueError(f"Invalid type(s). Must be one or more of: {valid_types}")

        timestamp = self._get_timestamp()
        memory_id = self._generate_id()

        memory_record = {
            "id": memory_id,
            "keyword": keyword,
            "title": title,
            "summary": summary,
            "memory_types": types,
            "related_ids": related_ids or [],
            "important_keywords_related": important_keywords or [],
            "created_at": timestamp,
            "updated_at": timestamp
        }

        db = self._load_db()
        db["memories"].append(memory_record)
        self._update_indexes(db, memory_record)
        db["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_db(db)

        return {**memory_record, "status": "success"}

    def get_memory_by_id(self, memory_id: str) -> Optional[dict]:
        """Retrieve a specific memory by its ID."""
        db = self._load_db()
        if memory_id not in db["indexes"]["by_id"]:
            return None
        for m in db["memories"]:
            if m["id"] == memory_id:
                return m
        return None

    def get_memory_by_keyword(self, keyword: str) -> Optional[dict]:
        """Retrieve a specific memory by its keyword."""
        db = self._load_db()
        if keyword not in db["indexes"]["by_keyword"]:
            return None
        mem_id = db["indexes"]["by_keyword"][keyword]
        for m in db["memories"]:
            if m["id"] == mem_id:
                return m
        return None

    def get_all_memories(self) -> List[dict]:
        """Get all memory items."""
        db = self._load_db()
        return db["memories"]

    def search(
        self,
        pattern: str = None,
        types: List[str] = None,
        keyword: str = None
    ) -> List[dict]:
        """Search memories by regex pattern, type filter, or keyword."""
        db = self._load_db()
        results = []

        if keyword:
            # Search by exact keyword match
            if keyword in db["indexes"]["by_keyword"]:
                mem_id = db["indexes"]["by_keyword"][keyword]
                for m in db["memories"]:
                    if m["id"] == mem_id:
                        results.append(m)
                        break
        elif types:
            # Filter by type tags
            valid_types = self.__valid_types()
            if any(t not in valid_types for t in types):
                raise ValueError(f"Invalid type(s). Must be one or more of: {valid_types}")

            all_ids = set()
            for t in types:
                if t in db["indexes"]["by_types"]:
                    all_ids.update(db["indexes"]["by_types"][t])

            for m_id in all_ids:
                for m in db["memories"]:
                    if m["id"] == m_id and (not pattern or any(
                        p.lower() in m.get("summary", "").lower() or
                        p.lower() in m.get("title", "").lower() or
                        p.lower() in " ".join(m.get("important_keywords_related", [])).lower()
                        for p in pattern.split() if p
                    )):
                        results.append(m)
        elif pattern:
            # Search by regex pattern across all memories
            import re
            try:
                compiled_pattern = re.compile(pattern, re.IGNORECASE)
                for m in db["memories"]:
                    searchable_text = f"{m.get('title', '')} {m.get('summary', '')} {' '.join(m.get('important_keywords_related', []))}"
                    if compiled_pattern.search(searchable_text):
                        results.append(m)
            except re.error:
                pass

        return results

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a specific memory by ID."""
        db = self._load_db()
        for i, m in enumerate(db["memories"]):
            if m["id"] == memory_id:
                self._remove_from_indexes(db, memory_id)
                del db["memories"][i]
                db["last_updated"] = datetime.now(timezone.utc).isoformat()
                self._save_db(db)
                return True
        return False

    def update_memory(self, memory_id: str, updates: dict) -> bool:
        """Update an existing memory.

        Args:
            memory_id: The unique identifier of the memory to update (must be a valid UUID4 string)
            updates: Dictionary of fields to update. Valid keys are:
                     "keyword", "title", "summary", "memory_types", "related_ids", "important_keywords_related"
        Returns:
            True if updated successfully, False if not found
        """
        db = self._load_db()
        for m in db["memories"]:
            if m["id"] == memory_id:
                # Validate update keys
                valid_keys = ("keyword", "title", "summary", "memory_types", "related_ids", "important_keywords_related")
                for key, value in updates.items():
                    if key not in valid_keys:
                        raise ValueError(f"Invalid field '{key}'. Valid fields are: {valid_keys}")
                    m[key] = value

                # Update indexes
                self._remove_from_indexes(db, memory_id)
                self._update_indexes(db, m)
                m["updated_at"] = self._get_timestamp()
                db["last_updated"] = datetime.now(timezone.utc).isoformat()
                self._save_db(db)
                return True
        return False

    def get_memory_stats(self) -> dict:
        """Get statistics about the memory database."""
        db = self._load_db()
        stats = {}
        for t in self.__valid_types():
            ids = db["indexes"]["by_types"].get(t, [])
            stats[t] = {
                "count": len(ids),
                "ids": ids
            }
        return {
            "total_memories": len(db["memories"]),
            "last_updated": db.get("last_updated"),
            "type_stats": stats
        }


# Placeholder class for MCP tool discovery and documentation purposes
class MemoryDBTool:
    """Memory Database Tool Class for FastMCP."""
    pass


@mcp.tool
def save_memory(
    keyword: str = "",
    title: str = "",
    summary: str = "",
    types: str = '["personal"]',
    related_ids: str = "[]",
    important_keywords: str = "[]"
) -> str:
    """Save a memory item to the database.

    Args:
        keyword: Unique identifier/ID for this memory item (acts as title)
        title: Short descriptive title - should be expressive enough to help determine if reading the summary is needed
               Think of it as a "should I read more?" indicator. Make it descriptive but concise.
        summary: Detailed description of the experience/knowledge
                 IMPORTANT: Include specific details like dates, numbers, links, names, and any non-general information
                 that shouldn't get lost. The summary should summarize what was learned from the conversation.
        types: JSON array string of type tags (e.g., '["personal", "document"]')
               Valid tags: personal, document, reference, chat, chitchat, technical
        related_ids: JSON array string of memory IDs this is related to
        important_keywords: JSON array string of keywords for lookup/searching

    Returns:
        JSON string with the saved memory data and timestamp
    """
    try:
        mem = MemoryDB()

        # Parse JSON arrays from strings
        try:
            types_list = json.loads(types) if isinstance(types, str) else types
            related_ids_list = json.loads(related_ids) if isinstance(related_ids, str) else related_ids
            important_keywords_list = json.loads(important_keywords) if isinstance(important_keywords, str) else important_keywords
        except (json.JSONDecodeError, TypeError):
            types_list = ["personal"]
            related_ids_list = []
            important_keywords_list = []

        result = mem.save_memory(
            keyword=keyword,
            title=title,
            summary=summary,
            types=types_list or ["personal"],
            related_ids=related_ids_list or [],
            important_keywords=important_keywords_list or []
        )

        return json.dumps({
            "status": "success",
            "message": f"Memory saved with keyword '{keyword}'",
            "id": result["id"],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_memory_by_id(memory_id: str = "") -> str:
    """Retrieve a specific memory by its ID.

    Args:
        memory_id: The unique identifier of the memory to retrieve

    Returns:
        JSON string containing the memory data
    """
    try:
        mem = MemoryDB()
        result = mem.get_memory_by_id(memory_id)

        if not result:
            return json.dumps({
                "status": "not_found",
                "message": f"No memory found with ID '{memory_id}'",
                "data": None
            }, indent=2)

        return json.dumps({
            "status": "success",
            "message": f"Memory retrieved successfully",
            "data": result
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_memory_by_keyword(keyword: str = "") -> str:
    """Retrieve a specific memory by its keyword.

    Args:
        keyword: The unique identifier/ID of the memory item to retrieve

    Returns:
        JSON string containing the memory data
    """
    try:
        mem = MemoryDB()
        result = mem.get_memory_by_keyword(keyword)

        if not result:
            return json.dumps({
                "status": "not_found",
                "message": f"No memory found with keyword '{keyword}'",
                "data": None
            }, indent=2)

        return json.dumps({
            "status": "success",
            "message": f"Memory retrieved successfully",
            "data": result
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def search(
    pattern: str = "",
    types: str = None,
    keyword: str = ""
) -> str:
    """Search memories by regex pattern, type filter, or keyword.

    Args:
        pattern: Regex pattern to match against titles, summaries, and keywords
        types: JSON array string of type tags to filter by (e.g., '["personal"]')
        keyword: Exact keyword/ID to search for

    Returns:
        JSON string containing list of matched memory data
    """
    try:
        mem = MemoryDB()

        if types and isinstance(types, str):
            types_list = json.loads(types)
        else:
            types_list = None

        results = mem.search(
            pattern=pattern if pattern else None,
            types=types_list,
            keyword=keyword if keyword else None
        )

        return json.dumps({
            "status": "success",
            "total_matches": len(results),
            "results": results
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_all_memories() -> str:
    """Get all memory items.

    Returns:
        JSON string containing list of all memories
    """
    try:
        mem = MemoryDB()
        results = mem.get_all_memories()

        return json.dumps({
            "status": "success",
            "total_memories": len(results),
            "memories": results
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_memory_stats() -> str:
    """Get statistics about the memory database.

    Returns:
        JSON string containing counts and details for each memory type
    """
    try:
        mem = MemoryDB()
        stats = mem.get_memory_stats()

        return json.dumps({
            "status": "success",
            "statistics": stats
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def delete_memory(memory_id: str = "") -> str:
    """Delete a specific memory by ID.

    Args:
        memory_id: The unique identifier of the memory to delete

    Returns:
        JSON string with deletion result
    """
    try:
        mem = MemoryDB()
        success = mem.delete_memory(memory_id)

        return json.dumps({
            "status": "success" if success else "not_found",
            "message": f"Memory {'deleted successfully' if success else 'not found'} with ID '{memory_id}'"
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def update_memory(
    memory_id: str = "",
    updates: str = "{}"
) -> str:
    """Update an existing memory.

    Args:
        memory_id: The unique identifier (UUID4 string) of the memory to update.
                   Use get_all_memories or search to find valid IDs.
        updates: JSON string with fields to update. Example: '{"summary": "Updated summary text"}'
                 Valid keys are: keyword, title, summary, memory_types, related_ids, important_keywords_related

    Returns:
        JSON string with update result including status, message, and applied updates

    Usage example:
        To update a memory's summary:
          update_memory("uuid4-string-here", '{"summary": "New summary text"}')

        To update multiple fields at once:
          update_memory("uuid4-string-here", '{"title": "New Title", "summary": "Updated content"}')
    """
    try:
        mem = MemoryDB()
        
        # Validate memory_id is not empty
        if not memory_id or not memory_id.strip():
            return json.dumps({
                "status": "error",
                "message": "memory_id is required. Use get_all_memories to find valid IDs."
            }, indent=2)

        # Parse updates JSON string
        try:
            update_dict = json.loads(updates) if isinstance(updates, str) else updates
        except (json.JSONDecodeError, TypeError):
            return json.dumps({
                "status": "error",
                "message": f"Invalid JSON in 'updates' parameter. Got: '{updates}'"
            }, indent=2)

        success = mem.update_memory(memory_id, update_dict or {})

        if not success:
            return json.dumps({
                "status": "not_found",
                "message": f"No memory found with ID '{memory_id}'. Use get_all_memories to list all memories."
            }, indent=2)

        return json.dumps({
            "status": "success" if success else "not_found",
            "message": f"Memory {'updated successfully' if success else 'not found'} with ID '{memory_id}'",
            "updates_applied": list(update_dict.keys()) if update_dict else []
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_all_memory_types() -> str:
    """Get a list of all memory type tags that have been used.

    Returns:
        JSON string containing available memory types with counts
    """
    try:
        mem = MemoryDB()
        stats = mem.get_memory_stats()

        return json.dumps({
            "status": "success",
            "type_counts": stats["type_stats"],
            "description": "These are the memory type tags that have been used"
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# Convenience function for direct usage without instantiation
def create_memory():
    """Create and return a new MemoryDB instance.

    Returns:
        New MemoryDB instance
    """
    return MemoryDB()


if __name__ == "__main__":
    print("memorydb - LLM Memory Database System")
    print("=" * 50)
    print("This module provides memory management via FastMCP tools.")
    print("Available tools:")
    print("  - save_memory: Save a new memory item")
    print("  - get_memory_by_id: Retrieve specific memory by ID")
    print("  - get_memory_by_keyword: Retrieve specific memory by keyword")
    print("  - search: Search memories by pattern or type filter")
    print("  - get_all_memories: Get all stored memories")
    print("  - get_memory_stats: View memory statistics")
    print("  - delete_memory: Delete a memory item")
    print("  - update_memory: Update an existing memory")
    print("  - get_all_memory_types: Show available type tags")

    # Run the MCP server
    mcp.run()