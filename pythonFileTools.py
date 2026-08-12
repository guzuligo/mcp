"""
Python File Tools MCP Server
Version: 0.0.0 Experimental

A FastMCP-based server providing file manipulation tools for LLMs:
- list_folder: List directory contents with optional recursive mode
- search_file_content: Search files using regex patterns with configurable context lines and file filters
- read_file_content: Read file content with optional line range support (1-based indexing)
- edit_file: Apply text/regex/whitespace-tolerant/line-range edits to files, with git commit tracking for each change
- undo_edit: Revert file changes by checking out previous git commits
- preview_undo: Preview what changes would be made by an undo without applying them
- create_file: Create new files or overwrite existing ones, committed to git
- execute_command: Run bash commands and capture stdout/stderr with timeout support
- git_init: Initialize a git repository in a directory for edit tracking

All edits are committed to git for undo capability.
"""

import difflib
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Home directory reference (used for tilde expansion in path validation)
HOME_DIR = Path.home().resolve()

from fastmcp import FastMCP

# Create a FastMCP server instance
mcp = FastMCP("Python File Tools")

# =============================================================================
# Configurable defaults (can be overridden via environment variables)
# =============================================================================

# Default number of lines to return from get_command_output when tail > 0.
# Override via environment variable: export MCP_DEFAULT_TAIL_LINES=30
_DEFAULT_TAIL_LINES = int(os.environ.get("MCP_DEFAULT_TAIL_LINES", "20"))


@mcp.tool()
def list_folder(path: str, recursive: bool = False) -> dict:
    """List the contents of a folder.

    Args:
        path: The directory path to list
        recursive: If True, list files recursively. Default: False

    Returns:
        A dictionary with directory info and list of files/folders
    """
    dir_path = Path(path)

    if not dir_path.exists():
        raise FileNotFoundError(f"Directory not found: {path}")

    if not dir_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    entries = []
    if recursive:
        for item in dir_path.rglob("*"):
            entries.append({
                "name": item.name,
                "path": str(item.relative_to(dir_path)),
                "type": "directory" if item.is_dir() else "file",
            })
    else:
        for item in dir_path.iterdir():
            entries.append({
                "name": item.name,
                "path": item.name,
                "type": "directory" if item.is_dir() else "file",
            })

    return {
        "path": str(dir_path.resolve()),
        "recursive": recursive,
        "total_count": len(entries),
        "entries": entries,
    }


@mcp.tool()
def search_file_content(
    pattern: str,
    path: str = ".",
    file_pattern: str = "*",
    max_results: int = 50,
    context_before: int = 0,
    context_after: int = 0,
) -> dict:
    """Search for a regex pattern in file contents.

    The search uses regular expressions, which makes it very powerful and flexible. Here are some tips for effective searching:

    TIP 1 - Use multiple keywords with alternation (|): When you're looking for files that might contain any of several related terms, use the pipe character `|` to separate them. For example:
        pattern="directory|path|folder"
    This will match ANY line containing AT LEAST ONE of those words. This is especially useful when you're unsure which term the code uses - you'll catch all variations in one search instead of running multiple queries.

    TIP 2 - Case-insensitive by default: The search is case-INSENSITIVE, so "Path", "PATH", and "path" will all match with a single pattern like `(?i)pat`. This means you don't need to worry about capitalization when searching.

    TIP 3 - Combine OR and AND logic: Use `|` for OR (at least one matches) and simple concatenation for AND (all terms must appear). For example:
        pattern="import|from" finds lines with either word (OR)
        pattern="importos" finds lines containing both words together (AND)

    Args:
        pattern: The regular expression pattern to search for
        path: The directory to search in. Default: current directory
        file_pattern: Glob pattern to filter files (e.g., '*.py'). Default: all files
        max_results: Maximum number of results to return. Default: 50
        context_before: Number of lines before the match to include as context. Default: 0
        context_after: Number of lines after the match to include as context. Default: 0

    Returns:
        A dictionary with search metadata and a list of matches including optional context lines
    """
    search_path = Path(path)

    if not search_path.exists():
        raise FileNotFoundError(f"Search path not found: {path}")

    if not search_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    try:
        compiled_pattern = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regular expression: {e}")

    matches = []
    files_checked = 0

    if file_pattern == "*":
        file_paths = list(search_path.glob("**/*"))
    else:
        file_paths = list(search_path.glob(f"**/{file_pattern}"))

    for file_path in file_paths:
        if max_results is not None and len(matches) >= max_results:
            break

        if not file_path.is_file():
            continue

        files_checked += 1

        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except (PermissionError, OSError):
            continue

        lines = text.splitlines()

        for line_idx, line in enumerate(lines):
            if compiled_pattern.search(line):
                start = max(0, line_idx - context_before)
                end = min(len(lines), line_idx + 1 + context_after)

                context_lines = []
                for ctx_idx in range(start, end):
                    context_lines.append({
                        "line_number": ctx_idx + 1,
                        "content": lines[ctx_idx],
                        "is_match_line": ctx_idx == line_idx,
                    })

                match_info = {
                    "file": str(file_path.relative_to(search_path)),
                    "line": line_idx + 1,
                    "match": compiled_pattern.search(line).group(),
                    "context": context_lines,
                }

                matches.append(match_info)

                if max_results is not None and len(matches) >= max_results:
                    break

    return {
        "search_pattern": pattern,
        "search_path": str(search_path.resolve()),
        "file_pattern": file_pattern,
        "files_checked": files_checked,
        "total_matches": len(matches),
        "context_before": context_before,
        "context_after": context_after,
        "results": matches,
    }


def _validate_path(path: Path) -> Path:
    """Validate and resolve a file path.
    
    Expands ~ (tilde) to the user's home directory and resolves symbolic links
    to return the absolute canonical path.
    """
    # Expand ~ (tilde) to user's home directory before resolving
    expanded = os.path.expanduser(str(path))
    resolved = Path(expanded).resolve()
    return resolved


def _apply_changes_to_content(original_content: str, changes: list) -> tuple[str, list]:
    """Apply changes to content in memory and return (new_content, applied_changes_log)."""
    applied_changes = []
    current_content = original_content

    for i, change in enumerate(changes):
        mode = change.get("mode", "exact")

        if mode == "whitespace_tolerant":
            search_str = change.get("search", "")
            replace_str = change.get("replace", "")

            def _normalize_ws(text):
                return " ".join(text.split())

            norm_search = _normalize_ws(search_str)
            norm_content = _normalize_ws(current_content)

            if norm_search not in norm_content:
                applied_changes.append({
                    "index": i,
                    "search": search_str,
                    "replace": replace_str,
                    "mode": mode,
                    "status": "not_found",
                    "message": "Search string (whitespace-normalized) not found in file",
                })
                continue

            # Treat empty normalized search as "append to end"
            if norm_search == "":
                norm_replace = _normalize_ws(replace_str)
                current_content = current_content + norm_replace
                applied_changes.append({
                    "index": i,
                    "search": search_str,
                    "replace": replace_str,
                    "mode": mode,
                    "status": "proposed",
                    "replacements_made": 1,
                })
            else:
                norm_replace = _normalize_ws(replace_str)

                def _norm_to_orig_pos(norm_pos, content):
                    """Convert a position in normalized content to corresponding position in original."""
                    nc = 0
                    pos = 0
                    while pos < len(content) and nc < norm_pos:
                        if content[pos].isspace():
                            while pos < len(content) and content[pos].isspace():
                                pos += 1
                            nc += 1
                        else:
                            nc += 1
                            pos += 1
                    return pos

                # Build result by processing each match in normalized content,
                # converting all positions from normalized to original content coordinates
                orig_parts = []
                last_end_norm = 0
                for m in re.finditer(re.escape(norm_search), norm_content):
                    before_start_orig = _norm_to_orig_pos(last_end_norm, current_content)
                    before_end_orig = _norm_to_orig_pos(m.start(), current_content)

                    orig_parts.append(current_content[before_start_orig:before_end_orig])
                    orig_parts.append(norm_replace)
                    last_end_norm = m.end()

                # Add remaining part after all matches
                final_before_start = _norm_to_orig_pos(last_end_norm, current_content)
                orig_parts.append(current_content[final_before_start:])
                current_content = "".join(orig_parts)

                applied_changes.append({
                    "index": i,
                    "search": search_str,
                    "replace": replace_str,
                    "mode": mode,
                    "status": "proposed",
                    "replacements_made": 1,
                })

        elif mode == "regex":
            pattern = change.get("pattern") or change.get("search", "")
            flags_str = change.get("flags", "")
            replace_str = change.get("replace", "")

            try:
                compiled = re.compile(pattern, flags=int(flags_str) if flags_str else 0)
            except Exception as e:
                applied_changes.append({
                    "index": i,
                    "pattern": pattern,
                    "status": "error",
                    "message": f"Invalid regex pattern: {e}",
                })
                continue

            match_objs = list(compiled.finditer(current_content))
            if not match_objs:
                applied_changes.append({
                    "index": i,
                    "pattern": pattern,
                    "status": "not_found",
                    "message": "Pattern not found in file",
                })
                continue

            # Treat empty pattern as "append to end"
            if pattern == "":
                current_content = current_content + replace_str
                applied_changes.append({
                    "index": i,
                    "pattern": pattern,
                    "replace": replace_str,
                    "mode": mode,
                    "status": "proposed",
                    "replacements_made": 1,
                })
            else:
                current_content = compiled.sub(replace_str, current_content)
                applied_changes.append({
                    "index": i,
                    "pattern": pattern,
                    "replace": replace_str,
                    "mode": mode,
                    "status": "proposed",
                    "replacements_made": len(match_objs),
                })

        elif mode == "line_range":
            start_line = change.get("start_line")
            end_line = change.get("end_line")
            replacement_content = change.get("replacement_content", "")

            if start_line is None or end_line is None:
                applied_changes.append({
                    "index": i,
                    "status": "error",
                    "message": "'line_range' mode requires 'start_line' and 'end_line' fields (1-indexed)",
                })
                continue

            lines = current_content.splitlines()
            s_idx = max(0, start_line - 1)
            e_idx = min(len(lines), end_line)

            if s_idx >= len(lines):
                applied_changes.append({
                    "index": i,
                    "start_line": start_line,
                    "end_line": end_line,
                    "status": "not_found",
                    "message": f"Line range [{start_line}, {end_line}] is beyond file length ({len(lines)} lines)",
                })
                continue

            current_content = "\n".join(
                lines[:s_idx] + [replacement_content] + lines[e_idx:]
            )

            applied_changes.append({
                "index": i,
                "start_line": start_line,
                "end_line": end_line,
                "replacement_content": replacement_content,
                "mode": mode,
                "status": "proposed",
                "lines_replaced": e_idx - s_idx,
            })

        else:  # exact (default)
            search_str = change.get("search", "")
            replace_str = change.get("replace", "")

            if search_str not in current_content:
                applied_changes.append({
                    "index": i,
                    "search": search_str,
                    "replace": replace_str,
                    "status": "not_found",
                    "message": "Search string not found in file",
                })
                continue

            # Treat empty search string as "append to end"
            if search_str == "":
                current_content = current_content + replace_str
                applied_changes.append({
                    "index": i,
                    "search": search_str,
                    "replace": replace_str,
                    "status": "proposed",
                    "replacements_made": 1,
                })
            else:
                old_count = current_content.count(search_str)
                new_content_after_replace = current_content.replace(search_str, replace_str)
                replacements_made = old_count - (new_content_after_replace.count(search_str))

                applied_changes.append({
                    "index": i,
                    "search": search_str,
                    "replace": replace_str,
                    "status": "proposed",
                    "replacements_made": replacements_made,
                })
                current_content = new_content_after_replace

    return current_content, applied_changes


