"""Tests for Pydantic LLM response validation models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from code_scanner.models import (
    Issue,
    LLMIssue,
    LLMScanResponse,
    LLMToolCall,
    LLMToolCallResponse,
    LLMToolResult,
    sanitize_llm_response,
    _sanitize_string,
)


# ---------------------------------------------------------------------------
# LLMIssue tests
# ---------------------------------------------------------------------------

class TestLLMIssue:
    """Tests for LLMIssue Pydantic model."""

    def test_valid_full_response(self):
        """Standard issue dict with all fields."""
        data = {
            "file_path": "src/main.py",
            "line_number": 42,
            "description": "Unused variable",
            "suggested_fix": "Remove the variable",
            "code_snippet": "x = 1",
        }
        issue = LLMIssue.model_validate(data)
        assert issue.file_path == "src/main.py"
        assert issue.line_number == 42
        assert issue.description == "Unused variable"
        assert issue.suggested_fix == "Remove the variable"
        assert issue.code_snippet == "x = 1"

    def test_alias_file_to_file_path(self):
        """LLMs often use 'file' instead of 'file_path'."""
        data = {"file": "src/utils.py", "line_number": 10, "description": "bug"}
        issue = LLMIssue.model_validate(data)
        assert issue.file_path == "src/utils.py"

    def test_alias_line_to_line_number(self):
        """LLMs often use 'line' instead of 'line_number'."""
        data = {"file_path": "a.py", "line": 99, "description": "oops"}
        issue = LLMIssue.model_validate(data)
        assert issue.line_number == 99

    def test_alias_fix_to_suggested_fix(self):
        """LLMs often use 'fix' instead of 'suggested_fix'."""
        data = {"file_path": "a.py", "line_number": 1, "fix": "do this"}
        issue = LLMIssue.model_validate(data)
        assert issue.suggested_fix == "do this"

    def test_all_aliases_combined(self):
        """All aliases used at once."""
        data = {"file": "x.py", "line": 5, "description": "d", "fix": "f", "code_snippet": "c"}
        issue = LLMIssue.model_validate(data)
        assert issue.file_path == "x.py"
        assert issue.line_number == 5
        assert issue.suggested_fix == "f"

    def test_canonical_names_preferred_over_aliases(self):
        """If both canonical and alias are present and canonical is truthy, canonical wins."""
        data = {"file_path": "canonical.py", "file": "alias.py", "line_number": 10, "line": 20}
        issue = LLMIssue.model_validate(data)
        assert issue.file_path == "canonical.py"
        assert issue.line_number == 10

    def test_alias_wins_when_canonical_is_none(self):
        """When canonical key is None, alias value is used (backward compat)."""
        data = {"file_path": None, "file": "fallback.py", "line_number": None, "line": 5}
        issue = LLMIssue.model_validate(data)
        assert issue.file_path == "fallback.py"
        assert issue.line_number == 5

    def test_none_string_fields_coerced(self):
        """None values in string fields become empty strings."""
        data = {
            "file_path": None,
            "line_number": 1,
            "description": None,
            "suggested_fix": None,
            "code_snippet": None,
        }
        issue = LLMIssue.model_validate(data)
        assert issue.file_path == ""
        assert issue.description == ""
        assert issue.suggested_fix == ""
        assert issue.code_snippet == ""

    def test_none_line_number_coerced(self):
        """None line_number becomes 0."""
        data = {"file_path": "a.py", "line_number": None}
        issue = LLMIssue.model_validate(data)
        assert issue.line_number == 0

    def test_string_line_number_coerced(self):
        """String line numbers are coerced to int."""
        data = {"file_path": "a.py", "line_number": "42"}
        issue = LLMIssue.model_validate(data)
        assert issue.line_number == 42

    def test_invalid_line_number_coerced_to_zero(self):
        """Non-numeric line_number values become 0."""
        data = {"file_path": "a.py", "line_number": "not_a_number"}
        issue = LLMIssue.model_validate(data)
        assert issue.line_number == 0

    def test_negative_line_number_coerced_to_zero(self):
        """Negative line numbers are coerced to 0."""
        data = {"file_path": "a.py", "line_number": -5}
        issue = LLMIssue.model_validate(data)
        assert issue.line_number == 0

    def test_empty_dict(self):
        """Empty dict uses all defaults."""
        issue = LLMIssue.model_validate({})
        assert issue.file_path == ""
        assert issue.line_number == 0
        assert issue.description == ""
        assert issue.suggested_fix == ""
        assert issue.code_snippet == ""

    def test_extra_fields_ignored(self):
        """Extra fields from LLM are silently ignored."""
        data = {
            "file_path": "a.py",
            "line_number": 1,
            "severity": "high",
            "confidence": 0.9,
            "extra_unknown": True,
        }
        issue = LLMIssue.model_validate(data)
        assert issue.file_path == "a.py"
        assert not hasattr(issue, "severity")

    # -- New validators for file_path sanitization --

    def test_file_path_directory_traversal_removed(self):
        """Directory traversal patterns (../) are stripped from file_path."""
        issue = LLMIssue.model_validate({"file_path": "../../etc/passwd", "line_number": 1})
        assert ".." not in issue.file_path
        assert "etc/passwd" in issue.file_path

    def test_file_path_backslashes_normalized(self):
        """Backslashes in file_path are converted to forward slashes."""
        issue = LLMIssue.model_validate({"file_path": "src\\utils\\helper.py", "line_number": 1})
        assert "\\" not in issue.file_path
        assert issue.file_path == "src/utils/helper.py"

    def test_file_path_leading_slash_stripped(self):
        """Leading slashes are stripped from file_path."""
        issue = LLMIssue.model_validate({"file_path": "/src/main.py", "line_number": 1})
        assert not issue.file_path.startswith("/")
        assert issue.file_path == "src/main.py"

    def test_file_path_null_bytes_stripped(self):
        """Null bytes in file_path are removed."""
        issue = LLMIssue.model_validate({"file_path": "src/\x00main.py", "line_number": 1})
        assert "\x00" not in issue.file_path

    def test_file_path_truncated_to_max_length(self):
        """Extremely long file paths are truncated."""
        long_path = "a" * 2000
        issue = LLMIssue.model_validate({"file_path": long_path, "line_number": 1})
        assert len(issue.file_path) <= 1024

    # -- New validators for string sanitization --

    def test_description_nonprintable_chars_stripped(self):
        """Non-printable characters in description are removed."""
        issue = LLMIssue.model_validate({
            "file_path": "a.py",
            "line_number": 1,
            "description": "bug\x00\x01\x02 here",
        })
        assert "\x00" not in issue.description
        assert "\x01" not in issue.description
        assert "bug here" in issue.description

    def test_description_truncated_to_max_length(self):
        """Extremely long descriptions are truncated."""
        long_desc = "x" * 10000
        issue = LLMIssue.model_validate({
            "file_path": "a.py",
            "line_number": 1,
            "description": long_desc,
        })
        assert len(issue.description) <= 8192

    def test_suggested_fix_nonprintable_chars_stripped(self):
        """Non-printable characters in suggested_fix are removed."""
        issue = LLMIssue.model_validate({
            "file_path": "a.py",
            "line_number": 1,
            "suggested_fix": "fix\x00me",
        })
        assert "\x00" not in issue.suggested_fix

    def test_code_snippet_truncated_to_max_length(self):
        """Extremely long code snippets are truncated."""
        long_code = "x = 1\n" * 5000
        issue = LLMIssue.model_validate({
            "file_path": "a.py",
            "line_number": 1,
            "code_snippet": long_code,
        })
        assert len(issue.code_snippet) <= 16384

    # -- Edge case: float line_number --

    def test_float_line_number_coerced(self):
        """Float line numbers are coerced to int."""
        data = {"file_path": "a.py", "line_number": 3.14}
        issue = LLMIssue.model_validate(data)
        assert issue.line_number == 3


# ---------------------------------------------------------------------------
# LLMScanResponse tests
# ---------------------------------------------------------------------------

class TestLLMScanResponse:
    """Tests for LLMScanResponse Pydantic model."""

    def test_valid_response_with_issues(self):
        """Standard response with issues list."""
        data = {
            "issues": [
                {"file": "a.py", "line_number": 1, "description": "issue 1"},
                {"file_path": "b.py", "line": 2, "description": "issue 2"},
            ]
        }
        resp = LLMScanResponse.model_validate(data)
        assert len(resp.issues) == 2
        assert resp.issues[0].file_path == "a.py"
        assert resp.issues[1].line_number == 2

    def test_empty_issues_list(self):
        """Response with no issues."""
        resp = LLMScanResponse.model_validate({"issues": []})
        assert resp.issues == []

    def test_missing_issues_key(self):
        """Response without 'issues' key defaults to empty list."""
        resp = LLMScanResponse.model_validate({})
        assert resp.issues == []

    def test_issues_is_none(self):
        """issues=None is coerced to empty list."""
        resp = LLMScanResponse.model_validate({"issues": None})
        assert resp.issues == []

    def test_issues_is_not_list(self):
        """Non-list issues value is coerced to empty list."""
        resp = LLMScanResponse.model_validate({"issues": "not a list"})
        assert resp.issues == []

    def test_issues_is_dict(self):
        """Dict issues value is coerced to empty list."""
        resp = LLMScanResponse.model_validate({"issues": {"key": "value"}})
        assert resp.issues == []

    def test_extra_top_level_fields_ignored(self):
        """Extra top-level fields are silently ignored."""
        resp = LLMScanResponse.model_validate({
            "issues": [],
            "summary": "everything looks good",
            "confidence": 0.95,
        })
        assert resp.issues == []

    def test_partial_issue_data(self):
        """Issues with only some fields still validate."""
        data = {"issues": [{"file": "x.py"}]}
        resp = LLMScanResponse.model_validate(data)
        assert len(resp.issues) == 1
        assert resp.issues[0].file_path == "x.py"
        assert resp.issues[0].line_number == 0
        assert resp.issues[0].description == ""

    def test_invalid_issue_in_list_raises(self):
        """A completely invalid item (e.g. a string) in issues list raises."""
        data = {"issues": ["not a dict"]}
        with pytest.raises(ValidationError):
            LLMScanResponse.model_validate(data)

    def test_issues_with_malformed_content_sanitized(self):
        """Issues containing non-printable chars are sanitized."""
        data = {
            "issues": [{
                "file_path": "a.py",
                "line_number": 1,
                "description": "bug\x00\x01with\x02nulls",
            }]
        }
        resp = LLMScanResponse.model_validate(data)
        assert len(resp.issues) == 1
        assert "\x00" not in resp.issues[0].description

    def test_negative_line_numbers_coerced(self):
        """Negative line numbers in issues are coerced to 0."""
        data = {
            "issues": [{
                "file_path": "a.py",
                "line_number": -1,
                "description": "bug",
            }]
        }
        resp = LLMScanResponse.model_validate(data)
        assert resp.issues[0].line_number == 0

    def test_directory_traversal_in_issue_sanitized(self):
        """Issue file paths with ../ are sanitized."""
        data = {
            "issues": [{
                "file_path": "../../dangerous.py",
                "line_number": 1,
            }]
        }
        resp = LLMScanResponse.model_validate(data)
        assert ".." not in resp.issues[0].file_path

    def test_multiple_issues_with_some_invalid(self):
        """Mix of valid and borderline issues all validate."""
        data = {
            "issues": [
                {"file": "a.py", "line_number": 1, "description": "ok"},
                {"file_path": None, "line_number": None, "description": None},
                {"file": "b.py", "line_number": -5, "description": "neg line"},
                {"file": "../../traverse.py", "line_number": 42},
            ]
        }
        resp = LLMScanResponse.model_validate(data)
        assert len(resp.issues) == 4


# ---------------------------------------------------------------------------
# LLMToolCall tests
# ---------------------------------------------------------------------------

class TestLLMToolCall:
    """Tests for LLMToolCall Pydantic model."""

    def test_valid_tool_call(self):
        tc = LLMToolCall.model_validate({
            "tool_name": "search_text",
            "arguments": {"patterns": ["foo"]},
        })
        assert tc.tool_name == "search_text"
        assert tc.arguments == {"patterns": ["foo"]}

    def test_empty_arguments_default(self):
        tc = LLMToolCall.model_validate({"tool_name": "list_directory"})
        assert tc.arguments == {}

    def test_missing_tool_name_raises(self):
        with pytest.raises(ValidationError):
            LLMToolCall.model_validate({"arguments": {}})

    def test_unknown_tool_name_accepted_with_warning(self):
        """Unknown tool names are accepted but logged as warning."""
        tc = LLMToolCall.model_validate({
            "tool_name": "hack_the_planet",
            "arguments": {},
        })
        assert tc.tool_name == "hack_the_planet"

    def test_known_tool_name_validates(self):
        """Known tool names pass validation."""
        for tool_name in ["search_text", "read_file", "find_usages", "symbol_exists"]:
            tc = LLMToolCall.model_validate({"tool_name": tool_name})
            assert tc.tool_name == tool_name

    def test_null_arguments_coerced_to_dict(self):
        """None arguments are coerced to empty dict."""
        tc = LLMToolCall.model_validate({
            "tool_name": "search_text",
            "arguments": None,
        })
        assert tc.arguments == {}

    def test_non_dict_arguments_coerced_to_dict(self):
        """Non-dict arguments are coerced to empty dict."""
        tc = LLMToolCall.model_validate({
            "tool_name": "search_text",
            "arguments": "not a dict",
        })
        assert tc.arguments == {}

    def test_extra_fields_ignored_in_tool_call(self):
        """Extra fields from LLM are ignored in tool calls."""
        tc = LLMToolCall.model_validate({
            "tool_name": "read_file",
            "arguments": {"file_path": "a.py"},
            "malicious_field": "should_be_dropped",
        })
        assert tc.tool_name == "read_file"
        assert not hasattr(tc, "malicious_field")


# ---------------------------------------------------------------------------
# LLMToolCallResponse tests
# ---------------------------------------------------------------------------

class TestLLMToolCallResponse:
    """Tests for LLMToolCallResponse Pydantic model."""

    def test_valid_tool_call_response(self):
        data = {
            "tool_calls": [
                {"tool_name": "search_text", "arguments": {"patterns": ["x"]}},
                {"tool_name": "read_file", "arguments": {"file_path": "a.py"}},
            ]
        }
        resp = LLMToolCallResponse.model_validate(data)
        assert len(resp.tool_calls) == 2
        assert resp.tool_calls[0].tool_name == "search_text"

    def test_is_tool_call_true(self):
        assert LLMToolCallResponse.is_tool_call({"tool_calls": []}) is True

    def test_is_tool_call_false(self):
        assert LLMToolCallResponse.is_tool_call({"issues": []}) is False

    def test_model_dump_roundtrip(self):
        """Validate → dump → validate roundtrip."""
        data = {"tool_calls": [{"tool_name": "read_file", "arguments": {"file_path": "a.py"}}]}
        resp = LLMToolCallResponse.model_validate(data)
        dumped = resp.model_dump()
        resp2 = LLMToolCallResponse.model_validate(dumped)
        assert resp2.tool_calls[0].tool_name == "read_file"

    def test_empty_tool_calls_raises(self):
        """tool_calls key missing raises validation error."""
        with pytest.raises(ValidationError):
            LLMToolCallResponse.model_validate({})

    def test_tool_calls_with_nonprintable_content_sanitized(self):
        """Tool call arguments with non-printable chars are sanitized."""
        data = {
            "tool_calls": [{
                "tool_name": "search_text",
                "arguments": {"patterns": ["foo\x00bar"]},
            }]
        }
        resp = LLMToolCallResponse.model_validate(data)
        assert len(resp.tool_calls) == 1
        assert "\x00" not in resp.tool_calls[0].arguments["patterns"][0]

    def test_extra_top_level_fields_in_tool_call_response_ignored(self):
        """Extra fields in tool call response are ignored."""
        data = {
            "tool_calls": [{"tool_name": "read_file", "arguments": {}}],
            "summary": "some text",
            "extra": True,
        }
        resp = LLMToolCallResponse.model_validate(data)
        assert len(resp.tool_calls) == 1

    def test_tool_calls_nested_dict_sanitized(self):
        """Nested dicts in tool call arguments are sanitized."""
        data = {
            "tool_calls": [{
                "tool_name": "read_file",
                "arguments": {
                    "nested": {"value": "clean\x00me", "deeper": {"x": "y\x01z"}},
                },
            }]
        }
        resp = LLMToolCallResponse.model_validate(data)
        args = resp.tool_calls[0].arguments
        assert "\x00" not in str(args)
        assert "\x01" not in str(args)


# ---------------------------------------------------------------------------
# LLMToolResult tests
# ---------------------------------------------------------------------------

class TestLLMToolResult:
    """Tests for LLMToolResult Pydantic model."""

    def test_valid_success_result(self):
        res = LLMToolResult(
            success=True,
            data={"matches": [{"file": "a.py", "line": 1}]},
        )
        assert res.success is True
        assert res.data == {"matches": [{"file": "a.py", "line": 1}]}
        assert res.error is None
        assert res.warning is None

    def test_valid_failure_result(self):
        res = LLMToolResult(
            success=False,
            data=None,
            error="Tool execution failed: file not found",
        )
        assert res.success is False
        assert res.error == "Tool execution failed: file not found"

    def test_error_message_nonprintable_stripped(self):
        res = LLMToolResult(success=False, error="fail\x00\x01ed")
        assert "\x00" not in res.error
        assert "\x01" not in res.error
        assert res.error == "failed"

    def test_warning_message_nonprintable_stripped(self):
        res = LLMToolResult(success=True, data="ok", warning="warn\x00ing")
        assert "\x00" not in res.warning
        assert res.warning == "warning"

    def test_string_data_sanitized(self):
        res = LLMToolResult(success=True, data="clean\x00me\x01please")
        assert "\x00" not in str(res.data)
        assert "\x01" not in str(res.data)

    def test_dict_data_sanitized(self):
        res = LLMToolResult(success=True, data={"key": "value\x00bad"})
        assert "\x00" not in res.data["key"]

    def test_list_data_sanitized(self):
        res = LLMToolResult(success=True, data=["item1\x00", "item2\x01"])
        assert "\x00" not in res.data[0]
        assert "\x01" not in res.data[1]

    def test_missing_optional_fields_default(self):
        res = LLMToolResult(success=True, data=["result"])
        assert res.error is None
        assert res.warning is None

    def test_warning_with_success(self):
        res = LLMToolResult(success=True, data="ok", warning="partial results")
        assert res.success is True
        assert res.warning == "partial results"

    def test_long_error_truncated(self):
        long_error = "e" * 10000
        res = LLMToolResult(success=False, error=long_error)
        assert len(res.error) <= 8192

    def test_long_warning_truncated(self):
        long_warning = "w" * 10000
        res = LLMToolResult(success=True, data="ok", warning=long_warning)
        assert len(res.warning) <= 8192

    def test_dict_data_nested_sanitized(self):
        res = LLMToolResult(success=True, data={
            "nested": {"deep": "value\x00bad"},
            "list_val": [{"x": "y\x01z"}],
        })
        data_str = str(res.data)
        assert "\x00" not in data_str
        assert "\x01" not in data_str

    def test_non_string_non_dict_non_list_data_preserved(self):
        res = LLMToolResult(success=True, data=42)
        assert res.data == 42

    def test_non_string_non_dict_non_list_data_none_accepted(self):
        res = LLMToolResult(success=True, data=None)
        assert res.data is None


# ---------------------------------------------------------------------------
# sanitize_llm_response tests
# ---------------------------------------------------------------------------

class TestSanitizeLLMResponse:
    """Tests for the sanitize_llm_response utility function."""

    def test_clean_dict_passes_through(self):
        data = {"key": "value", "num": 42, "flag": True, "none": None}
        result = sanitize_llm_response(data)
        assert result == data

    def test_null_bytes_in_string_values_stripped(self):
        data = {"key": "val\x00ue"}
        result = sanitize_llm_response(data)
        assert result["key"] == "value"

    def test_nonprintable_chars_in_string_values_stripped(self):
        data = {"key": "val\x01\x02ue"}
        result = sanitize_llm_response(data)
        assert result["key"] == "value"

    def test_long_string_truncated(self):
        data = {"key": "a" * 10000}
        result = sanitize_llm_response(data)
        assert len(result["key"]) <= 8192

    def test_nested_dict_sanitized(self):
        data = {"outer": {"inner": "val\x00ue"}}
        result = sanitize_llm_response(data)
        assert result["outer"]["inner"] == "value"

    def test_list_of_strings_sanitized(self):
        data = {"items": ["a\x00", "b\x01", "c"]}
        result = sanitize_llm_response(data)
        assert result["items"] == ["a", "b", "c"]

    def test_list_of_dicts_sanitized(self):
        data = {"items": [{"x": "y\x00z"}, {"a": "b\x01c"}]}
        result = sanitize_llm_response(data)
        assert result["items"][0]["x"] == "yz"
        assert result["items"][1]["a"] == "bc"

    def test_int_float_bool_none_preserved(self):
        data = {
            "int_val": 42,
            "float_val": 3.14,
            "bool_val": True,
            "none_val": None,
            "neg_val": -1,
        }
        result = sanitize_llm_response(data)
        assert result == data

    def test_non_standard_types_converted_to_string(self):
        data = {"complex": complex(1, 2)}
        result = sanitize_llm_response(data)
        assert isinstance(result["complex"], str)

    def test_deeply_nested_structure_sanitized(self):
        data = {
            "level1": {
                "level2": {
                    "level3": ["a\x00b\x01c", {"d": "e\x02f"}],
                },
            },
        }
        result = sanitize_llm_response(data)
        assert result["level1"]["level2"]["level3"][0] == "abc"
        assert result["level1"]["level2"]["level3"][1]["d"] == "ef"

    def test_issues_like_response_sanitized(self):
        data = {
            "issues": [
                {
                    "file_path": "src/\x00main.py",
                    "line_number": 42,
                    "description": "bug\x01here",
                },
            ],
        }
        result = sanitize_llm_response(data)
        assert "\x00" not in result["issues"][0]["file_path"]
        assert "\x01" not in result["issues"][0]["description"]


# ---------------------------------------------------------------------------
# _sanitize_string tests
# ---------------------------------------------------------------------------

class TestSanitizeString:
    """Tests for the internal _sanitize_string function."""

    def test_null_bytes_stripped(self):
        assert _sanitize_string("a\x00b", 100) == "ab"

    def test_nonprintable_chars_stripped(self):
        assert _sanitize_string("a\x01b\x02c", 100) == "abc"

    def test_tabs_newlines_preserved(self):
        assert _sanitize_string("a\tb\nc", 100) == "a\tb\nc"

    def test_truncation_at_max_length(self):
        assert len(_sanitize_string("x" * 100, 10)) == 10

    def test_empty_string(self):
        assert _sanitize_string("", 100) == ""

    def test_only_nonprintable(self):
        assert _sanitize_string("\x00\x01\x02", 100) == ""


# ---------------------------------------------------------------------------
# Issue.from_llm_response integration tests
# ---------------------------------------------------------------------------

class TestIssueFromLLMResponse:
    """Tests for Issue.from_llm_response() with Pydantic validation."""

    def test_from_raw_dict(self):
        """Raw dict is validated through LLMIssue."""
        data = {"file": "src/main.py", "line": 42, "description": "bug", "fix": "fix it"}
        issue = Issue.from_llm_response(data, check_query="find bugs")
        assert issue.file_path == "src/main.py"
        assert issue.line_number == 42
        assert issue.description == "bug"
        assert issue.suggested_fix == "fix it"
        assert issue.check_query == "find bugs"

    def test_from_llm_issue(self):
        """Pre-validated LLMIssue is accepted directly."""
        llm_issue = LLMIssue(file_path="a.py", line_number=10, description="test")
        issue = Issue.from_llm_response(llm_issue, check_query="check")
        assert issue.file_path == "a.py"
        assert issue.line_number == 10

    def test_none_values_coerced(self):
        """None values in raw dict are coerced properly."""
        data = {"file": None, "line_number": None, "description": None}
        issue = Issue.from_llm_response(data, check_query="q")
        assert issue.file_path == ""
        assert issue.line_number == 0
        assert issue.description == ""

    def test_custom_timestamp(self):
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        data = {"file_path": "a.py", "line_number": 1}
        issue = Issue.from_llm_response(data, check_query="q", timestamp=ts)
        assert issue.timestamp == ts

    def test_default_timestamp(self):
        data = {"file_path": "a.py", "line_number": 1}
        issue = Issue.from_llm_response(data, check_query="q")
        assert isinstance(issue.timestamp, datetime)
        assert issue.timestamp.tzinfo is not None

    def test_malformed_file_path_sanitized(self):
        """LLM hallucinated file paths with nulls/traversal are sanitized."""
        data = {"file_path": "../../\x00etc/passwd", "line_number": 1}
        issue = Issue.from_llm_response(data, check_query="q")
        assert ".." not in issue.file_path
        assert "\x00" not in issue.file_path

    def test_negative_line_number_coerced(self):
        """Negative line number is coerced to 0."""
        data = {"file_path": "a.py", "line_number": -10}
        issue = Issue.from_llm_response(data, check_query="q")
        assert issue.line_number == 0

    def test_long_description_truncated(self):
        """Extremely long descriptions are truncated."""
        data = {"file_path": "a.py", "description": "x" * 20000}
        issue = Issue.from_llm_response(data, check_query="q")
        assert len(issue.description) <= 8192
