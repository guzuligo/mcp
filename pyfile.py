"""
Python File Tools MCP Server — Compact, LLM-friendly design.

MODULE-LEVEL TOOL REFERENCE
===========================
All 14 MCP tools return dicts with a "status" key:
  "success" | "error" | "exists" | "no_changes" | "running" | "completed" | "failed"

TOOL QUICK REFERENCE
  list_folder(path, recursive) → entries, total_count, path
  search_file_content(pattern, path, file_pattern, max_results, context_before, context_after) → results, total_matches, files_checked
  read_file_content(path, start_line, end_line, encoding) → content, total_lines, path
  edit_file(path, changes, encoding, git_dir) → status, diff, commit_hash, applied_changes
  undo_edit(path, steps) → status, commit_hash, steps_reverted
  preview_undo(path, steps) → diff, status
  create_file(path, content, overwrite, encoding, git_dir) → status, commit_hash, path
  execute_command(command, working_dir, timeout, wait_time, visible, show_output) → process_id, stdout, stderr, is_complete
  get_command_output(process_id, wait_time, tail) → stdout, stderr, status, return_code
  check_progress(process_id) → is_running, current_output_length
  get_command_history(limit) → history, total_count
  kill_process(process_id) → status, message
  wait_for_process(process_id, wait_time) → process_completed, return_code
  git_init(path, overwrite) → status, message

⚠️ LLM TOOL USE REMINDERS:
  edit_file:     REQUIRED=path (absolute) + changes=[array]. File must be in git-initialized dir. Read file first!
  create_file:   REQUIRED=path (absolute) + content. Parent dir must exist. Git must be initialized. Set overwrite=True if exists.
  undo_edit:     Needs git history from prior edit_file. Run git_init + edit_file first if no commits.
  execute_cmd:   REQUIRED=command only. Use absolute paths for working_dir. Long-running: poll with process_id.
  git_init:      REQUIRED=path (absolute to EXISTING dir). Run BEFORE edit_file/create_file if no .git.

CHANGE MODES (edit_file)
  exact:          {"search": "old", "replace": "new"}
  whitespace_tolerant: {"search": "old", "replace": "new"}
  regex:          {"pattern": "regex", "replace": "new", "flags": 0}
  line_range:     {"start_line": 1, "end_line": 5, "replacement_content": "new"}
"""

import difflib, getpass, os, re, shutil, socket, subprocess, sys, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME_DIR = Path.home().resolve()
_DEFAULT_TAIL_LINES = int(os.environ.get("MCP_DEFAULT_TAIL_LINES", "20"))

from fastmcp import FastMCP
mcp = FastMCP("Python File Tools")

# ─── Shared Helpers ───────────────────────────────────────────────────────────

def _validate_path(path: Path) -> Path:
    """Validate path is within home directory for security."""
    resolved = Path(os.path.expanduser(str(path))).resolve()
    if not str(resolved).startswith(str(HOME_DIR)):
        raise ValueError(f"Access denied: paths outside home directory are not allowed. Requested: {resolved}")
    return resolved

def _is_git_repo(directory: Path) -> bool:
    """Walk up from directory to find a .git folder."""
    current = directory.resolve()
    while True:
        if (current / ".git").is_dir():
            return True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False

