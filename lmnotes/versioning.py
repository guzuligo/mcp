"""
versioning.py — Git integration service for lmnotes.

Contains all git-related operations as a service that wraps a Notebook instance.
No circular dependencies — imports Notebook only for TYPE_CHECKING.
"""

import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from lmnotes.notebook import Notebook


class GitService:
    """Service for git integration operations."""

    def __init__(self, notebook: "Notebook"):
        self.nb = notebook

    def git_init(self) -> None:
        """Initialize git repo in notebook folder. Safe to call repeatedly."""
        root = Path(self.nb.folder)
        git_dir = root / ".git"
        if git_dir.exists():
            self._ensure_git_user_config(str(root))
            return
        try:
            subprocess.run(
                ["git", "init", "--initial-branch=main"],
                cwd=str(root), capture_output=True, check=True
            )
            gi = root / ".gitignore"
            if not gi.exists():
                gi.write_text("*.pyc\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
            self._ensure_git_user_config(str(root))
            idx = root / "index.md"
            if idx.exists():
                subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "Initial notebook structure"],
                    cwd=str(root), capture_output=True, check=True
                )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    @staticmethod
    def _ensure_git_user_config(repo_path: str) -> None:
        """Ensure git user.email and user.name are configured locally."""
        try:
            subprocess.run(
                ["git", "config", "user.email", "lmnotes@local"],
                cwd=repo_path, capture_output=True, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "LMNotes"],
                cwd=repo_path, capture_output=True, check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    def git_commit(self, message: str) -> dict:
        """Stage and commit all changes in the notebook folder."""
        root = Path(self.nb.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"status": "skipped", "message": "Git not initialized"}
        try:
            subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True, check=True)
            check = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(root), capture_output=True
            )
            if check.returncode == 0:
                return {"status": "skipped", "message": "No changes to commit"}
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(root), capture_output=True, text=True, check=True
            )
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root), capture_output=True, text=True
            )
            return {"status": "ok", "commit_hash": hash_result.stdout.strip()}
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.strip() if e.stderr else str(e)
            return {"status": "error", "message": f"Git commit failed: {stderr}"}
        except FileNotFoundError:
            return {"status": "error", "message": "Git not found. Is git installed?"}

    def git_diff_file(self, filepath: Path, from_rev: str = "HEAD", to_rev: str = None) -> str:
        """Get diff for a file between two revisions."""
        root = Path(self.nb.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return ""
        try:
            rel_path = filepath.relative_to(root)
            if to_rev is None:
                result = subprocess.run(
                    ["git", "diff", from_rev, "--", str(rel_path)],
                    cwd=str(root), capture_output=True, text=True
                )
            else:
                result = subprocess.run(
                    ["git", "diff", f"{from_rev}", to_rev, "--", str(rel_path)],
                    cwd=str(root), capture_output=True, text=True
                )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""

    def git_log(self, note_id: str) -> dict:
        """Return commit history for a specific note file."""
        root = Path(self.nb.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"status": "success", "note_id": note_id, "commits": [], "message": "Git not initialized in this notebook."}

        # Import here to avoid circular dependency at module level
        from lmnotes.utils import find_note_file  # pylint: disable=import-outside-toplevel
        filepath = find_note_file(self.nb, note_id, "")
        if not filepath:
            for f in root.rglob(f"*{note_id}*.md"):
                if "_" in f.name and f.name != "index.md":
                    filepath = f
                    break
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}

        try:
            rel_path = filepath.relative_to(root)
            result = subprocess.run(
                ["git", "log", "--pretty=format:%H|%ai|%s", "--", str(rel_path)],
                cwd=str(root), capture_output=True, text=True
            )
            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) == 3:
                    commits.append({"hash": parts[0], "date": parts[1][:10], "message": parts[2]})
            return {"status": "success", "note_id": note_id, "filepath": str(filepath), "commits": commits}
        except (subprocess.CalledProcessError, FileNotFoundError):
            return {"status": "error", "message": "Failed to read git log"}

    def git_diff(self, note_id: str, from_rev: str = "HEAD", to_rev: str = "") -> dict:
        """Show diff between two revisions of a note."""
        root = Path(self.nb.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"status": "success", "note_id": note_id, "diff": "", "message": "Git not initialized."}

        from lmnotes.utils import find_note_file  # pylint: disable=import-outside-toplevel
        filepath = find_note_file(self.nb, note_id, "")
        if not filepath:
            for f in root.rglob(f"*{note_id}*.md"):
                if "_" in f.name and f.name != "index.md":
                    filepath = f
                    break
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}

        from_r = from_rev if from_rev else "HEAD"

        try:
            rel_path = filepath.relative_to(root)
            if not to_rev:
                result = subprocess.run(
                    ["git", "diff", from_r, "--", str(rel_path)],
                    cwd=str(root), capture_output=True, text=True
                )
                return {"status": "success", "note_id": note_id, "from_rev": from_r, "to_rev": "(working tree)", "diff": result.stdout.strip() or "(no changes)"}
            to_r = to_rev
            result = subprocess.run(
                ["git", "diff", from_r, to_r, "--", str(rel_path)],
                cwd=str(root), capture_output=True, text=True
            )
            return {"status": "success", "note_id": note_id, "from_rev": from_r, "to_rev": to_r, "diff": result.stdout.strip() or "(no changes)"}
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return {"status": "error", "message": f"Failed to read git diff: {e}"}

    def git_checkout(self, note_id: str, revision: str) -> dict:
        """Restore a note to a previous git revision."""
        root = Path(self.nb.folder)
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"status": "error", "message": "Git not initialized in this notebook."}

        from lmnotes.utils import find_note_file  # pylint: disable=import-outside-toplevel
        filepath = find_note_file(self.nb, note_id, "")
        if not filepath:
            for f in root.rglob(f"*{note_id}*.md"):
                if "_" in f.name and f.name != "index.md":
                    filepath = f
                    break
        if not filepath:
            return {"status": "error", "message": f"Note with ID '{note_id}' not found"}

        try:
            rel_path = filepath.relative_to(root)
            subprocess.run(
                ["git", "checkout", revision, "--", str(rel_path)],
                cwd=str(root), capture_output=True, check=True
            )
            # Update indexes after checkout
            from lmnotes.utils import VALID_FOLDERS  # pylint: disable=import-outside-toplevel
            folder = filepath.parent.name if filepath.parent != root else ""
            if folder in VALID_FOLDERS:
                self.nb._update_index(folder)
            self.nb._update_root_index()
            self.git_commit(f"Restore note {note_id} to {revision}")
            return {"status": "success", "note_id": note_id, "restored_to": revision, "filepath": str(filepath)}
        except subprocess.CalledProcessError as e:
            return {"status": "error", "message": f"Git checkout failed: {e.stderr.strip() or str(e)}"}
        except FileNotFoundError:
            return {"status": "error", "message": "Git not found. Is git installed?"}