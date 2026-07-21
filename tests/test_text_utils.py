"""Unit tests for text utilities."""

import pytest
from pathlib import Path

from code_scanner.text_utils import (
    levenshtein_distance,
    similarity_ratio,
    fuzzy_match,
    find_similar_strings,
    normalize_whitespace,
    truncate_output,
    suggest_similar_files,
    format_validation_error,
    validate_file_path,
    validate_line_number,
    code_snippet_still_present,
    MAX_OUTPUT_LINES,
    MAX_OUTPUT_BYTES,
)


class TestLevenshteinDistance:
    """Tests for Levenshtein distance calculation."""

    def test_identical_strings(self):
        """Test distance is 0 for identical strings."""
        assert levenshtein_distance("hello", "hello") == 0

    def test_empty_string(self):
        """Test distance with empty string."""
        assert levenshtein_distance("", "hello") == 5
        assert levenshtein_distance("hello", "") == 5
        assert levenshtein_distance("", "") == 0

    def test_single_insertion(self):
        """Test single character insertion."""
        assert levenshtein_distance("cat", "cart") == 1

    def test_single_deletion(self):
        """Test single character deletion."""
        assert levenshtein_distance("cart", "cat") == 1

    def test_single_substitution(self):
        """Test single character substitution."""
        assert levenshtein_distance("cat", "bat") == 1

    def test_multiple_edits(self):
        """Test multiple edits required."""
        assert levenshtein_distance("kitten", "sitting") == 3


class TestSimilarityRatio:
    """Tests for similarity ratio calculation."""

    def test_identical_strings(self):
        """Test identical strings have ratio 1.0."""
        assert similarity_ratio("hello", "hello") == 1.0

    def test_completely_different(self):
        """Test completely different strings have low ratio."""
        ratio = similarity_ratio("abc", "xyz")
        assert ratio < 0.5

    def test_similar_strings(self):
        """Test similar strings have high ratio."""
        ratio = similarity_ratio("hello", "hella")
        assert ratio >= 0.8

    def test_empty_strings(self):
        """Test empty strings have ratio 1.0."""
        assert similarity_ratio("", "") == 1.0


class TestFuzzyMatch:
    """Tests for fuzzy matching."""

    def test_exact_match(self):
        """Test exact match returns True."""
        assert fuzzy_match("hello", "hello")

    def test_similar_match(self):
        """Test similar strings match with default threshold."""
        assert fuzzy_match("hello world", "hello worlb")

    def test_different_strings_dont_match(self):
        """Test very different strings don't match."""
        assert not fuzzy_match("hello", "goodbye")

    def test_custom_threshold(self):
        """Test custom threshold."""
        # Very strict threshold
        assert not fuzzy_match("hello", "hella", threshold=0.95)
        # Lenient threshold
        assert fuzzy_match("hello", "hella", threshold=0.7)


class TestFindSimilarStrings:
    """Tests for finding similar strings."""

    def test_find_similar(self):
        """Test finding similar strings."""
        candidates = ["hello", "world", "hella", "help"]
        results = find_similar_strings("hello", candidates)
        
        assert len(results) > 0
        # "hello" should be first (exact match)
        assert results[0][0] == "hello"
        assert results[0][1] == 1.0

    def test_max_results(self):
        """Test max_results parameter."""
        candidates = ["a", "ab", "abc", "abcd", "abcde"]
        results = find_similar_strings("abc", candidates, max_results=2)
        
        assert len(results) == 2

    def test_threshold_filtering(self):
        """Test threshold filters out dissimilar strings."""
        candidates = ["hello", "xyz", "world"]
        results = find_similar_strings("hello", candidates, threshold=0.8)
        
        assert len(results) == 1
        assert results[0][0] == "hello"