def _configure_git_user(git_repo_dir: Path) -> dict | None:
    """Configure git user.name and user.email if not already set."""
    try:
        r = subprocess.run(["git", "-C", str(git_repo_dir), "config", "user.name"],
                           capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not r.stdout.strip():
            username, hostname = getpass.getuser(), socket.gethostname()
            email = f"{username}@{hostname}"
            for key, val in [("user.name", username), ("user.email", email)]:
                if subprocess.run(["git", "-C", str(git_repo_dir), "config", key, val],
                                  capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL).returncode != 0:
                    return {"status": "error", "message": f"Failed to set git {key}"}
    except Exception as e:
        return {"status": "error", "message": f"Git user configuration failed: {e}"}
    return None

def _git_commit_file(git_repo_dir: Path, file_path: Path, message: str) -> tuple[str, dict | None]:
    """Run git add + git commit for a single file. Returns (commit_hash, error_dict)."""
    for step, cmd in [("add", ["git", "-C", str(git_repo_dir), "add", str(file_path)])]:
        if subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).returncode != 0:
            return "unknown", {"status": "error", "message": f"git {step} failed"}
    err = _configure_git_user(git_repo_dir)
    if err:
        return "unknown", err
    if subprocess.run(["git", "-C", str(git_repo_dir), "commit", "-m", message],
                       capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).returncode != 0:
        return "unknown", {"status": "error", "message": "git commit failed"}
    r = subprocess.run(["git", "-C", str(git_repo_dir), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    return (r.stdout.strip() if r.returncode == 0 else "unknown"), None

def _ts():
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _diff_output(orig, new, name):
    """Generate unified diff between original and new content."""
    lines = list(difflib.unified_diff(orig.splitlines(keepends=True), new.splitlines(keepends=True),
                                       fromfile=f"a/{name}", tofile=f"b/{name}"))
    return "".join(lines) if lines else ""

def _rs(fp, ch, changes, diff_out, st="success"):
    return {"path": str(fp), "status": st, "content_changed": bool(diff_out),
            "total_changes": len(changes), "applied_changes": changes, "diff": diff_out,
            "commit_hash": ch if diff_out else None}

def _rn(fp, changes):
    return {"path": str(fp), "status": "no_changes", "content_changed": False,
            "total_changes": len(changes), "applied_changes": changes}

def _rg(fp, msg):
    return {"path": str(fp), "status": "error", "message": msg,
            "content_changed": False, "total_changes": 0, "applied_changes": [], "diff": None, "commit_hash": None}

# ─── Mode Handlers for _apply_changes ─────────────────────────────────────────

def _handler_exact(content, change):
    """Exact string search/replace handler."""
    search, replace = change.get("search", ""), change.get("replace", "")
    if not search:
        return content + replace, {"index": change.get("index", 0), "search": search, "replace": replace, "status": "proposed", "replacements_made": 1}
    if search not in content:
        return content, {"index": change.get("index", 0), "search": search, "replace": replace, "status": "not_found", "message": "Search string not found"}
    cnt = content.count(search)
    new = content.replace(search, replace)
    made = cnt - new.count(search)
    return new, {"index": change.get("index", 0), "search": search, "replace": replace, "status": "proposed", "replacements_made": made}

def _handler_whitespace(content, change):
    """Whitespace-tolerant search/replace handler — normalizes whitespace before matching."""
    search, replace = change.get("search", ""), change.get("replace", "")
    norm = lambda t: " ".join(t.split())
    ns, nc = norm(search), norm(content)
    if not ns:
        return content + replace, {"index": change.get("index", 0), "search": search, "replace": replace, "mode": "whitespace_tolerant", "status": "proposed", "replacements_made": 1}
    if ns not in nc:
        return content, {"index": change.get("index", 0), "search": search, "replace": replace, "mode": "whitespace_tolerant", "status": "not_found", "message": "Search string (whitespace-normalized) not found"}

    def _norm_to_orig_pos(norm_pos, orig_content):
        """Convert a position in normalized content to corresponding position in original."""
        nc_count = 0
        pos = 0
        while pos < len(orig_content) and nc_count < norm_pos:
            if orig_content[pos].isspace():
                while pos < len(orig_content) and orig_content[pos].isspace():
                    pos += 1
                nc_count += 1
            else:
                nc_count += 1
                pos += 1
        return pos

    orig_parts = []
    last_end_norm = 0
    for m in re.finditer(re.escape(ns), nc):
        before_start_orig = _norm_to_orig_pos(last_end_norm, content)
        before_end_orig = _norm_to_orig_pos(m.start(), content)
        orig_parts.append(content[before_start_orig:before_end_orig])
        orig_parts.append(replace)
        last_end_norm = m.end()
    final_before_start = _norm_to_orig_pos(last_end_norm, content)
    orig_parts.append(content[final_before_start:])
    return "".join(orig_parts), {"index": change.get("index", 0), "search": search, "replace": replace, "mode": "whitespace_tolerant", "status": "proposed", "replacements_made": 1}

def _handler_regex(content, change):
    """Regex-based search/replace handler."""
    pattern = change.get("pattern") or change.get("search", "")
    flags = int(change.get("flags", "")) if change.get("flags") else 0
    replace = change.get("replace", "")
    try:
        compiled = re.compile(pattern, flags=flags)
    except Exception as e:
        return content, {"index": change.get("index", 0), "pattern": pattern, "status": "error", "message": f"Invalid regex: {e}"}
    matches = list(compiled.finditer(content))
    if not matches:
        return content, {"index": change.get("index", 0), "pattern": pattern, "status": "not_found", "message": "Pattern not found"}
    if not pattern:
        return content + replace, {"index": change.get("index", 0), "pattern": pattern, "replace": replace, "mode": "regex", "status": "proposed", "replacements_made": 1}
    return compiled.sub(replace, content), {"index": change.get("index", 0), "pattern": pattern, "replace": replace, "mode": "regex", "status": "proposed", "replacements_made": len(matches)}

def _handler_line_range(content, change):
    """Line-range replacement handler — replaces lines from start_line to end_line (1-based, inclusive)."""
    s, e = change.get("start_line"), change.get("end_line")
    repl = change.get("replacement_content", "")
    if s is None or e is None:
        return content, {"index": change.get("index", 0), "status": "error", "message": "line_range requires start_line and end_line"}
    trailing_newline = ""
    if content.endswith("\n"):
        trailing_newline = "\n"
        content_body = content[:-1]
        lines = content_body.splitlines() if content_body else []
    else:
        lines = content.splitlines()
    si, ei = max(0, s - 1), min(len(lines), e)
    if si >= len(lines):
        return content, {"index": change.get("index", 0), "start_line": s, "end_line": e, "status": "not_found",
                         "message": f"Line range [{s},{e}] beyond file ({len(lines)} lines)"}
    result = "\n".join(lines[:si] + [repl] + lines[ei:]) + trailing_newline
    return result, {"index": change.get("index", 0), "start_line": s, "end_line": e,
                    "replacement_content": repl, "mode": "line_range", "status": "proposed", "lines_replaced": ei - si}

_MODE_HANDLERS = {"exact": _handler_exact, "whitespace_tolerant": _handler_whitespace,
                  "regex": _handler_regex, "line_range": _handler_line_range}

def _apply_changes_to_content(original_content: str, changes: list) -> tuple[str, list]:
    """Apply a list of changes to content sequentially, returning new content and result list."""
    applied, content = [], original_content
    for i, change in enumerate(changes):
        change["index"] = i
        handler = _MODE_HANDLERS.get(change.get("mode", "exact"), _handler_exact)
        content, result = handler(content, change)
        applied.append(result)
    return content, applied

# ─── MCP Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def list_folder(path: str, recursive: bool = False) -> dict:
    """List directory contents. Args: path (str), recursive (bool, default False). Returns: {path, recursive, total_count, entries}."""
    d = Path(path)
    if not d.exists(): raise FileNotFoundError(f"Directory not found: {path}")
    if not d.is_dir(): raise NotADirectoryError(f"Not a directory: {path}")
    entries = [{"name": i.name, "path": str(i.relative_to(d)) if recursive else i.name,
                "type": "directory" if i.is_dir() else "file"}
               for i in (d.rglob("*") if recursive else d.iterdir())]
    return {"path": str(d.resolve()), "recursive": recursive, "total_count": len(entries), "entries": entries}

@mcp.tool()
def search_file_content(pattern: str, path: str = ".", file_pattern: str = "*",
                        max_results: int = 50, context_before: int = 0, context_after: int = 0) -> dict:
    """Search files using regex. Args: pattern, path, file_pattern, max_results, context_before, context_after. Returns: {search_pattern, search_path, file_pattern, files_checked, total_matches, context_before, context_after, results}."""
    sp = Path(path)
    if not sp.exists(): raise FileNotFoundError(f"Search path not found: {path}")
    if not sp.is_dir(): raise NotADirectoryError(f"Not a directory: {path}")
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regular expression: {e}")
    matches, files_checked = [], 0
    file_paths = sp.glob(f"**/{file_pattern}" if file_pattern != "*" else "**/*")
    for fp in file_paths:
        if max_results and len(matches) >= max_results: break
        if not fp.is_file(): continue
        files_checked += 1
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except (PermissionError, OSError): continue
        lines = text.splitlines()
        for li, line in enumerate(lines):
            if compiled.search(line):
                s, e = max(0, li - context_before), min(len(lines), li + 1 + context_after)
                ctx = [{"line_number": ci + 1, "content": lines[ci], "is_match_line": ci == li} for ci in range(s, e)]
                matches.append({"file": str(fp.relative_to(sp)), "line": li + 1,
                                "match": compiled.search(line).group(), "context": ctx})
                if max_results and len(matches) >= max_results: break
    return {"search_pattern": pattern, "search_path": str(sp.resolve()), "file_pattern": file_pattern,
            "files_checked": files_checked, "total_matches": len(matches),
            "context_before": context_before, "context_after": context_after, "results": matches}

@mcp.tool()
def read_file_content(path: str, start_line: int = None, end_line: int = None, encoding: str = "utf-8") -> dict:
    """Read file with optional line range (1-based). Args: path, start_line, end_line, encoding. Returns: {path, total_lines, start_line, end_line, content}."""
    fp = _validate_path(Path(path))
    if not fp.exists(): raise FileNotFoundError(f"File not found: {fp}")
    if not fp.is_file(): raise IsADirectoryError(f"Not a file: {fp}")
    content = fp.read_text(encoding=encoding)
    lines = content.splitlines()
    si = max(0, start_line - 1) if start_line is not None else 0
    ei = min(len(lines), end_line) if end_line is not None else len(lines)
    return {"path": str(fp), "total_lines": len(lines), "start_line": start_line, "end_line": end_line,
            "content": "\n".join(lines[si:ei])}

@mcp.tool()
def edit_file(path: str, changes: list, encoding: str = "utf-8", git_dir: str = None) -> dict:
    """Apply edits to a file and commit to git. Each change is a dict with 'mode' key:
       exact: {"search": "old", "replace": "new"} | whitespace_tolerant: {"search": "old", "replace": "new"}
       regex: {"pattern": "regex", "replace": "new", "flags": 0} | line_range: {"start_line": 1, "end_line": 5, "replacement_content": "new"}
       Args: path, changes (list of dicts), encoding, git_dir. Returns: {path, status, content_changed, total_changes, applied_changes, diff, commit_hash, message}."""
    fp = _validate_path(Path(path))
    if not fp.exists(): raise FileNotFoundError(f"File not found: {fp}")
    if not fp.is_file(): raise IsADirectoryError(f"Not a file: {fp}")
    orig = fp.read_text(encoding=encoding)
    new, applied = _apply_changes_to_content(orig, changes)
    if new != orig:
        git_repo = Path(git_dir).resolve() if git_dir else fp.parent.resolve()
        if not _is_git_repo(git_repo):
            return _rg(fp, f"No git repository found for '{fp}'. Initialize git first.")
        diff_out = _diff_output(orig, new, fp.name)
        fp.write_text(new, encoding=encoding)
        commit_msg = f"{_ts()} - edit_file: {len([c for c in applied if c.get('status')=='proposed'])} change(s) applied"
        commit_hash, err = _git_commit_file(git_repo, fp, commit_msg)
        if err:
            return {**_rg(fp, err["message"]), "applied_changes": applied, "diff": diff_out}
        return _rs(fp, commit_hash, applied, diff_out)
    return _rn(fp, applied)

@mcp.tool()
def undo_edit(path: str, steps: int = 1) -> dict:
    """Revert file to N git revisions back. Args: path, steps (default 1). Returns: {path, status, steps_reverted, commit_hash, commit_message, message}."""
    fp = _validate_path(Path(path))
    if not fp.exists(): raise FileNotFoundError(f"File not found: {fp}")
    parent = fp.parent.resolve()
    try:
        r = subprocess.run(["git", "-C", str(parent), "checkout", f"HEAD~{steps}", "--", str(fp)],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return {"path": str(fp), "status": "error", "message": f"git checkout failed: {r.stderr.strip()}",
                    "steps_reverted": 0, "commit_hash": None, "commit_message": None}
        msg = f"{_ts()} - undo_edit: reverted {steps} step(s)"
        err = _configure_git_user(parent)
        if err:
            return {"path": str(fp), "status": "error", "message": err["message"],
                    "steps_reverted": 0, "commit_hash": None, "commit_message": None}
        for cmd in [["git", "-C", str(parent), "add", str(fp)], ["git", "-C", str(parent), "commit", "-m", msg]]:
            if subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).returncode != 0:
                return {"path": str(fp), "status": "error", "message": "git operation failed",
                        "steps_reverted": 0, "commit_hash": None, "commit_message": None}
        rh = subprocess.run(["git", "-C", str(parent), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        ch = rh.stdout.strip() if rh.returncode == 0 else "unknown"
    except subprocess.TimeoutExpired:
        return {"path": str(fp), "status": "error", "message": "Git operation timed out",
                "steps_reverted": 0, "commit_hash": None, "commit_message": None}
    except FileNotFoundError:
        return {"path": str(fp), "status": "error", "message": "git is not installed or not in PATH",
                "steps_reverted": 0, "commit_hash": None, "commit_message": None}
    return {"path": str(fp), "status": "undone", "steps_reverted": steps, "commit_hash": ch, "commit_message": msg}

@mcp.tool()
def preview_undo(path: str, steps: int = 1) -> dict:
    """Preview git diff before undo. Args: path, steps (default 1). Returns: {path, steps, diff, status, message}."""
    fp = _validate_path(Path(path))
    if not fp.exists(): raise FileNotFoundError(f"File not found: {fp}")
    try:
        for cmd in [["git", "diff", f"HEAD~{steps}", "HEAD", "--", str(fp)], ["git", "diff", f"HEAD~{steps}", "--", str(fp)]]:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                diff_out = r.stdout.strip(); break
        else:
            diff_out = ""
    except subprocess.TimeoutExpired:
        return {"path": str(fp), "status": "error", "message": "Git operation timed out", "steps": steps, "diff": ""}
    except FileNotFoundError:
        return {"path": str(fp), "status": "error", "message": "git is not installed or not in PATH", "steps": steps, "diff": ""}
    return {"path": str(fp), "steps": steps, "diff": diff_out,
            "status": "preview_ready" if diff_out else "no_changes_found"}

@mcp.tool()
def create_file(path: str, content: str, overwrite: bool = False, encoding: str = "utf-8", git_dir: str = None) -> dict:
    """Create new file and commit to git. Args: path, content, overwrite (default False), encoding, git_dir. Returns: {path, status, content_changed, total_changes, applied_changes, diff, commit_hash, message}."""
    fp = _validate_path(Path(path))
    if not fp.parent.exists():
        return {"path": str(fp), "status": "error", "message": f"Parent directory does not exist: {fp.parent}",
                "content_changed": False, "total_changes": 0, "applied_changes": [], "diff": None, "commit_hash": None}
    git_repo = Path(git_dir).resolve() if git_dir else fp.parent.resolve()
    if not _is_git_repo(git_repo):
        return _rg(fp, f"No git repository found for '{fp}'. Initialize git first.")
    exists = fp.exists()
    if exists and not overwrite:
        return {"path": str(fp), "status": "exists", "content_changed": False, "total_changes": 0,
                "applied_changes": [], "diff": None, "commit_hash": None,
                "message": f"File already exists: {fp}. Set overwrite=True."}
    commit_msg = f"{_ts()} - create_file: {'overwrite' if exists else 'create'}"
    fp.write_text(content, encoding=encoding)
    commit_hash, err = _git_commit_file(git_repo, fp, commit_msg)
    if err:
        return {"path": str(fp), "status": "error", "message": err["message"],
                "content_changed": False, "total_changes": 0, "applied_changes": [], "diff": None, "commit_hash": None}
    status = "created" if not exists else "overwritten"
    return _rs(fp, commit_hash if not exists else None,
               [{"index": 0, "status": status, "replacements_made": 1}],
               f"Created file: {fp.name}" if not exists else f"Overwritten: {fp.name}")

# ─── Process Manager ──────────────────────────────────────────────────────────

_process_registry: dict[str, dict[str, Any]] = {}
_process_history: list[dict[str, Any]] = []
_MAX_HISTORY = 10

def _add_history(entry):
    """Add a process entry to the history list, keeping only the last _MAX_HISTORY entries."""
    _process_history.insert(0, entry)
    if len(_process_history) > _MAX_HISTORY:
        del _process_history[_MAX_HISTORY:]

def _read_output_file(ofile):
    """Read the output file and return (stdout, stderr) tuple."""
    try:
        with open(ofile, 'r', encoding='utf-8', errors='replace') as f:
            return f.read(), ''
    except FileNotFoundError:
        return '', ''

def _get_output(pid):
    """Get stdout and stderr for a process."""
    if pid not in _process_registry:
        return "", ""
    e = _process_registry[pid]
    if e.get("is_visible"):
        fs, fe = e.get("_final_stdout"), e.get("_final_stderr")
        if fs is not None or fe is not None:
            return fs or "", fe or ""
        return _read_output_file(e["output_file"])
    sm, em = ''.join(e.get("stdout_chunks", [])), ''.join(e.get("stderr_chunks", []))
    try:
        with open(e["output_file"], 'r', encoding='utf-8') as f:
            content = f.read()
        fs, fe = '', ''
        for line in content.splitlines():
            if line.startswith("[STDOUT]"): fs += line[8:]
            elif line.startswith("[STDERR]"): fe += line[9:]
    except FileNotFoundError:
        fs, fe = '', ''
    return (sm if sm else fs), (em if em else fe)

def _tail(text, n):
    """Return (tail_text, total_lines, returned_lines) — last n lines of text with line counts."""
    if n <= 0: return text, len(text.splitlines()) if text else 0, len(text.splitlines()) if text else 0
    lines = text.splitlines()
    t = len(lines)
    if t <= n: return text, t, t
    return '\n'.join(lines[-n:]), t, n

def _visible_args(command, output_file):
    """Build command to launch a visible terminal window for the given shell command."""
    if sys.platform == "win32":
        tc = command.replace('"', "'")
        return ["powershell", "-NoProfile", "-Command", f"{tc} 2>&1 | Tee-Object -FilePath \"{output_file}\"; Start-Sleep -Seconds 5"], subprocess.CREATE_NEW_CONSOLE
    if sys.platform == "darwin":
        sc = f'tell application "Terminal" to do script "{command} 2>&1 | tee {output_file}; sleep 5"'
        return ["osascript", "-e", sc], 0
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not display: return None, 0
    wc = f'{command} 2>&1 | tee {output_file}; sleep 5'
    for name, cmd in [("gnome-terminal", ["gnome-terminal", "--", "sh", "-c", wc]),
                      ("konsole", ["konsole", "-e", "sh", "-c", wc]),
                      ("xterm", ["xterm", "-e", "sh", "-c", wc]),
                      ("xfce4-terminal", ["xfce4-terminal", "--", "sh", "-c", wc]),
                      ("lxterminal", ["lxterminal", "-e", "sh", "-c", wc]),
                      ("mate-terminal", ["mate-terminal", "-e", "sh", "-c", wc])]:
        if subprocess.run(["which", name], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0:
            return cmd, 0
    return None, 0

def _monitor_visible(pid, ofile):
    """Background thread: wait for a visible process to finish, then capture its output."""
    try:
        e = _process_registry.get(pid)
        if not e: return
        while e["process"].poll() is None: time.sleep(0.5)
        poll = e["process"].poll(); time.sleep(1)
        so, se = _read_output_file(ofile)
        if pid in _process_registry:
            _process_registry[pid]["_final_stdout"] = so; _process_registry[pid]["_final_stderr"] = se
            _process_registry[pid]["_final_return_code"] = poll
        _add_history({"process_id": pid, "command": e["command"], "return_code": poll,
                      "start_time": e.get("start_time"), "end_time": time.time(), "stdout": so, "stderr": se})
    except Exception: pass

def _get_latest_process_id():
    """Get the process_id of the most recently started process from registry or history."""
    if _process_registry:
        return next(reversed(_process_registry))
    if _process_history:
        return _process_history[0].get("process_id")
    return None

def _make_response(cmd, wd, pid, ofile, proc, start_time, wait_time, visible, show_output, extra_err=None):
    """Build a unified response dict for execute_command."""
    end, et = time.time(), time.time() - start_time
    pr = proc.poll() if proc else None
    if extra_err:
        so, se = _read_output_file(ofile) if ofile else ("", "")
        if extra_err.get("timeout"):
            se = extra_err.get("stderr", "") + f"\n[TIMEOUT] Command timed out after {extra_err.get('timeout_val', '')} seconds"
        sp, st, sr = _tail(so, 50) if show_output else (so, len(so.splitlines()) if so else 0, len(so.splitlines()) if so else 0)
        ep, et2, er = _tail(se, 50) if show_output else (se, len(se.splitlines()) if se else 0, len(se.splitlines()) if se else 0)
        return {"command": cmd, "working_dir": os.path.abspath(wd), "process_id": pid, "output_file": ofile,
                "return_code": -1, "stdout": sp, "stderr": ep, "success": False, "is_complete": True,
                "execution_time_seconds": round(et, 3), "visible": False, "show_output": show_output,
                "output_summary": {"stdout": {"total_lines": st, "returned_lines": sr, "lines_omitted": st - sr},
                                   "stderr": {"total_lines": et2, "returned_lines": er, "lines_omitted": et2 - er}}}
    so, se = _get_output(pid) if pid else ("", "")
    sp, st, sr = _tail(so, 50) if show_output else (so, len(so.splitlines()) if so else 0, len(so.splitlines()) if so else 0)
    ep, et2, er = _tail(se, 50) if show_output else (se, len(se.splitlines()) if se else 0, len(se.splitlines()) if se else 0)
    return {"command": cmd, "working_dir": os.path.abspath(wd), "process_id": pid, "output_file": ofile,
            "return_code": pr if pr is not None else None, "stdout": sp, "stderr": ep,
            "success": (pr == 0) if pr is not None else None, "is_complete": pr is not None,
            "execution_time_seconds": round(et, 3), "recommended_wait_time": wait_time,
            "visible": visible, "show_output": show_output,
            "output_summary": {"stdout": {"total_lines": st, "returned_lines": sr, "lines_omitted": st - sr},
                               "stderr": {"total_lines": et2, "returned_lines": er, "lines_omitted": et2 - er}}}

@mcp.tool()
def execute_command(command: str, working_dir: str = ".", timeout: int = 300, wait_time: int = 4,
                    visible: bool = False, show_output: bool = False) -> dict:
    """Execute a shell command asynchronously. Returns process_id for use with get_command_output(), check_progress(), kill_process().
       By default, commands run in hidden mode (no terminal window). Use visible=True for interactive GUI apps.
       For fast commands, returns full output immediately with "instant_feedback": True.
       Args: command, working_dir, timeout (default 300), wait_time (default 4), visible (default False), show_output (default False).
       Returns: {command, working_dir, process_id, output_file, return_code, stdout, stderr, success, is_complete, execution_time_seconds, recommended_wait_time, visible, show_output, instant_feedback, output_summary}."""
    start_time = time.time()
    pid = str(uuid.uuid4())
    ofile = f"/tmp/mcp_cmd_output_{pid}.txt"
    eff_timeout = max(timeout, wait_time)
    try:
        use_visible, proc = False, None
        if visible:
            args, flags = _visible_args(command, ofile)
            if args is not None: use_visible = True
        if not use_visible:
            proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=working_dir, stdin=subprocess.DEVNULL, universal_newlines=True)
        else:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    cwd=working_dir, stdin=subprocess.DEVNULL, universal_newlines=True, creationflags=flags)
        _process_registry[pid] = {"process": proc, "output_file": ofile, "command": command,
                                   "start_time": start_time, "effective_timeout": eff_timeout,
                                   "is_visible": use_visible, "stdout_chunks": [], "stderr_chunks": []}
        if not use_visible:
            def reader(pipe, key):
                try:
                    while True:
                        l = (proc.stdout if key == "stdout" else proc.stderr).readline()
                        if l == '': break
                        _process_registry[pid][key + "_chunks"].append(l)
                except Exception: pass
            _stdout_thread = threading.Thread(target=reader, args=("", "stdout"), daemon=True)
            _stderr_thread = threading.Thread(target=reader, args=("", "stderr"), daemon=True)
            _stdout_thread.start()
            _stderr_thread.start()
        else:
            threading.Thread(target=_monitor_visible, args=(pid, ofile), daemon=True).start()

        # Instant feedback for fast commands: wait briefly and check if complete
        if not use_visible:
            time.sleep(0.1)
            pr = proc.poll()
            if pr is not None:
                _stdout_thread.join(timeout=1.0)
                _stderr_thread.join(timeout=1.0)
                so = ''.join(_process_registry[pid].get("stdout_chunks", []))
                se = ''.join(_process_registry[pid].get("stderr_chunks", []))
                _add_history({"process_id": pid, "command": command, "return_code": pr,
                              "start_time": start_time, "end_time": time.time(), "stdout": so, "stderr": se})
                del _process_registry[pid]
                et = time.time() - start_time
                sp, st, sr = _tail(so, 50)
                ep, et2, er = _tail(se, 50)
                return {"command": command, "working_dir": os.path.abspath(working_dir), "process_id": pid,
                        "output_file": ofile, "return_code": pr, "stdout": sp, "stderr": ep,
                        "success": (pr == 0), "is_complete": True,
                        "execution_time_seconds": round(et, 3), "recommended_wait_time": 0,
                        "visible": False, "show_output": show_output, "instant_feedback": True,
                        "output_summary": {"stdout": {"total_lines": st, "returned_lines": sr, "lines_omitted": st - sr},
                                           "stderr": {"total_lines": et2, "returned_lines": er, "lines_omitted": et2 - er}}}

        return _make_response(command, working_dir, pid, ofile, proc, start_time, wait_time, use_visible, show_output)
    except subprocess.TimeoutExpired:
        try: proc.kill()
        except (ProcessLookupError, AttributeError): pass
        return _make_response(command, working_dir, pid, ofile, None, start_time, wait_time, False, show_output,
                              {"timeout": True, "timeout_val": eff_timeout})
    except Exception as e:
        return _make_response(command, working_dir, pid, ofile, None, start_time, wait_time, False, show_output,
                              {"stderr": f"Error executing command: {e}"})

@mcp.tool()
def get_command_output(process_id: str = None, wait_time: int = 4, tail: int = None,
                       wait_for_completion: bool = False, timeout: int = 10) -> dict:
    """Get output from a previously started command.
       Auto process_id: If not provided, uses the most recent process from registry/history.
       wait_for_completion=True: Blocks until process finishes (up to timeout seconds).
       Args: process_id (optional), wait_time (default 4), tail (default from env), wait_for_completion (default False), timeout (default 10).
       Returns: {process_id, inferred_process_id, stdout, stderr, success, is_complete, status, return_code, last_updated, recommended_wait_time, output_summary, message, waited_seconds, completed_within_timeout}."""
    tail = tail if tail is not None else _DEFAULT_TAIL_LINES
    inferred = None
    if not process_id:
        process_id = _get_latest_process_id()
        if process_id is None:
            return {"stdout": "", "stderr": "", "success": False, "is_complete": False,
                    "message": "No running or completed processes found. Run execute_command first."}
        inferred = process_id

    # Blocking wait mode
    if wait_for_completion:
        start_wait = time.time()
        while (time.time() - start_wait) < timeout:
            if process_id in _process_registry:
                e = _process_registry[process_id]
                pr = e["process"].poll()
                if pr is not None:
                    so, se = _get_output(process_id)
                    sr, st, srt = _tail(so, tail); er, et, ert = _tail(se, tail)
                    w = round(time.time() - start_wait, 2)
                    return {"process_id": process_id, "inferred_process_id": inferred, "stdout": sr, "stderr": er,
                            "success": (pr == 0), "is_complete": True,
                            "status": "completed" if pr == 0 else "failed",
                            "return_code": pr, "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "recommended_wait_time": 0, "waited_seconds": w, "completed_within_timeout": True,
                            "output_summary": {"stdout": {"total_lines": st, "returned_lines": srt, "lines_omitted": st - srt},
                                               "stderr": {"total_lines": et, "returned_lines": ert, "lines_omitted": et - ert}}}
                time.sleep(0.5)
                continue
            elif any(e.get("process_id") == process_id for e in _process_history):
                for e in _process_history:
                    if e.get("process_id") == process_id:
                        so, se = e.get("stdout", ""), e.get("stderr", "")
                        sr, st, srt = _tail(so, tail); er, et, ert = _tail(se, tail)
                        w = round(time.time() - start_wait, 2)
                        return {"process_id": process_id, "inferred_process_id": inferred, "stdout": sr, "stderr": er,
                                "success": e.get("return_code") == 0, "is_complete": True,
                                "status": "completed" if e.get("return_code") == 0 else "failed",
                                "return_code": e.get("return_code"),
                                "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "recommended_wait_time": 0, "waited_seconds": w, "completed_within_timeout": True,
                                "output_summary": {"stdout": {"total_lines": st, "returned_lines": srt, "lines_omitted": st - srt},
                                                   "stderr": {"total_lines": et, "returned_lines": ert, "lines_omitted": et - ert}}}
            else:
                w = round(time.time() - start_wait, 2)
                return {"process_id": process_id, "inferred_process_id": inferred, "stdout": "", "stderr": "",
                        "success": None, "is_complete": True, "status": "not_found",
                        "message": "Process was removed from registry.",
                        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "recommended_wait_time": 0, "waited_seconds": w, "completed_within_timeout": False,
                        "output_summary": {"stdout": {"total_lines": 0, "returned_lines": 0, "lines_omitted": 0},
                                           "stderr": {"total_lines": 0, "returned_lines": 0, "lines_omitted": 0}}}
        # Timeout reached
        if process_id in _process_registry:
            e = _process_registry[process_id]
            pr = e["process"].poll()
            if pr is not None:
                so, se = _get_output(process_id)
                sr, st, srt = _tail(so, tail); er, et, ert = _tail(se, tail)
                w = round(time.time() - start_wait, 2)
                return {"process_id": process_id, "inferred_process_id": inferred, "stdout": sr, "stderr": er,
                        "success": (pr == 0), "is_complete": True,
                        "status": "completed" if pr == 0 else "failed",
                        "return_code": pr, "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "recommended_wait_time": 0, "waited_seconds": w, "completed_within_timeout": True,
                        "output_summary": {"stdout": {"total_lines": st, "returned_lines": srt, "lines_omitted": st - srt},
                                           "stderr": {"total_lines": et, "returned_lines": ert, "lines_omitted": et - ert}}}
            so, se = _get_output(process_id)
            sr, st, srt = _tail(so, tail); er, et, ert = _tail(se, tail)
            w = round(time.time() - start_wait, 2)
            return {"process_id": process_id, "inferred_process_id": inferred, "stdout": sr, "stderr": er,
                    "success": None, "is_complete": False, "status": "running",
                    "return_code": None, "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "recommended_wait_time": wait_time, "waited_seconds": w, "completed_within_timeout": False,
                    "message": "Timeout reached. Process still running.",
                    "output_summary": {"stdout": {"total_lines": st, "returned_lines": srt, "lines_omitted": st - srt},
                                       "stderr": {"total_lines": et, "returned_lines": ert, "lines_omitted": et - ert}}}

    # Normal polling mode
    for e in _process_history:
        if e["process_id"] == process_id:
            so, se = e.get("stdout", ""), e.get("stderr", "")
            sr, st, srt = _tail(so, tail); er, et, ert = _tail(se, tail)
            return {"process_id": process_id, "inferred_process_id": inferred, "stdout": sr, "stderr": er,
                    "success": e.get("return_code") == 0, "is_complete": True,
                    "status": "completed" if e.get("return_code") == 0 else "failed",
                    "return_code": e.get("return_code"), "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "recommended_wait_time": wait_time,
                    "output_summary": {"stdout": {"total_lines": st, "returned_lines": srt, "lines_omitted": st - srt},
                                       "stderr": {"total_lines": et, "returned_lines": ert, "lines_omitted": et - ert}}}
    if process_id not in _process_registry:
        return {"process_id": process_id, "inferred_process_id": inferred, "stdout": "", "stderr": "",
                "success": False, "is_complete": True, "status": "not_found",
                "message": f"Process '{process_id}' not found.",
                "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "recommended_wait_time": wait_time}
    e, proc, pr = _process_registry[process_id], _process_registry[process_id]["process"], _process_registry[process_id]["process"].poll()
    ic = pr is not None; so, se = _get_output(process_id)
    sr, st, srt = _tail(so, tail); er, et, ert = _tail(se, tail)
    return {"process_id": process_id, "inferred_process_id": inferred, "stdout": sr, "stderr": er,
            "success": (pr == 0) if ic else None, "is_complete": ic,
            "status": "completed" if (ic and pr == 0) else ("failed" if ic and pr is not None else "running"),
            "return_code": pr if ic else None, "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "recommended_wait_time": wait_time,
            "output_summary": {"stdout": {"total_lines": st, "returned_lines": srt, "lines_omitted": st - srt},
                               "stderr": {"total_lines": et, "returned_lines": ert, "lines_omitted": et - ert}}}

@mcp.tool()
def check_progress(process_id: str) -> dict:
    """Check if a command is still running. Args: process_id. Returns: {process_id, command, is_running, return_code, output_file, current_output_length, status, message}."""
    for e in _process_history:
        if e["process_id"] == process_id:
            return {"process_id": process_id, "command": e.get("command", ""), "is_running": False,
                    "return_code": e.get("return_code"), "status": "completed" if e.get("return_code") == 0 else "failed",
                    "output_file": e.get("output_file"),
                    "current_output_length": len(e.get("stdout", "")) + len(e.get("stderr", ""))}
    if process_id not in _process_registry:
        return {"status": "error", "message": f"No process found for ID '{process_id}'."}
    e, proc, pr = _process_registry[process_id], _process_registry[process_id]["process"], _process_registry[process_id]["process"].poll()
    ir = pr is None; so, se = _get_output(process_id)
    return {"process_id": process_id, "command": e["command"], "is_running": ir,
            "return_code": pr if not ir else None, "output_file": e["output_file"],
            "current_output_length": len(so) + len(se),
            "status": "running" if ir else ("completed" if pr == 0 else "failed")}

@mcp.tool()
def get_command_history(limit: int = 10) -> dict:
    """List recent command executions. Args: limit (default 10). Returns: {total_count, history}."""
    completed = [{"process_id": pid, "command": e["command"],
                  "status": "completed" if e.get("return_code") == 0 else ("failed" if e.get("return_code") is not None else "unknown"),
                  "start_time": e.get("start_time")}
                 for pid, e in _process_registry.items() if e["process"].poll() is not None]
    all_e = {}
    for h in _process_history: all_e[h["process_id"]] = h
    for c in completed:
        if c["process_id"] not in all_e: all_e[c["process_id"]] = c
    sorted_e = sorted(all_e.values(), key=lambda x: x.get("start_time") or 0, reverse=True)[:limit]
    return {"total_count": len(sorted_e), "history": sorted_e}

@mcp.tool()
def kill_process(process_id: str) -> dict:
    """Kill a running process. Sends SIGTERM first, then SIGKILL after 3s. Args: process_id. Returns: {status, killed_process_id, message}."""
    if process_id not in _process_registry:
        return {"status": "error", "message": f"No running process found for ID '{process_id}'."}
    e, proc = _process_registry[process_id], _process_registry[process_id]["process"]
    try:
        try:
            proc.terminate(); proc.wait(timeout=3); sm = "terminated"
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(); sm = "killed with SIGKILL"
        fs = ''.join(e.get("stdout_chunks", [])); fe = ''.join(e.get("stderr_chunks", []))
        if e.get("is_visible", False):
            fs, fe = _read_output_file(e["output_file"]) if not fs else (fs, ''.join(e.get("stderr_chunks", [])))
        _add_history({"process_id": process_id, "command": e["command"],
                      "return_code": getattr(proc, 'returncode', None), "start_time": e.get("start_time"),
                      "end_time": time.time(), "stdout": fs, "stderr": fe})
        del _process_registry[process_id]
        return {"status": "success", "killed_process_id": process_id,
                "message": f"Process '{e['command']}' was {sm}."}
    except Exception as ex:
        return {"status": "error", "killed_process_id": process_id, "message": f"Error killing process: {ex}"}

@mcp.tool()
def wait_for_process(process_id: str = None, wait_time: int = 10) -> dict:
    """Wait for a process to complete (blocking). Args: process_id (optional), wait_time (default 10). Returns: {status, waited_seconds, process_id, process_completed, command, return_code, success, status_msg, message}."""
    start, elapsed = time.time(), 0.0
    if process_id:
        while elapsed < wait_time:
            if process_id not in _process_registry:
                return {"status": "success", "waited_seconds": round(time.time() - start, 2),
                        "process_id": process_id, "process_completed": True, "message": "Process was removed from registry."}
            e, pr = _process_registry[process_id], _process_registry[process_id]["process"].poll()
            if pr is not None:
                return {"status": "success", "waited_seconds": round(time.time() - start, 2),
                        "process_id": process_id, "command": e["command"], "return_code": pr,
                        "process_completed": True, "success": pr == 0,
                        "status_msg": "completed" if pr == 0 else "failed"}
            time.sleep(1); elapsed = time.time() - start
        return {"status": "success", "waited_seconds": round(time.time() - start, 2),
                "process_id": process_id, "command": e["command"], "process_completed": False,
                "message": "Wait time elapsed. Process is still running."}
    time.sleep(wait_time)
    return {"status": "success", "waited_seconds": round(time.time() - start, 2),
            "message": f"Waited {round(time.time() - start, 2)} seconds."}

@mcp.tool()
def execute_command_with_terminal(command: str, working_dir: str = ".", timeout: int = 300, wait_time: int = 4, show_output: bool = False) -> dict:
    """Execute a command in a visible terminal window.
       For sudo commands and interactive prompts that require user input.
       Always runs with visible=True. Falls back to hidden mode if no display available.
       Args: command, working_dir, timeout (default 300), wait_time (default 4), show_output (default False).
       Returns: Same as execute_command with visible=True."""
    return execute_command(command=command, working_dir=working_dir, timeout=timeout,
                           wait_time=wait_time, visible=True, show_output=show_output)


@mcp.tool()
def git_init(path: str, overwrite: bool = False) -> dict:
    """Initialize git repository. Args: path, overwrite (default False). Returns: {path, status, message}."""
    d = _validate_path(Path(path))
    if not d.exists(): return {"path": str(d), "status": "error", "message": f"Directory does not exist: {d}"}
    if not d.is_dir(): return {"path": str(d), "status": "error", "message": f"Not a directory: {d}"}
    gd = d / ".git"
    if gd.exists() and not overwrite:
        return {"path": str(d), "status": "exists", "content_changed": False, "total_changes": 0,
                "message": f"A git repository already exists at '{d}'. Set overwrite=True to reinitialize."}
    try:
        if gd.exists(): shutil.rmtree(str(gd))
        r = subprocess.run(["git", "init", str(d)], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return {"path": str(d), "status": "error", "message": f"git init failed: {r.stderr.strip()}"}
        return {"path": str(d), "status": "success", "message": f"Git repository initialized at '{d}'."}
    except subprocess.TimeoutExpired:
        return {"path": str(d), "status": "error", "message": "git init timed out"}

if __name__ == "__main__":
    mcp.run()