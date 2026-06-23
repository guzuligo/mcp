"""
memorylite - Lightweight LLM Memory Database System (SQLite Backend)

Replaces JSON file storage with SQLite for better query performance,
transactional safety, and SQL-based searching instead of regex/string_matching.

MIGRATION GUIDE (from memorydb):
    JSON file (.json  →  SQLite database (.db)
    Regex search       →  SQL SELECT with WHERE/LIKE/IN clauses
    In-memory indexes   →  Database-level B-tree indexes
    File read/write     →  ACID transactions

DATABASE SCHEMA:
    CREATE TABLE memories (
        id TEXT PRIMARY KEY,                                    -- YYMMDDhhmmss format timestamp-based ID (e.g., 260506193000 = 2026-05-06 19:30:00)
        title TEXT NOT NULL,                                     -- Short descriptive title (acts as 'should I read more?' indicator)
        summary TEXT NOT NULL,                                   -- Detailed description with specific details
        memory_type INTEGER NOT NULL DEFAULT 0,                  -- Integer type code (see MEMORY TYPE CODES below)
        related_ids TEXT NOT NULL DEFAULT '[]',                  -- JSON array string: ['id1','id2'] - related memory IDs
        keywords TEXT NOT NULL DEFAULT '[]',                      -- JSON array string: '["kw1","kw2"]' - semantic keywords
        created_at TEXT NOT NULL,                               -- ISO format timestamp of creation
        updated_at TEXT NOT NULL                                -- ISO format timestamp of last update
    );

    MEMORY TYPE CODES (memory_type):
        RESERVED TYPES (0-6) - Built-in categories:
          0 = Unspecified — Default type when no specific category applies
          1 = Personal — Related to the user: their life, feelings, experiences, relationships, personal goals
          2 = Document — Summary or information extracted from a specific document provided to the LLM
          3 = Reference — General knowledge reference: internet search results, pasted content from external sources
          4 = Chat — General conversation without a specific topic or purpose
          5 = Chitchat — Casual conversation, not significant, nothing new was learned
          6 = Technical — Coding sessions, git repos, programming languages, math, science, new procedures

        RESERVED RANGE (7-99) — Reserved for future built-in use

        USER-DEFINED RANGE (100+) — Custom types defined by the user:
          100 = Custom type index memory — Use this memory's keywords to define your custom type meanings
                Example: keywords: '["101=Health", "102=Finance", "103=Education"]'
          101+ = User-defined custom types — Reference the meanings defined in your type 100 memory

    NOTE ABOUT EACH FIELD'S PURPOSE FOR LLMs:
      - id: Unique identifier for this memory (YYMMDDhhmmss format)
      - title: Short descriptive heading that summarizes what the memory is about
      - summary: Detailed description with specific details (dates, numbers, links, names)
                 Should contain specifics that won't be obvious from reading just the title.
      - memory_type: Integer type code for filtering/grouping (see type codes above)
      - related_ids: Links to other memories by their ID for establishing connections
      - keywords: SEMANTIC KEYWORDS/PHRASES useful for SEARCHING and MATCHING
                 These are terms the user might remember and search by later. Include product names,
                 technical terms, key concepts, or phrases that capture the essence of this memory.
                 For user-defined types (100+), type 100 memories use keywords to define type meanings.

    CREATE INDEX idx_memories_id ON memories(id);
    CREATE INDEX idx_memories_created_at ON memories(created_at);

    NOTE: related_ids and keywords are stored as JSON array strings.
          Use json_each() for SQL-based array membership checks.

    ALL QUERIES SHOULD SPECIFY COLUMNS EXPLICITLY (avoid SELECT *) and use details_level 
    parameter to control output detail (0=minimal, 1=excludes summary, 2=full).
"""

import argparse
import json
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Set
from fastmcp import FastMCP

# Thread lock for thread-safe database operations
_db_lock = threading.Lock()

# Selection store: selection_id -> selection data
# Used by select_memory/edit_selection tools
_selection_store: Dict[str, dict] = {}

# Selection ID counter for unique IDs
_selection_counter = 0

mcp = FastMCP("memorylite")


# Memory type code mapping: number -> (name, description)
MEMORY_TYPE_MAP = {
    0: ("unspecified", "Default type when no specific category applies"),
    1: ("personal", "Related to the user: their life, feelings, experiences, relationships, personal goals"),
    2: ("document", "Summary or information extracted from a specific document provided to the LLM"),
    3: ("reference", "General knowledge reference: internet search results, pasted content from external sources"),
    4: ("chat", "General conversation without a specific topic or purpose"),
    5: ("chitchat", "Casual conversation, not significant, nothing new was learned"),
    6: ("technical", "Coding sessions, git repos, programming languages, math, science, new procedures, technical references"),
}

# Reserved range: 7-99 (available for future use)
MEMORY_TYPE_RESERVED_START = 7
MEMORY_TYPE_RESERVED_END = 99

# User-defined range: 100+ (user defines meanings via type 100 memories)
MEMORY_TYPE_USER_DEFINED_START = 100

# Reverse mapping: lowercase name -> number
MEMORY_TYPE_REVERSE = {v[0].lower(): k for k, v in MEMORY_TYPE_MAP.items()}


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


def _convert_to_memory_type(types_input) -> int:
    """Convert various input formats to a single memory type integer.
    
    Handles:
    - Integer values directly (0-99 reserved, 100+ user-defined)
    - String type names (e.g., "personal", "DOCUMENT")
    - String type codes (e.g., "0", "3", "150")
    - List of type names (takes first valid one)
    - List of type numbers (takes first valid one)
    - None or empty -> 0 (unspecified)
    
    Returns:
        int: Memory type code (0-99 reserved, 100+ user-defined), defaults to 0 for invalid input
    """
    # Handle None or empty
    if types_input is None:
        return 0
    
    # Handle list - take first element
    if isinstance(types_input, (list, tuple)):
        if len(types_input) == 0:
            return 0
        types_input = types_input[0]
    
    # Handle integer directly
    if isinstance(types_input, int):
        if types_input >= 0:
            return types_input
        return 0  # Negative, default to unspecified
    
    # Handle string input
    if isinstance(types_input, str):
        types_input = types_input.strip()
        if not types_input:
            return 0
        
        # Try as numeric code first (supports 0-99 reserved, 100+ user-defined)
        try:
            code = int(types_input)
            if code >= 0:
                return code
            return 0
        except ValueError:
            pass
        
        # Try as type name (case-insensitive) - only for reserved types 0-6
        lower_name = types_input.lower().strip()
        if lower_name in MEMORY_TYPE_REVERSE:
            return MEMORY_TYPE_REVERSE[lower_name]
        
        # Try partial matching (e.g., "personal" matches "personal", "chat" matches "chitchat")
        for name, code in MEMORY_TYPE_REVERSE.items():
            if lower_name == name or name.startswith(lower_name) or lower_name.startswith(name):
                return code
        
        # Invalid type name, default to 0
        return 0
    
    # Fallback for unexpected types
    return 0


def _convert_memory_type_to_name(type_code: int) -> str:
    """Convert a memory type integer to its name string."""
    if type_code in MEMORY_TYPE_MAP:
        return MEMORY_TYPE_MAP[type_code][0]
    elif MEMORY_TYPE_RESERVED_START <= type_code <= MEMORY_TYPE_RESERVED_END:
        return f"reserved-{type_code}"
    elif type_code >= MEMORY_TYPE_USER_DEFINED_START:
        return f"custom-{type_code}"
    return "unspecified"


def _convert_memory_type_to_description(type_code: int) -> str:
    """Convert a memory type integer to its description string."""
    if type_code in MEMORY_TYPE_MAP:
        return MEMORY_TYPE_MAP[type_code][1]
    elif MEMORY_TYPE_RESERVED_START <= type_code <= MEMORY_TYPE_RESERVED_END:
        return "Reserved for future use"
    elif type_code >= MEMORY_TYPE_USER_DEFINED_START:
        if type_code == MEMORY_TYPE_USER_DEFINED_START:
            return "User-defined custom type index (use keywords to define custom type meanings)"
        return "User-defined custom type"
    return "Unknown type"


def get_memory_type_info(type_code: int) -> dict:
    """Get complete information about a memory type code.
    
    Args:
        type_code: Integer type code (0-99 reserved, 100+ user-defined)
        
    Returns:
        Dict with code, name, and description
    """
    return {
        "type": type_code,
        "name": _convert_memory_type_to_name(type_code),
        "description": _convert_memory_type_to_description(type_code)
    }


def get_all_memory_type_info() -> list:
    """Get information about all reserved memory types.
    
    Returns:
        List of dicts with complete type information for all reserved types (0-99) plus user-defined range
    """
    result = []
    
    # Reserved types 0-6 (defined)
    for code in range(0, 7):
        result.append(get_memory_type_info(code))
    
    # Reserved types 7-99
    result.append({
        "type": f"{MEMORY_TYPE_RESERVED_START}-{MEMORY_TYPE_RESERVED_END}",
        "name": "reserved-range",
        "description": "Reserved for future built-in use"
    })
    
    # User-defined range
    result.append({
        "type": f"{MEMORY_TYPE_USER_DEFINED_START}+",
        "name": "user-defined",
        "description": "User-defined custom types. Use type 100 memories to define your custom type meanings via keywords."
    })
    
    return result