class TestNormalizeWhitespace:
    """Tests for whitespace normalization."""

    def test_collapse_spaces(self):
        """Test collapsing multiple spaces."""
        assert normalize_whitespace("hello    world") == "hello world"

    def test_strip_leading_trailing(self):
        """Test stripping leading/trailing whitespace."""
        assert normalize_whitespace("  hello  ") == "hello"

    def test_normalize_tabs_newlines(self):
        """Test normalizing tabs and newlines."""
        assert normalize_whitespace("hello\tworld\n") == "hello world"


class TestTruncateOutput:
    """Tests for output truncation."""

    def test_no_truncation_needed(self):
        """Test content below limits is not truncated."""
        content = "short content"
        result, was_truncated, hint = truncate_output(content)
        
        assert result == content
        assert not was_truncated
        assert hint == ""

    def test_line_limit_truncation(self):
        """Test truncation by line limit."""
        content = "\n".join([f"line {i}" for i in range(MAX_OUTPUT_LINES + 100)])
        result, was_truncated, hint = truncate_output(content)
        
        assert was_truncated
        assert len(result.split("\n")) == MAX_OUTPUT_LINES
        assert "search_text" in hint.lower()

    def test_byte_limit_truncation(self):
        """Test truncation by byte limit."""
        content = "x" * (MAX_OUTPUT_BYTES + 1000)
        result, was_truncated, hint = truncate_output(content)
        
        assert was_truncated
        assert len(result.encode("utf-8")) <= MAX_OUTPUT_BYTES
        assert "search_text" in hint.lower()

    def test_custom_limits(self):
        """Test custom limits."""
        content = "\n".join([f"line {i}" for i in range(100)])
        result, was_truncated, hint = truncate_output(content, max_lines=50)
        
        assert was_truncated
        assert len(result.split("\n")) == 50


class TestFormatValidationError:
    """Tests for validation error formatting."""

    def test_basic_error(self):
        """Test basic error formatting."""
        error = format_validation_error("file_path", "", "non-empty string")
        
        assert "file_path" in error
        assert "non-empty string" in error

    def test_error_with_hint(self):
        """Test error with hint."""
        error = format_validation_error(
            "file_path", "", "non-empty string",
            "Provide the relative path."
        )
        
        assert "Provide the relative path." in error


class TestValidateFilePath:
    """Tests for file path validation."""

    def test_empty_path_invalid(self, tmp_path):
        """Test empty path is invalid."""
        is_valid, error, suggestions = validate_file_path("", tmp_path)
        
        assert not is_valid
        assert "file_path" in error.lower()

    def test_valid_path(self, tmp_path):
        """Test valid path."""
        test_file = tmp_path / "test.py"
        test_file.write_text("content")
        
        is_valid, error, suggestions = validate_file_path("test.py", tmp_path)
        
        assert is_valid
        assert error == ""
        assert suggestions is None

    def test_not_found_with_suggestions(self, tmp_path):
        """Test file not found provides suggestions."""
        (tmp_path / "main.py").write_text("content")
        (tmp_path / "test.py").write_text("content")
        
        is_valid, error, suggestions = validate_file_path("maim.py", tmp_path)
        
        assert not is_valid
        assert "not found" in error.lower()
        assert suggestions is not None
        # Should suggest main.py as similar
        assert "main.py" in suggestions

    def test_outside_repo_denied(self, tmp_path):
        """Test path outside repo is denied."""
        is_valid, error, suggestions = validate_file_path("../outside.txt", tmp_path)
        
        assert not is_valid
        assert "denied" in error.lower()

    def test_directory_not_file(self, tmp_path):
        """Test directory path returns error."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        
        is_valid, error, suggestions = validate_file_path("subdir", tmp_path)
        
        assert not is_valid
        assert "directory" in error.lower()


class TestValidateLineNumber:
    """Tests for line number validation."""

    def test_valid_line_number(self):
        """Test valid line number."""
        is_valid, error = validate_line_number(5, 100)
        
        assert is_valid
        assert error == ""

    def test_zero_invalid(self):
        """Test line number 0 is invalid."""
        is_valid, error = validate_line_number(0, 100)
        
        assert not is_valid
        assert "1-based" in error.lower()

    def test_negative_invalid(self):
        """Test negative line number is invalid."""
        is_valid, error = validate_line_number(-1, 100)
        
        assert not is_valid

    def test_beyond_file_length(self):
        """Test line number beyond file length."""
        is_valid, error = validate_line_number(150, 100)
        
        assert not is_valid
        assert "100" in error


class TestSuggestSimilarFiles:
    """Tests for file suggestion functionality."""

    def test_suggest_similar_name(self, tmp_path):
        """Test suggesting files with similar names."""
        (tmp_path / "main.py").write_text("content")
        (tmp_path / "utils.py").write_text("content")
        
        suggestions = suggest_similar_files("maim.py", tmp_path)
        
        assert "main.py" in suggestions

    def test_suggest_from_subdirectory(self, tmp_path):
        """Test suggestions include files in subdirectories."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("content")
        
        suggestions = suggest_similar_files("maim.py", tmp_path)
        
        # Should find main.py in src directory
        assert any("main.py" in s for s in suggestions)

    def test_skip_hidden_directories(self, tmp_path):
        """Test hidden directories are skipped."""
        hidden = tmp_path / ".git"
        hidden.mkdir()
        (hidden / "config").write_text("content")
        
        suggestions = suggest_similar_files("config", tmp_path)
        
        # Should not find .git/config
        assert not any(".git" in s for s in suggestions)

    def test_max_suggestions(self, tmp_path):
        """Test max_suggestions parameter."""
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text("content")

        suggestions = suggest_similar_files("file.py", tmp_path, max_suggestions=3)

        assert len(suggestions) <= 3