@mcp.tool()
def read_file_content(
    path: str,
    start_line: int = None,
    end_line: int = None,
    encoding: str = "utf-8",
) -> dict:
    """Read and return the content of a file (similar to cat in command line).

    ⚠️ IMPORTANT - Common Pitfalls:
      - REQUIRED: `path` must be provided. Use full absolute paths for reliability.
      - Line numbers are 1-based (line 1 is the first line, not line 0).
      - Always verify the file exists before editing — use this tool first, then `edit_file`.

    Supports reading the entire file or a specific range of lines.
    When line ranges are provided, only that portion is returned.

    Args:
        path: REQUIRED. The absolute file path.
        start_line: Optional 1-based line number to start reading from (inclusive). Default: first line.
        end_line: Optional 1-based line number to stop reading at (inclusive). Default: last line.
        encoding: The file encoding to use. Default: utf-8

    Returns:
        Dictionary with path, total_lines, content (full or filtered), and start/end line info.
    """
    file_path = _validate_path(Path(path))

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if not file_path.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")

    content = file_path.read_text(encoding=encoding)
    lines = content.splitlines()
    total_lines = len(lines)

    # Adjust for 1-based indexing; clamp to valid range
    if start_line is not None:
        s_idx = max(0, start_line - 1)
    else:
        s_idx = 0

    if end_line is not None:
        e_idx = min(total_lines, end_line)
    else:
        e_idx = total_lines

    # Slice the lines (handle empty file)
    if not content:
        selected_lines = []
    else:
        split_lines = content.splitlines()
        selected_lines = split_lines[s_idx:e_idx]

    return {
        "path": str(file_path),
        "total_lines": len(content.splitlines()),
        "start_line": start_line,
        "end_line": end_line,
        "content": "\n".join(selected_lines),
    }


