"""Standalone test for edit_file with 4 search modes. (Outdated
)"""
import re as _re
from pathlib import Path


def edit_file(path, changes, encoding="utf-8", dry_run=False):
    """Edit a file by applying a series of search/replace operations.

    Supports four search modes specified per change object via the 'mode' field (defaults to 'exact'):
      1. 'exact'   - Standard exact string matching (default). Uses `search` and `replace` fields.
      2. 'whitespace_tolerant' - Ignores differences in whitespace (spaces, tabs, newlines).
                                  Normalizes all whitespace sequences to a single space for comparison.
                                  Uses `search` and `replace` fields.
      3. 'regex'   - Treats the search string as a regular expression pattern.
                      Supports back-references in replace via \\1, \\2, etc.
                      Uses `pattern` (or `search`) and optional `flags` fields.
      4. 'line_range' - Operates on line number ranges instead of text content.
                        Each change must include: start_line, end_line, replacement_content.
    """
    file_path = Path(path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    original_content = file_path.read_text(encoding=encoding)
    applied_changes = []
    current_content = original_content

    for i, change in enumerate(changes):
        mode = change.get("mode", "exact")

        # ── Mode 2: whitespace_tolerant ──────────────
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

            norm_replace = _normalize_ws(replace_str)
            parts = []
            last_end = 0
            for m in _re.finditer(_re.escape(norm_search), norm_content):
                parts.append(current_content[last_end:m.end()])
                parts.append(norm_replace)
                last_end = m.end()
            parts.append(current_content[last_end:])
            current_content = "".join(parts)

            applied_changes.append({
                "index": i,
                "search": search_str,
                "replace": replace_str,
                "mode": mode,
                "status": "applied" if not dry_run else "would_apply",
                "replacements_made": 1,
            })

        # ── Mode 3: regex ──────────────
        elif mode == "regex":
            pattern = change.get("pattern") or change.get("search", "")
            flags_str = change.get("flags", "")
            replace_str = change.get("replace", "")

            try:
                compiled = _re.compile(pattern, flags=int(flags_str) if flags_str else 0)
            except Exception as e:
                applied_changes.append({
                    "index": i,
                    "pattern": pattern,
                    "status": "error",
                    "message": f"Invalid regex pattern: {e}",
                })
                continue

            matches = list(compiled.finditer(current_content))
            if not matches:
                applied_changes.append({
                    "index": i,
                    "pattern": pattern,
                    "status": "not_found",
                    "message": "Pattern not found in file",
                })
                continue

            current_content = compiled.sub(replace_str, current_content)
            applied_changes.append({
                "index": i,
                "pattern": pattern,
                "replace": replace_str,
                "mode": mode,
                "status": "applied" if not dry_run else "would_apply",
                "replacements_made": len(matches),
            })

        # ── Mode 4: line_range ──────────────
        elif mode == "line_range":
            start_line = change.get("start_line")
            end_line = change.get("end_line")
            replacement_content = change.get("replacement_content", "")

            if start_line is None or end_line is None:
                applied_changes.append({
                    "index": i,
                    "status": "error",
                    "message": "'line_range' mode requires 'start_line' and 'end_line (1-indexed)",
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
                "status": "applied" if not dry_run else "would_apply",
                "lines_replaced": e_idx - s_idx,
            })

        # ── Mode 1: exact (default) ──────────────
        else:
            search_str = change.get("search", "")
            replace_str = change.get("replace", "")

            if search_str not in current_content:
                applied_changes.append({
                    "index": i,
                    "search": search_str,
                    "replace": replace_str,
                    "status": "not_found",
                    "message": f"Search string not found in file",
                })
                continue

            old_count = current_content.count(search_str)
            new_content_after_replace = current_content.replace(search_str, replace_str)
            replacements_made = old_count - (new_content_after_replace.count(search_str))

            applied_changes.append({
                "index": i,
                "search": search_str,
                "replace": replace_str,
                "status": "applied" if not dry_run else "would_apply",
                "replacements_made": replacements_made,
            })
            current_content = new_content_after_replace

    if not dry_run and current_content != original_content:
        file_path.write_text(current_content, encoding=encoding)

    return {
        "path": str(file_path),
        "dry_run": dry_run,
        "content_changed": current_content != original_content,
        "total_changes": len(changes),
        "applied_changes": applied_changes,
    }


def _test():
    import json

    # === TEST 1: Exact mode (default) ===
    print("=" * 60)
    print("TEST 1: Exact Mode (Default)")
    print("=" * 60)
    with open("/tmp/test_exact.txt", "w") as f:
        f.write("Hello World\nThis is a test.\nFoo bar baz\n")

    result = edit_file(
        "/tmp/test_exact.txt",
        [{"search": "World", "replace": "Earth"}]
    )
    print(json.dumps(result, indent=2))
    with open("/tmp/test_exact.txt", "r") as f:
        print("File content:", repr(f.read()))

    # === TEST 2: Whitespace Tolerant Mode ===
    print("\n" + "=" * 60)
    print("TEST 2: Whitespace Tolerant Mode")
    print("=" * 60)
    with open("/tmp/test_ws.txt", "w") as f:
        f.write("Hello   World\nThis is a test.\nFoo bar baz\n")

    result = edit_file(
        "/tmp/test_ws.txt",
        [{"search": "Hello World", "replace": "Hi Earth", "mode": "whitespace_tolerant"}]
    )
    print(json.dumps(result, indent=2))
    with open("/tmp/test_ws.txt", "r") as f:
        print("File content:", repr(f.read()))

    # === TEST 3: Regex Mode ===
    print("\n" + "=" * 60)
    print("TEST 3: Regex Mode")
    print("=" * 60)
    with open("/tmp/test_regex.txt", "w") as f:
        f.write("Hello World\nThis is a test.\nFoo bar baz\n")

    result = edit_file(
        "/tmp/test_regex.txt",
        [{"pattern": r"(\w+) (\w+)", "replace": r"\2 \1", "mode": "regex"}]
    )
    print(json.dumps(result, indent=2))
    with open("/tmp/test_regex.txt", "r") as f:
        print("File content:", repr(f.read()))

    # === TEST 4: Line Range Mode ===
    print("\n" + "=" * 60)
    print("TEST 4: Line Range Mode")
    print("=" * 60)
    with open("/tmp/test_linerange.txt", "w") as f:
        f.write("Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n")

    result = edit_file(
        "/tmp/test_linerange.txt",
        [{"start_line": 2, "end_line": 4, "replacement_content": "REPLACED LINE", "mode": "line_range"}]
    )
    print(json.dumps(result, indent=2))
    with open("/tmp/test_linerange.txt", "r") as f:
        print("File content:", repr(f.read()))

    # === TEST 5: Dry Run ===
    print("\n" + "=" * 60)
    print("TEST 5: Dry Run (exact mode)")
    print("=" * 60)
    with open("/tmp/test_dryrun.txt", "w") as f:
        f.write("Hello World\nThis is a test.\nFoo bar baz\n")

    result = edit_file(
        "/tmp/test_dryrun.txt",
        [{"search": "World", "replace": "Earth"}],
        dry_run=True
    )
    print(json.dumps(result, indent=2))
    with open("/tmp/test_dryrun.txt", "r") as f:
        print("File content (should be unchanged):", repr(f.read()))

    # === TEST 6: Multiple changes in one call ===
    print("\n" + "=" * 60)
    print("TEST 6: Multiple Changes (mixed modes)")
    print("=" * 60)
    with open("/tmp/test_multi.txt", "w") as f:
        f.write("Hello World\nFoo bar baz\nLine 3\nLine 4\n")

    result = edit_file(
        "/tmp/test_multi.txt",
        [
            {"search": "World", "replace": "Earth"},
            {"pattern": r"(\w+) (\w+)", "replace": r"\2 \1", "mode": "regex"},
            {"start_line": 3, "end_line": 4, "replacement_content": "REPLACED", "mode": "line_range"},
        ]
    )
    print(json.dumps(result, indent=2))
    with open("/tmp/test_multi.txt", "r") as f:
        print("File content:", repr(f.read()))


if __name__ == "__main__":
    _test()