class MemoryLite:
    """LLM Memory Management System using SQLite as backend."""

    # Configuration - can be modified at the top level
    DB_FILE = None  # Will default to ~/.swordmemory/memory.db if not set

    def _get_db_path(self) -> Path:
        """Get the path to the database file.

        Handles two cases for DB_FILE:
          - If it ends with '/' or '\\' → treat as directory, append 'memory.db'
          - Otherwise → use as full file path
        """
        if self.DB_FILE is not None:
            p = Path(self.DB_FILE)
            # Check if it's a directory (ends with / or \\)
            stripped = str(self.DB_FILE).rstrip()
            if stripped.endswith('/') or stripped.endswith('\\'):
                return Path(stripped.rstrip('/').rstrip('\\\\')) / "memory.db"
            else:
                return p
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

    def _check_db_exists(self) -> bool:
        """Check if the database file exists.
        
        Returns True if the DB file exists or can be created, False otherwise.
        Also returns True after _init_db() is called since it creates the schema.
        """
        db_path = self._get_db_path()
        return Path(db_path).exists()

    def ensure_db_initialized(self) -> None:
        """Ensure the database file exists and is initialized.
        
        Creates the DB file if it doesn't exist, then initializes the schema.
        Also repairs any malformed data in existing records.
        Raises FileNotFoundError if the parent directory doesn't exist or can't be created.
        """
        db_path = self._get_db_path()
        db_file = Path(db_path)
        
        # If path ends with / or \, it's a directory - ensure dir exists
        stripped = str(db_path).rstrip('/\\')
        if stripped.endswith('/') or stripped.endswith('\\'):
            dir_path = Path(stripped)
            dir_path.mkdir(parents=True, exist_ok=True)
        else:
            # It's a file path - ensure parent directory exists
            parent_dir = db_file.parent
            if not parent_dir.exists():
                parent_dir.mkdir(parents=True, exist_ok=True)
        
        self._init_db()
        self.repair_database()

    def _get_connection_safe(self) -> sqlite3.Connection:
        """Get a SQLite connection with proper settings and error handling.
        
        Returns (conn, None) on success or (None, error_message) if the DB file doesn't exist yet.
        """
        try:
            return self._get_connection(), None
        except sqlite3.OperationalError as e:
            if "unable to open database file" in str(e):
                db_path = self._get_db_path()
                if not Path(db_path).exists():
                    return None, f"Database not yet initiated. Save a memory first."
                raise
            raise

    def _init_db(self) -> None:
        """Initialize the database schema if not exists."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    memory_type INTEGER NOT NULL DEFAULT 0,
                    related_ids TEXT NOT NULL DEFAULT '[]',
                    keywords TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memories_id ON memories(id);
                CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
            """)

    def repair_database(self) -> dict:
        """Repair malformed data in existing database records.
        
        Scans all records and fixes any malformed JSON fields (related_ids, keywords).
        Also handles legacy schema issues (e.g., old keyword column references).
        
        Returns:
            Dict with repair statistics
        """
        repair_stats = {
            "total_records": 0,
            "fixed_records": 0,
            "fixed_fields": {}
        }
        
        db = self._get_connection()
        try:
            # Get all records
            rows = db.execute(
                "SELECT id, related_ids, keywords FROM memories"
            ).fetchall()
            
            repair_stats["total_records"] = len(rows)
            records_to_update = []
            
            for row in rows:
                mem_id, related_ids, keywords = row
                needs_fix = False
                fixed_fields = {}
                
                # Fix related_ids
                if related_ids is not None:
                    parsed = self._safe_parse_json(related_ids, None)
                    if parsed is None or not isinstance(parsed, list):
                        # Try to fix common issues
                        if isinstance(related_ids, str):
                            # Try stripping whitespace and re-parsing
                            stripped = related_ids.strip()
                            if stripped:
                                try:
                                    parsed = json.loads(stripped)
                                    if isinstance(parsed, list):
                                        fixed_fields["related_ids"] = stripped
                                        needs_fix = True
                                except (json.JSONDecodeError, TypeError):
                                    # Try to fix common LLM formatting issues
                                    fixed_str = stripped.replace("'", '"').replace("None", "null")
                                    try:
                                        parsed = json.loads(fixed_str)
                                        if isinstance(parsed, list):
                                            fixed_fields["related_ids"] = fixed_str
                                            needs_fix = True
                                    except (json.JSONDecodeError, TypeError):
                                        fixed_fields["related_ids"] = "[]"
                                        needs_fix = True
                            else:
                                fixed_fields["related_ids"] = "[]"
                                needs_fix = True
                        else:
                            fixed_fields["related_ids"] = "[]"
                            needs_fix = True
                
                # Fix keywords
                if keywords is not None:
                    parsed = self._safe_parse_json(keywords, None)
                    if parsed is None or not isinstance(parsed, list):
                        if isinstance(keywords, str):
                            stripped = keywords.strip()
                            if stripped:
                                try:
                                    parsed = json.loads(stripped)
                                    if isinstance(parsed, list):
                                        fixed_fields["keywords"] = stripped
                                        needs_fix = True
                                except (json.JSONDecodeError, TypeError):
                                    fixed_str = stripped.replace("'", '"').replace("None", "null")
                                    try:
                                        parsed = json.loads(fixed_str)
                                        if isinstance(parsed, list):
                                            fixed_fields["keywords"] = fixed_str
                                            needs_fix = True
                                    except (json.JSONDecodeError, TypeError):
                                        fixed_fields["keywords"] = "[]"
                                        needs_fix = True
                            else:
                                fixed_fields["keywords"] = "[]"
                                needs_fix = True
                        else:
                            fixed_fields["keywords"] = "[]"
                            needs_fix = True
                
                if needs_fix:
                    records_to_update.append((mem_id, fixed_fields))
                    repair_stats["fixed_records"] += 1
                    for field in fixed_fields:
                        repair_stats["fixed_fields"][field] = repair_stats["fixed_fields"].get(field, 0) + 1
            
            # Apply fixes
            for mem_id, fixed_fields in records_to_update:
                set_clause = ", ".join(f"{k} = ?" for k in fixed_fields.keys())
                values = list(fixed_fields.values()) + [mem_id]
                db.execute(
                    f"UPDATE memories SET {set_clause} WHERE id = ?",
                    tuple(values)
                )
            
            if records_to_update:
                db.commit()
                
        finally:
            db.close()
        
        return repair_stats

    def _get_timestamp(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    def save_memory(
        self,
        title: str,
        summary: str,
        memory_type: int = 0,
        related_ids: List[str] = None,
        important_keywords: List[str] = None
    ) -> dict:
        """Save a new memory item to the database.

        Args:
            title: Short descriptive title
            summary: Detailed description with specific details
            memory_type: Integer type code (0-99 reserved, 100+ user-defined, see MEMORY_TYPE_MAP)
            related_ids: List of related memory IDs
            important_keywords: List of semantic keywords for searching

        Returns:
            dict with saved memory data
        """
        # Validate memory_type - must be non-negative integer
        if not isinstance(memory_type, int) or memory_type < 0:
            memory_type = 0

        timestamp = self._get_timestamp()
        memory_id = self._generate_memory_id(timestamp)

        db = self._get_connection()
        try:
            db.execute(
                """INSERT INTO memories 
                   (id, title, summary, memory_type, related_ids, keywords, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    title,
                    summary,
                    memory_type,
                    json.dumps(related_ids or []),
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
            "title": title,
            "summary": summary,
            "memory_type": memory_type,
            "memory_type_name": _convert_memory_type_to_name(memory_type),
            "related_ids": related_ids or [],
            "keywords": important_keywords or [],
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": "success"
        }

    def get_memory_by_id(self, memory_id: str, details_level: int = 2) -> Optional[dict]:
        """Retrieve a specific memory by its ID.

        Args:
            memory_id: The unique identifier of the memory
            details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full
        """
        db = self._get_connection()
        try:
            row = db.execute(
                f"SELECT {self._select_columns_for_level(details_level)} FROM memories WHERE id = ?",
                (memory_id,)
            ).fetchone()
            return self._row_to_dict(row, details_level) if row else None
        finally:
            db.close()

    def get_memories_by_ids(self, memory_ids: List[str], details_level: int = 1) -> List[dict]:
        """Retrieve multiple memories by their IDs.

        Args:
            memory_ids: List of unique identifiers to retrieve
            details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full

        Returns:
            List of matching memory records
        """
        if not memory_ids:
            return []

        db = self._get_connection()
        try:
            placeholders = ','.join(['?' for _ in memory_ids])
            columns = self._select_columns_for_level(details_level)
            query = f"SELECT {columns} FROM memories WHERE id IN ({placeholders})"
            rows = db.execute(query, memory_ids).fetchall()
            return [self._row_to_dict(row, details_level) for row in rows] if rows else []
        finally:
            db.close()

    def get_all_memories(self, details_level: int = 1) -> List[dict]:
        """Get all memory items.

        Args:
            details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full
        """
        db = self._get_connection()
        try:
            columns = self._select_columns_for_level(details_level)
            rows = db.execute(
                f"SELECT {columns} FROM memories ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_dict(row, details_level) for row in rows] if rows else []
        finally:
            db.close()

    def search(
        self,
        pattern: str = None,
        memory_type: int = None,
        details_level: int = 1,
        wordJoin: str = "OR"
    ) -> List[dict]:
        """Search memories across all text fields in a single query.

        Searches title, summary, and keywords.
        
        When pattern contains spaces, each space-separated token becomes a separate LIKE condition.
        The wordJoin parameter controls how these conditions are combined:
          - "OR" (default): Each word is searched independently; ANY word matching returns the record
          - "AND": Each word must appear in the result; ALL words must match

        Args:
            pattern: Text to search for (each space-separated token becomes a separate LIKE condition)
            memory_type: Filter by memory type code (0-6)
            details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full
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
                
                # Build OR/AND grouped conditions per field (title, summary, keywords)
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

            if memory_type is not None:
                conditions.append("memory_type = ?")
                params.append(memory_type)

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            columns = self._select_columns_for_level(details_level)
            query = f"SELECT {columns} FROM memories WHERE {where_clause}"

            rows = db.execute(query, params).fetchall()
            return [self._row_to_dict(row, details_level) for row in rows] if rows else []
        finally:
            db.close()

    def update_memory(self, memory_id, updates: dict) -> bool:
        """Update an existing memory.

        Args:
            memory_id: The unique identifier of the memory to update (will be converted to string if needed)
            updates: Dictionary of fields to update

        Returns:
            True if updated successfully, False if not found
        """
        # Ensure memory_id is always a string for consistent SQLite matching
        if not isinstance(memory_id, str):
            memory_id = str(memory_id).strip() if memory_id else ""
        
        valid_keys = ("title", "summary", "memory_type", "related_ids", "keywords")
        for key in updates.keys():
            if key not in valid_keys:
                raise ValueError(f"Invalid field '{key}'. Valid fields are: {valid_keys}")

        timestamp = self._get_timestamp()
        
        # Process updates
        processed_updates = {}
        json_fields = {"related_ids", "keywords"}
        
        for key, value in updates.items():
            if key == "memory_type":
                # Convert to integer memory type
                processed_updates[key] = _convert_to_memory_type(value)
            elif key in json_fields:
                if isinstance(value, list):
                    processed_updates[key] = json.dumps(value)
                elif isinstance(value, str):
                    # Already a string - try to parse as JSON, keep as-is if not valid JSON
                    try:
                        parsed = json.loads(value)
                        if isinstance(parsed, list):
                            processed_updates[key] = json.dumps(parsed)
                        else:
                            processed_updates[key] = value
                    except (json.JSONDecodeError, TypeError):
                        processed_updates[key] = value
            else:
                processed_updates[key] = value

        if not processed_updates:
            return False

        set_clause = ", ".join(f"{k} = ?" for k in processed_updates.keys())
        
        db = self._get_connection()
        try:
            values = list(processed_updates.values()) + [timestamp, memory_id]
            rows_affected = db.execute(
                f"UPDATE memories SET {set_clause}, updated_at = ? WHERE id = ?",
                tuple(values)
            ).rowcount
            db.commit()
            return rows_affected > 0
        finally:
            db.close()

    def append_to_summary(self, memory_id: str, summary_addition: str, separator: str = "\n\n---\n\n") -> dict:
        """Append text to an existing memory's summary field.

        Args:
            memory_id: The unique identifier of the memory to update
            summary_addition: Text to append to the summary
            separator: Custom separator between old and new content (default: "\\n\\n---\\n\\n")

        Returns:
            Dict with status, original_summary_length, new_summary_length, and message
        """
        db = self._get_connection()
        try:
            row = db.execute(
                "SELECT summary, updated_at FROM memories WHERE id = ?",
                (memory_id,)
            ).fetchone()

            if not row:
                return {
                    "status": "not_found",
                    "message": f"No memory found with ID '{memory_id}'"
                }

            original_summary = row[0]
            new_summary = original_summary + separator + summary_addition
            timestamp = self._get_timestamp()

            db.execute(
                "UPDATE memories SET summary = ?, updated_at = ? WHERE id = ?",
                (new_summary, timestamp, memory_id)
            )
            db.commit()

            return {
                "status": "success",
                "message": f"Summary appended to memory '{memory_id}'",
                "original_summary_length": len(original_summary),
                "new_summary_length": len(new_summary),
                "separator_used": repr(separator)
            }
        finally:
            db.close()

    def append_to_keywords(self, memory_id: str, new_keywords: List[str]) -> dict:
        """Append new keywords to an existing memory's keywords list.

        Duplicates are automatically filtered out.

        Args:
            memory_id: The unique identifier of the memory to update
            new_keywords: List of keyword strings to add

        Returns:
            Dict with status, added_count, removed_duplicates, total_count, and message
        """
        db = self._get_connection()
        try:
            row = db.execute(
                "SELECT keywords, updated_at FROM memories WHERE id = ?",
                (memory_id,)
            ).fetchone()

            if not row:
                return {
                    "status": "not_found",
                    "message": f"No memory found with ID '{memory_id}'"
                }

            existing_keywords = self._safe_parse_json(row[0], [])
            new_set = set(existing_keywords)
            added = [kw for kw in new_keywords if kw not in new_set]
            duplicates = len(new_keywords) - len(added)
            
            if not added:
                return {
                    "status": "success",
                    "message": f"No new keywords to add to memory '{memory_id}' (all were duplicates)",
                    "added_count": 0,
                    "removed_duplicates": duplicates,
                    "total_count": len(new_set)
                }

            # Add the new keywords to the set before converting to list
            new_set.update(added)
            updated_keywords = list(new_set)
            timestamp = self._get_timestamp()

            db.execute(
                "UPDATE memories SET keywords = ?, updated_at = ? WHERE id = ?",
                (json.dumps(updated_keywords), timestamp, memory_id)
            )
            db.commit()

            return {
                "status": "success",
                "message": f"Added {len(added)} new keywords to memory '{memory_id}'",
                "added_keywords": added,
                "added_count": len(added),
                "removed_duplicates": duplicates,
                "total_count": len(updated_keywords)
            }
        finally:
            db.close()

    def append_to_related_ids(self, memory_id: str, new_related_ids: List[str]) -> dict:
        """Append new related IDs to an existing memory's related_ids list.

        Duplicates are automatically filtered out.

        Args:
            memory_id: The unique identifier of the memory to update
            new_related_ids: List of memory ID strings to add

        Returns:
            Dict with status, added_count, removed_duplicates, total_count, and message
        """
        db = self._get_connection()
        try:
            row = db.execute(
                "SELECT related_ids, updated_at FROM memories WHERE id = ?",
                (memory_id,)
            ).fetchone()

            if not row:
                return {
                    "status": "not_found",
                    "message": f"No memory found with ID '{memory_id}'"
                }

            existing_ids = self._safe_parse_json(row[0], [])
            new_set = set(existing_ids)
            added = [rid for rid in new_related_ids if rid not in new_set]
            duplicates = len(new_related_ids) - len(added)

            if not added:
                return {
                    "status": "success",
                    "message": f"No new related IDs to add to memory '{memory_id}' (all were duplicates)",
                    "added_count": 0,
                    "removed_duplicates": duplicates,
                    "total_count": len(new_set)
                }

            # Add the new related IDs to the set before converting to list
            new_set.update(added)
            updated_ids = list(new_set)
            timestamp = self._get_timestamp()

            db.execute(
                "UPDATE memories SET related_ids = ?, updated_at = ? WHERE id = ?",
                (json.dumps(updated_ids), timestamp, memory_id)
            )
            db.commit()

            return {
                "status": "success",
                "message": f"Added {len(added)} new related IDs to memory '{memory_id}'",
                "added_related_ids": added,
                "added_count": len(added),
                "removed_duplicates": duplicates,
                "total_count": len(updated_ids)
            }
        finally:
            db.close()

    def select_memory(
        self,
        memory_id: str,
        pattern: str = None,
        mode: str = "exact",
        start_line: int = None,
        end_line: int = None
    ) -> dict:
        """Select/search text within a memory's summary.

        Modes:
          - "exact": Exact string matching (case-sensitive)
          - "regex": Regular expression pattern
          - "lines": Line range selection (1-based line numbers)

        Args:
            memory_id: The unique identifier of the memory
            pattern: Search pattern (required for exact/regex modes)
            mode: Search mode - "exact", "regex", or "lines"
            start_line: Start line number (for "lines" mode, 1-based)
            end_line: End line number (for "lines" mode, 1-based, inclusive)

        Returns:
            Dict with selection_id, occurrences, matched_text, truncated, and match positions
        """
        global _selection_counter

        db = self._get_connection()
        try:
            row = db.execute(
                "SELECT summary FROM memories WHERE id = ?",
                (memory_id,)
            ).fetchone()

            if not row:
                return {
                    "status": "not_found",
                    "message": f"No memory found with ID '{memory_id}'",
                    "occurrences": 0,
                    "matched_text": "",
                    "truncated": False,
                    "selection_id": None
                }

            summary = row[0] or ""
            matches = []

            if mode == "lines":
                if start_line is None or end_line is None:
                    return {
                        "status": "error",
                        "message": "Lines mode requires start_line and end_line parameters",
                        "occurrences": 0,
                        "matched_text": "",
                        "truncated": False,
                        "selection_id": None
                    }

                lines = summary.split("\n")
                s_idx = max(0, start_line - 1)
                e_idx = min(len(lines), end_line)

                if s_idx >= len(lines):
                    return {
                        "status": "not_found",
                        "message": f"Line range [{start_line}, {end_line}] is beyond memory content ({len(lines)} lines)",
                        "occurrences": 0,
                        "matched_text": "",
                        "truncated": False,
                        "selection_id": None
                    }

                selected_lines = lines[s_idx:e_idx]
                matched_text = "\n".join(selected_lines)
                matches = [{"start": 0, "end": len(matched_text)}]

            elif mode == "regex":
                if not pattern:
                    return {
                        "status": "error",
                        "message": "Regex mode requires a pattern",
                        "occurrences": 0,
                        "matched_text": "",
                        "truncated": False,
                        "selection_id": None
                    }
                try:
                    compiled = re.compile(pattern)
                except re.error as e:
                    return {
                        "status": "error",
                        "message": f"Invalid regex pattern: {e}",
                        "occurrences": 0,
                        "matched_text": "",
                        "truncated": False,
                        "selection_id": None
                    }
                matches = [{"start": m.start(), "end": m.end()} for m in compiled.finditer(summary)]

            else:  # exact mode (default)
                if not pattern:
                    return {
                        "status": "error",
                        "message": "Exact mode requires a pattern",
                        "occurrences": 0,
                        "matched_text": "",
                        "truncated": False,
                        "selection_id": None
                    }
                idx = 0
                while True:
                    pos = summary.find(pattern, idx)
                    if pos == -1:
                        break
                    matches.append({"start": pos, "end": pos + len(pattern)})
                    idx = pos + 1

            if not matches:
                return {
                    "status": "not_found",
                    "message": "No results found",
                    "occurrences": 0,
                    "matched_text": "",
                    "truncated": False,
                    "selection_id": None
                }

            # Build combined matched text and apply truncation if > 500 chars
            # For multiple matches, concatenate all matched portions
            matched_portions = []
            for m in matches:
                matched_portions.append(summary[m["start"]:m["end"]])
            full_matched = "\n".join(matched_portions) if len(matches) > 1 else summary[matches[0]["start"]:matches[0]["end"]]

            # If single match, show the match with context (up to 200 chars before and after)
            if len(matches) == 1:
                m = matches[0]
                context_before = max(0, m["start"] - 200)
                context_after = min(len(summary), m["end"] + 200)
                full_matched = summary[context_before:context_after]

            # Apply truncation if total > 500 chars
            truncated = False
            if len(full_matched) > 500:
                truncated = True
                first_200 = full_matched[:200]
                last_200 = full_matched[-200:]
                removed = len(full_matched) - 400
                full_matched = first_200 + "\n...<truncated>..." + last_200

            # Generate unique selection ID
            _selection_counter += 1
            sel_id = f"sel_{memory_id}_{_selection_counter}"

            # Store selection in global store
            _selection_store[sel_id] = {
                "memory_id": memory_id,
                "mode": mode,
                "pattern": pattern,
                "start_line": start_line,
                "end_line": end_line,
                "matches": matches,
                "summary": summary,
                "full_matched": full_matched,
                "truncated": truncated
            }

            return {
                "status": "success",
                "memory_id": memory_id,
                "mode": mode,
                "occurrences": len(matches),
                "matched_text": full_matched,
                "truncated": truncated,
                "selection_id": sel_id,
                "match_positions": matches
            }
        finally:
            db.close()

    def edit_selection(self, selection_id: str, replacement: str, occurrence: int = 1) -> dict:
        """Edit text based on a previous selection.

        Args:
            selection_id: The selection ID from select_memory
            replacement: Text to replace matched content with
            occurrence: Which occurrence to edit - 1=first, 2=second, 0=all

        Returns:
            Dict with status, changes_made, and edit details
        """
        global _selection_store

        if selection_id not in _selection_store:
            return {
                "status": "error",
                "message": f"Selection '{selection_id}' not found. Please call select_memory first."
            }

        sel = _selection_store[selection_id]
        memory_id = sel["memory_id"]
        matches = sel["matches"]
        summary = sel["summary"]
        mode = sel["mode"]

        # Determine which matches to edit based on occurrence parameter
        if occurrence == 0:
            # Replace all occurrences
            indices_to_edit = list(range(len(matches)))
        elif occurrence < 0 or occurrence > len(matches):
            return {
                "status": "error",
                "message": f"Occurrence {occurrence} is out of range. Found {len(matches)} match(es)."
            }
        else:
            # Replace specific occurrence (1-based)
            indices_to_edit = [occurrence - 1]

        if not indices_to_edit:
            return {
                "status": "error",
                "message": "No matches selected for editing."
            }

        # Apply replacements in reverse order to preserve positions
        new_summary = summary
        edits_applied = []
        for idx in sorted(indices_to_edit, reverse=True):
            m = matches[idx]
            new_summary = new_summary[:m["start"]] + replacement + new_summary[m["end"]:]
            edits_applied.append({
                "occurrence": idx + 1,
                "old_text": summary[m["start"]:m["end"]],
                "new_text": replacement,
                "position": m
            })

        # Update database
        db = self._get_connection()
        try:
            timestamp = self._get_timestamp()
            db.execute(
                "UPDATE memories SET summary = ?, updated_at = ? WHERE id = ?",
                (new_summary, timestamp, memory_id)
            )
            db.commit()
        finally:
            db.close()

        # Nullify the selection (remove from store)
        del _selection_store[selection_id]

        return {
            "status": "success",
            "memory_id": memory_id,
            "selection_id": selection_id,
            "selection_nullified": True,
            "changes_made": len(edits_applied),
            "edits": edits_applied,
            "message": f"Selection '{selection_id}' has been nullified. Call select_memory again to select new content."
        }

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
            
            # Get counts per reserved type (0-6)
            type_counts = {}
            for type_code in MEMORY_TYPE_MAP:
                count = db.execute(
                    "SELECT COUNT(*) FROM memories WHERE memory_type = ?",
                    (type_code,)
                ).fetchone()[0]
                type_counts[type_code] = {
                    "name": MEMORY_TYPE_MAP[type_code][0],
                    "description": MEMORY_TYPE_MAP[type_code][1],
                    "count": count
                }

            # Get counts for reserved range (7-99)
            reserved_count = db.execute(
                "SELECT COUNT(*) FROM memories WHERE memory_type >= ? AND memory_type <= ?",
                (MEMORY_TYPE_RESERVED_START, MEMORY_TYPE_RESERVED_END)
            ).fetchone()[0]
            type_counts["reserved_range"] = {
                "name": "reserved-range",
                "description": "Reserved for future use (7-99)",
                "count": reserved_count
            }

            # Get counts for user-defined range (100+)
            custom_count = db.execute(
                "SELECT COUNT(*) FROM memories WHERE memory_type >= ?",
                (MEMORY_TYPE_USER_DEFINED_START,)
            ).fetchone()[0]
            type_counts["user_defined_range"] = {
                "name": "user-defined",
                "description": "User-defined custom types (100+)",
                "count": custom_count
            }

            return {
                "total_memories": total or 0,
                "last_updated": db.execute("SELECT MAX(updated_at) FROM memories").fetchone()[0],
                "type_stats": type_counts
            }
        finally:
            db.close()

    def get_all_types(self) -> List[dict]:
        """Get all unique memory type codes used in the database with counts.

        Returns:
            List of dicts with type code, name, and count
        """
        db = self._get_connection()
        try:
            rows = db.execute(
                """SELECT memory_type, COUNT(*) as count 
                   FROM memories 
                   GROUP BY memory_type 
                   ORDER BY memory_type"""
            ).fetchall()
            return [
                {"type": row[0], "name": _convert_memory_type_to_name(row[0]), "count": row[1]}
                for row in rows
            ] if rows else []
        finally:
            db.close()

    def get_all_keywords(self, pattern: str = None) -> List[dict]:
        """Get all keyword/title pairs with optional pattern filter.

        Args:
            pattern: Optional string to filter keywords by title

        Returns:
            List of dicts with title and keywords info
        """
        db = self._get_connection()
        try:
            where_clause = "WHERE 1=1"
            params = []
            
            if pattern:
                where_clause += " AND title LIKE ?"
                params.append(f"%{pattern}%")

            select_clause = "SELECT title, keywords FROM memories"
            full_query = f"{select_clause} {where_clause} GROUP BY id ORDER BY created_at DESC"
            rows = db.execute(full_query, params).fetchall()
            
            result = []
            for row in rows:
                kw_list = []
                if row[1]:
                    try:
                        kw_list = json.loads(row[1])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append({"title": row[0], "keywords": kw_list})
            
            return result if result else []
        finally:
            db.close()

    def get_all_words(self, pattern: str = None) -> dict:
        """Extract all words from every text field in the database.

        This method scans all text fields (title, summary, keywords)
        and returns a breakdown of which words appear in which fields.

        Args:
            pattern: Optional string to filter results - only includes words containing this substring

        Returns:
            Dict with field-level word breakdown
        """
        db = self._get_connection()
        try:
            rows = db.execute(
                "SELECT id, title, summary, keywords FROM memories"
            ).fetchall()

            result = {
                "title": set(),
                "summary": set(),
                "keywords": set()
            }

            for row in rows:
                # row[0]=id, row[1]=title, row[2]=summary, row[3]=keywords
                
                if row[1]:  # title
                    for word in self._extract_words(row[1]):
                        result["title"].add(word)

                if row[2]:  # summary
                    for word in self._extract_words(row[2]):
                        result["summary"].add(word)

                if row[3]:  # keywords (JSON string)
                    try:
                        kw_list = json.loads(row[3])
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

    def _select_columns_for_level(self, details_level: int) -> str:
        """Build column list for SELECT queries based on detail level.
        
        Args:
            details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full
        """
        if details_level == 0:
            return "id, title, keywords"
        elif details_level == 1:
            return "id, title, memory_type, related_ids, keywords, created_at, updated_at"
        else:  # details_level == 2
            return "id, title, summary, memory_type, related_ids, keywords, created_at, updated_at"

    def _safe_parse_json(self, value, default=None):
        """Safely parse a JSON string, returning default on any error.
        
        Handles malformed data gracefully by catching all exceptions.
        """
        if value is None:
            return default if default is not None else []
        if isinstance(value, (list, dict)):
            return value
        if not isinstance(value, str):
            return default if default is not None else []
        try:
            result = json.loads(value)
            return result if result is not None else (default if default is not None else [])
        except (json.JSONDecodeError, TypeError, ValueError):
            return default if default is not None else []

    def _row_to_dict(self, row, details_level: int = 2) -> Optional[dict]:
        """Convert a SQLite row to dictionary format.

        Args:
            row: SQLite row data (already fetched from database)
            details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full
        """
        if not row:
            return None
        
        # Map column names based on detail level
        if details_level == 0:
            all_columns = ["id", "title", "keywords"]
        elif details_level == 1:
            all_columns = ["id", "title", "memory_type", "related_ids", "keywords", "created_at", "updated_at"]
        else:  # details_level == 2
            all_columns = ["id", "title", "summary", "memory_type", "related_ids", "keywords", "created_at", "updated_at"]
        
        # Build dict from the existing row data
        row_data = {}
        for i, col in enumerate(all_columns):
            if i < len(row):
                row_data[col] = row[i]

        # Parse JSON arrays from strings with safe error handling
        for field in ["related_ids", "keywords"]:
            row_data[field] = self._safe_parse_json(row_data.get(field), [])

        # Add memory type name
        if "memory_type" in row_data:
            row_data["memory_type_name"] = _convert_memory_type_to_name(row_data["memory_type"])

        return row_data


# ============================================================================
# FastMCP Tool Functions
# ============================================================================

from typing import Union

from typing import Optional as Opt

def _handle_db_error(func):
    """Decorator that catches 'unable to open database file' errors and returns a descriptive message."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "unable to open database file" in str(e):
                return json.dumps({
                    "status": "error",
                    "message": "Database not yet initiated. Save a memory first."
                }, indent=2)
            raise
    return wrapper

@mcp.tool
def memorylite_save_memory(
    title: str = "",
    summary: str = "",
    memory_type: Opt[Union[str, int]] = 0,
    related_ids: Opt[Union[str, list]] = "[]",
    keywords: Opt[Union[str, list]] = "[]"
) -> str:
    """Save a memory item to the database.

    USE THIS TOOL whenever you learn something worth remembering - a conversation topic, 
    technical detail, document content, search result, or anything the user might want to 
    recall later.

    IMPORTANT - UNDERSTANDING THE FIELDS:
      - title: A SHORT descriptive label (like a document title or email subject). Keep it concise.
        Example: "Python Asyncio Tutorial", "Meeting with Sarah about Q4 plans"
      
      - summary: This is NOT just a brief summary. The summary field stores the FULL content:
        * Complete conversation transcripts worth remembering
        * Full document excerpts or references
        * Detailed technical notes with all specifics
        * Search results with all relevant information
        * Any content where you might want to see the COMPLETE details later
        Include EVERYTHING: dates, names, numbers, code snippets, links, quotes.
        Think of it as a storage field for complete information, not a summary field.
      
      - keywords: SEMANTIC KEYWORDS/PHRASES for searching. These are terms the user might 
        remember and search by later. Include product names, technical terms, key concepts.

    WHAT TO INCLUDE in keywords:
      - Product names, model numbers, technical specifications
      - Key concepts, frameworks, or methodologies mentioned
      - Names of people, organizations, or locations relevant to this memory
      - Specific dates, version numbers, or identifiers that could be searched later
      - Phrases that capture the essence of what this memory is about
    
    WHAT NOT TO INCLUDE:
      - Generic stop words (the, a, an, for, etc.)
      - Anything too broad to be useful as a search term

    Args:
        title: Short descriptive title (acts as 'should I read more?' indicator)
        summary: FULL detailed content - complete information, not a brief summary.
                 Include all specifics: dates, numbers, links, names, code, quotes.
        memory_type: Integer type code (0-6) or type name string:
                     0=unspecified, 1=personal, 2=document, 3=reference, 4=chat, 5=chitchat, 6=technical
        related_ids: JSON array string or list of memory IDs this is related to
                     Example: '["id1", "id2"]' or ["id1", "id2"]
        keywords: JSON array string or list of semantic keywords for lookup/searching
                  Example: '["python", "asyncio", "tutorial"]' or ["python", "asyncio", "tutorial"]

    Returns:
        JSON string with the saved memory data and timestamp. Includes a warning about 
        empty keywords if not populated.

    EXAMPLE USAGE:
      # Save a technical conversation
      memorylite_save_memory(
          title="Python Asyncio Tutorial Notes",
          summary="Full notes from the asyncio tutorial: asyncio.create_task() creates a task from a coroutine...",
          memory_type="technical",
          keywords='["python", "asyncio", "concurrency", "coroutine"]'
      )
    """
    try:
        # Validate required parameters
        if not title or not title.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'title' is required and cannot be empty. Provide a short descriptive title for this memory.",
                "hint": "Example: title='Python Asyncio Tutorial Notes'"
            }, indent=2)

        if not summary or not summary.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'summary' is required and cannot be empty. Provide the full detailed content to store.",
                "hint": "The summary field stores complete information: dates, names, numbers, code snippets, links, quotes. Think of it as storage for full content, not a brief summary."
            }, indent=2)

        mem = MemoryLite()
        mem.ensure_db_initialized()  # Ensure DB exists and is initialized

        # Convert memory_type - tolerant of various input formats
        memory_type_int = _convert_to_memory_type(memory_type)

        # Handle both string and already-parsed list inputs from LLMs
        related_ids_list = related_ids if isinstance(related_ids, list) else (_parse_json_list_or_fix(related_ids) if isinstance(related_ids, str) else None)
        keywords_list = keywords if isinstance(keywords, list) else (_parse_json_list_or_fix(keywords) if isinstance(keywords, str) else None)

        result = mem.save_memory(
            title=title,
            summary=summary,
            memory_type=memory_type_int,
            related_ids=related_ids_list or [],
            important_keywords=keywords_list or []
        )

        # Build the result with a warning about keywords if empty
        kw_list = keywords if isinstance(keywords, list) else (_parse_json_list_or_fix(keywords) if isinstance(keywords, str) else None)
        warning_note = ""
        if not kw_list or len(kw_list) == 0:
            warning_note = " WARNING: 'keywords' is empty. Consider adding semantic keywords/phrases that capture the essence of this memory for better searchability later."

        return json.dumps({
            "status": "success",
            "message": f"Memory saved: '{title}'",
            "id": result["id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "memory_type": memory_type_int,
            "memory_type_name": result["memory_type_name"],
            "keywords": kw_list,
            "note": warning_note or None
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to save memory: {str(e)}",
            "hint": "Check that title and summary are provided and not empty."
        }, indent=2)


@mcp.tool
def memorylite_get_memory_by_id(memory_id: str = "", details_level: int = 2) -> str:
    """Retrieve a specific memory by its ID.

    Args:
        memory_id: The unique identifier of the memory to retrieve
        details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full (default: 2)

    Returns:
        JSON string containing the memory data
    """
    try:
        mem = MemoryLite()
        result = mem.get_memory_by_id(memory_id, details_level)

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
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_get_memories_by_ids(memory_ids_str: str = "", details_level: int = 1) -> str:
    """Retrieve multiple memories by their IDs.

    Args:
        memory_ids_str: Comma-separated string of memory IDs (e.g., "id1,id2,id3")
        details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full (default: 1)

    Returns:
        JSON string containing the retrieved memories
    """
    try:
        mem = MemoryLite()
        ids = [x.strip() for x in memory_ids_str.split(",")] if memory_ids_str else []
        
        results = mem.get_memories_by_ids(ids, details_level)

        return json.dumps({
            "status": "success",
            "total_found": len(results),
            "memories": results
        }, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_search(
    pattern: str = "",
    memory_type: Opt[Union[str, int]] = None,
    details_level: int = 1,
    wordJoin: str = "OR"
) -> str:
    """Search memories by pattern across all fields or type filter.

    Searches title, summary, and keywords in a single query.

    HOW THE PATTERN IS PROCESSED:
      The pattern is split by spaces - each space-separated token becomes a separate LIKE condition.
      
      wordJoin controls how these conditions are combined:
        - "OR" (default): Each word is searched independently; ANY word matching returns the record
          Example: search(pattern="meeting about budget", wordJoin="OR") 
                   → finds memories containing "meeting OR about OR budget
        
        - "AND": EACH word must appear in the result; ALL words must match
          Example: search(pattern="meeting about budget", wordJoin="AND")
                   → only returns memories containing "meeting AND about AND budget

    MEMORY TYPE FILTER:
      memory_type: Integer (0-6) or type name string:
        0=unspecified, 1=personal, 2=document, 3=reference, 4=chat, 5=chitchat, 6=technical

    USE CASES:
      - Use OR (default) for broad discovery: find anything related to any of these terms
      - Use AND for precise filtering: require all terms appear in each result
      
    EXAMPLE WORKFLOW for deep memory exploration:
      Step 1: search(memory_type=1)              → All personal memories
      Step 2: search(pattern="meeting")          → Memories about meetings  
      Step 3: search(memory_type=2)              → All document memories
      Step 4: search(pattern="budget", wordJoin="AND") → Budget-related with ALL words

    Args:
        pattern: Text to search for (each space-separated token becomes a separate LIKE condition)
        memory_type: Integer type code (0-6) or type name string to filter by
        details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full (default: 1)
        wordJoin: How to combine multi-word patterns - "OR" (any word matches, default) or "AND" (all words must match)

    Returns:
        JSON string containing list of matched memory data. Each call returns ALL matching results - 
        no pagination needed. Make multiple calls with different filters to get comprehensive coverage.
    """
    try:
        mem = MemoryLite()
        
        # Convert memory_type - tolerant of various input formats
        memory_type_int = _convert_to_memory_type(memory_type) if memory_type is not None else None
        
        results = mem.search(
            pattern=pattern if pattern else None,
            memory_type=memory_type_int,
            details_level=details_level,
            wordJoin=wordJoin
        )

        return json.dumps({
            "status": "success",
            "total_matches": len(results),
            "results": results
        }, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_get_all_memories(details_level: int = 1) -> str:
    """Get all memory items.

    Args:
        details_level: 0=minimal (title+keywords only), 1=excludes summary, 2=full (default: 1)

    Returns:
        JSON string containing list of all memories
    """
    try:
        mem = MemoryLite()
        results = mem.get_all_memories(details_level)

        return json.dumps({
            "status": "success",
            "total_memories": len(results),
            "memories": results
        }, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_get_all_types() -> str:
    """Get all unique memory type codes used in the database with counts.

    MEMORY TYPE CODES:
      RESERVED TYPES (0-6):
        0 = Unspecified — Default type when no specific category applies
        1 = Personal — Related to the user: their life, feelings, experiences, relationships, personal goals
        2 = Document — Summary or information extracted from a specific document provided to the LLM
        3 = Reference — General knowledge reference: internet search results, pasted content from external sources
        4 = Chat — General conversation without a specific topic or purpose
        5 = Chitchat — Casual conversation, not significant, nothing new was learned
        6 = Technical — Coding sessions, git repos, programming languages, math, science, new procedures

      RESERVED RANGE (7-99): Reserved for future built-in use

      USER-DEFINED RANGE (100+): Custom types defined by the user
        100 = Custom type index — Use keywords to define your custom type meanings
        101+ = User-defined custom types

    Returns:
        JSON string containing available memory types and their usage counts
    """
    try:
        mem = MemoryLite()
        types_list = mem.get_all_types()
        all_type_info = get_all_memory_type_info()

        return json.dumps({
            "status": "success",
            "types_in_db": types_list,
            "all_type_codes": all_type_info,
            "description": "types_in_db shows what's actually in the database; all_type_codes shows all available codes"
        }, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_get_all_keywords(pattern: str = "") -> str:
    """Get all keywords with optional pattern filter.

    Args:
        pattern: Optional text to filter keywords by title

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
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_get_all_words(pattern: str = "") -> str:
    """Extract all words from every text field in the database.

    Scans title, summary, and keywords fields.
    Returns a breakdown of which words appear in which fields.

    Args:
        pattern: Optional string to filter results - only includes words containing this substring

    Returns:
        JSON string with field-level word breakdown

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
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_get_memory_stats() -> str:
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
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_delete_memory(memory_id: str = "") -> str:
    """Delete a specific memory by ID.

    USE THIS TOOL to permanently remove a memory from the database.
    This action cannot be undone - the memory and all its data will be lost.

    Args:
        memory_id: The unique identifier of the memory to delete (e.g., "260623120000")
                   Get valid IDs from memorylite_get_all_memories() or memorylite_search()

    Returns:
        JSON string with deletion result. Status "success" if deleted, "not_found" if ID doesn't exist.
    
    EXAMPLE:
        memorylite_delete_memory(memory_id="260623120000")
    """
    try:
        # Validate memory_id parameter
        if not memory_id or not memory_id.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'memory_id' is required and cannot be empty.",
                "hint": "Get valid memory IDs from memorylite_get_all_memories() or memorylite_search()"
            }, indent=2)

        mem = MemoryLite()
        success = mem.delete_memory(memory_id)

        return json.dumps({
            "status": "success" if success else "not_found",
            "message": f"Memory {'deleted successfully' if success else 'not found with ID'} with ID '{memory_id}'",
            "deleted_id": memory_id if success else None
        }, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first using memorylite_save_memory()."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to delete memory: {str(e)}",
            "hint": "Check that memory_id is a valid format (YYMMDDhhmmss)"
        }, indent=2)


@mcp.tool
def memorylite_update_memory(
    memory_id: str = "",
    updates: Union[str, dict] = "{}"
) -> str:
    """Update an existing memory's fields.

    USE THIS TOOL to modify any existing memory. You can update ONE field or MULTIPLE fields at once.
    Only the fields you specify will be changed - all other fields remain unchanged.

    HOW TO USE:
      1. First, get the memory_id using memorylite_get_all_memories() or memorylite_search()
      2. Call this tool with the memory_id and the fields you want to change

    UPDATES FORMAT (CHOOSE ONE):
      Option A - JSON string: '{"title": "New Title", "summary": "New content"}'
      Option B - Dict: {"title": "New Title", "summary": "New content"}
      Both work the same way. JSON string is more common when LLMs call tools.

    VALID FIELDS TO UPDATE:
      - title: New short descriptive title
      - summary: New full detailed content (see save_memory for what goes here)
      - memory_type: New type code (0-6) or type name string
      - related_ids: New list of related memory IDs (replaces existing list)
      - keywords: New list of keywords (replaces existing list)

    COMPLETE EXAMPLES:

      # Update just the title
      memorylite_update_memory(
          memory_id="260623120000",
          updates='{"title": "Updated Title"}'
      )

      # Update multiple fields at once
      memorylite_update_memory(
          memory_id="260623120000",
          updates='{"title": "Better Title", "memory_type": "technical", "keywords": ["python", "new"]}'
      )

      # Update summary with corrected information
      memorylite_update_memory(
          memory_id="260623120000",
          updates='{"summary": "Corrected detailed content here..."}'
      )

    TIP: Use memorylite_append_to_summary() to ADD content without losing existing content.
         Use memorylite_update_memory() to REPLACE content entirely.

    Args:
        memory_id: The unique identifier of the memory to update (get from get_all_memories)
        updates: JSON string or dict specifying which fields to update.
                 Example: '{"title": "New Title", "summary": "New content"}'

    Returns:
        JSON string with status, message, and the actual values that were applied.
    """
    try:
        mem = MemoryLite()
        
        # Ensure memory_id is always a string (FastMCP may pass as int/str)
        if not isinstance(memory_id, str):
            memory_id = str(memory_id).strip() if memory_id else ""
        
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
                # Check if parsing returned a non-dict (e.g., a raw string)
                if not isinstance(update_dict, dict):
                    return json.dumps({
                        "status": "error",
                        "message": f"Invalid 'updates' parameter. Expected a JSON object with fields to update (e.g., '{{'title': 'New Title'}}'). Got a non-object value.",
                        "hint": "Use format: updates='{{\"title\": \"New Title\", \"summary\": \"New content\"}}'"
                    }, indent=2)
            except (json.JSONDecodeError, TypeError):
                return json.dumps({
                    "status": "error",
                    "message": f"Failed to parse 'updates' parameter as JSON.",
                    "hint": "Use format: updates='{{\"title\": \"New Title\"}}' or updates={{\"title\": \"New Title\"}}",
                    "invalid_value": updates
                }, indent=2)
        else:
            update_dict = {}

        # Validate that at least one valid field is being updated
        valid_keys = {"title", "summary", "memory_type", "related_ids", "keywords"}
        if update_dict:
            invalid_fields = [k for k in update_dict.keys() if k not in valid_keys]
            if invalid_fields:
                return json.dumps({
                    "status": "error",
                    "message": f"Invalid field(s) in 'updates': {invalid_fields}",
                    "hint": f"Valid fields are: {list(valid_keys)}",
                    "example": '{"title": "New Title", "memory_type": "technical"}'
                }, indent=2)

        # Convert string values in updates dict to proper types
        converted_updates = {}
        for k, v in (update_dict or {}).items():
            if k == "memory_type":
                # Convert to integer memory type
                converted_updates[k] = _convert_to_memory_type(v)
            elif k in ("related_ids", "keywords"):
                if isinstance(v, str):
                    try:
                        parsed = json.loads(v)
                        if isinstance(parsed, list):
                            converted_updates[k] = json.dumps(parsed)
                        else:
                            converted_updates[k] = v
                    except (json.JSONDecodeError, TypeError):
                        converted_updates[k] = v
                else:
                    converted_updates[k] = v
            else:
                converted_updates[k] = v

        success = mem.update_memory(memory_id, converted_updates or {})

        if not success:
            return json.dumps({
                "status": "not_found",
                "message": f"No memory found with ID '{memory_id}'. Use get_all_memories to list all memories."
            }, indent=2)

        # Build response with actual updated values (not just keys)
        updates_applied = {}
        if converted_updates:
            for k, v in converted_updates.items():
                try:
                    if k in ("related_ids", "keywords") and isinstance(v, str):
                        parsed_val = json.loads(v)
                        updates_applied[k] = parsed_val
                    elif k == "memory_type":
                        updates_applied[k] = v
                        updates_applied[k + "_name"] = _convert_memory_type_to_name(v)
                    else:
                        updates_applied[k] = v
                except (json.JSONDecodeError, TypeError):
                    updates_applied[k] = v

        return json.dumps({
            "status": "success" if success else "not_found",
            "message": f"Memory {'updated successfully' if success else 'not found'} with ID '{memory_id}'",
            "updates_applied": updates_applied if converted_updates else {}
        }, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def memorylite_append_to_summary(
    memory_id: str = "",
    summary_addition: str = "",
    separator: str = "\n\n---\n\n"
) -> str:
    """Append text to an existing memory's summary field.

    USE THIS TOOL to ADD new information to a memory WITHOUT losing existing content.
    Unlike update_memory (which REPLACES content), this tool APPENDS to what's already there.

    USE CASES:
      - Adding follow-up information discovered later
      - Adding meeting notes as discussion progresses
      - Building up a research document incrementally

    Args:
        memory_id: The unique identifier of the memory to append to
                   Get valid IDs from memorylite_get_all_memories() or memorylite_search()
        summary_addition: Text to append to the summary (required - cannot be empty)
        separator: Custom separator between old and new content (default: "\\n\\n---\\n\\n").
                   You can use any string, e.g., "\\n\\n## Update:\\n\\n" or " ||| "

    Returns:
        JSON string with status, original/new summary lengths, and separator used.

    EXAMPLE:
        memorylite_append_to_summary(
            memory_id="260623120000",
            summary_addition="Additional note: The meeting was rescheduled to Friday."
        )
    """
    try:
        # Validate required parameters
        if not memory_id or not memory_id.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'memory_id' is required and cannot be empty.",
                "hint": "Get valid memory IDs from memorylite_get_all_memories() or memorylite_search()"
            }, indent=2)

        if not summary_addition or not summary_addition.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'summary_addition' is required and cannot be empty.",
                "hint": "Provide the text you want to append to the memory's summary."
            }, indent=2)

        mem = MemoryLite()
        mem.ensure_db_initialized()

        result = mem.append_to_summary(memory_id, summary_addition, separator)

        return json.dumps(result, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first using memorylite_save_memory()."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to append to summary: {str(e)}"
        }, indent=2)


@mcp.tool
def memorylite_append_to_keywords(
    memory_id: str = "",
    keywords: Opt[Union[str, list]] = "[]"
) -> str:
    """Append new keywords to an existing memory's keywords list.

    USE THIS TOOL to add semantic keywords for searching WITHOUT losing existing keywords.
    Duplicates are automatically filtered out.

    Args:
        memory_id: The unique identifier of the memory to update
                   Get valid IDs from memorylite_get_all_memories() or memorylite_search()
        keywords: JSON array string or list of keyword strings to add
                  Example: '["python", "asyncio"]' or ["python", "asyncio"]

    Returns:
        JSON string with status, added keywords, counts, and duplicates removed.

    EXAMPLE:
        memorylite_append_to_keywords(
            memory_id="260623120000",
            keywords='["machine-learning", "neural-networks"]'
        )
    """
    try:
        # Validate required parameters
        if not memory_id or not memory_id.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'memory_id' is required and cannot be empty.",
                "hint": "Get valid memory IDs from memorylite_get_all_memories() or memorylite_search()"
            }, indent=2)

        # Handle both string and list inputs
        if isinstance(keywords, str):
            keywords_list = _parse_json_list_or_fix(keywords) if keywords else []
        else:
            keywords_list = keywords if keywords else []

        # Validate that at least one keyword is provided
        if not keywords_list or (isinstance(keywords_list, list) and len(keywords_list) == 0):
            return json.dumps({
                "status": "error",
                "message": "Parameter 'keywords' is required and cannot be empty.",
                "hint": "Provide a list of keywords: keywords='[\"python\", \"asyncio\"]'"
            }, indent=2)

        mem = MemoryLite()
        mem.ensure_db_initialized()

        result = mem.append_to_keywords(memory_id, keywords_list)

        return json.dumps(result, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first using memorylite_save_memory()."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to append keywords: {str(e)}"
        }, indent=2)


@mcp.tool
def memorylite_append_to_related_ids(
    memory_id: str = "",
    related_ids: Opt[Union[str, list]] = "[]"
) -> str:
    """Append new related IDs to an existing memory's related_ids list.

    USE THIS TOOL to link this memory to other memories WITHOUT losing existing links.
    Duplicates are automatically filtered out.

    Args:
        memory_id: The unique identifier of the memory to update
                   Get valid IDs from memorylite_get_all_memories() or memorylite_search()
        related_ids: JSON array string or list of memory ID strings to add
                     Example: '["260623110000", "260623115000"]' or ["260623110000", "260623115000"]

    Returns:
        JSON string with status, added IDs, counts, and duplicates removed.

    EXAMPLE:
        memorylite_append_to_related_ids(
            memory_id="260623120000",
            related_ids='["260623110000"]'
        )
    """
    try:
        # Validate required parameters
        if not memory_id or not memory_id.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'memory_id' is required and cannot be empty.",
                "hint": "Get valid memory IDs from memorylite_get_all_memories() or memorylite_search()"
            }, indent=2)

        # Handle both string and list inputs
        if isinstance(related_ids, str):
            related_ids_list = _parse_json_list_or_fix(related_ids) if related_ids else []
        else:
            related_ids_list = related_ids if related_ids else []

        # Validate that at least one related ID is provided
        if not related_ids_list or (isinstance(related_ids_list, list) and len(related_ids_list) == 0):
            return json.dumps({
                "status": "error",
                "message": "Parameter 'related_ids' is required and cannot be empty.",
                "hint": "Provide a list of memory IDs: related_ids='[\"260623110000\"]'"
            }, indent=2)

        mem = MemoryLite()
        mem.ensure_db_initialized()

        result = mem.append_to_related_ids(memory_id, related_ids_list)

        return json.dumps(result, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first using memorylite_save_memory()."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to append related IDs: {str(e)}"
        }, indent=2)


@mcp.tool
def memorylite_select_memory(
    memory_id: str = "",
    pattern: str = "",
    mode: str = "exact",
    start_line: int = None,
    end_line: int = None
) -> str:
    """Select/search text within a memory's summary field.

    THIS IS THE FIRST STEP IN THE EDIT WORKFLOW. You must call select_memory BEFORE 
    calling edit_selection.

    COMPLETE EDIT WORKFLOW (SELECT -> EDIT):
      Step 1: Call select_memory to find text and get a selection_id
      Step 2: Call edit_selection with that selection_id to make changes
      Note: After editing, the selection_id is used up. Call select_memory again for more edits.

    SEARCH MODES:
      Mode 1 - "exact" (most common): Find exact text matches
        Example: select_memory(memory_id="abc123", pattern="old_name", mode="exact")
        Finds every occurrence of "old_name" in the memory's summary.

      Mode 2 - "regex": Find text using regular expressions
        Example: select_memory(memory_id="abc123", pattern=r"\\d{{4}}-\\d{{2}}-\\d{{2}}", mode="regex")
        Finds all dates in YYYY-MM-DD format.

      Mode 3 - "lines": Select specific line ranges
        Example: select_memory(memory_id="abc123", mode="lines", start_line=5, end_line=10)
        Selects lines 5 through 10 (1-based line numbers).

    WHAT YOU GET BACK:
      - selection_id: A unique ID you MUST use in the next step (edit_selection)
      - occurrences: How many matches were found
      - matched_text: A preview of the matched text (truncated if very long)

    EXAMPLE WORKFLOW - Find and replace a name:
      # Step 1: Find all occurrences of "John" in the memory
      select_memory(memory_id="260623120000", pattern="John", mode="exact")
      # Returns: {"selection_id": "sel_260623120000_1", "occurrences": 3, ...}
      
      # Step 2: Replace all 3 occurrences with "Jane"
      edit_selection(selection_id="sel_260623120000_1", replacement="Jane", occurrence=0)
      # occurrence=0 means replace ALL occurrences
      # occurrence=1 means replace only the FIRST occurrence
      # occurrence=2 means replace only the SECOND occurrence

    EXAMPLE WORKFLOW - Find and replace just the first occurrence:
      # Step 1: Find occurrences
      select_memory(memory_id="260623120000", pattern="TODO", mode="exact")
      # Returns: {"selection_id": "sel_260623120000_1", "occurrences": 5, ...}
      
      # Step 2: Replace only the first TODO
      edit_selection(selection_id="sel_260623120000_1", replacement="DONE", occurrence=1)

    Args:
        memory_id: The unique identifier of the memory to search within
        pattern: Text to search for (required for exact/regex modes)
        mode: Search mode - "exact" (default), "regex", or "lines"
        start_line: Start line number (for "lines" mode only, 1-based)
        end_line: End line number (for "lines" mode only, 1-based, inclusive)

    Returns:
        JSON with status, selection_id (required for edit_selection), occurrences count, 
        and matched_text preview.
    """
    try:
        # Validate required parameters
        if not memory_id or not memory_id.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'memory_id' is required and cannot be empty.",
                "hint": "Get valid memory IDs from memorylite_get_all_memories() or memorylite_search()"
            }, indent=2)

        # Validate mode parameter
        valid_modes = ["exact", "regex", "lines"]
        if mode not in valid_modes:
            return json.dumps({
                "status": "error",
                "message": f"Invalid mode '{mode}'. Must be one of: {valid_modes}",
                "hint": "Use mode='exact' for text matching, mode='regex' for regex patterns, or mode='lines' for line ranges"
            }, indent=2)

        # Validate lines mode requires start_line and end_line
        if mode == "lines":
            if start_line is None or end_line is None:
                return json.dumps({
                    "status": "error",
                    "message": "Lines mode requires both 'start_line' and 'end_line' parameters.",
                    "hint": "Example: start_line=1, end_line=10"
                }, indent=2)
            if start_line < 1 or end_line < start_line:
                return json.dumps({
                    "status": "error",
                    "message": f"Invalid line range: start_line={start_line}, end_line={end_line}.",
                    "hint": "start_line must be >= 1 and end_line must be >= start_line"
                }, indent=2)
        else:
            # exact/regex modes require a pattern
            if not pattern or not pattern.strip():
                return json.dumps({
                    "status": "error",
                    "message": f"Parameter 'pattern' is required for mode='{mode}'.",
                    "hint": "Provide the text or regex pattern to search for"
                }, indent=2)

        mem = MemoryLite()
        mem.ensure_db_initialized()

        result = mem.select_memory(
            memory_id=memory_id,
            pattern=pattern if pattern else None,
            mode=mode,
            start_line=start_line,
            end_line=end_line
        )

        return json.dumps(result, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first using memorylite_save_memory()."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to select memory: {str(e)}"
        }, indent=2)


@mcp.tool
def memorylite_edit_selection(
    selection_id: str = "",
    replacement: str = "",
    occurrence: int = 1
) -> str:
    """Replace selected text with new text.

    THIS IS THE SECOND STEP IN THE EDIT WORKFLOW. You MUST call select_memory FIRST
    to get a selection_id before using this tool.

    COMPLETE EDIT WORKFLOW (SELECT -> EDIT):
      Step 1: select_memory(...) -> gets selection_id
      Step 2: edit_selection(selection_id=selection_id, replacement="new text", occurrence=N)
      Note: After editing, the selection_id is used up. You must call select_memory again 
            for any additional edits.

    OCCURRENCE PARAMETER (which matches to replace):
      occurrence=0  -> Replace ALL occurrences (every match found by select_memory)
      occurrence=1  -> Replace only the FIRST occurrence (default)
      occurrence=2  -> Replace only the SECOND occurrence
      occurrence=3  -> Replace only the THIRD occurrence
      ...and so on

    COMPLETE EXAMPLE - Replace all occurrences:
      # Step 1: Find all "old_name" in the memory
      select_memory(memory_id="260623120000", pattern="old_name", mode="exact")
      # Returns: {"selection_id": "sel_260623120000_1", "occurrences": 3}
      
      # Step 2: Replace ALL 3 occurrences with "new_name"
      edit_selection(selection_id="sel_260623120000_1", replacement="new_name", occurrence=0)

    COMPLETE EXAMPLE - Replace only specific occurrences:
      # Step 1: Find all "TODO" in the memory
      select_memory(memory_id="260623120000", pattern="TODO", mode="exact")
      # Returns: {"selection_id": "sel_260623120000_1", "occurrences": 5}
      
      # Step 2: Replace only the FIRST TODO with "DONE"
      edit_selection(selection_id="sel_260623120000_1", replacement="DONE", occurrence=1)
      
      # Step 3: For more edits, call select_memory again!
      select_memory(memory_id="260623120000", pattern="TODO", mode="exact")
      # ... then edit_selection again with the new selection_id

    IMPORTANT NOTES:
      - The selection_id expires after editing (is "nullified")
      - You cannot reuse a selection_id - always call select_memory first for new selections
      - The replacement text completely replaces the matched text

    Args:
        selection_id: The selection ID returned from select_memory (required!)
        replacement: The text to replace selected content with
        occurrence: Which occurrence to edit: 0=ALL, 1=first, 2=second, etc.

    Returns:
        JSON with status, number of changes made, and details of what was edited.
        Also includes a notice that the selection_id has been nullified.
    """
    try:
        # Validate required parameters
        if not selection_id or not selection_id.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'selection_id' is required and cannot be empty.",
                "hint": "Get a selection_id from memorylite_select_memory() first. Selection IDs expire after use."
            }, indent=2)

        if not replacement or not replacement.strip():
            return json.dumps({
                "status": "error",
                "message": "Parameter 'replacement' is required and cannot be empty.",
                "hint": "Provide the text to replace the selected content with"
            }, indent=2)

        # Validate occurrence parameter
        if not isinstance(occurrence, int) or occurrence < 0:
            return json.dumps({
                "status": "error",
                "message": f"Invalid occurrence value: {occurrence}. Must be a non-negative integer.",
                "hint": "Use occurrence=0 for ALL matches, occurrence=1 for first, occurrence=2 for second, etc."
            }, indent=2)

        mem = MemoryLite()
        mem.ensure_db_initialized()

        result = mem.edit_selection(selection_id, replacement, occurrence)

        return json.dumps(result, indent=2)
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            return json.dumps({
                "status": "error",
                "message": "Database not yet initiated. Save a memory first using memorylite_save_memory()."
            }, indent=2)
        return json.dumps({"status": "error", "message": str(e)})
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Failed to edit selection: {str(e)}"
        }, indent=2)


# ============================================================================
# CLI Argument Parsing and Main Entry Point
# ============================================================================


def _parse_args():
    """Parse command-line arguments for database path configuration."""
    parser = argparse.ArgumentParser(
        description="memorylite - Lightweight LLM Memory Database System (SQLite Backend)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python memorylite.py --path /home/user/memory/     # Use directory as base for memory.db
  python memorylite.py --path /home/user/mydb.db      # Use full file path directly
  python memorylite.py                                # Use default ~/.swordmemory/memory.db

When the path ends with a slash (e.g., /some/dir/), it is treated as a directory
and 'memory.db' will be appended to form the database file path.
        """
    )
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Database path: if ends with '/' treats as directory (appends memory.db), "
             "otherwise uses as full file path"
    )
    return parser.parse_args()