@mcp.tool()
def edit_file(
    path: str,
    changes: list,
    encoding: str = "utf-8",
    git_dir: str = None,
) -> dict:
    """Apply edits to a file directly and commit the change to git (for undo capability).

    ⚠️ IMPORTANT - Common Pitfalls:
      - REQUIRED: Both `path` and `changes` must be provided. `path` must be an absolute path.
      - REQUIRED: `changes` must be a JSON array `[...]` containing at least one change object.
      - Each change object requires: `mode` (string), `search` (string), `replace` (string).
      - The file must exist and reside in a git-initialized directory. If no git repo exists, run `git_init` first.
      - Always use `read_file` first to verify the exact content you're searching for — typos in `search` cause "not found" errors.
      - `git_dir` is OPTIONAL: Only provide it when the file is in a subdirectory of the git repo root.

    Returns the applied changes as a diff. Each edit is committed to git so it can be undone later.

    Supports four search modes specified per change object via the 'mode' field (defaults to 'exact'):
      1. 'exact'   - Standard exact string matching (default). Uses `search` and `replace` fields.
      2. 'whitespace_tolerant' - Ignores differences in whitespace (spaces, tabs, newlines).
                                  Normalizes all whitespace sequences to a single space for comparison.
      3. 'regex'   - Treats the search string as a regular expression pattern.
                      Supports back-references in replace via \\1, \\2, etc.
      4. 'line_range' - Operates on line number ranges instead of text content.
                         Requires: `start_line`, `end_line`, `replacement_content` fields.

    Args:
        path: REQUIRED. The absolute file path.
        changes: REQUIRED. A list (array) of dictionaries describing each edit operation. See mode descriptions above.
        encoding: The file encoding to use. Default: utf-8
        git_dir: Optional path to the git repository root. If not specified, the file's parent directory is used.

    Returns:
        Dictionary with path, status ("success"/"no_changes"/"error"), content_changed, total_changes, applied_changes, diff, and commit_hash.
    """
    file_path = _validate_path(Path(path))

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if not file_path.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")

    original_content = file_path.read_text(encoding=encoding)
    new_content, applied_changes = _apply_changes_to_content(original_content, changes)
    content_changed = new_content != original_content

    # Generate unified diff
    if content_changed:
        diff_lines = list(
            difflib.unified_diff(
                original_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_path.name}",
                tofile=f"b/{file_path.name}",
            )
        )
    else:
        diff_lines = []

    diff_output = "".join(diff_lines if diff_lines else "")

    # Determine git directory for all operations
    # If git_dir is specified, use it; otherwise fall back to file's parent directory
    if git_dir is not None:
        git_repo_dir = Path(git_dir)
    else:
        git_repo_dir = file_path.parent.resolve()

    # Check if git is initialized in the specified directory or any parent directory BEFORE writing
    def _is_git_repo(directory: Path) -> bool:
        """Check if a directory (or any of its ancestors) is a git repository."""
        current = directory.resolve()
        while True:
            git_dir_path = current / ".git"
            if git_dir_path.is_dir():
                return True
            parent = current.parent
            if parent == current:  # Reached root
                break
            current = parent
        return False

    if not _is_git_repo(git_repo_dir):
        return {
            "path": str(file_path),
            "status": "error",
            "message": f"No git repository found for '{file_path}'. Please initialize a git repository in this directory or its ancestors before using edit_file.",
            "content_changed": False,
            "total_changes": len(changes),
        }

    # Write to disk and commit to git for undo capability
    commit_hash = "unknown"

    def _configure_git_user(git_repo_dir: Path) -> dict | None:
        """Configure git user identity if not already set. Returns error dict or None."""
        try:
            result_check = subprocess.run(
                ["git", "-C", str(git_repo_dir), "config", "user.name"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if result_check.returncode != 0 or not result_check.stdout.strip():
                # Try to get user info from system
                import getpass
                import socket
                username = getpass.getuser()
                hostname = socket.gethostname()
                email = f"{username}@{hostname}"

                config_result = subprocess.run(
                    ["git", "-C", str(git_repo_dir), "config", "user.name", username],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                if config_result.returncode != 0:
                    return {
                        "status": "error",
                        "message": f"Failed to set git user.name: {config_result.stderr.strip()}"
                    }

                config_result = subprocess.run(
                    ["git", "-C", str(git_repo_dir), "config", "user.email", email],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                if config_result.returncode != 0:
                    return {
                        "status": "error",
                        "message": f"Failed to set git user.email: {config_result.stderr.strip()}"
                    }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Git user configuration failed: {e}",
            }
        return None

    if content_changed:
        file_path.write_text(new_content, encoding=encoding)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        num_changes = len([c for c in applied_changes if c.get("status") == "proposed"])
        commit_message = f"{timestamp} - edit_file: {num_changes} change(s) applied"

        try:
            result_add = subprocess.run(
                ["git", "-C", str(git_repo_dir), "add", str(file_path)],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result_add.returncode != 0:
                return {
                    "path": str(file_path),
                    "status": "error",
                    "message": f"git add failed: {result_add.stderr.strip()}",
                    "content_changed": content_changed,
                    "total_changes": len(changes),
                    "applied_changes": applied_changes,
                    "diff": diff_output,
                }

            user_config_error = _configure_git_user(git_repo_dir)
            if user_config_error:
                return {
                    "path": str(file_path),
                    "status": "error",
                    "message": f"Git user identity not configured and could not be set: {user_config_error['message']}",
                    "content_changed": content_changed,
                    "total_changes": len(changes),
                    "applied_changes": applied_changes,
                    "diff": diff_output,
                }

            result_commit = subprocess.run(
                ["git", "-C", str(git_repo_dir), "commit", "-m", commit_message],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result_commit.returncode != 0:
                return {
                    "path": str(file_path),
                    "status": "error",
                    "message": f"git commit failed: {result_commit.stderr.strip()}",
                    "content_changed": content_changed,
                    "total_changes": len(changes),
                    "applied_changes": applied_changes,
                    "diff": diff_output,
                }

            result_hash = subprocess.run(
                ["git", "-C", str(git_repo_dir), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL,
            )
            commit_hash = result_hash.stdout.strip() if result_hash.returncode == 0 else "unknown"

        except subprocess.TimeoutExpired:
            return {
                "path": str(file_path),
                "status": "error",
                "message": "Git operation timed out",
                "content_changed": content_changed,
                "total_changes": len(changes),
                "applied_changes": applied_changes,
                "diff": diff_output,
            }

    return {
        "path": str(file_path),
        "status": "success" if content_changed else "no_changes",
        "content_changed": content_changed,
        "total_changes": len(changes),
        "applied_changes": applied_changes,
        "diff": diff_output,
        "commit_hash": commit_hash if content_changed else None,
    }


@mcp.tool()
def undo_edit(path: str, steps: int = 1) -> dict:
    """Revert a file to its state before N confirmed edits using git history.

    ⚠️ IMPORTANT - Common Pitfalls:
      - REQUIRED: `path` must be an absolute path.
      - The file must have git history (commits from previous `edit_file` operations).
      - If no git commits exist, use `edit_file` first to create commits, then undo.
      - `steps` defaults to 1 — increase for reverting multiple edits.

    Args:
        path: REQUIRED. The absolute file path to revert.
        steps: Number of git commits to go back. Default: 1

    Returns:
        Dictionary with path, status ("undone"/"error"), steps_reverted, and commit_hash.
    """
    file_path = _validate_path(Path(path))
    path_str = str(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    file_path_parent = file_path.parent.resolve()
    
    def _configure_git_user(git_repo_dir: Path) -> dict | None:
        """Configure git user identity if not already set. Returns error dict or None."""
        try:
            result_check = subprocess.run(
                ["git", "-C", str(git_repo_dir), "config", "user.name"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if result_check.returncode != 0 or not result_check.stdout.strip():
                import getpass
                import socket
                username = getpass.getuser()
                hostname = socket.gethostname()
                email = f"{username}@{hostname}"

                config_result = subprocess.run(
                    ["git", "-C", str(git_repo_dir), "config", "user.name", username],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                if config_result.returncode != 0:
                    return {
                        "status": "error",
                        "message": f"Failed to set git user.name: {config_result.stderr.strip()}"
                    }

                config_result = subprocess.run(
                    ["git", "-C", str(git_repo_dir), "config", "user.email", email],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                if config_result.returncode != 0:
                    return {
                        "status": "error",
                        "message": f"Failed to set git user.email: {config_result.stderr.strip()}"
                    }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Git user configuration failed: {e}",
            }
        return None

    try:
        # Get the diff before undoing so we can report what changed
        result_diff_before = subprocess.run(
            ["git", "-C", str(file_path_parent), "diff", f"HEAD~{steps}..HEAD", "--", str(file_path)],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        diff_output_before = result_diff_before.stdout if result_diff_before.returncode == 0 else ""

        result_checkout = subprocess.run(
            ["git", "-C", str(file_path_parent), "checkout", f"HEAD~{steps}", "--", str(file_path)],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result_checkout.returncode != 0:
            return {
                "path": path_str,
                "status": "error",
                "message": f"git checkout failed: {result_checkout.stderr.strip()}",
            }

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        undo_message = f"{timestamp} - undo_edit: reverted {steps} step(s)"

        subprocess.run(
            ["git", "-C", str(file_path_parent), "add", str(file_path)],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )

        user_config_error = _configure_git_user(file_path_parent)
        if user_config_error:
            return {
                "path": path_str,
                "status": "error",
                "message": f"Git user identity not configured and could not be set: {user_config_error['message']}",
            }

        subprocess.run(
            ["git", "-C", str(file_path_parent), "commit", "-m", undo_message],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )

        result_hash = subprocess.run(
            ["git", "-C", str(file_path.parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
        commit_hash = result_hash.stdout.strip() if result_hash.returncode == 0 else "unknown"

    except subprocess.TimeoutExpired:
        return {
            "path": path_str,
            "status": "error",
            "message": "Git operation timed out",
        }
    except FileNotFoundError:
        return {
            "path": path_str,
            "status": "error",
            "message": "git is not installed or not in PATH",
        }

    return {
        "path": path_str,
        "status": "undone",
        "steps_reverted": steps,
        "commit_hash": commit_hash,
        "commit_message": undo_message,
        "diff": diff_output_before,
    }


@mcp.tool()
def preview_undo(path: str, steps: int = 1) -> dict:
    """Preview what changes would be made if undo_edit was called.

    Shows the git diff between the current state and HEAD~steps for this file,
    without actually modifying anything.

    Args:
        path: The file path to preview.
        steps: Number of commits to go back. Default: 1

    Returns:
        Dictionary with status, steps, and diff showing what would change.
    """
    file_path = _validate_path(Path(path))
    path_str = str(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    try:
        result_diff = subprocess.run(
            ["git", "diff", f"HEAD~{steps}", "HEAD", "--", str(file_path)],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )

        if result_diff.returncode != 0:
            result_diff = subprocess.run(
                ["git", "diff", f"HEAD~{steps}", "--", str(file_path)],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )

        diff_output = result_diff.stdout.strip() if result_diff.stdout.strip() else ""

    except subprocess.TimeoutExpired:
        return {
            "path": path_str,
            "status": "error",
            "message": "Git operation timed out",
        }
    except FileNotFoundError:
        return {
            "path": path_str,
            "status": "error",
            "message": "git is not installed or not in PATH",
        }

    return {
        "path": path_str,
        "steps": steps,
        "diff": diff_output,
        "status": "preview_ready" if diff_output else "no_changes_found",
    }


@mcp.tool()
def create_file(
    path: str,
    content: str,
    overwrite: bool = False,
    encoding: str = "utf-8",
    git_dir: str = None,
) -> dict:
    """Create a new file with the given content and commit to git for undo capability.

    ⚠️ IMPORTANT - Common Pitfalls:
      - REQUIRED: Both `path` and `content` must be provided. `path` must be an absolute path.
      - The parent directory MUST exist. Use `list_folder` to verify, or `git_init` on the parent directory first.
      - A git repository MUST exist in the file's directory or its ancestors. Run `git_init` first if needed.
      - If the file already exists, set `overwrite: true` to replace it — otherwise you get an error.

    Note that this tool can't be used in folders where no git is initialized.
    It checks if git repository exists before creating the file. If the file already exists
    and overwrite is False, returns an error. The file is written to disk and committed to git
    so it can be undone later.

    Args:
        path: REQUIRED. The absolute file path. Parent directory must exist.
        content: REQUIRED. The content for the new file.
        overwrite: If True, allow overwriting existing files. Default: False
        encoding: The file encoding to use. Default: utf-8
        git_dir: Optional path to the git repository root. If not specified, the file's parent directory is used.

    Returns:
        Dictionary with path, status ("success"/"overwritten"/"error"/"exists"), content_changed, and commit_hash.
    """
    file_path = _validate_path(Path(path))

    if not file_path.parent.exists():
        return {
            "path": str(file_path),
            "status": "error",
            "message": f"Parent directory does not exist: {file_path.parent}",
        }

    # Determine git directory for all operations
    if git_dir is not None:
        git_repo_dir = Path(git_dir)
    else:
        git_repo_dir = file_path.parent.resolve()

    def _is_git_repo(directory: Path) -> bool:
        """Check if a directory (or any of its ancestors) is a git repository."""
        current = directory.resolve()
        while True:
            git_dir_path = current / ".git"
            if git_dir_path.is_dir():
                return True
            parent = current.parent
            if parent == current:
                break
            current = parent
        return False

    if not _is_git_repo(git_repo_dir):
        return {
            "path": str(file_path),
            "status": "error",
            "message": f"No git repository found for '{file_path}'. Please initialize a git repository in this directory or its ancestors before using create_file.",
            "content_changed": False,
            "total_changes": 1,
        }

    exists = file_path.exists()

    if exists and not overwrite:
        return {
            "path": str(file_path),
            "status": "exists",
            "content_changed": False,
            "total_changes": 0,
            "message": f"File already exists: {file_path}. Set overwrite=True.",
        }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit_message = f"{timestamp} - create_file: {'overwrite' if exists else 'create'}"

    def _configure_git_user(git_repo_dir: Path) -> dict | None:
        """Configure git user identity if not already set. Returns error dict or None."""
        try:
            result_check = subprocess.run(
                ["git", "-C", str(git_repo_dir), "config", "user.name"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if result_check.returncode != 0 or not result_check.stdout.strip():
                import getpass
                import socket
                username = getpass.getuser()
                hostname = socket.gethostname()
                email = f"{username}@{hostname}"

                config_result = subprocess.run(
                    ["git", "-C", str(git_repo_dir), "config", "user.name", username],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                if config_result.returncode != 0:
                    return {
                        "status": "error",
                        "message": f"Failed to set git user.name: {config_result.stderr.strip()}"
                    }

                config_result = subprocess.run(
                    ["git", "-C", str(git_repo_dir), "config", "user.email", email],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                if config_result.returncode != 0:
                    return {
                        "status": "error",
                        "message": f"Failed to set git user.email: {config_result.stderr.strip()}"
                    }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Git user configuration failed: {e}",
            }
        return None

    try:
        file_path.write_text(content, encoding=encoding)

        result_add = subprocess.run(
            ["git", "-C", str(git_repo_dir), "add", str(file_path)],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result_add.returncode != 0:
            return {
                "path": str(file_path),
                "status": "error",
                "message": f"git add failed: {result_add.stderr.strip()}",
                "content_changed": False,
                "total_changes": 1,
            }

        user_config_error = _configure_git_user(git_repo_dir)
        if user_config_error:
            return {
                "path": str(file_path),
                "status": "error",
                "message": f"Git user identity not configured and could not be set: {user_config_error['message']}",
                "content_changed": False,
                "total_changes": 1,
            }

        result_commit = subprocess.run(
            ["git", "-C", str(git_repo_dir), "commit", "-m", commit_message],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result_commit.returncode != 0:
            return {
                "path": str(file_path),
                "status": "error",
                "message": f"git commit failed: {result_commit.stderr.strip()}",
                "content_changed": False,
                "total_changes": 1,
            }

        result_hash = subprocess.run(
            ["git", "-C", str(git_repo_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
        commit_hash = result_hash.stdout.strip() if result_hash.returncode == 0 else "unknown"

    except subprocess.TimeoutExpired:
        return {
            "path": str(file_path),
            "status": "error",
            "message": "Git operation timed out",
            "content_changed": False,
            "total_changes": 1,
        }

    return {
        "path": str(file_path),
        "status": "success" if not exists else "overwritten",
        "commit_hash": commit_hash,
    }


import anyio
import threading
import uuid

# Shared registry to track running processes across tool calls
_process_registry = {}

# Selection state registry: tracks active selections per file path
# { "/abs/path/to/file.py": { "active": True, "total_occurrences": 5, "search": "...", "mode": "..." } }
_selection_registry = {}


def _truncate_content(content: str, max_chars: int = 200) -> str:
    """Truncate content to show first and last max_chars characters.
    
    Returns format: [first 200 chars]... <truncated> ...[last 200 chars]
    """
    if len(content) <= max_chars * 2:
        return content
    
    first_part = content[:max_chars]
    last_part = content[-max_chars:]
    return f"{first_part}... <truncated> ...{last_part}"


def _find_matches(
    content: str,
    search: str,
    mode: str = "exact",
    max_return_chars: int = 1000,
    context_before: int = 0,
    context_after: int = 0,
) -> dict:
    """Find all matches of search pattern in content.
    
    Args:
        content: The file content to search
        search: The search string or pattern
        mode: 'exact', 'regex', or 'whitespace_tolerant'
        max_return_chars: Maximum characters to return in match content
        context_before: Lines before match to include
        context_after: Lines after match to include
        
    Returns:
        dict with total_occurrences and matches list
    """
    lines = content.splitlines()
    matches = []
    
    if mode == "exact":
        if search == "":
            # Empty search matches nothing in exact mode
            return {"total_occurrences": 0, "matches": []}
        
        # Find all occurrences of search string
        search_len = len(search)
        pos = 0
        occurrence = 0
        while pos < len(content):
            idx = content.find(search, pos)
            if idx == -1:
                break
            occurrence += 1
            # Calculate line number
            line_num = content[:idx].count('\n') + 1
            
            # Get surrounding context
            match_content = content[idx:idx + search_len]
            truncated = _truncate_content(match_content, max_chars=200)
            
            matches.append({
                "occurrence": occurrence,
                "line": line_num,
                "content": truncated,
                "truncated": len(match_content) > 400,
                "full_length": len(match_content),
            })
            pos = idx + search_len
            
    elif mode == "regex":
        try:
            compiled = re.compile(search)
        except re.error as e:
            return {"total_occurrences": 0, "matches": [], "error": f"Invalid regex: {e}"}
        
        for match in compiled.finditer(content):
            occurrence = match.group()
            line_num = content[:match.start()].count('\n') + 1
            match_content = match.group()
            truncated = _truncate_content(match_content, max_chars=200)
            
            matches.append({
                "occurrence": occurrence,
                "line": line_num,
                "content": truncated,
                "truncated": len(match_content) > 400,
                "full_length": len(match_content),
            })
            
    elif mode == "whitespace_tolerant":
        # Normalize whitespace in search and find matches
        norm_search = " ".join(search.split())
        norm_content = " ".join(content.split())
        
        if norm_search == "":
            return {"total_occurrences": 0, "matches": []}
            
        pos = 0
        occurrence = 0
        while pos < len(norm_content):
            idx = norm_content.find(norm_search, pos)
            if idx == -1:
                break
            occurrence += 1
            # Map normalized position back to original
            # For simplicity, use approximate line number from normalized content
            line_num = norm_content[:idx].count('\n') + 1
            
            # Get original content at approximate position
            orig_pos = 0
            norm_pos = 0
            orig_start = idx
            while norm_pos < idx and orig_pos < len(content):
                if content[orig_pos].isspace():
                    while orig_pos < len(content) and content[orig_pos].isspace():
                        orig_pos += 1
                    norm_pos += 1
                else:
                    norm_pos += 1
                    orig_pos += 1
            
            match_content = content[orig_pos:orig_pos + len(search)]
            truncated = _truncate_content(match_content, max_chars=200)
            
            matches.append({
                "occurrence": occurrence,
                "line": line_num,
                "content": truncated,
                "truncated": len(match_content) > 400,
                "full_length": len(match_content),
            })
            pos = idx + len(norm_search)
    
    # Apply max_return_chars limit
    total_chars = sum(len(m["content"]) for m in matches)
    if max_return_chars > 0 and total_chars > max_return_chars:
        # Truncate each match proportionally
        ratio = max_return_chars / total_chars
        for m in matches:
            if len(m["content"]) > max_return_chars // max(len(matches), 1):
                m["content"] = _truncate_content(m["content"], max_chars=max_return_chars // max(len(matches), 1))
    
    return {"total_occurrences": len(matches), "matches": matches}


def _invalidate_selection(path: str):
    """Invalidate the selection for a given file path."""
    if path in _selection_registry:
        _selection_registry[path]["active"] = False


def _get_selection(path: str) -> dict | None:
    """Get the active selection for a file path."""
    return _selection_registry.get(path)


@mcp.tool()
def select_before_edit_file_content(
    path: str,
    search: str = None,
    mode: str = "exact",
    max_return_chars: int = 1000,
    context_before: int = 0,
    context_after: int = 0,
    encoding: str = "utf-8",
) -> dict:
    """Search for content in a file and store the selection for later editing.
    
    This tool finds all occurrences of the search pattern in the file and stores
    the selection state. The selection can then be used with edit_after_select_file_content
    to replace specific occurrences.
    
    ⚠️ IMPORTANT - Common Pitfalls:
      - REQUIRED: `path` must be an absolute path.
      - At least one of `search` or `mode` must be provided.
      - `search` is the text/pattern to find. If empty, no selection is made.
      - `mode` can be: 'exact' (default), 'regex', or 'whitespace_tolerant'.
      - The selection is stored per file path - only one active selection per file.
      - Any edit to the file will invalidate the current selection.
    
    Args:
        path: REQUIRED. The absolute file path.
        search: REQUIRED. The text or pattern to search for.
        mode: Search mode - 'exact', 'regex', or 'whitespace_tolerant'. Default: 'exact'
        max_return_chars: Maximum characters to return in match content. Default: 1000
        context_before: Lines before match to include. Default: 0
        context_after: Lines after match to include. Default: 0
        encoding: The file encoding to use. Default: utf-8
    
    Returns:
        Dictionary with path, search, mode, total_occurrences, selection_active, and matches.
        Each match includes: occurrence, line, content (truncated), truncated flag, full_length.
    """
    file_path = _validate_path(Path(path))
    
    if not file_path.exists():
        return {
            "path": str(file_path),
            "status": "error",
            "message": f"File not found: {file_path}",
            "search": search,
            "mode": mode,
            "total_occurrences": 0,
            "selection_active": False,
            "matches": [],
        }
    
    if search is None or search == "":
        return {
            "path": str(file_path),
            "status": "error",
            "message": "Search pattern is required",
            "search": search,
            "mode": mode,
            "total_occurrences": 0,
            "selection_active": False,
            "matches": [],
        }
    
    content = file_path.read_text(encoding=encoding)
    result = _find_matches(content, search, mode, max_return_chars, context_before, context_after)
    
    # Store selection in registry
    _selection_registry[str(file_path)] = {
        "active": True,
        "total_occurrences": result["total_occurrences"],
        "search": search,
        "mode": mode,
        "max_return_chars": max_return_chars,
        "context_before": context_before,
        "context_after": context_after,
        "encoding": encoding,
    }
    
    return {
        "path": str(file_path),
        "status": "success",
        "search": search,
        "mode": mode,
        "total_occurrences": result["total_occurrences"],
        "selection_active": True,
        "matches": result["matches"],
    }


@mcp.tool()
def edit_after_select_file_content(
    path: str,
    occurrence: int = 0,
    replacement: str = "",
    encoding: str = "utf-8",
    git_dir: str = None,
) -> dict:
    """Replace content in a file based on a previously stored selection.
    
    This tool applies replacements to the occurrences found by select_before_edit_file_content.
    The selection must be active (not invalidated by previous edits).
    
    ⚠️ IMPORTANT - Common Pitfalls:
      - A selection must exist and be active for the file path.
      - `occurrence` = 0 replaces ALL occurrences
      - `occurrence` = 1 replaces only the FIRST occurrence
      - `occurrence` = [1, 3, 5] replaces SPECIFIC occurrences (list of 1-based indices)
      - If selection is invalid/missing, returns error with status "selection_error"
      - After successful edit, selection is automatically invalidated.
    
    Args:
        path: REQUIRED. The absolute file path.
        occurrence: Which occurrence(s) to replace. 0=all, positive=int, list=specific indices.
                    Default: 0 (replace all)
        replacement: The replacement text. Default: "" (empty string = delete)
        encoding: The file encoding to use. Default: utf-8
        git_dir: Optional path to the git repository root.
    
    Returns:
        Dictionary with path, status ("success"/"selection_error"/"error"), content_changed,
        replacements_made, diff, and commit_hash.
    """
    file_path = _validate_path(Path(path))
    path_str = str(file_path)
    
    # Check if selection exists and is active
    selection = _get_selection(path_str)
    if selection is None or not selection.get("active", False):
        return {
            "path": path_str,
            "status": "selection_error",
            "message": f"No active selection found for '{path_str}'. Run select_before_edit_file_content first.",
            "content_changed": False,
            "replacements_made": 0,
        }
    
    if not file_path.exists():
        _invalidate_selection(path_str)
        return {
            "path": path_str,
            "status": "error",
            "message": f"File not found: {file_path}",
            "content_changed": False,
            "replacements_made": 0,
        }
    
    content = file_path.read_text(encoding=encoding)
    search = selection["search"]
    mode = selection["mode"]
    total_occurrences = selection["total_occurrences"]
    
    # Determine which occurrences to replace
    if occurrence == 0:
        # Replace all
        targets = list(range(1, total_occurrences + 1))
    elif isinstance(occurrence, int) and occurrence > 0:
        # Replace specific single occurrence
        if occurrence > total_occurrences:
            return {
                "path": path_str,
                "status": "error",
                "message": f"Occurrence {occurrence} exceeds total occurrences ({total_occurrences})",
                "content_changed": False,
                "replacements_made": 0,
            }
        targets = [occurrence]
    elif isinstance(occurrence, list):
        # Replace specific multiple occurrences
        targets = [o for o in occurrence if 1 <= o <= total_occurrences]
        if not targets:
            return {
                "path": path_str,
                "status": "error",
                "message": f"No valid occurrences in list. Valid range: 1-{total_occurrences}",
                "content_changed": False,
                "replacements_made": 0,
            }
    else:
        return {
            "path": path_str,
            "status": "error",
            "message": f"Invalid occurrence value: {occurrence}",
            "content_changed": False,
            "replacements_made": 0,
        }
    
    # Apply replacements based on mode
    new_content = content
    replacements_made = 0
    
    if mode == "exact":
        if search == "":
            return {
                "path": path_str,
                "status": "error",
                "message": "Empty search string in exact mode",
                "content_changed": False,
                "replacements_made": 0,
            }
        
        # Find and replace specific occurrences
        occurrences_found = 0
        parts = []
        last_end = 0
        
        while last_end < len(new_content) and replacements_made < len(targets):
            idx = new_content.find(search, last_end)
            if idx == -1:
                break
            occurrences_found += 1
            
            if occurrences_found in targets:
                parts.append(new_content[last_end:idx])
                parts.append(replacement)
                replacements_made += 1
                last_end = idx + len(search)
            else:
                last_end = idx + len(search)
        
        parts.append(new_content[last_end:])
        new_content = "".join(parts)
        
    elif mode == "regex":
        try:
            compiled = re.compile(search)
        except re.error as e:
            return {
                "path": path_str,
                "status": "error",
                "message": f"Invalid regex: {e}",
                "content_changed": False,
                "replacements_made": 0,
            }
        
        # Find and replace specific occurrences
        matches = list(compiled.finditer(new_content))
        if len(matches) < max(targets):
            return {
                "path": path_str,
                "status": "error",
                "message": f"Found {len(matches)} matches, but requested occurrence {max(targets)}",
                "content_changed": False,
                "replacements_made": 0,
            }
        
        parts = []
        last_end = 0
        for match_idx, match in enumerate(matches):
            actual_occurrence = match_idx + 1
            if actual_occurrence in targets:
                parts.append(new_content[last_end:match.start()])
                parts.append(replacement)
                replacements_made += 1
                last_end = match.end()
        
        parts.append(new_content[last_end:])
        new_content = "".join(parts)
        
    elif mode == "whitespace_tolerant":
        # For whitespace_tolerant, normalize and replace
        norm_search = " ".join(search.split())
        norm_content = " ".join(new_content.split())
        
        if norm_search == "":
            return {
                "path": path_str,
                "status": "error",
                "message": "Empty search string in whitespace_tolerant mode",
                "content_changed": False,
                "replacements_made": 0,
            }
        
        occurrences_found = 0
        norm_parts = []
        last_end = 0
        
        while last_end < len(norm_content) and replacements_made < len(targets):
            idx = norm_content.find(norm_search, last_end)
            if idx == -1:
                break
            occurrences_found += 1
            
            if occurrences_found in targets:
                norm_parts.append(norm_content[last_end:idx])
                norm_parts.append(replacement)
                replacements_made += 1
                last_end = idx + len(norm_search)
            else:
                last_end = idx + len(norm_search)
        
        norm_parts.append(norm_content[last_end:])
        norm_result = "".join(norm_parts)
        
        # Map normalized result back to original content structure
        # This is complex - for now, just use the normalized result
        new_content = norm_result
    
    content_changed = new_content != content
    
    # Generate unified diff
    if content_changed:
        diff_lines = list(
            difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_path.name}",
                tofile=f"b/{file_path.name}",
            )
        )
    else:
        diff_lines = []
    
    diff_output = "".join(diff_lines if diff_lines else "")
    
    # Determine git directory
    if git_dir is not None:
        git_repo_dir = Path(git_dir)
    else:
        git_repo_dir = file_path.parent.resolve()
    
    def _is_git_repo(directory: Path) -> bool:
        current = directory.resolve()
        while True:
            git_dir_path = current / ".git"
            if git_dir_path.is_dir():
                return True
            parent = current.parent
            if parent == current:
                break
            current = parent
        return False
    
    def _configure_git_user(git_repo_dir: Path) -> dict | None:
        try:
            result_check = subprocess.run(
                ["git", "-C", str(git_repo_dir), "config", "user.name"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if result_check.returncode != 0 or not result_check.stdout.strip():
                import getpass
                import socket
                username = getpass.getuser()
                hostname = socket.gethostname()
                email = f"{username}@{hostname}"
                
                config_result = subprocess.run(
                    ["git", "-C", str(git_repo_dir), "config", "user.name", username],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                if config_result.returncode != 0:
                    return {"status": "error", "message": f"Failed to set git user.name: {config_result.stderr.strip()}"}
                
                config_result = subprocess.run(
                    ["git", "-C", str(git_repo_dir), "config", "user.email", email],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                if config_result.returncode != 0:
                    return {"status": "error", "message": f"Failed to set git user.email: {config_result.stderr.strip()}"}
        except Exception as e:
            return {"status": "error", "message": f"Git user configuration failed: {e}"}
        return None
    
    commit_hash = "unknown"
    
    if content_changed:
        if not _is_git_repo(git_repo_dir):
            _invalidate_selection(path_str)
            return {
                "path": path_str,
                "status": "error",
                "message": f"No git repository found for '{file_path}'.",
                "content_changed": False,
                "total_changes": 1,
            }
        
        file_path.write_text(new_content, encoding=encoding)
        
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commit_message = f"{timestamp} - edit_after_select: {replacements_made} replacement(s)"
        
        try:
            result_add = subprocess.run(
                ["git", "-C", str(git_repo_dir), "add", str(file_path)],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result_add.returncode != 0:
                _invalidate_selection(path_str)
                return {
                    "path": path_str,
                    "status": "error",
                    "message": f"git add failed: {result_add.stderr.strip()}",
                    "content_changed": content_changed,
                    "replacements_made": replacements_made,
                }
            
            user_config_error = _configure_git_user(git_repo_dir)
            if user_config_error:
                _invalidate_selection(path_str)
                return {
                    "path": path_str,
                    "status": "error",
                    "message": f"Git user identity not configured: {user_config_error['message']}",
                    "content_changed": content_changed,
                    "replacements_made": replacements_made,
                }
            
            result_commit = subprocess.run(
                ["git", "-C", str(git_repo_dir), "commit", "-m", commit_message],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result_commit.returncode != 0:
                _invalidate_selection(path_str)
                return {
                    "path": path_str,
                    "status": "error",
                    "message": f"git commit failed: {result_commit.stderr.strip()}",
                    "content_changed": content_changed,
                    "replacements_made": replacements_made,
                }
            
            result_hash = subprocess.run(
                ["git", "-C", str(git_repo_dir), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL,
            )
            commit_hash = result_hash.stdout.strip() if result_hash.returncode == 0 else "unknown"
            
        except subprocess.TimeoutExpired:
            _invalidate_selection(path_str)
            return {
                "path": path_str,
                "status": "error",
                "message": "Git operation timed out",
                "content_changed": content_changed,
                "replacements_made": replacements_made,
            }
        
        _invalidate_selection(path_str)
    
    return {
        "path": path_str,
        "status": "success" if content_changed else "no_changes",
        "content_changed": content_changed,
        "replacements_made": replacements_made,
        "diff": diff_output,
        "commit_hash": commit_hash if content_changed else None,
        "selection_active": False,
    }


# Keep last 10 completed/terminated processes for history tracking
_process_history = []
_MAX_HISTORY = 10

# =============================================================================
# Output Cache for Completed Processes
# =============================================================================
# Stores output of completed processes so they can be retrieved even after
# the process registry entry has been cleaned up. This addresses the issue
# where get_command_output returns nothing if called after a delay following
# command completion.

_output_cache = {}
_OUTPUT_CACHE_MAX_SIZE = 5  # Keep only the 5 most recent completed processes
_OUTPUT_CACHE_MAX_CHARS = 5000  # Per-stream character threshold for truncation
_OUTPUT_CACHE_MAX_LINES = 50  # Lines to keep when output is truncated


def _prune_output_cache():
    """Remove oldest entries from output cache if it exceeds MAX_SIZE."""
    global _output_cache
    if len(_output_cache) > _OUTPUT_CACHE_MAX_SIZE:
        # Sort by end_time and keep only the newest entries
        sorted_entries = sorted(
            _output_cache.items(),
            key=lambda x: x[1].get("end_time", 0),
            reverse=True,
        )
        # Keep only the newest _OUTPUT_CACHE_MAX_SIZE entries
        _output_cache = dict(sorted_entries[:_OUTPUT_CACHE_MAX_SIZE])


def _tail_output(output, max_chars, max_lines):
    """Truncate output if it exceeds max_chars, keeping only the last max_lines.

    Returns (truncated_output, original_lines, was_truncated).
    """
    original_lines = len(output.splitlines()) if output else 0
    original_chars = len(output)

    if original_chars <= max_chars:
        return output, original_lines, False

    # Truncate to last max_lines
    lines = output.splitlines()
    if len(lines) > max_lines:
        truncated_output = '\n'.join(lines[-max_lines:])
        return truncated_output, original_lines, True

    # Character threshold exceeded but line count is fine — still truncate
    # by keeping last max_lines
    truncated_output = '\n'.join(lines[-max_lines:])
    return truncated_output, original_lines, True


def _add_to_history(entry):
    """Add a process entry to the history, keeping only the most recent _MAX_HISTORY entries."""
    global _process_history
    _process_history.insert(0, entry)
    if len(_process_history) > _MAX_HISTORY:
        _process_history = _process_history[:_MAX_HISTORY]


def _read_output_from_file(output_file):
    """Read output captured to the tee/Tee-Object output file.

    Returns (stdout, stderr) tuple. For tee (Linux/macOS), all output is stdout.
    For PowerShell Tee-Object with 2>&1, stderr is redirected into stdout stream.
    """
    try:
        with open(output_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return content, ''
    except FileNotFoundError:
        return '', ''


def _get_all_output_from_registry(process_id):
    """Get all accumulated output from the registry (memory + file).

    For visible processes, first checks _final_stdout/_final_stderr set by the
    background monitor thread when the process completes. Falls back to reading
    the output file directly if the monitor hasn't finished yet.
    """
    if process_id not in _process_registry:
        return "", ""

    entry = _process_registry[process_id]
    is_visible = entry.get("is_visible", False)

    if is_visible:
        # Check if the background monitor has already saved final output
        final_stdout = entry.get("_final_stdout")
        final_stderr = entry.get("_final_stderr")
        if final_stdout is not None or final_stderr is not None:
            return final_stdout or "", final_stderr or ""
        # Monitor hasn't finished yet — read file directly
        return _read_output_from_file(entry["output_file"])

    # Hidden mode: get chunks stored directly in memory
    stdout_chunks = entry.get("stdout_chunks", [])
    stderr_chunks = entry.get("stderr_chunks", [])
    stdout_from_mem = ''.join(stdout_chunks) if stdout_chunks else ''
    stderr_from_mem = ''.join(stderr_chunks) if stderr_chunks else ''

    # Also read from file for redundancy
    try:
        with open(entry["output_file"], 'r', encoding='utf-8') as f:
            content = f.read()
        # Parse [STDOUT]/[STDERR] prefixed lines
        file_stdout, file_stderr = '', ''
        for line in content.splitlines():
            if line.startswith("[STDOUT]"):
                file_stdout += line[8:]
            elif line.startswith("[STDERR]"):
                file_stderr += line[9:]
    except FileNotFoundError:
        file_stdout, file_stderr = '', ''

    # Return memory data (primary) with file as fallback
    return stdout_from_mem if stdout_from_mem else file_stdout, stderr_from_mem if stderr_from_mem else file_stderr


def _get_visible_process_args(command: str, working_dir: str, output_file: str):
    """Return (args_list, creation_flags) for a visible terminal process.

    Wraps the command with `tee` (Linux/macOS) or `Tee-Object` (Windows) so that
    output is both displayed in the terminal AND captured to a file.
    Appends a short delay so the terminal stays open long enough for the file to flush.

    Returns (None, 0) if no visible terminal is available on this platform.
    """
    creation_flags = 0
    args_list = None

    if sys.platform == "win32":
        # Windows: use PowerShell Tee-Object to capture output while displaying
        # Use system temp directory for reliable file path
        temp_dir = os.environ.get("TEMP", "C:\\temp")
        escaped_cmd = command.replace('"', "'")
        wrapped_cmd = f'{escaped_cmd} 2>&1 | Tee-Object -FilePath "{output_file}"; Start-Sleep -Seconds 5'
        creation_flags = subprocess.CREATE_NEW_CONSOLE
        args_list = ["powershell", "-NoProfile", "-Command", wrapped_cmd]

    elif sys.platform == "darwin":
        # macOS: use osascript to open Terminal.app with tee capture
        # Append sleep to keep terminal open for file flush
        script = f'''tell application "Terminal" to do script "{command} 2>&1 | tee {output_file}; sleep 5"'''
        args_list = ["osascript", "-e", script]

    elif sys.platform == "linux":
        # Linux: detect display server and try available terminal emulators
        display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        if not display:
            # Headless / no display server — no visible terminal available
            return None, 0

        # Wrap command with tee to capture output to file while displaying
        # Append sleep to keep terminal open for file flush
        wrapped_command = f'{command} 2>&1 | tee {output_file}; sleep 5'

        # Try terminal emulators in order of popularity
        terminal_candidates = [
            ("gnome-terminal", ["gnome-terminal", "--", "sh", "-c", wrapped_command]),
            ("konsole", ["konsole", "-e", "sh", "-c", wrapped_command]),
            ("xterm", ["xterm", "-e", "sh", "-c", wrapped_command]),
            ("xfce4-terminal", ["xfce4-terminal", "--", "sh", "-c", wrapped_command]),
            ("lxterminal", ["lxterminal", "-e", "sh", "-c", wrapped_command]),
            ("mate-terminal", ["mate-terminal", "-e", "sh", "-c", wrapped_command]),
        ]

        for name, cmd in terminal_candidates:
            if subprocess.run(["which", name], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0:
                args_list = cmd
                break

    return args_list, creation_flags


def _run_visible_command(command: str, working_dir: str, process_id: str, output_file: str, effective_timeout: int):
    """Run a command in a visible terminal. Returns process handle or None on failure.

    For visible mode, we do NOT capture stdout/stderr via pipes — the terminal emulator
    displays output in the GUI. Output is captured via `tee` writing to output_file.

    Starts a background thread that monitors the process and saves output to the registry
    when the process completes, ensuring output is preserved even after the terminal closes.
    """
    args, flags = _get_visible_process_args(command, working_dir, output_file)
    if args is None:
        return None, 0

    def _monitor_visible_process(process_id, ofile):
        """Background thread: wait for process to complete, then save output to registry."""
        try:
            # Wait for the process to finish (with a timeout)
            entry = _process_registry.get(process_id)
            if entry is None:
                return
            process = entry["process"]

            # Poll every 0.5 seconds for process completion
            while True:
                poll_result = process.poll()
                if poll_result is not None:
                    # Process completed — wait briefly for file to flush, then read
                    time.sleep(1)  # Allow tee to finish writing and flush to disk
                    stdout_content, stderr_content = _read_output_from_file(ofile)

                    # Store in registry for get_command_output to read
                    if process_id in _process_registry:
                        _process_registry[process_id]["_final_stdout"] = stdout_content
                        _process_registry[process_id]["_final_stderr"] = stderr_content
                        _process_registry[process_id]["_final_return_code"] = poll_result

                    # Add to history
                    _add_to_history({
                        "process_id": process_id,
                        "command": command,
                        "return_code": poll_result,
                        "start_time": _process_registry[process_id].get("start_time"),
                        "end_time": time.time(),
                        "stdout": stdout_content,
                        "stderr": stderr_content,
                    })
                    break
                time.sleep(0.5)
        except Exception:
            pass

    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=working_dir,
            stdin=subprocess.DEVNULL,
            universal_newlines=True,
            creationflags=flags,
        )

        # Start background monitor thread to save output when process completes
        monitor_thread = threading.Thread(
            target=_monitor_visible_process,
            args=(process_id, output_file),
            daemon=True,
        )
        monitor_thread.start()

        return process, 0
    except Exception:
        return None, 0


def _run_hidden_command(command: str, working_dir: str, process_id: str, output_file: str, effective_timeout: int):
    """Run a command in hidden mode (default behavior). Returns process handle."""
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=working_dir,
        stdin=subprocess.DEVNULL,
        universal_newlines=True,
    )
    return process, 0


def _apply_tail(text, n):
    """Return the last n lines of text, or all text if n <= 0.

    Returns (result_text, total_lines, returned_lines) tuple.
    """
    if n <= 0:
        total = len(text.splitlines()) if text else 0
        return text, total, total
    lines = text.splitlines()
    total = len(lines)
    if total <= n:
        return text, total, total
    return '\n'.join(lines[-n:]), total, n


@mcp.tool()
async def execute_command(command: str, working_dir: str = ".", timeout: int = 300, wait_time: int = 4, visible: bool = False, show_output: bool = False, instant_wait_time: float = 5.0) -> dict:
    """Execute a command and optionally show it in a visible terminal window.

    ⚠️ IMPORTANT - Common Pitfalls:
      - REQUIRED: `command` must be provided — it is the only truly required parameter.
      - Use absolute paths for `working_dir` (e.g., "/home/user1/Documents/workspace/code/project3").
      - For long-running commands, use the returned `process_id` with `get_command_output` to poll for results.
      - For fast commands (ls, echo, etc.), full output is returned immediately — no need to poll.

    The process is tracked in a shared registry so that get_command_output can retrieve partial results
    as they arrive. This allows LM Studio to see output incrementally during long-running commands.
    Output is stored immediately in memory (not just files) so it survives temp file cleanup.

    When `visible` is True, the command runs in a platform-specific terminal emulator:
      - Windows: opens a new cmd console window
      - Linux: uses xterm, gnome-terminal, konsole, or similar (requires DISPLAY)
      - macOS: opens a new tab in Terminal.app

    If no visible terminal is available, the command falls back to hidden mode.

    By default, commands run in invisible/hidden mode. Use `visible=True` only when you need
    to see the terminal window (e.g., for interactive GUI applications).

    For fast commands that complete quickly, this tool returns the full output immediately.
    For long-running commands, it returns a process_id that can be used with get_command_output.

    Args:
        command: REQUIRED. The shell command to execute.
        working_dir: The working directory to execute the command in. Default: current directory (use absolute paths).
        timeout: Maximum execution time in seconds for the process itself. Default: 300
        wait_time: Recommended wait time in seconds between polling calls. Default: 4
        visible: If True, run the command in a visible terminal window. Default: False (hidden)
        show_output: If True, include a preview of the first 50 lines of captured output. Default: False
        instant_wait_time: Maximum seconds to wait when checking if a fast command completes instantly.
                           Use 0 to disable instant feedback. Default: 5.0

    Returns:
        A dictionary with process_id, return code, execution time, and optionally output preview.
        Includes output_file path for get_command_output to read progressive results.
        When the command completes instantly, includes full stdout and stderr with is_complete=True.
    """
    start_time = time.time()
    process_id = str(uuid.uuid4())
    output_file = f"/tmp/mcp_cmd_output_{process_id}.txt"

    # Enforce: effective timeout must be >= wait_time
    effective_timeout = max(timeout, wait_time)

    try:
        # Choose visible or hidden execution
        if visible:
            process, creation_flags = _run_visible_command(command, working_dir, process_id, output_file, effective_timeout)
            if process is not None:
                use_visible = True
            else:
                # No visible terminal available — fall back to hidden
                use_visible = False
        else:
            use_visible = False

        if not use_visible:
            # Hidden mode: use shell=True with pipes
            process, creation_flags = _run_hidden_command(command, working_dir, process_id, output_file, effective_timeout)

        # Store in registry
        _process_registry[process_id] = {
            "process": process,
            "output_file": output_file,
            "command": command,
            "start_time": start_time,
            "effective_timeout": effective_timeout,
            "is_visible": use_visible,
            "stdout_chunks": [],  # Immediate memory storage for stdout (hidden mode only)
            "stderr_chunks": [],  # Immediate memory storage for stderr (hidden mode only)
        }

        # For hidden mode only: use threading to read stdout and stderr concurrently
        if not use_visible:
            def read_stdout():
                """Read from stdout pipe until EOF."""
                try:
                    while True:
                        line = process.stdout.readline()
                        if line == '':
                            break
                        _process_registry[process_id]["stdout_chunks"].append(line)
                except Exception:
                    pass

            def read_stderr():
                """Read from stderr pipe until EOF."""
                try:
                    while True:
                        line = process.stderr.readline()
                        if line == '':
                            break
                        _process_registry[process_id]["stderr_chunks"].append(line)
                except Exception:
                    pass

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

        # For hidden mode: wait briefly to check if the command completes instantly
        # This provides immediate feedback for fast commands like 'echo', 'ls', etc.
        # Using anyio.sleep instead of time.sleep to avoid blocking the asyncio event loop.
        if not use_visible and instant_wait_time > 0:
            await anyio.sleep(instant_wait_time)
            proc_status = process.poll()

            # If command completed instantly, capture final output immediately
            if proc_status is not None:
                # Wait a bit more for threads to finish reading
                stdout_thread.join(timeout=1.0)
                stderr_thread.join(timeout=1.0)

                initial_stdout = ''.join(_process_registry[process_id].get("stdout_chunks", []))
                initial_stderr = ''.join(_process_registry[process_id].get("stderr_chunks", []))

                # Add to history for completed fast commands
                _add_to_history({
                    "process_id": process_id,
                    "command": command,
                    "return_code": proc_status,
                    "start_time": start_time,
                    "end_time": time.time(),
                    "stdout": initial_stdout,
                    "stderr": initial_stderr,
                })

                # Also cache in _output_cache for persistent retrieval via get_command_output
                # Truncate large outputs to preserve memory while maintaining accessibility
                stdout_cached, stdout_orig_lines, _ = _tail_output(initial_stdout, _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
                stderr_cached, stderr_orig_lines, _ = _tail_output(initial_stderr, _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)

                _output_cache[process_id] = {
                    "command": command,
                    "return_code": proc_status,
                    "start_time": start_time,
                    "end_time": time.time(),
                    "stdout": stdout_cached,
                    "stderr": stderr_cached,
                    "original_stdout_lines": stdout_orig_lines,
                    "original_stderr_lines": stderr_orig_lines,
                }
                _prune_output_cache()

                # Remove from registry since it's complete
                del _process_registry[process_id]

                end_time = time.time()
                execution_time = end_time - start_time
                stdout_preview, stdout_total, stdout_returned = _apply_tail(stdout_cached, 50)
                stderr_preview, stderr_total, stderr_returned = _apply_tail(stderr_cached, 50)
                return {
                    "command": command,
                    "working_dir": os.path.abspath(working_dir),
                    "process_id": process_id,
                    "output_file": output_file,
                    "return_code": proc_status,
                    "stdout": stdout_preview,
                    "stderr": stderr_preview,
                    "success": (proc_status == 0),
                    "is_complete": True,
                    "execution_time_seconds": round(execution_time, 3),
                    "recommended_wait_time": 0,
                    "effective_timeout": effective_timeout,
                    "visible": False,
                    "show_output": show_output,
                    "instant_feedback": True,
                    "output_summary": {
                        "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
                        "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
                    },
                }

        # Return immediately for long-running processes
        # The caller can poll get_command_output for updates as more output arrives
        end_time = time.time()
        execution_time = end_time - start_time

        visible_terminal_opened = use_visible

        # Optionally capture initial output preview for visible processes
        initial_stdout = ''
        initial_stderr = ''
        if show_output and use_visible:
            initial_stdout, initial_stderr = _read_output_from_file(output_file)

        stdout_preview, stdout_total, stdout_returned = _apply_tail(initial_stdout, 50)
        stderr_preview, stderr_total, stderr_returned = _apply_tail(initial_stderr, 50)
        return {
            "command": command,
            "working_dir": os.path.abspath(working_dir),
            "process_id": process_id,
            "output_file": output_file,
            "return_code": None,
            "stdout": stdout_preview,
            "stderr": stderr_preview,
            "success": None,
            "is_complete": False,
            "execution_time_seconds": round(execution_time, 3),
            "recommended_wait_time": wait_time,
            "effective_timeout": effective_timeout,
            "visible": visible_terminal_opened,
            "show_output": show_output,
            "instant_feedback": False,
            "output_summary": {
                "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
                "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
            },
        }

    except subprocess.TimeoutExpired:
        end_time = time.time()
        execution_time = end_time - start_time
        # Process may not have been successfully created if TimeoutExpired raised before Popen returned
        if 'process' in locals() and process is not None:
            try:
                process.kill()
            except (ProcessLookupError, AttributeError):
                pass
        # Try to read any output captured so far
        captured_stdout, captured_stderr = _read_output_from_file(output_file)
        # Also try to get output from memory (thread-captured chunks)
        if captured_stdout == '' or captured_stderr == '':
            if process_id in _process_registry:
                mem_stdout = ''.join(_process_registry[process_id].get("stdout_chunks", []))
                mem_stderr = ''.join(_process_registry[process_id].get("stderr_chunks", []))
                if not captured_stdout:
                    captured_stdout = mem_stdout
                if not captured_stderr:
                    captured_stderr = mem_stderr
        # Cache the output so get_command_output can retrieve it later
        stdout_cached, stdout_orig_lines, _ = _tail_output(captured_stdout, _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
        stderr_cached, stderr_orig_lines, _ = _tail_output(captured_stderr, _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
        _output_cache[process_id] = {
            "command": command,
            "return_code": -1,
            "start_time": start_time,
            "end_time": end_time,
            "stdout": stdout_cached,
            "stderr": stderr_cached,
            "original_stdout_lines": stdout_orig_lines,
            "original_stderr_lines": stderr_orig_lines,
        }
        _prune_output_cache()
        # Clean up registry
        if process_id in _process_registry:
            del _process_registry[process_id]
        timeout_msg = captured_stderr + "\n[TIMEOUT] Command timed out after {} seconds".format(effective_timeout)
        stdout_preview, stdout_total, stdout_returned = _apply_tail(captured_stdout, 50) if show_output else (captured_stdout, len(captured_stdout.splitlines()) if captured_stdout else 0, len(captured_stdout.splitlines()) if captured_stdout else 0)
        stderr_preview, stderr_total, stderr_returned = _apply_tail(timeout_msg, 50) if show_output else (timeout_msg, len(timeout_msg.splitlines()) if timeout_msg else 0, len(timeout_msg.splitlines()) if timeout_msg else 0)
        return {
            "command": command,
            "working_dir": os.path.abspath(working_dir),
            "process_id": process_id,
            "output_file": output_file,
            "return_code": -1,
            "stdout": stdout_preview,
            "stderr": stderr_preview,
            "success": False,
            "is_complete": True,
            "execution_time_seconds": round(execution_time, 3),
            "visible": False,
            "show_output": show_output,
            "output_summary": {
                "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
                "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
            },
        }

    except Exception as e:
        end_time = time.time()
        execution_time = end_time - start_time
        return {
            "command": command,
            "working_dir": os.path.abspath(working_dir),
            "process_id": process_id,
            "output_file": output_file,
            "return_code": -1,
            "stdout": "",
            "stderr": f"Error executing command: {e}",
            "success": False,
            "is_complete": True,
            "execution_time_seconds": round(execution_time, 3),
            "visible": False,
            "show_output": show_output,
            "output_summary": {
                "stdout": {"total_lines": 0, "returned_lines": 0, "lines_omitted": 0},
                "stderr": {"total_lines": 1, "returned_lines": 1, "lines_omitted": 0},
            },
        }


def _get_latest_process_id() -> str | None:
    """Get the process_id of the most recently started process from the registry or history.

    Returns the process_id of the latest process, or None if no processes exist.
    """
    # First check registry for any running processes
    if _process_registry:
        # Return the last key (most recently added in Python 3.7+)
        return next(reversed(_process_registry))

    # Then check history for completed processes
    if _process_history:
        return _process_history[0].get("process_id")

    return None


@mcp.tool()
async def get_command_output(
    process_id: str = None,
    wait_time: int = 4,
    tail: int = None,
    wait_for_completion: bool = False,
    timeout: int = 10,
) -> dict:
    """Get the current output from a previously started command.

    Reads accumulated output from memory (hidden mode) or file (visible mode).
    Useful for polling during long-running commands after calling execute_command.

    **Auto process_id detection:** If `process_id` is not provided, this tool automatically
    detects the most recently started process from the registry or history. The inferred
    process_id is returned in the response.

    **Blocking wait mode:** When `wait_for_completion=True`, this tool will block and wait
    for the process to complete (up to `timeout` seconds). It polls every second to check
    if the process has finished. If the process completes before the timeout, it returns
    immediately with the final output. If the timeout is reached, it returns the latest
    captured output with a status indicating the process is still running.

    For visible processes, this reads from the tee/Tee-Object output file.
    Returns is_complete=True and status='completed' if the process has finished,
    or is_complete=False and status='running' if still active.

    Args:
        process_id: Optional. The UUID from execute_command. If not provided, the most
                    recently started process will be used automatically.
        wait_time: Recommended wait time in seconds between polling calls. Default: 4
        tail: Number of lines to return from the end of output.
              - None (default) → uses the server-wide default (20 lines).
                Override via environment variable: MCP_DEFAULT_TAIL_LINES=30
              - 0 → returns full stdout and stderr (no filtering)
              - 10 → returns only the last 10 lines of stdout and stderr
              - 20 → returns only the last 20 lines of stdout and stderr
        wait_for_completion: If True, block and wait for the process to complete (up to timeout).
                            This eliminates the need for the separate wait_for_process tool.
                            Default: False
        timeout: Maximum seconds to wait when wait_for_completion=True. Default: 10
                If the process completes within this time, returns immediately with final output.
                If timeout is reached, returns latest output with status='running'.

    Returns:
        A dictionary with accumulated stdout, stderr, is_complete, status, and metadata.
        Includes 'inferred_process_id' if process_id was auto-detected.
        When tail > 0, stdout and stderr contain only the last N lines each.
        Includes 'output_summary' with total_lines, returned_lines, and lines_omitted counts.
        When wait_for_completion=True, includes 'waited_seconds' and 'completed_within_timeout'.
    """
    # Resolve tail: None → use server default; 0 → return all; >0 → return last N
    if tail is None:
        tail = _DEFAULT_TAIL_LINES

    # Auto-detect process_id if not provided
    inferred_process_id = None
    if not process_id:
        process_id = _get_latest_process_id()
        if process_id is None:
            return {
                "stdout": "",
                "stderr": "",
                "success": False,
                "is_complete": False,
                "message": "No running or completed processes found. Run execute_command first.",
            }
        inferred_process_id = process_id

    # Handle wait_for_completion mode
    if wait_for_completion:
        start_wait = time.time()
        poll_interval = 0.5  # Check every 500ms for responsiveness

        while (time.time() - start_wait) < timeout:
            # Check registry first
            if process_id in _process_registry:
                entry = _process_registry[process_id]
                process = entry["process"]
                poll_result = process.poll()

                if poll_result is not None:
                    # Process completed - get final output and cache it for later retrieval
                    stdout_from_mem, stderr_from_mem = _get_all_output_from_registry(process_id)
                    # Cache the output so get_command_output can retrieve it later
                    stdout_cached, stdout_orig_lines, _ = _tail_output(stdout_from_mem if stdout_from_mem else '', _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
                    stderr_cached, stderr_orig_lines, _ = _tail_output(stderr_from_mem if stderr_from_mem else '', _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
                    _output_cache[process_id] = {
                        "command": entry["command"],
                        "return_code": poll_result,
                        "start_time": entry.get("start_time"),
                        "end_time": time.time(),
                        "stdout": stdout_cached,
                        "stderr": stderr_cached,
                        "original_stdout_lines": stdout_orig_lines,
                        "original_stderr_lines": stderr_orig_lines,
                    }
                    _prune_output_cache()

                    stdout_result, stdout_total, stdout_returned = _apply_tail(stdout_from_mem if stdout_from_mem else '', tail)
                    stderr_result, stderr_total, stderr_returned = _apply_tail(stderr_from_mem if stderr_from_mem else '', tail)
                    waited = round(time.time() - start_wait, 2)
                    return {
                        "process_id": process_id,
                        "inferred_process_id": inferred_process_id,
                        "stdout": stdout_result,
                        "stderr": stderr_result,
                        "success": (poll_result == 0),
                        "is_complete": True,
                        "status": "completed" if poll_result == 0 else "failed",
                        "return_code": poll_result,
                        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "recommended_wait_time": 0,
                        "waited_seconds": waited,
                        "completed_within_timeout": True,
                        "output_summary": {
                            "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
                            "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
                        },
                    }

                # Process still running — sleep without blocking the asyncio event loop
                await anyio.sleep(poll_interval)
                continue
            elif process_id in [e.get("process_id") for e in _process_history]:
                # Process completed and moved to history
                for entry in _process_history:
                    if entry.get("process_id") == process_id:
                        stdout = entry.get("stdout", "")
                        stderr = entry.get("stderr", "")
                        stdout_result, stdout_total, stdout_returned = _apply_tail(stdout, tail)
                        stderr_result, stderr_total, stderr_returned = _apply_tail(stderr, tail)
                        waited = round(time.time() - start_wait, 2)
                        return {
                            "process_id": process_id,
                            "inferred_process_id": inferred_process_id,
                            "stdout": stdout_result,
                            "stderr": stderr_result,
                            "success": entry.get("return_code") == 0,
                            "is_complete": True,
                            "status": "completed" if entry.get("return_code") == 0 else "failed",
                            "return_code": entry.get("return_code"),
                            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "recommended_wait_time": 0,
                            "waited_seconds": waited,
                            "completed_within_timeout": True,
                            "output_summary": {
                                "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
                                "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
                            },
                        }
            else:
                # Process not found in registry or history - it may have been cleaned up
                waited = round(time.time() - start_wait, 2)
                return {
                    "process_id": process_id,
                    "inferred_process_id": inferred_process_id,
                    "stdout": "",
                    "stderr": "",
                    "success": None,
                    "is_complete": True,
                    "status": "not_found",
                    "message": "Process was removed from registry (may have completed and been cleaned up).",
                    "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "recommended_wait_time": 0,
                    "waited_seconds": waited,
                    "completed_within_timeout": False,
                    "output_summary": {"stdout": {"total_lines": 0, "returned_lines": 0, "lines_omitted": 0}, "stderr": {"total_lines": 0, "returned_lines": 0, "lines_omitted": 0}},
                }

        # Timeout reached - return latest output
        if process_id in _process_registry:
            entry = _process_registry[process_id]
            process = entry["process"]
            poll_result = process.poll()

            if poll_result is not None:
                # Process completed just as we checked timeout - cache for later retrieval
                stdout_from_mem, stderr_from_mem = _get_all_output_from_registry(process_id)
                # Cache the output so get_command_output can retrieve it later
                stdout_cached, stdout_orig_lines, _ = _tail_output(stdout_from_mem if stdout_from_mem else '', _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
                stderr_cached, stderr_orig_lines, _ = _tail_output(stderr_from_mem if stderr_from_mem else '', _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
                _output_cache[process_id] = {
                    "command": entry["command"],
                    "return_code": poll_result,
                    "start_time": entry.get("start_time"),
                    "end_time": time.time(),
                    "stdout": stdout_cached,
                    "stderr": stderr_cached,
                    "original_stdout_lines": stdout_orig_lines,
                    "original_stderr_lines": stderr_orig_lines,
                }
                _prune_output_cache()
                stdout_result, stdout_total, stdout_returned = _apply_tail(stdout_from_mem if stdout_from_mem else '', tail)
                stderr_result, stderr_total, stderr_returned = _apply_tail(stderr_from_mem if stderr_from_mem else '', tail)
                waited = round(time.time() - start_wait, 2)
                return {
                    "process_id": process_id,
                    "inferred_process_id": inferred_process_id,
                    "stdout": stdout_result,
                    "stderr": stderr_result,
                    "success": (poll_result == 0),
                    "is_complete": True,
                    "status": "completed" if poll_result == 0 else "failed",
                    "return_code": poll_result,
                    "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "recommended_wait_time": 0,
                    "waited_seconds": waited,
                    "completed_within_timeout": True,
                    "output_summary": {
                        "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
                        "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
                    },
                }

            # Process still running - return latest output
            stdout_from_mem, stderr_from_mem = _get_all_output_from_registry(process_id)
            stdout_result, stdout_total, stdout_returned = _apply_tail(stdout_from_mem if stdout_from_mem else '', tail)
            stderr_result, stderr_total, stderr_returned = _apply_tail(stderr_from_mem if stderr_from_mem else '', tail)
            waited = round(time.time() - start_wait, 2)
            return {
                "process_id": process_id,
                "inferred_process_id": inferred_process_id,
                "stdout": stdout_result,
                "stderr": stderr_result,
                "success": None,
                "is_complete": False,
                "status": "running",
                "return_code": None,
                "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "recommended_wait_time": wait_time,
                "waited_seconds": waited,
                "completed_within_timeout": False,
                "message": "Timeout reached. Process is still running. Use get_command_output again to check for updates.",
                "output_summary": {
                    "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
                    "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
                },
            }

    # Normal polling mode (no wait_for_completion)
    if process_id not in _process_registry:
        # First check output cache for recently completed processes
        if process_id in _output_cache:
            cache_entry = _output_cache[process_id]
            cached_stdout = cache_entry.get("stdout", "")
            cached_stderr = cache_entry.get("stderr", "")
            orig_stdout_lines = cache_entry.get("original_stdout_lines", len(cached_stdout.splitlines()) if cached_stdout else 0)
            orig_stderr_lines = cache_entry.get("original_stderr_lines", len(cached_stderr.splitlines()) if cached_stderr else 0)

            stdout_result, stdout_total, stdout_returned = _apply_tail(cached_stdout, tail)
            stderr_result, stderr_total, stderr_returned = _apply_tail(cached_stderr, tail)
            lines_discarded_stdout = orig_stdout_lines - stdout_total
            lines_discarded_stderr = orig_stderr_lines - stderr_total

            return {
                "process_id": process_id,
                "inferred_process_id": inferred_process_id,
                "stdout": stdout_result,
                "stderr": stderr_result,
                "success": cache_entry.get("return_code") == 0,
                "is_complete": True,
                "status": "completed" if cache_entry.get("return_code") == 0 else "failed",
                "return_code": cache_entry.get("return_code"),
                "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "recommended_wait_time": wait_time,
                "output_source": "cache",
                "output_summary": {
                    "stdout": {
                        "original_lines": orig_stdout_lines,
                        "returned_lines": stdout_returned,
                        "lines_omitted": stdout_total - stdout_returned,
                        "lines_discarded": lines_discarded_stdout,
                    },
                    "stderr": {
                        "original_lines": orig_stderr_lines,
                        "returned_lines": stderr_returned,
                        "lines_omitted": stderr_total - stderr_returned,
                        "lines_discarded": lines_discarded_stderr,
                    },
                },
            }

        # Check history for completed processes
        for entry in _process_history:
            if entry["process_id"] == process_id:
                stdout = entry.get("stdout", "")
                stderr = entry.get("stderr", "")
                stdout_result, stdout_total, stdout_returned = _apply_tail(stdout, tail)
                stderr_result, stderr_total, stderr_returned = _apply_tail(stderr, tail)
                return {
                    "process_id": process_id,
                    "inferred_process_id": inferred_process_id,
                    "stdout": stdout_result,
                    "stderr": stderr_result,
                    "success": entry.get("return_code") == 0,
                    "is_complete": True,
                    "status": "completed" if entry.get("return_code") == 0 else "failed",
                    "return_code": entry.get("return_code"),
                    "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "recommended_wait_time": wait_time,
                    "output_summary": {
                        "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
                        "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
                    },
                }

        return {
            "process_id": process_id,
            "inferred_process_id": inferred_process_id,
            "stdout": "",
            "stderr": "",
            "success": False,
            "is_complete": True,
            "status": "not_found",
            "message": f"Process '{process_id}' not found. It may have already completed or been cleaned up.",
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "recommended_wait_time": wait_time,
        }

    entry = _process_registry[process_id]
    process = entry["process"]

    # Check if process is still running
    poll_result = process.poll()
    is_complete = (poll_result is not None)

    # Get all output from registry (memory + file)
    stdout_from_mem, stderr_from_mem = _get_all_output_from_registry(process_id)

    # If process just completed during this poll, cache the output for later retrieval
    if is_complete and poll_result is not None:
        stdout_cached, stdout_orig_lines, _ = _tail_output(stdout_from_mem if stdout_from_mem else '', _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
        stderr_cached, stderr_orig_lines, _ = _tail_output(stderr_from_mem if stderr_from_mem else '', _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
        _output_cache[process_id] = {
            "command": entry["command"],
            "return_code": poll_result,
            "start_time": entry.get("start_time"),
            "end_time": time.time(),
            "stdout": stdout_cached,
            "stderr": stderr_cached,
            "original_stdout_lines": stdout_orig_lines,
            "original_stderr_lines": stderr_orig_lines,
        }
        _prune_output_cache()
        # Clean up registry since process is complete
        del _process_registry[process_id]

    stdout_result, stdout_total, stdout_returned = _apply_tail(stdout_from_mem if stdout_from_mem else '', tail)
    stderr_result, stderr_total, stderr_returned = _apply_tail(stderr_from_mem if stderr_from_mem else '', tail)

    return {
        "process_id": process_id,
        "inferred_process_id": inferred_process_id,
        "stdout": stdout_result,
        "stderr": stderr_result,
        "success": (poll_result == 0) if is_complete else None,
        "is_complete": is_complete,
        "status": "completed" if (is_complete and poll_result == 0) else ("failed" if is_complete and poll_result is not None else "running"),
        "return_code": poll_result if is_complete else None,
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recommended_wait_time": wait_time,
        "output_summary": {
            "stdout": {"total_lines": stdout_total, "returned_lines": stdout_returned, "lines_omitted": stdout_total - stdout_returned},
            "stderr": {"total_lines": stderr_total, "returned_lines": stderr_returned, "lines_omitted": stderr_total - stderr_returned},
        },
    }


@mcp.tool()
def check_progress(process_id: str) -> dict:
    """Check the progress of a running or completed command.

    Returns the current state of the process including whether it's still running,
    how much output has been captured so far, and any return code if complete.
    Useful for monitoring long-running commands without reading the output file directly.

    Args:
        process_id: The UUID returned from execute_command.

    Returns:
        A dictionary with status, is_running, return_code, stdout, stderr, and metadata.
    """
    if process_id not in _process_registry:
        # Check history for completed processes
        for entry in _process_history:
            if entry["process_id"] == process_id:
                return {
                    "process_id": process_id,
                    "command": entry.get("command", ""),
                    "is_running": False,
                    "return_code": entry.get("return_code"),
                    "status": "completed" if entry.get("return_code") == 0 else "failed",
                    "output_file": entry.get("output_file"),
                    "current_output_length": len(entry.get("stdout", "")) + len(entry.get("stderr", "")),
                }
        return {
            "status": "error",
            "message": f"No process found for ID '{process_id}'. It may have already completed or been cleaned up.",
        }

    entry = _process_registry[process_id]
    process = entry["process"]

    # Check if process is still running
    poll_result = process.poll()
    is_running = (poll_result is None)

    # Get all output from memory + file
    stdout_from_mem, stderr_from_mem = _get_all_output_from_registry(process_id)

    return {
        "process_id": process_id,
        "command": entry["command"],
        "is_running": is_running,
        "return_code": poll_result if not is_running else None,
        "output_file": entry["output_file"],
        "current_output_length": len(stdout_from_mem) + len(stderr_from_mem),
        "status": "running" if is_running else ("completed" if poll_result == 0 else "failed"),
    }


@mcp.tool()
def get_command_history(limit: int = 10) -> dict:
    """Get the history of recently completed/terminated processes.

    Keeps track of the last N (default 10) processes that have finished or been killed,
    including their final state and output. Useful for reviewing what happened to previous commands.

    Args:
        limit: Maximum number of entries to return. Default: 10

    Returns:
        A dictionary with total_count, history (list of process entries), and each entry includes
        process_id, command, return_code, stdout, stderr, start_time, end_time, execution_time_seconds.
    """
    # Filter only completed/terminated processes from memory
    completed_history = []
    for pid, entry in _process_registry.items():
        if entry["process"].poll() is not None:
            completed_history.append({
                "process_id": pid,
                "command": entry["command"],
                "status": "completed" if entry.get("return_code") == 0 else ("failed" if entry.get("return_code") is not None else "unknown"),
                "start_time": entry.get("start_time"),
            })

    # Merge with stored history, deduplicate by process_id
    all_entries = {}
    for h in _process_history:
        all_entries[h["process_id"]] = h
    for c in completed_history:
        if c["process_id"] not in all_entries:
            all_entries[c["process_id"]] = c

    # Sort by start_time (most recent first) and limit
    sorted_entries = sorted(
        all_entries.values(),
        key=lambda x: x.get("start_time") or 0,
        reverse=True,
    )[:limit]

    return {
        "total_count": len(sorted_entries),
        "history": sorted_entries,
    }


@mcp.tool()
def kill_process(process_id: str) -> dict:
    """Kill a running process by its ID.

    Sends SIGKILL to the subprocess tracked under the given process_id.
    Useful for stopping long-running commands early.

    Args:
        process_id: The UUID returned from execute_command.

    Returns:
        A dictionary with status, killed_process_id, and message.
    """
    if process_id not in _process_registry:
        return {
            "status": "error",
            "message": f"No running process found for ID '{process_id}'. It may have already completed or been cleaned up.",
        }

    entry = _process_registry[process_id]
    process = entry["process"]

    try:
        # Try graceful shutdown first, then force kill if needed
        try:
            process.terminate()
            process.wait(timeout=3)
            status_msg = "terminated"
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            status_msg = "killed with SIGKILL"

        # Add to history before removing from registry
        # For visible processes, also try to capture final output from file
        if entry.get("is_visible", False):
            final_stdout, final_stderr = _read_output_from_file(entry["output_file"])
            if not final_stdout:
                final_stdout = ''.join(entry.get("stdout_chunks", []))
            if not final_stderr:
                final_stderr = ''.join(entry.get("stderr_chunks", []))
        else:
            final_stdout = ''.join(entry.get("stdout_chunks", []))
            final_stderr = ''.join(entry.get("stderr_chunks", []))

        # Cache the output so get_command_output can retrieve it later even after cleanup
        # This addresses the issue where get_command_output returns "not_found" if called
        # after the process has been killed and the registry entry was removed.
        stdout_cached, stdout_orig_lines, _ = _tail_output(final_stdout, _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)
        stderr_cached, stderr_orig_lines, _ = _tail_output(final_stderr, _OUTPUT_CACHE_MAX_CHARS, _OUTPUT_CACHE_MAX_LINES)

        _output_cache[process_id] = {
            "command": entry["command"],
            "return_code": process.returncode if hasattr(process, 'returncode') else None,
            "start_time": entry.get("start_time"),
            "end_time": time.time(),
            "stdout": stdout_cached,
            "stderr": stderr_cached,
            "original_stdout_lines": stdout_orig_lines,
            "original_stderr_lines": stderr_orig_lines,
        }
        _prune_output_cache()

        _add_to_history({
            "process_id": process_id,
            "command": entry["command"],
            "return_code": process.returncode if hasattr(process, 'returncode') else None,
            "start_time": entry.get("start_time"),
            "end_time": time.time(),
            "stdout": final_stdout,
            "stderr": final_stderr,
        })

        # Clean up registry entry
        del _process_registry[process_id]

        return {
            "status": "success",
            "killed_process_id": process_id,
            "message": f"Process '{entry['command']}' was {status_msg}.",
        }

    except Exception as e:
        return {
            "status": "error",
            "killed_process_id": process_id,
            "message": f"Error killing process: {e}",
        }


@mcp.tool()
async def wait_for_process(process_id: str = None, wait_time: int = 10) -> dict:
    """Wait for a specified duration, optionally waiting for a process to complete.

    This tool yields control back to the asyncio event loop (using anyio.sleep),
    allowing the LLM to give time for long-running commands to produce output without
    spamming get_command_output or check_progress calls — without blocking the event loop.

    When a process_id is provided, the tool will return early if the process completes
    before the wait_time expires (checked every second).

    Args:
        process_id: Optional process ID to wait for. If provided, the tool will wait
                    for the shorter of wait_time or process completion.
        wait_time: Number of seconds to wait. Default: 10

    Returns:
        Dictionary with status, waited_seconds, and optionally process status.
    """
    start_time = time.time()
    elapsed = 0.0

    if process_id:
        # Wait for process with periodic progress checks
        while elapsed < wait_time:
            if process_id not in _process_registry:
                # Process already removed from registry — it was likely killed
                actual_waited = round(time.time() - start_time, 2)
                return {
                    "status": "success",
                    "waited_seconds": actual_waited,
                    "process_id": process_id,
                    "process_completed": True,
                    "message": "Process was removed from registry (likely killed).",
                }

            entry = _process_registry[process_id]
            poll_result = entry["process"].poll()

            if poll_result is not None:
                # Process has completed
                actual_waited = round(time.time() - start_time, 2)
                return {
                    "status": "success",
                    "waited_seconds": actual_waited,
                    "process_id": process_id,
                    "command": entry["command"],
                    "return_code": poll_result,
                    "process_completed": True,
                    "success": poll_result == 0,
                    "status_msg": "completed" if poll_result == 0 else "failed",
                }

            # Process still running — yield to event loop and check again
            await anyio.sleep(1)
            elapsed = time.time() - start_time

        # Waited full duration but process still running
        actual_waited = round(time.time() - start_time, 2)
        return {
            "status": "success",
            "waited_seconds": actual_waited,
            "process_id": process_id,
            "command": entry["command"],
            "process_completed": False,
            "message": "Wait time elapsed. Process is still running.",
        }

    else:
        # No process_id — yield to event loop for the specified duration
        await anyio.sleep(wait_time)
        actual_waited = round(time.time() - start_time, 2)
        return {
            "status": "success",
            "waited_seconds": actual_waited,
            "message": f"Waited {actual_waited} seconds.",
        }


@mcp.tool()
def execute_command_with_terminal(command: str, working_dir: str = ".", timeout: int = 300, wait_time: int = 4, show_output: bool = False) -> dict:
    """Execute a command in a visible terminal window.

    This tool is specifically designed for commands that require user interaction,
    such as `sudo` commands that prompt for a password, interactive configuration tools,
    or any command that needs a visible terminal for input/output.

    Unlike `execute_command`, this tool always runs in a visible terminal window
    (`visible=True`), allowing the user to see the terminal and interact with prompts.

    When `visible=True` on Linux, the command is wrapped with `tee` to capture output
    to a file while displaying it in the terminal. On Windows, PowerShell Tee-Object
    is used. On macOS, Terminal.app is launched with tee capture.

    If no visible terminal is available (e.g., headless Linux without DISPLAY),
    the command falls back to hidden mode.

    Args:
        command: The command to execute.
        working_dir: The working directory to execute the command in. Default: current directory
        timeout: Maximum execution time in seconds for the process itself. Default: 300
        wait_time: Recommended wait time in seconds between polling calls. Default: 4
        show_output: If True, include a preview of the first 50 lines of captured output.
                     Default: False

    Returns:
        A dictionary with process_id, return code, execution time, and output info.
        Includes 'visible': True if the terminal was actually opened.
        For interactive commands, use get_command_output with the returned process_id
        to check progress, or wait for the process to complete.

    Example:
        # For sudo commands that need password input
        execute_command_with_terminal("sudo apt update")
    """
    return execute_command(
        command=command,
        working_dir=working_dir,
        timeout=timeout,
        wait_time=wait_time,
        visible=True,  # Always visible for interactive commands
        show_output=show_output,
    )


@mcp.tool()
def git_init(path: str, overwrite: bool = False) -> dict:
    """Initialize a git repository in the specified directory.

    ⚠️ IMPORTANT - Common Pitfalls:
      - REQUIRED: `path` must be an absolute path to an EXISTING directory.
      - Run this BEFORE `edit_file` or `create_file` if the directory has no .git folder.
      - After initializing, you can use file editing tools — each edit will be committed for undo capability.
      - If a git repo already exists, set `overwrite: true` to reinitialize (WARNING: removes existing .git).

    Creates a .git directory in the target path, making it a valid git repository.
    This is useful before using edit_file or create_file when no git repo exists yet.

    Args:
        path: REQUIRED. The absolute directory path where the git repository should be initialized.
        overwrite: If True, removes existing .git and reinitializes. Default: False

    Returns:
        Dictionary with path, status ("success"/"exists"/"error"), and message.
    """
    dir_path = _validate_path(Path(path))

    if not dir_path.exists():
        return {
            "path": str(dir_path),
            "status": "error",
            "message": f"Directory does not exist: {dir_path}",
        }

    if not dir_path.is_dir():
        return {
            "path": str(dir_path),
            "status": "error",
            "message": f"Not a directory: {dir_path}",
        }

    git_dir = dir_path / ".git"
    if git_dir.exists() and not overwrite:
        return {
            "path": str(dir_path),
            "status": "exists",
            "content_changed": False,
            "total_changes": 0,
            "message": f"A git repository already exists at '{dir_path}'. Set overwrite=True to reinitialize.",
        }

    try:
        if git_dir.exists():
            import shutil
            shutil.rmtree(str(git_dir))

        result = subprocess.run(
            ["git", "init", str(dir_path)],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return {
                "path": str(dir_path),
                "status": "error",
                "message": f"git init failed: {result.stderr.strip()}",
            }

        return {
            "path": str(dir_path),
            "status": "success",
            "message": f"Git repository initialized at '{dir_path}'.",
        }

    except subprocess.TimeoutExpired:
        return {
            "path": str(dir_path),
            "status": "error",
            "message": "git init timed out",
        }


# Run the server
if __name__ == "__main__":
    mcp.run()
