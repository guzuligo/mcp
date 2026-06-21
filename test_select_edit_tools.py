"""
Unit tests for select_before_edit_file_content and edit_after_select_file_content tools.
"""

import os
import sys
import tempfile
import shutil
import subprocess
import unittest

# Add the current directory to path to import the tools
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the helper functions from pythonFileTools
from pythonFileTools import (
    _truncate_content,
    _find_matches,
    _invalidate_selection,
    _get_selection,
    select_before_edit_file_content,
    edit_after_select_file_content,
    _selection_registry,
    _validate_path,
)
from pathlib import Path

# Use a test directory within the home directory to avoid security validation errors
HOME_DIR = Path.home()
TEST_BASE_DIR = HOME_DIR / ".mcp_test_workspace"


class TestTruncateContent(unittest.TestCase):
    """Tests for the _truncate_content helper function."""

    def test_short_content_no_truncation(self):
        """Short content should not be truncated."""
        content = "Hello, World!"
        result = _truncate_content(content, max_chars=200)
        self.assertEqual(result, content)

    def test_exact_boundary_no_truncation(self):
        """Content exactly at 2*max_chars should not be truncated."""
        content = "A" * 400  # 2 * 200
        result = _truncate_content(content, max_chars=200)
        self.assertEqual(result, content)

    def test_long_content_truncated(self):
        """Long content should be truncated with middle replaced."""
        content = "A" * 500 + "B" * 500
        result = _truncate_content(content, max_chars=200)
        self.assertTrue(result.startswith("A" * 200))
        self.assertIn("... <truncated> ...", result)
        self.assertTrue(result.endswith("B" * 200))

    def test_truncate_custom_max_chars(self):
        """Custom max_chars should work correctly."""
        content = "A" * 100 + "MIDDLE" + "B" * 100
        result = _truncate_content(content, max_chars=10)
        self.assertEqual(len(result.split("... <truncated> ...")[0]), 10)
        self.assertEqual(len(result.split("... <truncated> ...")[1]), 10)


class TestFindMatches(unittest.TestCase):
    """Tests for the _find_matches helper function."""

    def test_exact_mode_single_match(self):
        """Exact mode should find single occurrence."""
        content = "Hello World Hello"
        result = _find_matches(content, "Hello", mode="exact")
        self.assertEqual(result["total_occurrences"], 2)
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(result["matches"][0]["line"], 1)
        self.assertEqual(result["matches"][0]["content"], "Hello")

    def test_exact_mode_no_match(self):
        """Exact mode should return 0 matches for non-existent string."""
        content = "Hello World"
        result = _find_matches(content, "XYZ", mode="exact")
        self.assertEqual(result["total_occurrences"], 0)
        self.assertEqual(len(result["matches"]), 0)

    def test_exact_mode_empty_search(self):
        """Empty search should return 0 matches in exact mode."""
        content = "Hello World"
        result = _find_matches(content, "", mode="exact")
        self.assertEqual(result["total_occurrences"], 0)

    def test_exact_mode_multiline(self):
        """Exact mode should correctly count line numbers in multiline content."""
        content = "Line 1\nLine 2\nLine 3\nLine 2 again"
        result = _find_matches(content, "Line 2", mode="exact")
        self.assertEqual(result["total_occurrences"], 2)
        self.assertEqual(result["matches"][0]["line"], 2)
        self.assertEqual(result["matches"][1]["line"], 4)

    def test_regex_mode_single_match(self):
        """Regex mode should find pattern matches."""
        content = "Hello World Hello Universe"
        result = _find_matches(content, r"Hello\s+\w+", mode="regex")
        self.assertEqual(result["total_occurrences"], 2)

    def test_regex_mode_invalid_pattern(self):
        """Invalid regex should return error."""
        content = "Hello World"
        result = _find_matches(content, "[invalid", mode="regex")
        self.assertEqual(result["total_occurrences"], 0)
        self.assertIn("error", result)

    def test_whitespace_tolerant_mode(self):
        """Whitespace tolerant mode should ignore whitespace differences."""
        content = "Hello   World\nHello World"
        result = _find_matches(content, "Hello World", mode="whitespace_tolerant")
        self.assertEqual(result["total_occurrences"], 2)

    def test_whitespace_tolerant_empty_search(self):
        """Empty search in whitespace_tolerant should return 0 matches."""
        content = "Hello World"
        result = _find_matches(content, "", mode="whitespace_tolerant")
        self.assertEqual(result["total_occurrences"], 0)

    def test_match_contains_truncated_flag(self):
        """Each match should have truncated flag."""
        content = "A" * 500
        result = _find_matches(content, "A" * 500, mode="exact")
        self.assertIn("truncated", result["matches"][0])
        self.assertIn("full_length", result["matches"][0])


