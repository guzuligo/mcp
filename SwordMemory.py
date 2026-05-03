"""
SwordMemory - LLM Memory Management System (FastMCP Tool)

A FastMCP-based tool to help LLM manage memory using JSON files stored in the user's home
Documents folder under .swordmemory directory.

Memory types:
  - global: Static facts and chit-chat friendly data
  - experience: Subject-related experiences  
  - reference: Document-related knowledge

Each memory type has its own subdirectory containing indexed JSON files.
"""

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastmcp import FastMCP

mcp = FastMCP("SwordMemory")


class SwordMemory:
    """LLM Memory Management System using JSON file storage."""

    # Configuration - can be modified at the top level
    BASE_DIR = None  # Will default to ~/Documents/.swordmemory if not set

    def __init__(self, base_dir=None):
        """Initialize SwordMemory with optional custom base directory.

        Args:
            base_dir: Custom base directory path. If None, uses ~/Documents/.swordmemory
        """
        if base_dir is not None:
            self.base_path = Path(base_dir)
        elif self.BASE_DIR is not None:
            self.base_path = Path(self.BASE_DIR)
        else:
            home_docs = Path.home().joinpath("Documents")
            self.base_path = home_docs.joinpath(".swordmemory")

        # Ensure the base directory exists
        self.base_path.mkdir(parents=True, exist_ok=True)

        # Create required subdirectories if they don't exist
        for subdir in ["global", "experience", "reference"]:
            (self.base_path / subdir).mkdir(parents=True, exist_ok=True)

    def _get_librarian_path(self, memory_type: str) -> Path:
        """Get the path to the librarian.json file for a given memory type."""
        return self.base_path / memory_type / "librarian.json"

    def _load_librarian(self, memory_type: str) -> dict:
        """Load the librarian index for a given memory type."""
        librarian_path = self._get_librarian_path(memory_type)
        if not librarian_path.exists():
            return {"files": {}, "last_updated": None}
        with open(librarian_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_librarian(self, memory_type: str, data: dict) -> None:
        """Save the librarian index for a given memory type."""
        librarian_path = self._get_librarian_path(memory_type)
        with open(librarian_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

    def _generate_timestamp(self) -> str:
        """Generate a timestamp string for file naming."""
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def save_memory(
        self,
        keyword: str,
        title: str,
        summary: str,
        related_files: list = None,
        important_keywords: list = None,
        memory_type: str = "global"
    ) -> dict:
        """Save a memory item to the appropriate directory."""
        valid_types = ["global", "experience", "reference"]
        if memory_type not in valid_types:
            raise ValueError(f"Invalid memory type '{memory_type}'. Must be one of {valid_types}")

        timestamp = self._generate_timestamp()
        filename = f"{keyword}_{timestamp}.json"

        memory_record = {
            "keyword": keyword,
            "title": title,
            "summary": summary,
            "related_memory_files": related_files or [],
            "important_keywords_related": important_keywords or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "memory_type": memory_type,
        }

        file_path = self.base_path / memory_type / filename
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(memory_record, f, indent=2)

        librarian = self._load_librarian(memory_type)
        if "files" not in librarian:
            librarian["files"] = {}
        librarian["files"][filename] = {
            "keyword": keyword,
            "title": title,
            "timestamp": timestamp,
            "created_at": memory_record["created_at"],
        }
        librarian["last_updated"] = datetime.now(timezone.utc).isoformat()

        self._save_librarian(memory_type, librarian)

        return {"created_file": filename, **memory_record}

    def librarian_get_files(self, memory_type: Optional[str] = None) -> list:
        """Get all memory file names with optional type filter."""
        all_files = []
        valid_types = ["global", "experience", "reference"]

        if memory_type is not None:
            types_to_search = [memory_type]
        else:
            types_to_search = valid_types

        for mtype in types_to_search:
            librarian = self._load_librarian(mtype)
            if "files" in librarian and isinstance(librarian["files"], dict):
                for filename, metadata in librarian["files"].items():
                    entry = {"filename": filename, "memory_type": mtype}
                    if isinstance(metadata, dict):
                        entry.update(metadata)
                    all_files.append(entry)

        return all_files

    def get_file(
        self,
        filename: str,
        version: int = 0,
        include_fields=None
    ) -> dict:
        """Retrieve a specific memory file and its keywords."""
        valid_types = ["global", "experience", "reference"]
        result = None

        for mtype in valid_types:
            librarian = self._load_librarian(mtype)
            if filename in (librarian.get("files") or {}):
                file_path = self.base_path / mtype / filename
                if not file_path.exists():
                    return {}
                with open(file_path, "r", encoding="utf-8") as f:
                    result = json.load(f)
                result["memory_type"] = mtype
                break

        if result is None or not isinstance(result, dict):
            return {}

        if include_fields:
            filtered_result = {
                "keyword": result.get("keyword"),
                "important_keywords_related": result.get("important_keywords_related")
            }
            for field in include_fields:
                if field in result:
                    filtered_result[field] = result[field]
            return filtered_result

        result["all_keywords"] = [result.get("keyword", "")] + (result.get("important_keywords_related") or [])
        return result

    def get_summary(self, keyword: str, filename=None) -> dict:
        """Retrieve the summary for a specific memory item."""
        valid_types = ["global", "experience", "reference"]

        for mtype in valid_types:
            librarian = self._load_librarian(mtype)
            if filename is not None:
                file_path = self.base_path / mtype / filename
                if file_path.exists():
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return {**data, "memory_type": mtype}
            else:
                for fname, meta in (librarian.get("files") or {}).items():
                    if isinstance(meta, dict) and meta.get("keyword") == keyword:
                        file_path = self.base_path / mtype / fname
                        if file_path.exists():
                            with open(file_path, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            return {**data, "memory_type": mtype}

        return {}

    def search(self, pattern=None, memory_type=None) -> list:
        """Search memories by regex pattern or retrieve all if no pattern specified."""
        valid_types = ["global", "experience", "reference"]
        results = []

        if memory_type is not None and memory_type not in valid_types:
            raise ValueError(f"Invalid memory type '{memory_type}'. Must be one of {valid_types}")

        types_to_search = [memory_type] if memory_type else valid_types

        for mtype in types_to_search:
            librarian = self._load_librarian(mtype)
            files_index = librarian.get("files", {})

            for filename, metadata in files_index.items():
                file_path = self.base_path / mtype / filename
                if not file_path.exists():
                    continue

                with open(file_path, "r", encoding="utf-8") as f:
                    memory_data = json.load(f)

                if pattern is None or not pattern.strip():
                    results.append({**memory_data, "matched_file": filename, "memory_type": mtype})
                else:
                    try:
                        compiled_pattern = re.compile(pattern)
                        searchable_text = f"{metadata.get('title', '')} {memory_data.get('summary', '')} {' '.join(memory_data.get('important_keywords_related', []))} {' '.join(memory_data.get('related_memory_files', []))}"
                        if compiled_pattern.search(searchable_text):
                            results.append({
                                **memory_data,
                                "matched_file": filename,
                                "memory_type": mtype,
                                "match_details": {
                                    "title_match": bool(compiled_pattern.search(metadata.get("title", ""))),
                                    "summary_match": bool(compiled_pattern.search(memory_data.get("summary", ""))),
                                    "keywords_match": bool(compiled_pattern.search(" ".join(memory_data.get("important_keywords_related", []))))
                                }
                            })
                    except re.error:
                        continue

        return results

    def get_all_memory_types(self) -> list:
        """Get a list of all memory types that have been used."""
        valid_types = ["global", "experience", "reference"]
        return [t for t in valid_types if self._load_librarian(t).get("files")]

    def get_memory_stats(self) -> dict:
        """Get statistics about the memory system."""
        stats = {}
        for mtype in ["global", "experience", "reference"]:
            librarian = self._load_librarian(mtype)
            files = librarian.get("files") or {}
            stats[mtype] = {
                "file_count": len(files),
                "last_updated": librarian.get("last_updated"),
                "files": list(files.keys()) if isinstance(files, dict) else [],
            }
        return stats

    def delete_memory(self, filename: str, memory_type=None) -> bool:
        """Delete a specific memory file."""
        valid_types = ["global", "experience", "reference"]
        types_to_search = [memory_type] if memory_type else valid_types

        for mtype in types_to_search:
            file_path = self.base_path / mtype / filename
            if file_path.exists():
                os.remove(str(file_path))
                librarian = self._load_librarian(mtype)
                if "files" in librarian and filename in librarian["files"]:
                    del librarian["files"][filename]
                    librarian["last_updated"] = datetime.now(timezone.utc).isoformat()
                    self._save_librarian(mtype, librarian)
                return True
        return False

    def update_memory(self, filename: str, updates: dict, memory_type=None) -> bool:
        """Update an existing memory file."""
        valid_types = ["global", "experience", "reference"]
        types_to_search = [memory_type] if memory_type else valid_types

        for mtype in types_to_search:
            file_path = self.base_path / mtype / filename
            if not file_path.exists():
                continue

            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for key, value in updates.items():
                if key in ("keyword", "title", "summary", "related_memory_files", "important_keywords_related"):
                    data[key] = value

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            return True
        return False


@mcp.tool
def save_memory(
    keyword: str = "",
    title: str = "",
    summary: str = "",
    related_files: str = "[]",
    important_keywords: str = "[]",
    memory_type: str = "global"
) -> str:
    """Save a memory item to the appropriate directory.

    Args:
        keyword: Unique identifier/ID for this memory item (acts as title)
        title: Short descriptive title for quick scanning
        summary: Detailed description of the experience/knowledge
        related_files: List of files that help understand this memory more (JSON array string)
        important_keywords: List of keywords for lookup/searching (JSON array string)
        memory_type: Type of memory - 'global', 'experience', or 'reference'

    Returns:
        JSON string with the saved memory data and timestamp
    """
    try:
        mem = SwordMemory()
        
        # Parse JSON arrays from strings
        try:
            related_files_list = json.loads(related_files) if isinstance(related_files, str) else related_files
            important_keywords_list = json.loads(important_keywords) if isinstance(important_keywords, str) else important_keywords
        except (json.JSONDecodeError, TypeError):
            related_files_list = []
            important_keywords_list = []

        result = mem.save_memory(
            keyword=keyword,
            title=title,
            summary=summary,
            related_files=related_files_list or [],
            important_keywords=important_keywords_list or [],
            memory_type=memory_type
        )
        
        return json.dumps({
            "status": "success",
            "message": f"Memory saved with keyword '{keyword}' to {memory_type} directory",
            "filename": result.get("created_file"),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def librarian_get_files(memory_type: Optional[str] = None) -> str:
    """Get all memory file names with optional type filter.

    Args:
        memory_type: Filter by 'global', 'experience', or 'reference'.
                     If None, returns all files from all types.

    Returns:
        JSON string containing list of memory files and their metadata
    """
    try:
        mem = SwordMemory()
        files = mem.librarian_get_files(memory_type)
        
        if not files:
            return json.dumps({
                "status": "success",
                "message": "No memory files found",
                "files": []
            }, indent=2)
            
        return json.dumps({
            "status": "success",
            "total_files": len(files),
            "memory_type_filter": memory_type or "all",
            "files": files
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_file(
    filename: str = "",
    version: int = 0,
    include_fields: str = ""
) -> str:
    """Retrieve a specific memory file and its keywords.

    Args:
        filename: The name of the memory file to retrieve (e.g., 'keyword_timestamp.json')
        version: Which version to retrieve - 0 for latest, higher numbers for older versions
        include_fields: Comma-separated list of fields to include (title,related_memory_files,important_keywords_related)

    Returns:
        JSON string containing the memory data and keywords
    """
    try:
        mem = SwordMemory()
        
        # Parse include_fields from comma-separated string
        fields_list = [f.strip() for f in include_fields.split(",") if f.strip()] if include_fields else None
        
        result = mem.get_file(
            filename=filename,
            version=version,
            include_fields=fields_list
        )
        
        return json.dumps({
            "status": "success" if result else "not_found",
            "message": f"File '{filename}' retrieved successfully" if result else f"No file found matching '{filename}'",
            "data": result or {}
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_summary(keyword: str = "", filename: Optional[str] = None) -> str:
    """Retrieve the summary for a specific memory item.

    Args:
        keyword: The unique identifier/ID of the memory item to retrieve
        filename: Optional - if provided, loads from this specific file instead of searching by keyword

    Returns:
        JSON string containing the full summary and metadata
    """
    try:
        mem = SwordMemory()
        
        result = mem.get_summary(keyword=keyword, filename=filename)
        
        return json.dumps({
            "status": "success" if result else "not_found",
            "message": f"Summary for keyword '{keyword}' retrieved successfully" if result else f"No memory found with keyword '{keyword}'",
            "data": result or {}
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def search(
    pattern: Optional[str] = None,
    memory_type: Optional[str] = None
) -> str:
    """Search memories by regex pattern or retrieve all if no pattern specified.

    Args:
        pattern: Regex pattern to match against keywords, titles, summaries, and related files.
                 If None/empty, returns all matching memories (filtered by memory_type).
        memory_type: Filter results by 'global', 'experience', or 'reference'.
                     If None/empty, searches across all types.

    Returns:
        JSON string containing list of matched memory data
    """
    try:
        mem = SwordMemory()
        
        # Handle empty/null pattern as "all"
        search_pattern = pattern if pattern and pattern.strip() else None
        
        results = mem.search(
            pattern=search_pattern,
            memory_type=memory_type
        )
        
        return json.dumps({
            "status": "success",
            "pattern_used": pattern or "none (all memories)",
            "memory_type_filter": memory_type or "all",
            "total_matches": len(results),
            "results": results
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def get_memory_stats() -> str:
    """Get statistics about the memory system.

    Returns:
        JSON string containing counts and details for each memory type
    """
    try:
        mem = SwordMemory()
        stats = mem.get_memory_stats()
        
        return json.dumps({
            "status": "success",
            "statistics": stats
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def delete_memory(filename: str, memory_type: Optional[str] = None) -> str:
    """Delete a specific memory file.

    Args:
        filename: The name of the memory file to delete
        memory_type: Which type directory to look in. If None/empty, searches all types.

    Returns:
        JSON string with deletion result
    """
    try:
        mem = SwordMemory()
        success = mem.delete_memory(
            filename=filename,
            memory_type=memory_type if memory_type and memory_type.strip() else None
        )
        
        return json.dumps({
            "status": "success" if success else "not_found",
            "message": f"File '{filename}' deleted successfully" if success else f"No file found matching '{filename}'",
            "memory_type_filter": memory_type or "all types searched"
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def update_memory(
    filename: str,
    updates: str = "",
    memory_type: Optional[str] = None
) -> str:
    """Update an existing memory file.

    Args:
        filename: The name of the memory file to update
        updates: JSON string or "key=value" format for fields to update
                 (e.g., '{"title": "New Title"}' or 'title=New Title')
        memory_type: Which type directory to look in. If None/empty, searches all types.

    Returns:
        JSON string with update result
    """
    try:
        mem = SwordMemory()
        
        # Parse updates from various formats
        if isinstance(updates, str) and "=" in updates and not updates.startswith("{"):
            # Handle "key=value" format
            updates_dict = {}
            for pair in updates.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    updates_dict[k.strip()] = v.strip()
        else:
            try:
                updates_dict = json.loads(updates) if isinstance(updates, str) else updates
            except (json.JSONDecodeError, TypeError):
                updates_dict = {}
        
        success = mem.update_memory(
            filename=filename,
            updates=updates_dict or {},
            memory_type=memory_type if memory_type and memory_type.strip() else None
        )
        
        return json.dumps({
            "status": "success" if success else "not_found",
            "message": f"File '{filename}' updated successfully" if success else f"No file found matching '{filename}'",
            "updates_applied": list(updates_dict.keys()) if updates_dict else []
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def search_by_keyword(keyword: str, memory_type: Optional[str] = None) -> str:
    """Search for a specific keyword across all memories.

    Args:
        keyword: The exact keyword/ID to search for
        memory_type: Filter by 'global', 'experience', or 'reference'. If None, searches all types.

    Returns:
        JSON string containing matched memory data
    """
    try:
        mem = SwordMemory()
        
        # Search across all files in the specified type(s)
        results = []
        valid_types = ["global", "experience", "reference"]
        types_to_search = [memory_type] if memory_type and memory_type.strip() else valid_types
        
        for mtype in types_to_search:
            librarian = mem._load_librarian(mtype)
            files_index = librarian.get("files", {})
            
            for fname, meta in files_index.items():
                if isinstance(meta, dict) and meta.get("keyword") == keyword:
                    file_path = mem.base_path / mtype / fname
                    if file_path.exists():
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        results.append({**data, "matched_file": fname, "memory_type": mtype})
        
        return json.dumps({
            "status": "success",
            "keyword_searched": keyword,
            "total_matches": len(results),
            "results": results
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool
def list_all_memory_types() -> str:
    """Get a list of all memory types that have been used.

    Returns:
        JSON string containing available memory types with file counts
    """
    try:
        mem = SwordMemory()
        types = mem.get_all_memory_types()
        
        return json.dumps({
            "status": "success",
            "available_types": types,
            "description": "These are the memory categories that contain stored memories"
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# Convenience function for direct usage without instantiation
def create_memory(base_dir=None):
    """Create and return a new SwordMemory instance.

    Args:
        base_dir: Custom base directory path. If None, uses ~/Documents/.swordmemory

    Returns:
        New SwordMemory instance
    """
    return SwordMemory(base_dir)


@mcp.tool()
def evaluate(expression: str) -> float:
    """Evaluate any mathematical expression.

    Supports basic operations (+, -, *, /, **) and common math functions
    (sin, cos, tan, sqrt, log, log10, pow, abs, pi, e).

    Args:
        expression: A mathematical expression string, e.g. "2 + 3 * 4", "sqrt(16)", "sin(pi/2)"

    Returns:
        The numeric result of the expression
    """
    safe_namespace = {
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "sqrt": math.sqrt,
        "log": math.log,
        "log10": math.log10,
        "pow": pow,
        "abs": abs,
        "pi": math.pi,
        "e": math.e,
        "floor": math.floor,
        "ceil": math.ceil,
        "factorial": math.factorial,
    }

    try:
        result = eval(expression, {"__builtins__": {}}, safe_namespace)
        return float(result)
    except Exception as e:
        raise ValueError(f"Failed to evaluate expression: {e}")


# Example usage (uncomment to run directly):
if __name__ == "__main__" or True:  # Always runs for MCP server mode
    print("SwordMemory - FastMCP Memory Management Tool")
    print("=" * 50)
    print("This module provides memory management via FastMCP tools.")
    print("Available tools:")
    print("  - save_memory: Save a new memory item")
    print("  - librarian_get_files: List all memory files")
    print("  - get_file: Retrieve specific memory file")
    print("  - get_summary: Get summary for a keyword")
    print("  - search: Search memories by regex pattern")
    print("  - get_memory_stats: View memory statistics")
    print("  - delete_memory: Delete a memory file")
    print("  - update_memory: Update an existing memory")
    print("  - search_by_keyword: Search by exact keyword match")
    print("  - list_all_memory_types: Show available memory types")
    
    # Run the MCP server
    mcp.run()


# For running as standalone script with example data
if __name__ == "__main__":
    # Create a memory system instance for direct testing
    mem = create_memory()

    print("\n=== Saving sample memories ===\n")

    # Global memory - static facts about the user
    result = save_memory(
        keyword="user_name",
        title="User's Name",
        summary="The user's name is John Doe.",
        related_files='["about_user.txt"]',
        important_keywords='["name", "identity", "personal"]',
        memory_type="global"
    )
    print(result)

    # Experience memory - learned from interaction
    result = save_memory(
        keyword="debugging_tip",
        title="Debugging Tip: Check Logs First",
        summary="When debugging, always check the application logs before making changes. This saves time and prevents unnecessary modifications.",
        related_files='["debug_guide.md"]',
        important_keywords='["debugging", "logs", "best-practice"]',
        memory_type="experience"
    )
    print(result)

    # Reference memory - from a document
    result = save_memory(
        keyword="api_docs_v1",
        title="API Documentation v1",
        summary="The REST API endpoints are documented in the official docs. Key endpoints include /users, /data, and /status.",
        related_files='["api_reference.md"]',
        important_keywords='["API", "REST", "endpoints", "documentation"]',
        memory_type="reference"
    )
    print(result)

    print("\n=== All memories ===")
    all_memories = librarian_get_files()
    print(all_memories)

    print("\n=== Search by pattern ===")
    results = search(pattern="debug|logs", memory_type=None)
    print(results)

    print("\n=== Memory Statistics ===")
    stats = get_memory_stats()
    print(stats)