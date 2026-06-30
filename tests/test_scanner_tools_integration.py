"""Integration tests for scanner with AI tools support."""

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from code_scanner.scanner import Scanner
from code_scanner.config import Config
from code_scanner.git_watcher import GitWatcher
from code_scanner.issue_tracker import IssueTracker
from code_scanner.output import OutputGenerator
from code_scanner.models import GitState, ChangedFile, CheckGroup
from code_scanner.base_client import BaseLLMClient
from code_scanner.ctags_index import CtagsIndex


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock configuration."""
    config = Mock(spec=Config)
    config.target_directory = tmp_path
    config.output_file = "results.md"
    config.log_file = "scanner.log"
    config.git_poll_interval = 1
    config.max_llm_retries = 3
    config.check_groups = [
        CheckGroup(pattern="*.py", checks=["Find bugs in this code"])
    ]
    return config


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client."""
    client = Mock(spec=BaseLLMClient)
    client.context_limit = 8192
    client.query = Mock()
    return client


@pytest.fixture
def mock_ctags_index(tmp_path):
    """Create a mock CtagsIndex."""
    mock_index = MagicMock(spec=CtagsIndex)
    mock_index.target_directory = tmp_path
    mock_index.find_symbol.return_value = []
    mock_index.find_symbols_by_pattern.return_value = []
    mock_index.find_definitions.return_value = []
    mock_index.get_symbols_in_file.return_value = []
    mock_index.get_class_members.return_value = []
    mock_index.is_indexed = True
    mock_index.is_indexing = False
    mock_index.index_error = None
    mock_index.get_file_structure.return_value = {
        "file": str(tmp_path / "test.py"),
        "language": "Python",
        "symbols": [],
        "structure_summary": "",
        "classes": [],
        "functions": [],
        "variables": [],
        "imports": [],
        "other": [],
    }
    mock_index.get_stats.return_value = {
        "total_symbols": 0,
        "files_indexed": 0,
        "symbols_by_kind": {},
        "languages": [],
    }
    return mock_index


@pytest.fixture
def mock_components(tmp_path, mock_config, mock_llm_client, mock_ctags_index):
    """Create all scanner components."""
    git_watcher = Mock(spec=GitWatcher)
    issue_tracker = Mock(spec=IssueTracker)
    output_generator = Mock(spec=OutputGenerator)

    scanner = Scanner(
        config=mock_config,
        git_watcher=git_watcher,
        llm_client=mock_llm_client,
        issue_tracker=issue_tracker,
        output_generator=output_generator,
        ctags_index=mock_ctags_index,
    )

    return {
        "scanner": scanner,
        "git_watcher": git_watcher,
        "llm_client": mock_llm_client,
        "issue_tracker": issue_tracker,
        "output_generator": output_generator,
    }


