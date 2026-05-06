"""
memorylite - Lightweight LLM Memory Database System (SQLite Backend)

Replaces JSON file storage with SQLite for better query performance,
transactional safety, and SQL-based searching instead of regex/string matching.

MIGRATION GUIDE (from memorydb):
    JSON file (.json  →  SQLite database (.db)
    Regex search       →  SQL SELECT with WHERE/LIKE/IN clauses
    In-memory indexes   →  Database-level B-tree indexes
    File read/write     →  ACID transactions

DATABASE SCHEMA:
    CREATE TABLE memories (
        id TEXT PRIMARY KEY,                                    -- YYMMDDhhmmss format timestamp-based ID (e.g., 260506193000 = 2026-05-06 19:30:00)
        keyword TEXT NOT NULL UNIQUE,                           -- Unique keyword/ID for this memory
        title TEXT NOT NULL,                                     -- Short descriptive title (acts as 'should I read more?' indicator)
        summary TEXT NOT NULL,                                   -- Detailed description with specific details
        memory_types TEXT NOT NULL,                              -- JSON array string: '["personal","technical"]'
        related_ids TEXT NOT NULL,                               -- JSON array string: ['id1','id2'] - single related item reference
        related_items TEXT NOT NULL,                             -- JSON array string: ['id1','id2','id3'] - batch of related items for group updates
        keywords TEXT NOT NULL,                                   -- JSON array string: '["kw1","kw2"]'
        created_at TEXT NOT NULL,                               -- ISO format timestamp of creation
        updated_at TEXT NOT NULL                                -- ISO format timestamp of last update
    );

    NOTE ABOUT EACH FIELD'S PURPOSE FOR LLMs:
      - keyword: Unique identifier/ID for this memory (auto-generated or manually set)
      - title: Short descriptive heading that summarizes what the memory is about
      - summary: Detailed description with specific details (dates, numbers, links, names)
                 Should contain specifics that won't be obvious from reading just the title.
      - memory_types: Categorization tags for filtering/grouping (e.g., personal, document, reference)
      - related_ids: Links to other memories by their ID for establishing connections
      - related_items: GROUP of related memory IDs that should be updated together as a batch when one is modified
      - keywords: SEMANTIC KEYWORDS/PHRASES useful for SEARCHING and MATCHING
                 These are terms the user might remember and search by later. Include product names,
                 technical terms, key concepts, or phrases that capture the essence of this memory.
                 This field is PRIMARY for semantic recall - populate it with words/phrases a user
                 would naturally use when remembering or searching for this memory later.

    CREATE INDEX idx_memories_keyword ON memories(keyword);
    CREATE INDEX idx_memories_id ON memories(id);
    CREATE INDEX idx_memories_created_at ON memories(created_at);

    NOTE: memory_types, related_ids, related_items, keywords are stored as JSON array strings.
          Use json_each() for SQL-based array membership checks (e.g., checking if a type exists).

    ALL QUERIES SHOULD SPECIFY COLUMNS EXPLICITLY (avoid SELECT *) and use include_summary 
    parameter to conditionally include the summary field (default: False) to reduce I/O cost.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Set
from fastmcp import FastMCP

# Thread lock for thread-safe database operations
_db_lock = threading.Lock()

mcp = FastMCP("memorylite")


def _parse_json_or_fix(input_str: str) -> Any:
    """Parse a JSON string, with fallbacks for common LLM formatting issues.
    
    LLMs sometimes send parameters without proper double quotes around keys or values,
    e.g., '{summary:"the summary"}' instead of '{"summary": "the summary"}'.
    This function attempts to fix such cases before parsing.
    """
    if not input_str:
        return None
    
    # Try standard JSON parse first
    try:
        return json.loads(input_str)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # Attempt 1: Replace single-quoted strings with double quotes for both keys and values
    import re
    fixed = input_str
    
    # Fix unquoted keys: {key: "value"} -> {"key": "value"}
    fixed = re.sub(r'(\{|\s)([a-zA-Z_][a-zA-Z0-9_]*\s*:\s*)', r'\1"\2"', fixed)
    
    # Fix unquoted values: {key: value} -> {key: "value"} (only for non-reserved words)
    reserved_words = ['true', 'false', 'null', 'None', 'True', 'False', 'Null']
    def fix_value(m):
        val = m.group(0)
        if val in reserved_words or val.startswith('"') or val.startswith("'") or val.startswith('[') or val.startswith('{'):
            return f'"{val}"'
        return f'"{val}"'
    
    # Fix unquoted values (simple identifiers and strings with double quotes inside)
    fixed = re.sub(r':\s*([a-zA-Z_][a-zA-Z0-9_.]*|\d+|"[^"]*"|\[[^\]]*\]|\{[^}]*\})', lambda m: f": {m.group(1)}" if m.group(1) in reserved_words or m.group(1).startswith('"') else ': "{}"'.format(m.group(1)), fixed)
    
    # Fix unquoted string values (words that look like identifiers but should be strings)
    fixed = re.sub(r'(?<=[{,\s])([a-zA-Z_][a-zA-Z0-9_]*)(?=[,}])', lambda m: '"{}"'.format(m.group(1)) if m.group(1) not in reserved_words else m.group(1), fixed)
    
    try:
        return json.loads(fixed)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # Attempt 2: Replace all single quotes with double quotes (handles most LLM cases)
    replaced = input_str.replace("'", '"')
    try:
        return json.loads(replaced)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # Final fallback: return the raw string as-is for downstream handling
    return input_str


def _parse_json_list_or_fix(input_str: str) -> Any:
    """Parse a JSON list string with fixes for common LLM formatting issues.
    
    Similar to _parse_json_or_fix but specifically handles array inputs like ['personal']
    which should be ["personal"].
    """
    if not input_str:
        return None
    
    try:
        result = json.loads(input_str)
        # Ensure it's a list; wrap single values in a list
        if isinstance(result, str):
            return [result]
        return result
    except (json.JSONDecodeError, TypeError):
        pass
    
    # Try replacing single quotes with double quotes for arrays
    replaced = input_str.replace("'", '"')
    try:
        return json.loads(replaced)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # Fallback: treat as a single string wrapped in list
    if isinstance(input_str, str) and input_str.strip():
        return [input_str]
    
    return []


def _parse_json_dict_or_fix(input_str: str) -> Any:
    """Parse a JSON dict string with fixes for common LLM formatting issues.
    
    Similar to _parse_json_or_fix but specifically handles dict inputs like {key:"value"}
    which should be {"key": "value"}.
    """
    if not input_str or input_str == "{}":
        return {}
    
    try:
        result = json.loads(input_str)
        if isinstance(result, str):
            # If it's a string that looks like JSON, try parsing again
            try:
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return {"raw": result}
        return result
    except (json.JSONDecodeError, TypeError):
        pass
    
    replaced = input_str.replace("'", '"')
    try:
        return json.loads(replaced)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # Fallback: empty dict
    return {}


mcp = FastMCP("memorylite")


class MemoryLite:
    """LLM Memory Management System using SQLite as backend."""

    # Configuration - can be modified at the top level
    DB_FILE = None  # Will default to ~/.swordmemory/memory.db if not set

    VALID_TYPES = ["personal", "document", "reference", "chat", "chitchat", "technical"]

    def _get_db_path(self) -> Path:
        """Get the path to the database file."""
        if self.DB_FILE is not None:
            return Path(self.DB_FILE)
        else:
            home_docs = Path.home()
            db_dir = home_docs.joinpath(".swordmemory")
            db_dir.mkdir(parents=True, exist_ok=True)
            return db_dir / "memory.db"

    def _get_connection(self) -> sqlite3.Connection:
        """Get a SQLite connection with proper settings."""
        db_path = self._get_db_path()
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrency
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        """Initialize the database schema if not exists."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    keyword TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    memory_types TEXT NOT NULL DEFAULT '[]',
                    related_ids TEXT NOT NULL DEFAULT '[]',
                    related_items TEXT NOT NULL DEFAULT '[]',
                    keywords TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memories_keyword ON memories(keyword);
                CREATE INDEX IF NOT EXISTS idx_memories_id ON memories(id);
                CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
            """)

    def _get_timestamp(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    def save_memory(
        self,
        keyword: str,
        title: str,
        summary: str,
        types: List[str] = None,
        related_ids: List[str] = None,
        related_items: List[str] = None,
        important_keywords: List[str] = None
    ) -> dict:
        """Save a new memory item to the database.

        All fields are required except related_ids and important_keywords which default to empty lists.
        The summary should contain specific details (dates, numbers, links, names) that won't be obvious from reading just the title.
        """
        if not types or any(t not in self.VALID_TYPES for t in types):
            raise ValueError(f"Invalid type(s). Must be one or more of: {self.VALID_TYPES}")

        timestamp = self._get_timestamp()
        memory_id = self._generate_memory_id(timestamp)

        db = self._get_connection()
        try:
            db.execute(
                """INSERT INTO memories 
                   (id, keyword, title, summary, memory_types, related_ids, related_items, keywords, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    keyword,
                    title,
                    summary,
                    json.dumps(types or []),
                    json.dumps(related_ids or []),
                    json.dumps(related_items or []),
                    json.dumps(important_keywords or []),
                    timestamp,
                    timestamp
                )
            )
            db.commit()
        finally:
            db.close()

        return {
            "id": memory_id,
            "keyword": keyword,
            "title": title,
            "summary": summary,
            "memory_types": types or [],
            "related_ids": related_ids or [],
            "related_items": related_items or [],
            "keywords": important_keywords or [],
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": "success"
        }

    def get_memory_by_id(self, memory_id: str) -> Optional[dict]:
        """Retrieve a specific memory by its ID."""
        db = self._get_connection()
        try:
            row = db.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return self._row_to_dict(row, True) if row else None
        finally:
            db.close()

    def get_memories_by_ids(self, memory_ids: List[str], include_summary: bool = False) -> List[dict]:
        """Retrieve multiple memories by their IDs.

        Args:
            memory_ids: List of unique identifiers to retrieve
            include_summary: Whether to include the summary field (default: False)

        Returns:
            List of matching memory records
        """
        if not memory_ids:
            return []

        db = self._get_connection()
        try:
            placeholders = ','.join(['?' for _ in memory_ids])
            query = f"SELECT {self._select_columns(include_summary)} FROM memories WHERE id IN ({placeholders})"
            rows = db.execute(query, memory_ids).fetchall()
            return [self._row_to_dict(row, include_summary) for row in rows] if rows else []
        finally:
            db.close()

    def get_memory_by_keyword(self, keyword: str) -> Optional[dict]:
        """Retrieve a specific memory by its keyword."""
        db = self._get_connection()
        try:
            row = db.execute(
                "SELECT * FROM memories WHERE keyword = ?", (keyword,)
            ).fetchone()
            return self._row_to_dict(row if row else None, True)
        finally:
            db.close()

    def get_all_memories(self, include_summary: bool = False) -> List[dict]:
        """Get all memory items.

        Args:
            include_summary: Whether to include the summary field (default: False)
        """
        db = self._get_connection()
        try:
            columns = self._select_columns(include_summary)
            rows = db.execute(
                f"SELECT {columns} FROM memories ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_dict(row, include_summary) for row in rows] if rows else []
        finally:
            db.close()

    def search(
        self,
        pattern: str = None,
        types: List[str] = None,
        keyword: str = None,
        include_summary: bool = False,
        wordJoin: str = "OR"
    ) -> List[dict]:
        """Search memories across all text fields in a single query.

        Searches title, summary, keyword, memory_types, and keywords.
        
        When pattern contains spaces, each space-separated token becomes a separate LIKE condition.
        The wordJoin parameter controls how these conditions are combined:
          - "OR" (default): Each word is searched independently; ANY word matching returns the record
          - "AND": Each word must appear in the result; ALL words must match

        Args:
            pattern: Text to search for (each space-separated token becomes a separate LIKE condition)
            types: Filter by type tags (e.g., ["personal"])
            keyword: Exact keyword match
            include_summary: Whether to include the summary field (default: False)
            wordJoin: How to combine multi-word patterns - "OR" (any word matches, default) or "AND" (all words must match)

        Returns:
            List of matching memory records
        """
        db = self._get_connection()
        try:
            conditions = []
            params = []

            if pattern:
                # Split pattern by spaces - each word becomes a separate LIKE condition
                tokens = [w.strip() for w in pattern.split() if w.strip()]
                
                if not tokens:
                    return []
                
                # Build OR/AND grouped conditions per field (title, summary, keyword)
                # Each group has multiple LIKE clauses connected by the join operator
                for token in tokens:
                    title_cond = f"title LIKE ?"
                    summary_cond = f"summary LIKE ?"
                    kw_cond = f"keywords LIKE ?"
                    
                    if wordJoin.upper() == "AND":
                        # All words must match each field (each word appears in that field)
                        conditions.append(f"{title_cond} AND {summary_cond} AND {kw_cond}")
                        params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])
                    else:  # OR or any other value
                        # Any word matches any field (each word can match any field)
                        conditions.append(f"{title_cond} OR {summary_cond} OR {kw_cond}")
                        params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])

            if types:
                # Filter by type tags using json_each for JSON array membership
                for t in types:
                    conditions.append(
                        "id IN (SELECT id FROM memories, json_each(memory_types) WHERE json_each.value = ?)"
                    )
                    params.append(t)

            if keyword:
                conditions.append("keyword = ?")
                params.append(keyword)

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            query = f"SELECT {self._select_columns(include_summary)} FROM memories WHERE {where_clause}"

            rows = db.execute(query, params).fetchall()
            return [self._row_to_dict(row, include_summary) for row in rows] if rows else []
        finally:
            db.close()

    def update_memory(self, memory_id: str, updates: dict) -> bool:
        """Update an existing memory.

        Args:
            memory_id: The unique identifier of the memory to update
            updates: Dictionary of fields to update

        Returns:
            True if updated successfully, False if not found
        """
        valid_keys = ("keyword", "title", "summary", "memory_types", "related_ids", "related_items", "keywords")
        for key in updates.keys():
            if key not in valid_keys:
                raise ValueError(f"Invalid field '{key}'. Valid fields are: {valid_keys}")

        timestamp = self._get_timestamp()
        
        # Convert lists to JSON strings where needed
        json_fields = {"memory_types", "related_ids", "related_items", "keywords"}
        for key in updates:
            if key in json_fields and isinstance(updates[key], list):
                updates[key] = json.dumps(updates[key])

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        
        db = self._get_connection()
        try:
            values = list(updates.values()) + [memory_id, timestamp]
            rows_affected = db.execute(
                f"UPDATE memories SET {set_clause}, updated_at = ? WHERE id = ?",
                tuple(values)
            ).rowcount
            db.commit()
            return rows_affected > 0
        finally:
            db.close()

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a specific memory by ID."""
        db = self._get_connection()
        try:
            rows_deleted = db.execute(
                "DELETE FROM memories WHERE id = ?", (memory_id,)
            ).rowcount
            db.commit()
            return rows_deleted > 0
        finally:
            db.close()

    def get_memory_stats(self) -> dict:
        """Get statistics about the memory database."""
        db = self._get_connection()
        try:
            total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            
            # Get counts per type using json_each
            type_counts = {}
            for t in self.VALID_TYPES:
                count = db.execute(
                    "SELECT COUNT(DISTINCT id) FROM memories, json_each(memory_types) WHERE json_each.value = ?",
                    (t,)
                ).fetchone()[0]
                type_counts[t] = {"count": count}

            return {
                "total_memories": total or 0,
                "last_updated": db.execute("SELECT MAX(updated_at) FROM memories").fetchone()[0],
                "type_stats": type_counts
            }
        finally:
            db.close()

    def get_all_types(self) -> List[dict]:
        """Get all unique type tags used in the database with counts.

        Uses json_each to extract each type from JSON array strings.
        """
        db = self._get_connection()
        try:
            rows = db.execute(
                """SELECT json_each.value as type_name, COUNT(DISTINCT id) as count 
                   FROM memories, json_each(memory_types) 
                   GROUP BY type_name"""
            ).fetchall()
            return [{"type": row[0], "count": row[1]} for row in rows] if rows else []
        finally:
            db.close()

    def get_all_keywords(self, pattern: str = None) -> List[dict]:
        """Get all keywords with optional pattern filter.

        Args:
            pattern: Optional regex-like string to filter keywords by title or keyword name

        Returns:
            List of dicts with keyword and title info
        """
        db = self._get_connection()
        try:
            where_clause = "WHERE 1=1"
            params = []
            
            if pattern:
                where_clause += " AND (keyword LIKE ? OR title LIKE ?)"
                params.extend([f"%{pattern}%", f"%{pattern}%"])

            select_clause = "SELECT keyword, title FROM memories"
            full_query = f"{select_clause} {where_clause} GROUP BY keyword ORDER BY keyword" if where_clause != "WHERE 1=1" else f"{select_clause} GROUP BY keyword ORDER BY keyword"
            rows = db.execute(full_query, params).fetchall()
            return [{"keyword": row[0], "title": row[1]} for row in rows] if rows else []
        finally:
            db.close()

    def get_all_words(self, pattern: str = None) -> dict:
        """Extract all words from every text field in the database.

        This method scans all text fields (title, summary, keyword, memory_types, related_ids, related_items, important_keywords_related)
        and returns a breakdown of which words appear in which fields.

        Args:
            pattern: Optional string to filter results - only includes words containing this substring

        Returns:
            Dict with field-level word breakdown:
                {
                    "title": ["word1", "word2"],
                    "summary": ["word3", "word4"],
                    "keyword": ["word5"],
                    "memory_types": ["word6"],
                    "related_ids": ["word7"],
                    "related_items": ["word8"],
                    "keywords": ["word9"]
                }

        NOTE: This is an EXPENSIVE operation as it must fetch all records with their full text.
              Use only when a deep word-level search is required. For most use cases, 
              get_all_keywords() or search() should be preferred for better performance.
        """
        db = self._get_connection()
        try:
            # Get all memories with title and summary (include_summary=True)
            rows = db.execute(
                "SELECT id, keyword, title, summary, memory_types, related_ids, related_items, important_keywords_related FROM memories"
            ).fetchall()

            result = {
                "title": set(),
                "summary": set(),
                "keyword": set(),
                "memory_types": set(),
                "related_ids": set(),
                "related_items": set(),
                "keywords": set()
            }

            for row in rows:
                # row[0]=id, row[1]=keyword, row[2]=title, row[3=summary, 
                # row[4]=memory_types (JSON string), row[5]=related_ids, row[6]=related_items, row[7]=important_keywords_related
                
                # Extract words from each field
                if row[2]:  # title
                    for word in self._extract_words(row[2]):
                        result["title"].add(word)

                if row[3]:  # summary
                    for word in self._extract_words(row[3]):
                        result["summary"].add(word)

                if row[1]:  # keyword
                    for word in self._extract_words(row[1]):
                        result["keyword"].add(word)

                if row[4]:  # memory_types (JSON string like '["personal","technical"]')
                    try:
                        types_list = json.loads(row[4])
                        for t in types_list:
                            for word in self._extract_words(t):
                                result["memory_types"].add(word)
                    except (json.JSONDecodeError, TypeError):
                        pass

                if row[5]:  # related_ids (JSON string like '["id1","id2"]')
                    try:
                        ids_list = json.loads(row[5])
                        for id_val in ids_list:
                            for word in self._extract_words(id_val):
                                result["related_ids"].add(word)
                    except (json.JSONDecodeError, TypeError):
                        pass

                if row[6]:  # related_items (JSON string like '["id1","id2","id3"]')
                    try:
                        items_list = json.loads(row[6])
                        for item in items_list:
                            for word in self._extract_words(item):
                                result["related_items"].add(word)
                    except (json.JSONDecodeError, TypeError):
                        pass

                if row[7]:  # keywords (JSON string)
                    try:
                        kw_list = json.loads(row[7])
                        for kw in kw_list:
                            for word in self._extract_words(kw):
                                result["keywords"].add(word)
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Apply pattern filter if provided
            filtered_result = {}
            for field, words in result.items():
                if pattern:
                    filtered_result[field] = sorted([w for w in words if pattern.lower() in w.lower()])
                else:
                    filtered_result[field] = sorted(list(words))

            return {k: v for k, v in filtered_result.items()}
        finally:
            db.close()

    @staticmethod
    def _extract_words(text: str) -> set:
        """Extract words from text, filtering out common stop words and short tokens."""
        import re
        # Remove punctuation and split into words
        words = re.findall(r'\b[a-zA-Z0-9]+\b', str(text))
        # Filter out very short words (less than 2 chars) and common English stop words
        stop_words = {'the','a','an','and','or','but','in','on','at','to','for','of','is','it','as','by','this','that','with','from','be','are','was','were','have','has','had','do','does','did','will','would','could','should','may','might','can','shall','not','no','all','each','every','many','some','any','but','if','then','than','so','more','most','other','such','only','own','same','too','very','just','about','up','out','off','his','her','its','our','your','my','we','you','he','she','they','us','me','him','them'}
        return {w.lower() for w in words if len(w) >= 2 and w.lower() not in stop_words}

    def _generate_memory_id(self, timestamp_str: str) -> str:
        """Generate a memory ID from a timestamp string.
        
        Format: YYMMDDhhmmss (e.g., 260506193000 for 2026-05-06 19:30:00)
        Uses a sequence counter to handle multiple creations within the same second.
        
        Args:
            timestamp_str: ISO format timestamp string
            
        Returns:
            Formatted memory ID string
        """
        # Parse the timestamp and create YYMMDDhhmmss format
        try:
            dt = datetime.fromisoformat(timestamp_str)
            return dt.strftime("%y%m%d%H%M%S")
        except (ValueError, TypeError):
            # Fallback to current time if parsing fails
            now = datetime.now(timezone.utc)
            return now.strftime("%y%m%d%H%M%S")

    def _select_columns(self, include_summary: bool) -> str:
        """Build column list for SELECT queries based on whether summary is needed."""
        if include_summary:
            return "id, keyword, title, summary, memory_types, related_ids, related_items, keywords, created_at, updated_at"
        else:
            return "id, keyword, title, memory_types, related_ids, related_items, keywords, created_at, updated_at"

    def _row_to_dict(self, row, include_summary: bool = False) -> Optional[dict]:
        """Convert a SQLite row to dictionary format.

        Args:
            row: SQLite row data (already fetched from database)
            include_summary: Whether to include the summary field in the returned dict
                           (default: False for efficiency)
        """
        if not row:
            return None
        
        # Map column names from the schema based on what was selected
        # The columns match _select_columns() output, so we define them explicitly
        all_columns = ["id", "keyword", "title", "summary", "memory_types", 
                       "related_ids", "related_items", "keywords", "created_at", "updated_at"]
        
        # Build dict from the existing row data (no additional DB call needed)
        row_data = {}
        for i, col in enumerate(all_columns):
            if i < len(row):
                row_data[col] = row[i]

        # Parse JSON arrays from strings
        for field in ["memory_types", "related_ids", "related_items", "keywords"]:
            if isinstance(row_data.get(field, ""), str):
                try:
                    row_data[field] = json.loads(row_data[field])
                except (json.JSONDecodeError, TypeError):
                    row_data[field] = []

        # Remove summary field if not requested and it's present in the data
        if not include_summary and "summary" in row_data:
            del row_data["summary"]

        return row_data


# ============================================================================
# FastMCP Tool Functions
# ============================================================================

from typing import Union

from typing import Optional as Opt

@mcp.tool
def save_memory(
    keyword: str = "",
    title: str = "",
    summary: str = "",
    types: Opt[Union[str, list]] = '["personal"]',
    related_ids: Opt[Union[str, list]] = "[]",
    related_items: Opt[Union[str, list]] = "[]",
    keywords: Opt[Union[str, list]] = "[]"
) -> str:
    """Save a memory item to the database.

    IMPORTANT: The 'important_keywords' field contains SEMANTIC KEYWORDS/PHRASES useful for SEARCHING 
    and MATCHING related memories. These are terms the user might remember and search by later.
    
    WHAT TO INCLUDE in important_keywords:
      - Product names, model numbers, technical specifications
      - Key concepts, frameworks, or methodologies mentioned
      - Names of people, organizations, or locations relevant to this memory
      - Specific dates, version numbers, or identifiers that could be searched later
      - Phrases that capture the essence of what this memory is about
    
    WHAT NOT TO INCLUDE:
      - Words already covered by 'title' or 'summary' (avoid simple duplication)
      - Generic stop words (the, a, an, for, etc.)
      - Anything too broad to be useful as a search term
    
    REMEMBER: This field is PRIMARY for semantic recall. Populate it with the exact words/phrases 
    you'd use if you remembered this memory later but couldn't remember its title or summary.

    Args:
        keyword: Unique identifier/ID for this memory item
        title: Short descriptive title (acts as 'should I read more?' indicator)
        summary: Detailed description with specific details (dates, numbers, links, names)
                 Should contain specifics that won't be obvious from reading just the title.
        types: JSON array string of type tags (e.g., '["personal", "document"]')
        related_ids: JSON array string of memory IDs this is related to
        keywords: JSON array string of keywords for lookup/searching

    Returns:
        JSON string with the saved memory data and timestamp. Includes a warning about 
        empty important_keywords if not populated.
    """
    try:
        mem = MemoryLite()
        mem._init_db()  # Ensure schema exists

        # Handle both string and already-parsed list inputs from LLMs
        types_list = types if isinstance(types, list) else (_parse_json_list_or_fix(types) if isinstance(types, str) else None)
        related_ids_list = related_ids if isinstance(related_ids, list) else (_parse_json_list_or_fix(related_ids) if isinstance(related_ids, str) else None)
        related_items_list = related_items if isinstance(related_items, list) else (_parse_json_list_or_fix(related_items) if isinstance(related_items, str) else None)
        keywords_list = keywords if isinstance(keywords, list) else (_parse_json_list_or_fix(keywords) if isinstance(keywords, str) else None)

        result = mem.save_memory(
            keyword=keyword,
            title=title,
            summary=summary,
            types=types_list or ["personal"],
            related_ids=related_ids_list or [],
            related_items=related_items_list or [],
            important_keywords=keywords_list or []
        )

        # Build the result with a warning about keywords if empty
        kw_list = keywords if isinstance(keywords, list) else (_parse_json_list_or_fix(keywords) if isinstance(keywords, str) else None)
        warning_note = ""
        if not kw_list or len(kw_list) == 0:
            warning_note = " WARNING: 'keywords' is empty. Consider adding semantic keywords/phrases that capture the essence of this memory for better searchability later."

        return json.dumps({
            "status": "success",
            "message": f"Memory saved with keyword '{keyword}'{'' if kw_list else ''}",
            "id": result["id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "keywords": kw_list,
            "note": warning_note or None
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_memory_by_id(memory_id: str = "") -> str:
    """Retrieve a specific memory by its ID.

    Args:
        memory_id: The unique identifier of the memory to retrieve

    Returns:
        JSON string containing the memory data (without summary unless requested)
    """
    try:
        mem = MemoryLite()
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
def get_memories_by_ids(memory_ids_str: str = "") -> str:
    """Retrieve multiple memories by their IDs.

    Args:
        memory_ids_str: Comma-separated string of memory IDs (e.g., "id1,id2,id3")

    Returns:
        JSON string containing the retrieved memories
    """
    try:
        mem = MemoryLite()
        ids = [x.strip() for x in memory_ids_str.split(",")] if memory_ids_str else []
        
        results = mem.get_memories_by_ids(ids)

        return json.dumps({
            "status": "success",
            "total_found": len(results),
            "memories": results
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
        mem = MemoryLite()
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


from typing import Union

@mcp.tool
def search(
    pattern: str = "",
    types: Union[str, list] = None,
    keyword: str = "",
    wordJoin: str = "OR"
) -> str:
    """Search memories by pattern across all fields or type filter.

    Searches title, summary, keyword, memory_types, and keywords in a single query.

    HOW THE PATTERN IS PROCESSED:
      The pattern is split by spaces - each space-separated token becomes a separate LIKE condition.
      
      wordJoin controls how these conditions are combined:
        - "OR" (default): Each word is searched independently; ANY word matching returns the record
          Example: search(pattern="meeting about budget", wordJoin="OR") 
                   → finds memories containing "meeting" OR "about" OR "budget"
        
        - "AND": EACH word must appear in the result; ALL words must match
          Example: search(pattern="meeting about budget", wordJoin="AND")
                   → only returns memories containing "meeting" AND "about" AND "budget"

    USE CASES:
      - Use OR (default) for broad discovery: find anything related to any of these terms
      - Use AND for precise filtering: require all terms appear in each result
      
    EXAMPLE WORKFLOW for deep memory exploration:
      Step 1: search(types='["personal"]')           → All personal memories
      Step 2: search(pattern="meeting")              → Memories about meetings  
      Step 3: search(keyword="proj-alpha")            → Specific project memory
      Step 4: search(pattern="budget", wordJoin="AND") → Budget-related with ALL words

    Args:
        pattern: Text to search for (each space-separated token becomes a separate LIKE condition)
        types: JSON array string of type tags to filter by (e.g., '["personal"]')
        keyword: Exact keyword/ID to search for
        wordJoin: How to combine multi-word patterns - "OR" (any word matches, default) or "AND" (all words must match)

    Returns:
        JSON string containing list of matched memory data. Each call returns ALL matching results - 
        no pagination needed. Make multiple calls with different filters to get comprehensive coverage.
    """
    try:
        mem = MemoryLite()
        
        # Handle both string and already-parsed list inputs from LLMs
        types_list = types if isinstance(types, list) else _parse_json_list_or_fix(types) if isinstance(types, str) else None
        
        results = mem.search(
            pattern=pattern if pattern else None,
            types=types_list,
            keyword=keyword if keyword else None,
            wordJoin=wordJoin
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
    """Get all memory items (without summary by default for efficiency).

    Returns:
        JSON string containing list of all memories (title only, no full summaries)
    """
    try:
        mem = MemoryLite()
        results = mem.get_all_memories(include_summary=False)

        return json.dumps({
            "status": "success",
            "total_memories": len(results),
            "memories": results,
            "note": "Use include_summary=true with get_memory_by_id to retrieve full summaries"
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_all_types() -> str:
    """Get all unique type tags used in the database with counts.

    Returns:
        JSON string containing available memory types and their usage counts
    """
    try:
        mem = MemoryLite()
        types_list = mem.get_all_types()

        return json.dumps({
            "status": "success",
            "types": types_list,
            "description": "Each entry shows a type tag and how many memories use it"
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_all_keywords(pattern: str = "") -> str:
    """Get all keywords with optional pattern filter.

    Args:
        pattern: Optional text to filter keywords by title or keyword name

    Returns:
        JSON string containing list of all keywords and their titles
    """
    try:
        mem = MemoryLite()
        results = mem.get_all_keywords(pattern if pattern else None)

        return json.dumps({
            "status": "success",
            "total_keywords": len(results),
            "keywords": results
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_all_words(pattern: str = "") -> str:
    """Extract all words from every text field in the database.

        Scans title, summary, keyword, memory_types, and keywords fields.
    Returns a breakdown of which words appear in which fields.

    Args:
        pattern: Optional string to filter results - only includes words containing this substring

    Returns:
        JSON string with field-level word breakdown:
            {
                "title": ["word1", "word2"],
                "summary": ["word3", "word4"],
                "keyword": ["word5"],
                ...
            }

    NOTE: This is an EXPENSIVE operation as it must fetch all records with their full text.
          Use only when a deep word-level search is required. For most use cases, 
          get_all_keywords() or search() should be preferred for better performance.

    Example:
        # Get all words (no filter)
        get_all_words("")

        # Filter to words containing "foot"
        get_all_words("foot")  # Will match "football", "footprint", etc.
    """
    try:
        mem = MemoryLite()
        results = mem.get_all_words(pattern if pattern else None)

        return json.dumps({
            "status": "success",
            "pattern": pattern,
            "word_counts": {k: len(v) for k, v in results.items()},
            "words_by_field": results,
            "note": "Each field shows words extracted from that specific field's text"
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
        mem = MemoryLite()
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
        mem = MemoryLite()
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
    updates: Union[str, dict] = "{}"
) -> str:
    """Update an existing memory.

    Args:
        memory_id: The unique identifier (UUID4 string) of the memory to update
        updates: JSON string with fields to update. Example: '{"summary": "Updated summary text"}'
                 Valid keys are: keyword, title, summary, memory_types, related_ids, keywords

    Returns:
        JSON string with update result including status, message, and applied updates
    """
    try:
        mem = MemoryLite()
        
        if not memory_id or not memory_id.strip():
            return json.dumps({
                "status": "error",
                "message": "memory_id is required. Use get_all_memories to find valid IDs."
            }, indent=2)

        # Handle both string and already-parsed dict inputs from LLMs
        if isinstance(updates, dict):
            update_dict = updates
        elif isinstance(updates, str):
            try:
                update_dict = _parse_json_dict_or_fix(updates)
            except (json.JSONDecodeError, TypeError):
                return json.dumps({
                    "status": "error",
                    "message": f"Invalid JSON in 'updates' parameter. Got: '{updates}'"
                }, indent=2)
        else:
            update_dict = {}

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


# ============================================================================
# Main entry point
# ============================================================================

if __name__ == "__main__":
    print("memorylite - LLM Memory Database System (SQLite Backend)")
    print("=" * 50)
    print("This module provides memory management via FastMCP tools.")
    print("Available tools:")
    print("  - save_memory: Save a new memory item")
    print("  - get_memory_by_id: Retrieve specific memory by ID")
    print("  - get_memories_by_ids: Retrieve multiple memories by IDs")
    print("  - get_memory_by_keyword: Retrieve specific memory by keyword")
    print("  - search: Search memories across all fields in single query")
    print("  - get_all_memories: Get all stored memories (without summary)")
    print("  - get_all_types: Show available type tags with counts")
    print("  - get_all_keywords: List all keywords and titles")
    print("  - get_all_words: Extract all words from database with field-level breakdown (EXPENSIVE)")
    print("  - get_memory_stats: View memory statistics")
    print("  - delete_memory: Delete a memory item")
    print("  - update_memory: Update an existing memory")

    # Run the MCP server
    mcp.run()