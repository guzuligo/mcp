# lmnotes — Debug / Reference Notes

> This file is a **persistent reference** for the lmnotes codebase. It exists so that
> analysis and decisions survive across sessions without relying on the context window.
> Keep it in sync with the code.

---

## 1. Project layout (after rename)

The package folder was renamed from `lmnotes/` to `lmnotespy/` to remove the
naming conflict with the MCP entry-point module `lmnotes.py`.

```
lmnotes.py            # FastMCP entry point (module) — imports from lmnotespy
lmnotespy/            # the package (renamed from lmnotes/)
├── __init__.py       # re-exports
├── notebook.py       # Notebook class + global/session state
├── utils.py          # pure utilities
├── operations.py     # CRUD service
├── edits.py          # edit-workflow service
├── versioning.py     # git service
├── lmnotes.md        # general docs
└── manual.md         # per-tool manual (loaded at runtime)
test_lmnotes.py       # test suite
```

### Why the rename
Before the rename, both `lmnotes.py` (module) and `lmnotes/` (package) existed in the
same directory. Python gives the **package** priority, so `import lmnotes` resolved to
the package and shadowed the entry-point module. Renaming the package to `lmnotespy`
makes `lmnotes` an unambiguous alias for the entry-point module.

### Import mapping (old → new)
| Old | New |
|-----|-----|
| `from lmnotes.notebook import ...` | `from lmnotespy.notebook import ...` |
| `from lmnotes.utils import ...` | `from lmnotespy.utils import ...` |
| `from lmnotes.operations import ...` | `from lmnotespy.operations import ...` |
| `from lmnotes.edits import ...` | `from lmnotespy.edits import ...` |
| `from lmnotes.versioning import ...` | `from lmnotespy.versioning import ...` |
| `from lmnotes import notebook` | `from lmnotespy import notebook` |
| `import lmnotes as _lmn` | `import lmnotespy as _lmn` |
| `read_text("lmnotes", "manual.md")` | `read_text("lmnotespy", "manual.md")` |

### Files touched by the rename
- `lmnotespy/notebook.py` — `from lmnotes.*` → `from lmnotespy.*`; `read_text("lmnotes",...)` → `read_text("lmnotespy",...)`
- `lmnotespy/operations.py` — `import lmnotes as _lmn`, `from lmnotes.*`
- `lmnotespy/edits.py` — `import lmnotes as _lmn`, `from lmnotes.*`
- `lmnotespy/versioning.py` — `from lmnotes.*`
- `lmnotespy/utils.py` — `from lmnotes.notebook` (TYPE_CHECKING)
- `lmnotes/__init__.py` → `lmnotespy/__init__.py` — relative imports, no code change
- `lmnotes.py` — the 2 `from lmnotes...` imports only (NOT tool names, NOT `FastMCP("lmnotes")`)
- `test_lmnotes.py` — `import lmnotes` → `import lmnotespy`; `from lmnotes import` → `from lmnotespy import`; all `lmnotes.<attr>` → `lmnotespy.<attr>`

### Do NOT rename (false positives to avoid)
- MCP **tool names**: `lmnotes_create_note`, `lmnotes_init_notebook`, `lmnotes_manual`, etc.
- The MCP server id: `FastMCP("lmnotes")`
- Console display strings: `print("lmnotes - LLM Notebook System")`
- The **data folder** `~/.lmnotes/` and code like `Path(...)/".lmnotes"`
- The entry-point file name `lmnotes.py`
- This file: `lmnotes_debug.md`

---

## 2. Suspected code bugs (verify before asserting in tests)

> These are *hypotheses* found by reading the code. Confirm each with a focused test
> before deciding to fix the code vs. asserting current behavior.

1. **`parent_id` dropped on edit paths** — `update_note`, `append_to_note`, and the
   selection-edit paths in `edits.py` rebuild frontmatter from a fixed key set
   (`id, title, folder, tags, created, updated`). `parent_id` is not carried through,
   so editing a child note can orphan it. Check `build_frontmatter` in `utils.py` and
   the frontmatter dicts assembled in `edits.py`.
2. **Tags round-trip as a raw string** — `build_frontmatter` writes
   `tags: [git, rebase]` (unquoted). `parse_frontmatter` tries `json.loads` which fails
   on unquoted items and falls back to storing the *string* `"[git, rebase]"`. Consequences:
   `read_note`/`list_notes` may return `tags` as a string, and tag-only keyword search
   can break. Confirm with a create→read round-trip and a tag-only search.
3. **Empty replacement deletes** — `edit_selection(selection_id, replacement="")` maps to
   `None` inside `edits.py`, which routes into the *deletion* branch. An LLM that passes
   an empty replacement silently deletes the selected text.