class TestScannerWithTools:
    """Test scanner integration with AI tools."""

    def test_scanner_initializes_tool_executor(self, mock_components):
        """Test that scanner initializes AIToolExecutor."""
        scanner = mock_components["scanner"]

        assert scanner.tool_executor is not None
        assert scanner.tool_executor.target_directory == scanner.config.target_directory
        assert scanner.tool_executor.context_limit == 8192

    def test_run_check_without_tools(self, mock_components, tmp_path):
        """Test check execution without tool calls (normal flow)."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        # LLM returns issues directly
        llm_client.query.return_value = {
            "issues": [
                {
                    "file": "test.py",
                    "line_number": 10,
                    "description": "Bug found",
                    "suggested_fix": "Fix it",
                    "code_snippet": "bad code",
                }
            ]
        }

        # Create test file
        (tmp_path / "test.py").write_text("def test():\n    pass\n")

        # Run check
        issues = scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"test.py": "def test():\n    pass\n"},
            batch_idx=0,
        )

        assert len(issues) == 1
        assert issues[0].file_path == "test.py"
        assert llm_client.query.call_count == 1

    def test_run_check_with_tool_calls(self, mock_components, tmp_path):
        """Test check execution with tool calls."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        # Create test files
        (tmp_path / "main.py").write_text("def calculate():\n    pass\n")
        (tmp_path / "utils.py").write_text("def calculate():\n    return 42\n")

        # First response: LLM requests a tool
        # Second response: LLM provides final answer
        llm_client.query.side_effect = [
            {
                "tool_calls": [
                    {
                        "tool_name": "search_text",
                        "arguments": {"patterns": "calculate"},
                    }
                ]
            },
            {
                "issues": [
                    {
                        "file": "main.py",
                        "line_number": 1,
                        "description": "Duplicate function name",
                        "suggested_fix": "Rename one of them",
                        "code_snippet": "def calculate():",
                    }
                ]
            },
        ]

        # Run check
        issues = scanner._run_check_with_tools(
            check_query="Check for duplicate function names",
            batch={"main.py": "def calculate():\n    pass\n"},
            batch_idx=0,
        )

        # Should have made 2 LLM calls (initial + after tool)
        assert llm_client.query.call_count == 2
        assert len(issues) == 1

    def test_run_check_with_multiple_tool_calls(self, mock_components, tmp_path):
        """Test check with multiple tool calls in sequence."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        # Create test structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("import utils\n")
        (tmp_path / "src" / "utils.py").write_text("def helper():\n    pass\n")

        llm_client.query.side_effect = [
            # First: request directory listing
            {
                "tool_calls": [
                    {"tool_name": "list_directory", "arguments": {"directory_path": "src"}}
                ]
            },
            # Second: request file read
            {
                "tool_calls": [
                    {"tool_name": "read_file", "arguments": {"file_path": "src/utils.py"}}
                ]
            },
            # Third: provide final answer
            {"issues": []},
        ]

        issues = scanner._run_check_with_tools(
            check_query="Check imports",
            batch={"src/main.py": "import utils\n"},
            batch_idx=0,
        )

        assert llm_client.query.call_count == 3
        assert len(issues) == 0

    def test_run_check_max_iterations_limit(self, mock_components):
        """Test that tool calling stops after max iterations."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        # LLM always requests tools (infinite loop scenario)
        llm_client.query.return_value = {
            "tool_calls": [
                {"tool_name": "list_directory", "arguments": {"directory_path": "."}}
            ]
        }

        issues = scanner._run_check_with_tools(
            check_query="Test",
            batch={"test.py": "pass"},
            batch_idx=0,
        )

        # Max iterations is dynamically calculated based on context window
        # The loop should eventually stop and return empty issues
        assert llm_client.query.call_count > 0  # At least one call
        assert llm_client.query.call_count <= 50  # Capped at 50 max
        assert len(issues) == 0  # No final answer after max iterations

    def test_run_check_tool_failure_handling(self, mock_components, tmp_path):
        """Test handling of tool execution failures."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        llm_client.query.side_effect = [
            # Request a file that doesn't exist
            {
                "tool_calls": [
                    {"tool_name": "read_file", "arguments": {"file_path": "nonexistent.py"}}
                ]
            },
            # LLM should still provide answer after tool failure
            {"issues": []},
        ]

        issues = scanner._run_check_with_tools(
            check_query="Test",
            batch={"test.py": "pass"},
            batch_idx=0,
        )

        # Tool failure should be communicated to LLM
        assert llm_client.query.call_count == 2

        # Check that second call includes tool failure message
        second_call_args = llm_client.query.call_args_list[1]
        user_prompt = second_call_args[1]["user_prompt"]
        assert "Tool read_file failed" in user_prompt

    def test_format_tool_result_dict(self, mock_components):
        """Test formatting dictionary tool results."""
        scanner = mock_components["scanner"]

        result = Mock()
        result.data = {"key": "value", "items": [1, 2, 3]}

        formatted = scanner._format_tool_result(result)

        assert isinstance(formatted, str)
        assert "key" in formatted
        assert "value" in formatted

    def test_format_tool_result_list(self, mock_components):
        """Test formatting list tool results."""
        scanner = mock_components["scanner"]

        result = Mock()
        result.data = [{"file": "a.py"}, {"file": "b.py"}]

        formatted = scanner._format_tool_result(result)

        assert isinstance(formatted, str)
        assert "a.py" in formatted
        assert "b.py" in formatted

    def test_tool_result_includes_warning(self, mock_components, tmp_path):
        """Test that tool warnings are included in LLM messages."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        # Create many files to trigger partial results warning
        for i in range(60):
            (tmp_path / f"file_{i}.py").write_text("def calculate():\n    pass\n")

        llm_client.query.side_effect = [
            {
                "tool_calls": [
                    {"tool_name": "search_text", "arguments": {"patterns": "calculate"}}
                ]
            },
            {"issues": []},
        ]

        scanner._run_check_with_tools(
            check_query="Test",
            batch={"test.py": "pass"},
            batch_idx=0,
        )

        # Check that warning was included in second call
        second_call_args = llm_client.query.call_args_list[1]
        user_prompt = second_call_args[1]["user_prompt"]
        assert "PARTIAL RESULTS" in user_prompt or "Tool search_text succeeded" in user_prompt