class TestSelectionRegistry(unittest.TestCase):
    """Tests for selection registry functions."""

    def setUp(self):
        """Clear registry before each test."""
        _selection_registry.clear()

    def tearDown(self):
        """Clear registry after each test."""
        _selection_registry.clear()

    def test_set_and_get_selection(self):
        """Setting and getting selection should work."""
        path = str(HOME_DIR / "test_file.py")
        _selection_registry[path] = {
            "active": True,
            "total_occurrences": 5,
            "search": "test",
            "mode": "exact",
        }
        result = _get_selection(path)
        self.assertIsNotNone(result)
        self.assertTrue(result["active"])
        self.assertEqual(result["total_occurrences"], 5)

    def test_invalidate_selection(self):
        """Invalidating selection should set active to False."""
        path = str(HOME_DIR / "test_file.py")
        _selection_registry[path] = {"active": True, "search": "test"}
        _invalidate_selection(path)
        result = _get_selection(path)
        self.assertFalse(result["active"])

    def test_get_nonexistent_selection(self):
        """Getting nonexistent selection should return None."""
        result = _get_selection(str(HOME_DIR / "nonexistent/path.py"))
        self.assertIsNone(result)


class TestSelectBeforeEditFileContent(unittest.TestCase):
    """Tests for select_before_edit_file_content tool."""

    def setUp(self):
        """Set up test fixtures."""
        # Create test directory within home directory
        TEST_BASE_DIR.mkdir(parents=True, exist_ok=True)
        self.test_dir = TEST_BASE_DIR / "select_test"
        self.test_dir.mkdir(exist_ok=True)
        _selection_registry.clear()

    def tearDown(self):
        """Clean up test files."""
        shutil.rmtree(self.test_dir, ignore_errors=True)
        _selection_registry.clear()

    def _create_git_repo(self, directory):
        """Initialize a git repo in the given directory."""
        subprocess.run(
            ["git", "init", str(directory)],
            capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(directory), "config", "user.name", "Test User"],
            capture_output=True, text=True, timeout=5,
        )
        subprocess.run(
            ["git", "-C", str(directory), "config", "user.email", "test@test.com"],
            capture_output=True, text=True, timeout=5,
        )

    def test_select_file_not_found(self):
        """Should return error for nonexistent file."""
        test_file = self.test_dir / "nonexistent.py"
        result = select_before_edit_file_content(
            path=str(test_file),
            search="test",
        )
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["selection_active"])

    def test_select_missing_search_pattern(self):
        """Should return error when search is empty."""
        test_file = self.test_dir / "test.py"
        test_file.write_text("Hello World")
        result = select_before_edit_file_content(path=str(test_file), search="")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["total_occurrences"], 0)

    def test_select_exact_mode_success(self):
        """Should find and store selection in exact mode."""
        test_file = self.test_dir / "test.py"
        test_file.write_text("Hello World\nHello Universe\nGoodbye")

        result = select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
            mode="exact",
        )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["selection_active"])
        self.assertEqual(result["total_occurrences"], 2)
        self.assertEqual(len(result["matches"]), 2)

    def test_select_stores_in_registry(self):
        """Selection should be stored in registry."""
        test_file = self.test_dir / "test.py"
        test_file.write_text("Hello World")

        select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
        )

        selection = _get_selection(str(test_file))
        self.assertIsNotNone(selection)
        self.assertTrue(selection["active"])
        self.assertEqual(selection["search"], "Hello")

    def test_select_regex_mode(self):
        """Should find matches in regex mode."""
        test_file = self.test_dir / "test.py"
        test_file.write_text("abc123 def456 ghi789")

        result = select_before_edit_file_content(
            path=str(test_file),
            search=r"\d+",
            mode="regex",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_occurrences"], 3)

    def test_select_no_matches(self):
        """Should return 0 occurrences when no matches found."""
        test_file = self.test_dir / "test.py"
        test_file.write_text("Hello World")

        result = select_before_edit_file_content(
            path=str(test_file),
            search="XYZ",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_occurrences"], 0)
        self.assertTrue(result["selection_active"])


class TestEditAfterSelectFileContent(unittest.TestCase):
    """Tests for edit_after_select_file_content tool."""

    def setUp(self):
        """Set up test fixtures."""
        TEST_BASE_DIR.mkdir(parents=True, exist_ok=True)
        self.test_dir = TEST_BASE_DIR / "edit_test"
        self.test_dir.mkdir(exist_ok=True)
        _selection_registry.clear()

    def tearDown(self):
        """Clean up test files."""
        shutil.rmtree(self.test_dir, ignore_errors=True)
        _selection_registry.clear()

    def _create_git_repo(self, directory):
        """Initialize a git repo in the given directory."""
        subprocess.run(
            ["git", "init", str(directory)],
            capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(directory), "config", "user.name", "Test User"],
            capture_output=True, text=True, timeout=5,
        )
        subprocess.run(
            ["git", "-C", str(directory), "config", "user.email", "test@test.com"],
            capture_output=True, text=True, timeout=5,
        )

    def test_edit_no_active_selection(self):
        """Should return error when no active selection exists."""
        test_file = self.test_dir / "test.py"
        test_file.write_text("Hello World")

        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=0,
            replacement="Goodbye",
        )

        self.assertEqual(result["status"], "selection_error")
        self.assertEqual(result["replacements_made"], 0)

    def test_edit_after_select_replace_all(self):
        """Should replace all occurrences when occurrence=0."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)
        test_file.write_text("Hello World\nHello Universe\nHello World")

        # First select
        select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
        )

        # Then replace all
        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=0,
            replacement="Hi",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["replacements_made"], 3)
        self.assertFalse(result["selection_active"])

        # Verify file content
        content = test_file.read_text()
        self.assertNotIn("Hello", content)
        self.assertEqual(content.count("Hi"), 3)

    def test_edit_after_select_replace_specific_occurrence(self):
        """Should replace only specific occurrence when occurrence=1."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)
        test_file.write_text("Hello World\nHello Universe\nHello World")

        # First select
        select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
        )

        # Then replace only first occurrence
        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=1,
            replacement="Hi",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["replacements_made"], 1)

        # Verify file content
        content = test_file.read_text()
        lines = content.strip().split("\n")
        self.assertEqual(lines[0], "Hi World")
        self.assertEqual(lines[1], "Hello Universe")
        self.assertEqual(lines[2], "Hello World")

    def test_edit_after_select_replace_specific_list(self):
        """Should replace specific occurrences when occurrence=[1,3]."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)
        test_file.write_text("A\nB\nC\nD\nE")

        # First select
        select_before_edit_file_content(
            path=str(test_file),
            search="\n",
        )

        # Replace occurrences 1 and 3 (first and third newline positions)
        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=[1, 3],
            replacement=" | ",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["replacements_made"], 2)

    def test_edit_after_select_invalid_occurrence(self):
        """Should return error when occurrence exceeds total."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)
        test_file.write_text("Hello World")

        # Select only 1 occurrence
        select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
        )

        # Try to replace occurrence 5 (doesn't exist)
        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=5,
            replacement="Goodbye",
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("exceeds", result["message"])

    def test_edit_after_select_invalidates_selection(self):
        """Selection should be invalidated after successful edit."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)
        test_file.write_text("Hello World")

        select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
        )

        edit_after_select_file_content(
            path=str(test_file),
            occurrence=0,
            replacement="Hi",
        )

        selection = _get_selection(str(test_file))
        if selection:
            self.assertFalse(selection["active"])

    def test_edit_after_select_with_diff(self):
        """Result should include diff of changes."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)
        test_file.write_text("Hello World")

        select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
        )

        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=0,
            replacement="Hi",
        )

        self.assertIn("diff", result)
        self.assertIn("Hello", result["diff"])
        self.assertIn("Hi", result["diff"])

    def test_edit_after_select_no_changes(self):
        """Should return no_changes when replacement equals original."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)
        test_file.write_text("Hello World")

        select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
        )

        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=0,
            replacement="Hello",  # Same as original
        )

        self.assertEqual(result["status"], "no_changes")

    def test_edit_after_select_delete_text(self):
        """Should delete text when replacement is empty string."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)
        test_file.write_text("Hello World")

        select_before_edit_file_content(
            path=str(test_file),
            search="Hello ",
        )

        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=0,
            replacement="",  # Delete
        )

        self.assertEqual(result["status"], "success")
        content = test_file.read_text()
        self.assertNotIn("Hello", content)
        self.assertEqual(content.strip(), "World")


