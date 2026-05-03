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

All file operations are restricted to paths within the user's home directory. Each edit is committed to git for undo capability.
"""

import difflib
import math
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# Security: Restrict file operations to home directory
HOME_DIR = Path.home().resolve()

from fastmcp import FastMCP

# Create a FastMCP server instance
mcp = FastMCP("Python File Tools")


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
    """Validate that a path is within the home directory for security."""
    resolved = path.resolve()
    if not str(resolved).startswith(str(HOME_DIR)):
        raise ValueError(f"Access denied: paths outside home directory are not allowed. Requested: {resolved}")
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

    Supports reading the entire file or a specific range of lines.
    When line ranges are provided, only that portion is returned.

    Args:
        path: The file path (relative to home directory). Only paths within the home directory are allowed.
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

    Returns the applied changes as a diff. Each edit is committed to git so it can be undone later.

    Supports four search modes specified per change object via the 'mode` field (defaults to 'exact'):
      1. 'exact'   - Standard exact string matching (default). Uses `search` and `replace` fields.
      2. 'whitespace_tolerant' - Ignores differences in whitespace (spaces, tabs, newlines).
                                  Normalizes all whitespace sequences to a single space for comparison.
      3. 'regex'   - Treats the search string as a regular expression pattern.
                      Supports back-references in replace via \\1, \\2, etc.
      4. 'line_range' - Operates on line number ranges instead of text content.

    Args:
        path: The file path (relative to home directory). Only paths within the home directory are allowed.
        changes: A list of dictionaries describing each edit operation. See mode descriptions above.
        encoding: The file encoding to use. Default: utf-8
        git_dir: Optional path to the git repository root. If not specified, the file's parent directory is used. Use this when the file is in a subdirectory of the git repo.

    Returns:
        Dictionary with path, content_changed, total_changes, applied_changes, diff, and commit_hash.
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

    Args:
        path: The file path to revert.
        steps: Number of commits to go back. Default: 1

    Returns:
        Dictionary with status, steps_reverted, and new commit hash.
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

    Checks if git repository exists before creating the file. If the file already exists
    and overwrite is False, returns an error. The file is written to disk and committed to git
    so it can be undone later.

    Supports four search modes specified per change object via the 'mode` field (defaults to 'exact'):
      1. 'exact'   - Standard exact string matching (default). Uses `search` and `replace` fields.
      2. 'whitespace_tolerant' - Ignores differences in whitespace (spaces, tabs, newlines).
                                  Normalizes all whitespace sequences to a single space for comparison.
      3. 'regex'   - Treats the search string as a regular expression pattern.
                      Supports back-references in replace via \\1, \\2, etc.
      4. 'line_range' - Operates on line number ranges instead of text content.

    Args:
        path: The file path (relative to home directory). Only paths within the home directory are allowed.
        content: The content for the new file.
        overwrite: If True, allow overwriting existing files. Default: False
        encoding: The file encoding to use. Default: utf-8
        git_dir: Optional path to the git repository root. If not specified, the file's parent directory is used.

    Returns:
        Dictionary with path, content_changed, total_changes, applied_changes, diff, and commit_hash.
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
        "content_changed": True,
        "total_changes": 1,
        "applied_changes": [
            {
                "index": 0,
                "search": "",
                "replace": content,
                "status": "proposed",
                "replacements_made": 1,
            }
        ],
        "diff": content,
        "commit_hash": commit_hash if not exists else None,
    }


@mcp.tool()
def execute_command(command: str, working_dir: str = ".", timeout: int = 300) -> dict:
    """Execute a bash command and return the result.
       Note, use this only as a last resort if no other function could achieve what you need.

    Args:
        command: The bash command to execute.
        working_dir: The working directory to execute the command in. Default: current directory
        timeout: Maximum execution time in seconds. Default: 300

    Returns:
        A dictionary with stdout, stderr, return code, and execution time
    """
    start_time = time.time()
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            stdin=subprocess.DEVNULL,
        )

        end_time = time.time()
        execution_time = end_time - start_time

        return {
            "command": command,
            "working_dir": os.path.abspath(working_dir),
            "return_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.returncode == 0,
            "execution_time_seconds": round(execution_time, 3),
        }
    except subprocess.TimeoutExpired:
        end_time = time.time()
        execution_time = end_time - start_time
        return {
            "command": command,
            "working_dir": os.path.abspath(working_dir),
            "return_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "success": False,
            "execution_time_seconds": round(execution_time, 3),
        }
    except Exception as e:
        end_time = time.time()
        execution_time = end_time - start_time
        return {
            "command": command,
            "working_dir": os.path.abspath(working_dir),
            "return_code": -1,
            "stdout": "",
            "stderr": f"Error executing command: {e}",
            "success": False,
            "execution_time_seconds": round(execution_time, 3),
        }


@mcp.tool()
def git_init(path: str, overwrite: bool = False) -> dict:
    """Initialize a git repository in the specified directory.

    Creates a .git directory in the target path, making it a valid git repository.
    This is useful before using edit_file or create_file when no git repo exists yet.

    If a git repository already exists at the path and overwrite is False, returns an error.
    Set overwrite=True to reinitialize (which will remove existing .git).

    Args:
        path: The directory path where the git repository should be initialized (relative to home directory).
        overwrite: If True, removes existing .git and reinitializes. Default: False

    Returns:
        Dictionary with status, path, message indicating success or failure.
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
            "status": "initialized",
            "content_changed": True,
            "total_changes": 1,
            "applied_changes": [
                {
                    "index": 0,
                    "search": "",
                    "replace": ".git directory",
                    "mode": "exact",
                    "status": "proposed",
                    "replacements_made": 1,
                }
            ],
            "diff": "",
            "commit_hash": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "path": str(dir_path),
            "status": "error",
            "message": "git init timed out",
            "content_changed": False,
            "total_changes": 1,
        }


# Run the server
if __name__ == "__main__":
    mcp.run()