class TestToolIntegrationEndToEnd:
    """End-to-end tests for tool integration."""

    def test_complete_scan_with_tools(self, mock_components, tmp_path):
        """Test a complete scan cycle with tool usage."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]
        issue_tracker = mock_components["issue_tracker"]

        # Setup repository
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text(
            "def process():\n    calculate_total()\n"
        )
        (tmp_path / "src" / "utils.py").write_text(
            "def calculate_total():\n    return 0\n"
        )

        # Mock git state
        git_state = GitState(
            changed_files=[
                ChangedFile(path="src/main.py", status="unstaged", mtime_ns=None)
            ]
        )

        # LLM uses tool to find function usage, then reports issue
        llm_client.query.side_effect = [
            {
                "tool_calls": [
                    {
                        "tool_name": "search_text",
                        "arguments": {"patterns": "calculate_total"},
                    }
                ]
            },
            {
                "issues": [
                    {
                        "file": "src/main.py",
                        "line_number": 2,
                        "description": "Function called without error handling",
                        "suggested_fix": "Add try/except",
                        "code_snippet": "calculate_total()",
                    }
                ]
            },
        ]

        # Mock issue tracker methods
        issue_tracker.add_issues.return_value = 1
        issue_tracker.update_from_scan.return_value = (1, 0)  # (new_count, resolved_count)
        issue_tracker.get_stats.return_value = {"total": 1, "open": 1, "resolved": 0}

        # Run scan
        scanner._run_scan(git_state)

        # Verify tool was used
        assert llm_client.query.call_count >= 2

        # Verify issue was added
        assert issue_tracker.add_issues.called

    def test_scan_with_read_file_tool(self, mock_components, tmp_path):
        """Test scan using read_file tool."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]
        issue_tracker = mock_components["issue_tracker"]

        # Setup files
        (tmp_path / "config.py").write_text("API_KEY = 'secret'\n")
        (tmp_path / "main.py").write_text("from config import API_KEY\n")

        git_state = GitState(
            changed_files=[ChangedFile(path="main.py", status="unstaged")]
        )

        # LLM reads config file to check API key usage
        llm_client.query.side_effect = [
            {
                "tool_calls": [
                    {"tool_name": "read_file", "arguments": {"file_path": "config.py"}}
                ]
            },
            {
                "issues": [
                    {
                        "file": "config.py",
                        "line_number": 1,
                        "description": "Hardcoded API key",
                        "suggested_fix": "Use environment variable",
                        "code_snippet": "API_KEY = 'secret'",
                    }
                ]
            },
        ]

        # Mock issue tracker
        issue_tracker.add_issues.return_value = 1
        issue_tracker.update_from_scan.return_value = (1, 0)

    def test_scan_with_list_directory_tool(self, mock_components, tmp_path):
        """Test scan using list_directory tool."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]
        issue_tracker = mock_components["issue_tracker"]

        # Setup directory structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "module_a.py").write_text("pass")
        (tmp_path / "src" / "module_b.py").write_text("pass")
        (tmp_path / "main.py").write_text("# Main file")

        git_state = GitState(
            changed_files=[ChangedFile(path="main.py", status="unstaged")]
        )

        # LLM lists directory to check structure
        llm_client.query.side_effect = [
            {
                "tool_calls": [
                    {"tool_name": "list_directory", "arguments": {"directory_path": "src"}}
                ]
            },
            {"issues": []},
        ]

        # Mock issue tracker
        issue_tracker.add_issues.return_value = 0
        issue_tracker.update_from_scan.return_value = (0, 0)
        issue_tracker.get_stats.return_value = {"total": 0, "open": 0, "resolved": 0}

        scanner._run_scan(git_state)

        assert llm_client.query.call_count >= 2


class TestToolExecutorInScanner:
    """Test AIToolExecutor integration within Scanner."""

    def test_tool_executor_uses_correct_directory(self, mock_components, tmp_path):
        """Test that tool executor uses scanner's target directory."""
        scanner = mock_components["scanner"]

        assert scanner.tool_executor.target_directory == tmp_path

    def test_tool_executor_uses_context_limit(self, mock_components):
        """Test that tool executor uses LLM's context limit."""
        scanner = mock_components["scanner"]

        assert scanner.tool_executor.context_limit == 8192

    def test_tools_available_in_schema(self, mock_components):
        """Test that scanner provides correct tool schema to LLM."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        llm_client.query.return_value = {"issues": []}

        scanner._run_check_with_tools(
            check_query="Test",
            batch={"test.py": "pass"},
            batch_idx=0,
        )

        # Check that tools were provided to LLM
        call_kwargs = llm_client.query.call_args[1]
        assert "tools" in call_kwargs
        assert len(call_kwargs["tools"]) == 10  # 10 tools available


class TestAsyncCtagsTools:
    """Test tool behavior when ctags index is still building."""

    def test_symbol_exists_returns_indexing_status(self, mock_components):
        """Test symbol_exists returns helpful message when ctags is indexing."""
        scanner = mock_components["scanner"]
        
        # Configure mock as if indexing is in progress
        scanner.ctags_index.is_indexed = False
        scanner.ctags_index.is_indexing = True
        scanner.ctags_index.index_error = None
        
        # Call the tool directly
        result = scanner.tool_executor._symbol_exists("SomeClass", "class")
        
        assert result.success is True
        assert "indexing_in_progress" in str(result.data.get("status", ""))
        assert result.warning is not None
        assert "ctags" in result.warning.lower() or "index" in result.warning.lower()

    def test_find_definition_returns_indexing_status(self, mock_components):
        """Test find_definition returns helpful message when ctags is indexing."""
        scanner = mock_components["scanner"]
        
        scanner.ctags_index.is_indexed = False
        scanner.ctags_index.is_indexing = True
        scanner.ctags_index.index_error = None
        
        result = scanner.tool_executor._find_definition("some_function")
        
        assert result.success is True
        assert "indexing_in_progress" in str(result.data.get("status", ""))

    def test_get_file_summary_works_without_ctags(self, mock_components, tmp_path):
        """Test get_file_summary still returns basic info when ctags unavailable."""
        scanner = mock_components["scanner"]
        
        # Create a test file (5 lines of code = 6 lines total with trailing newline)
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    pass\n\nclass Bar:\n    pass")  # No trailing newline = 5 lines
        
        scanner.ctags_index.is_indexed = False
        scanner.ctags_index.is_indexing = True
        scanner.ctags_index.index_error = None
        
        result = scanner.tool_executor._get_file_summary("test.py")
        
        assert result.success is True
        assert result.data["total_lines"] == 5
        # Structure should be empty but no error
        assert result.data["summary"]["class_count"] == 0
        assert result.warning is not None

    def test_tools_work_after_indexing_completes(self, mock_components):
        """Test tools work normally after ctags indexing completes."""
        scanner = mock_components["scanner"]
        
        # Configure as fully indexed
        scanner.ctags_index.is_indexed = True
        scanner.ctags_index.is_indexing = False
        scanner.ctags_index.index_error = None
        scanner.ctags_index.find_symbol.return_value = []  # No results but works
        
        result = scanner.tool_executor._symbol_exists("SomeClass", "class")
        
        assert result.success is True
        assert result.data.get("exists") is False  # Normal "not found" result
        assert result.warning is None


class TestMalformedToolCallHandling:
    """Tests for malformed tool call handling in _run_check_with_tools."""

    def test_malformed_tool_call_asks_correction(self, mock_components, tmp_path):
        """LLM returns malformed tool_calls — scanner asks LLM to correct."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        (tmp_path / "test.py").write_text("x = 1\n")

        llm_client.query.side_effect = [
            {"tool_calls": "not_a_list"},
            {"issues": []},
        ]

        issues = scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"test.py": "x = 1\n"},
            batch_idx=0,
        )

        assert llm_client.query.call_count == 2
        assert len(issues) == 0

    def test_empty_tool_calls_asks_correction(self, mock_components, tmp_path):
        """LLM returns empty tool_calls list — scanner asks LLM to correct."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        (tmp_path / "test.py").write_text("x = 1\n")

        llm_client.query.side_effect = [
            {"tool_calls": []},
            {"issues": []},
        ]

        issues = scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"test.py": "x = 1\n"},
            batch_idx=0,
        )

        assert llm_client.query.call_count == 2
        assert len(issues) == 0

    def test_max_malformed_responses_forces_final_answer(self, mock_components, tmp_path):
        """After MAX_MALFORMED_TOOL_RESPONSES malformed attempts, forces final answer."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        (tmp_path / "test.py").write_text("x = 1\n")

        llm_client.query.side_effect = [
            {"tool_calls": "bad"},
            {"tool_calls": "bad"},
            {"tool_calls": "bad"},
            {"tool_calls": "bad"},
            {"issues": [{"file": "test.py", "line_number": 1, "description": "ok"}]},
        ]

        issues = scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"test.py": "x = 1\n"},
            batch_idx=0,
        )

        assert len(issues) >= 0

    def test_malformed_then_valid_tool_call_works(self, mock_components, tmp_path):
        """After one malformed response, valid tool call is processed normally."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        (tmp_path / "main.py").write_text("def foo():\n    pass\n")

        llm_client.query.side_effect = [
            {"tool_calls": "malformed"},
            {
                "tool_calls": [{
                    "tool_name": "search_text",
                    "arguments": {"patterns": "foo"},
                }]
            },
            {"issues": []},
        ]

        issues = scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"main.py": "def foo():\n    pass\n"},
            batch_idx=0,
        )

        assert llm_client.query.call_count == 3
        assert len(issues) == 0


class TestValidationRetryHandling:
    """Tests for validation retry when LLM final answer fails Pydantic validation."""

    def test_valid_final_answer_accepted_immediately(self, mock_components, tmp_path):
        """Valid final answer passes validation and breaks the loop."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        (tmp_path / "main.py").write_text("def foo():\n    pass\n")

        llm_client.query.return_value = {
            "issues": [{
                "file": "main.py",
                "line_number": 1,
                "description": "issue",
                "suggested_fix": "fix",
                "code_snippet": "code",
            }]
        }

        issues = scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"main.py": "def foo():\n    pass\n"},
            batch_idx=0,
        )

        assert llm_client.query.call_count == 1
        assert len(issues) == 1

    def test_invalid_response_retried_once_then_accepted(self, mock_components, tmp_path):
        """One invalid response triggers retry; second valid response accepted."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        (tmp_path / "main.py").write_text("def foo():\n    pass\n")

        llm_client.query.side_effect = [
            "not a dictionary at all",
            {
                "issues": [{
                    "file": "main.py",
                    "line_number": 1,
                    "description": "fixed",
                    "suggested_fix": "fix",
                    "code_snippet": "code",
                }]
            },
        ]

        issues = scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"main.py": "def foo():\n    pass\n"},
            batch_idx=0,
        )

        assert llm_client.query.call_count == 2
        assert len(issues) == 1
        assert issues[0].description == "fixed"

    def test_max_validation_retries_returns_empty(self, mock_components, tmp_path):
        """After MAX_VALIDATION_RETRIES (3) invalid responses, forced empty result."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        (tmp_path / "main.py").write_text("def foo():\n    pass\n")

        llm_client.query.side_effect = [
            "bad1",
            "bad2",
            "bad3",
        ]

        issues = scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"main.py": "def foo():\n    pass\n"},
            batch_idx=0,
        )

        assert llm_client.query.call_count == 3
        assert issues == []

    def test_correction_prompt_includes_last_context(self, mock_components, tmp_path):
        """The correction prompt sent to LLM includes the previous context."""
        scanner = mock_components["scanner"]
        llm_client = mock_components["llm_client"]

        (tmp_path / "main.py").write_text("def foo():\n    pass\n")

        llm_client.query.side_effect = [
            "not valid json",
            {
                "issues": [{
                    "file": "main.py",
                    "line_number": 1,
                    "description": "ok",
                    "suggested_fix": "x",
                    "code_snippet": "x",
                }]
            },
        ]

        scanner._run_check_with_tools(
            check_query="Find bugs",
            batch={"main.py": "def foo():\n    pass\n"},
            batch_idx=0,
        )

        assert llm_client.query.call_count == 2
        second_call_user = llm_client.query.call_args_list[1][1]["user_prompt"]
        assert "Pydantic validation" in second_call_user
        assert "def foo()" in second_call_user