def _setup_db_from_cli():
    """Set up the database path from CLI arguments."""
    args = _parse_args()
    if args.path:
        MemoryLite.DB_FILE = args.path


# ============================================================================
# Main entry point
# ============================================================================

if __name__ == "__main__":
    # Set up DB path from CLI arguments before any other operations
    _setup_db_from_cli()

    print("memorylite - LLM Memory Database System (SQLite Backend)")
    print("=" * 50)
    print("This module provides memory management via FastMCP tools.")
    print("Available tools:")
    print("  - memorylite_save_memory: Save a new memory item")
    print("  - memorylite_get_memory_by_id: Retrieve specific memory by ID")
    print("  - memorylite_get_memories_by_ids: Retrieve multiple memories by IDs")
    print("  - memorylite_search: Search memories across all fields in single query")
    print("  - memorylite_get_all_memories: Get all stored memories")
    print("  - memorylite_get_all_types: Show available type codes with counts")
    print("  - memorylite_get_all_keywords: List all keywords and titles")
    print("  - memorylite_get_all_words: Extract all words from database with field-level breakdown (EXPENSIVE)")
    print("  - memorylite_get_memory_stats: View memory statistics")
    print("  - memorylite_delete_memory: Delete a memory item")
    print("  - memorylite_update_memory: Update an existing memory (replaces fields)")
    print("  - memorylite_append_to_summary: Append text to a memory's summary (with configurable separator)")
    print("  - memorylite_append_to_keywords: Add keywords without losing existing ones")
    print("  - memorylite_append_to_related_ids: Add related IDs without losing existing links")
    print("  - memorylite_select_memory: Select/search text within a memory's summary (exact, regex, or lines mode)")
    print("  - memorylite_edit_selection: Edit previously selected text (use selection_id from select_memory)")
    print()
    print("Memory Type Codes:")
    print("  RESERVED TYPES (0-6):")
    for code, (name, desc) in sorted(MEMORY_TYPE_MAP.items()):
        print(f"    {code} = {name} — {desc}")
    print(f"  RESERVED RANGE: {MEMORY_TYPE_RESERVED_START}-{MEMORY_TYPE_RESERVED_END} — Reserved for future use")
    print(f"  USER-DEFINED RANGE: {MEMORY_TYPE_USER_DEFINED_START}+ — Custom types (define meanings via type {MEMORY_TYPE_USER_DEFINED_START} memories)")
    print()
    print("Details Levels:")
    print("  0 = minimal (title + keywords only)")
    print("  1 = excludes summary (default)")
    print("  2 = full (includes summary)")

    # Run the MCP server
    mcp.run()