4. **ID collision overwrites** — creating two notes with the same custom `note_id` in the
   same folder causes the second write to overwrite the first (no uniqueness guard).
5. **Ambiguous ID across folders** — `find_note_file` returns `None` when the same ID
   exists in 2+ folders (only returns when exactly one match is found).
6. **Detail level 2 mismatch** — docstring/docs say "paragraph (~5 lines)" but the
   implementation returns the first ~3 paragraphs. Confirm actual behavior.
7. **`_selection_store` never pruned** — selections accumulate in the global dict if made
   but never consumed (unbounded growth over a long session).
8. **`update_note` commit message** — the git commit message hardcodes "title changed"
   regardless of which field actually changed.

---

## 3. Missing test coverage (candidates to add)

### MCP layer (`lmnotes.py`) — currently never imported by tests
Because `import lmnotes` resolved to the package, the 27 `lmnotes_*` tool wrappers were
never exercised. After the rename these are importable as the `lmnotes` module. Add:
- Tag string → list parsing (`"git, rebase"` → `["git","rebase"]`)
- Keyword string → list parsing (space-separated)
- JSON output shape / `status` field for representative tools
- `_tool_run` "not initialized" path (no active session → error JSON)
- Empty `folder`/`tags` defaults

### `utils.py` pure functions
- `generate_id` — timestamp format + explicit timestamp
- `make_slug` — punctuation removal, empty → "untitled"
- `parse_frontmatter` — valid, missing front-matter, array vs string, malformed JSON array
- `build_frontmatter` — key ordering, tags list vs string, empty dict
- `resolve_folder` — trailing slash → `/.lmnotes`, default → `~/.lmnotes`
- `find_note_file` — found in one folder, ambiguous (2 folders) → None, missing → None
- `read_note_file` — existing file, missing file → None

### Versioning / git edge paths
- `git_commit` with no changes → `{"status": "skipped"}`
- `git_diff` with **both** `from_rev` and `to_rev`
- `git_checkout` **success** — assert the note content actually reverted (current test only
  asserts a dict is returned)
- `git_*` behavior when git is not initialized / git not installed
- Add `@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")` guard

### Move note tests
- `test_move_note_valid` — move note to different valid folder ✓
- `test_move_note_same_folder_no_change` — move to same folder returns info ✓
- `test_move_note_invalid_folder` — destination not in VALID_FOLDERS ✓
- `test_move_note_not_found` — note ID doesn't exist ✓
- `test_move_note_updates_index` — verify index.md files are rebuilt ✓
- `test_move_note_git_commit` — verify git commit message ✓
- `test_move_note_reparents_children` — children re-parented correctly ✓
- `test_move_note_with_new_title` — new title updates slug ✓

### Detail levels & safeguards (per `lmnotes.md`)
- `read_note` detail_level **2** (paragraph) and **3** (full) — currently 0 and 1 covered
- `search_notes` detail_level **2**
- **Token safeguard**: replace the empty `test_search_max_tokens_truncation` (asserts only
  `status == "success"`) with real assertions on the excluded/trimmed behavior
- **DEBUG = False**: assert `filepath`, `deleted_file`, `destination_path` are hidden
  (currently only exercised with `DEBUG = True`)

### Select/edit workflow
- `occurrence=1` / `occurrence=2` **success** for exact and regex (only out-of-range error
  is tested)
- `append_selection` in `lines` mode
- `select_note` truncation marker when the match exceeds the preview limit
- Reuse of a nullified `selection_id` → error
- `append_selection` / `delete_selection` full-flow with content assertions

### CRUD / references
- `list_folder` invalid folder → error path
- `read_index` on **root** (no `| ID |` table)
- `update_note` tags-only update; `updated` timestamp advances
- `copy_to_references`: duplicate filename (counter suffix), custom `note_id`, missing source
- `read_system_prompt` with *additional* notes (non-empty `notes` list)
- `create_note` ID collision (see bug #4) and parent/child round-trip (see bug #1)

### Session management
- `_selection_store` cleared between tests (currently leaks across tests)
- `get_session` after all sessions closed

---

## 4. Test hygiene

- Remove `time.sleep(0.1)` between note creates; use explicit distinct `note_id` values
  (already done elsewhere in the file) to avoid flaky CI.
- Replace weak `len(...) >= 1` assertions with exact counts where known.
- Derive the tool list in `TestManual` from `manual.md` sections instead of a hardcoded
  list, so the test catches drift when tools are added/renamed.
- Consider `pytest.mark.parametrize` for the repetitive create/read/search cases.

---

## 5. How to run

```bash
# Full suite
pytest test_lmnotes.py -v

# A single class
pytest test_lmnotes.py::TestManual -v

# Verbose tracebacks
pytest test_lmnotes.py -v --tb=long
```

Baseline before the rename: **77 tests pass** (per `lmnotes.md`).