class TestCodeSnippetStillPresent:
    """Tests for code_snippet_still_present presence check."""

    def test_snippet_present_returns_true(self):
        """Snippet embedded in larger file content returns True."""
        snippet = "const userFunds = Math.floor(bonus + balance)"
        file_content = (
            "import QtQuick\n\n"
            "Item {\n"
            f"    {snippet}\n"
            "    const other = 42\n"
            "}\n"
        )
        assert code_snippet_still_present(snippet, file_content) is True

    def test_snippet_absent_returns_false(self):
        """Snippet removed from file content returns False (legitimate fix)."""
        snippet = "const userFunds = Math.floor(bonus + balance)"
        file_content = "import QtQuick\nItem {\n    const other = 42\n}\n"
        assert code_snippet_still_present(snippet, file_content) is False

    def test_empty_snippet_returns_none(self):
        """Empty/whitespace snippet cannot be checked -> None."""
        assert code_snippet_still_present("", "some content") is None
        assert code_snippet_still_present("   \n  ", "some content") is None

    def test_no_file_content_returns_none(self):
        """Missing file content cannot be checked -> None."""
        assert code_snippet_still_present("some code", None) is None
        assert code_snippet_still_present("some code", "") is None

    def test_whitespace_tolerant(self):
        """Whitespace differences do not defeat the match."""
        snippet = "const x = Math.floor(a + b)"
        # Same code with different indentation and extra blank lines
        file_content = "Item {\n\n\n    const   x   =   Math.floor(a+b)\n}\n"
        assert code_snippet_still_present(snippet, file_content) is True

    def test_multiline_snippet_present(self):
        """Multi-line snippet (the userFunds case) is detected when present."""
        snippet = (
            "const userFunds = Math.floor(bonus + balance)\n"
            "const userFundsForDisplay = Math.floor(bonus + balance)"
        )
        file_content = (
            "Item {\n"
            "    const userFunds = Math.floor(bonus + balance)\n"
            "    const userFundsForDisplay = Math.floor(bonus + balance)\n"
            "    Text { text: 'hello' }\n"
            "}\n"
        )
        assert code_snippet_still_present(snippet, file_content) is True