class TestIntegrationSelectEdit(unittest.TestCase):
    """Integration tests for select + edit workflow."""

    def setUp(self):
        """Set up test fixtures."""
        TEST_BASE_DIR.mkdir(parents=True, exist_ok=True)
        self.test_dir = TEST_BASE_DIR / "integration_test"
        self.test_dir.mkdir(exist_ok=True)
        _selection_registry.clear()

    def tearDown(self):
        """Clean up test files."""
        shutil.rmtree(self.test_dir, ignore_errors=True)
        _selection_registry.clear()

    def _create_git_repo(self, directory):
        """Initialize a git repo in the given directory."""
        subprocess.run(
            ["git", "init", str(directory)],
            capture_output=True, text=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(directory), "config", "user.name", "Test User"],
            capture_output=True, text=True, timeout=5,
        )
        subprocess.run(
            ["git", "-C", str(directory), "config", "user.email", "test@test.com"],
            capture_output=True, text=True, timeout=5,
        )

    def test_full_workflow_select_then_edit(self):
        """Test complete select -> edit workflow."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)

        # Create file with multiple occurrences
        test_file.write_text("name = 'Alice'\nname = 'Bob'\nname = 'Charlie'")

        # Step 1: Select all 'name' occurrences
        select_result = select_before_edit_file_content(
            path=str(test_file),
            search="name",
            mode="exact",
        )
        self.assertEqual(select_result["status"], "success")
        self.assertEqual(select_result["total_occurrences"], 3)

        # Step 2: Replace first occurrence only
        edit_result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=1,
            replacement="full_name",
        )
        self.assertEqual(edit_result["status"], "success")
        self.assertEqual(edit_result["replacements_made"], 1)

        # Step 3: Verify result
        content = test_file.read_text()
        self.assertIn("full_name = 'Alice'", content)
        self.assertIn("name = 'Bob'", content)
        self.assertIn("name = 'Charlie'", content)

    def test_workflow_selection_invalidated_after_external_edit(self):
        """Selection should be invalidated if file is edited externally."""
        test_file = self.test_dir / "test.py"
        self._create_git_repo(self.test_dir)

        test_file.write_text("Hello World")

        # Select
        select_before_edit_file_content(
            path=str(test_file),
            search="Hello",
        )

        # Invalidate by writing directly
        test_file.write_text("Goodbye World")
        _invalidate_selection(str(test_file))

        # Try to edit - should fail
        result = edit_after_select_file_content(
            path=str(test_file),
            occurrence=0,
            replacement="Hi",
        )

        self.assertEqual(result["status"], "selection_error")


if __name__ == "__main__":
    unittest.main()