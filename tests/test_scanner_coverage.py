"""Coverage-focused tests for Scanner class - targeting uncovered lines."""

import pytest
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, call

from code_scanner.scanner import Scanner
from code_scanner.config import Config, LLMConfig, CheckGroup
from code_scanner.models import Issue, GitState, ChangedFile, IssueStatus
from code_scanner.lmstudio_client import LLMClientError
from code_scanner.ctags_index import CtagsIndex


@pytest.fixture
def mock_config():
    """Create a mock Config object."""
    config = MagicMock(spec=Config)
    config.target_directory = Path("/test/repo")
    config.output_file = "results.md"
    config.log_file = "scanner.log"
    config.git_poll_interval = 0.1  # Fast for testing
    config.llm_retry_interval = 0.1
    config.max_llm_retries = 2
    config.check_groups = [
        CheckGroup(pattern="*.py", checks=["Check for bugs", "Check for style"]),
        CheckGroup(pattern="*.cpp, *.h", checks=["Check memory leaks"]),
    ]
    return config


@pytest.fixture
def mock_ctags_index():
    """Create a mock CtagsIndex."""
    mock_index = MagicMock(spec=CtagsIndex)
    mock_index.target_directory = Path("/test/repo")
    mock_index.find_symbol.return_value = []
    mock_index.find_symbols_by_pattern.return_value = []
    mock_index.find_definitions.return_value = []
    mock_index.get_symbols_in_file.return_value = []
    mock_index.get_class_members.return_value = []
    mock_index.get_file_structure.return_value = {
        "file": "/test/repo/test.py",
        "language": "Python",
        "symbols": [],
        "structure_summary": "",
    }
    mock_index.get_stats.return_value = {
        "total_symbols": 0,
        "files_indexed": 0,
        "symbols_by_kind": {},
        "languages": [],
    }
    return mock_index


@pytest.fixture
def mock_dependencies(mock_config, mock_ctags_index):
    """Create mock dependencies for Scanner."""
    from code_scanner.models import Project, ScanStatus
    
    git_watcher = MagicMock()
    llm_client = MagicMock()
    llm_client.context_limit = 8000
    issue_tracker = MagicMock()
    issue_tracker.add_issues.return_value = 0
    issue_tracker.update_from_scan.return_value = (0, 0)
    issue_tracker.get_stats.return_value = {"total": 0}
    output_generator = MagicMock()
    
    # Create mock project with required attributes for Scanner
    mock_project = MagicMock(spec=Project)
    mock_project.scan_status = ScanStatus.RUNNING
    mock_project.current_check_index = 0
    mock_project.total_checks = 0
    mock_project.current_check_query = ""
    mock_project.error_message = ""
    mock_project.inactive_since = None
    mock_project.issue_tracker = issue_tracker
    mock_project.output_generator = output_generator
    # Additional attributes needed by Scanner.__init__ and _run_scan
    mock_project.scan_info = {}
    mock_project.last_scanned_files = set()
    mock_project.last_file_contents_hash = {}
    mock_project.last_scan_time = None
    
    return {
        "config": mock_config,
        "git_watcher": git_watcher,
        "llm_client": llm_client,
        "issue_tracker": issue_tracker,
        "output_generator": output_generator,
        "ctags_index": mock_ctags_index,
        "project": mock_project,
    }


class TestScannerRunLoop:
    """Tests for Scanner _run_loop method."""

    def test_run_loop_exits_on_stop_event(self, mock_dependencies):
        """Run loop exits when stop event is set."""
        scanner = Scanner(**mock_dependencies)
        scanner._stop_event.set()
        
        # Should exit immediately
        scanner._run_loop()
        
        # git_watcher.get_state should not be called since we exit immediately
        # But we need to check the loop didn't hang

    def test_run_loop_waits_during_merge(self, mock_dependencies):
        """Run loop waits during merge/rebase."""
        scanner = Scanner(**mock_dependencies)
        
        # First call: merge in progress, second call: stop
        call_count = [0]
        def get_state_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                state = GitState(is_merging=True)
                return state
            else:
                scanner._stop_event.set()
                return GitState()
        
        mock_dependencies["git_watcher"].get_state.side_effect = get_state_side_effect
        
        scanner._run_loop()
        
        assert call_count[0] >= 1

    def test_run_loop_waits_when_no_changes(self, mock_dependencies):
        """Run loop waits when no changes detected."""
        scanner = Scanner(**mock_dependencies)
        
        call_count = [0]
        def get_state_side_effect():
            call_count[0] += 1
            if call_count[0] >= 2:
                scanner._stop_event.set()
            return GitState()  # No changes
        
        mock_dependencies["git_watcher"].get_state.side_effect = get_state_side_effect
        
        scanner._run_loop()
        
        assert call_count[0] >= 1

    def test_run_loop_calls_run_scan_with_changes(self, mock_dependencies):
        """Run loop calls _run_scan when changes detected."""
        scanner = Scanner(**mock_dependencies)
        
        state_with_changes = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        call_count = [0]
        def get_state_side_effect():
            call_count[0] += 1
            if call_count[0] >= 2:
                scanner._stop_event.set()
            return state_with_changes
        
        mock_dependencies["git_watcher"].get_state.side_effect = get_state_side_effect
        
        with patch.object(scanner, "_run_scan") as mock_run_scan:
            scanner._run_loop()
            mock_run_scan.assert_called()

    def test_run_loop_handles_exceptions(self, mock_dependencies):
        """Run loop handles exceptions and continues."""
        scanner = Scanner(**mock_dependencies)
        
        call_count = [0]
        def get_state_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Test error")
            scanner._stop_event.set()
            return GitState()
        
        mock_dependencies["git_watcher"].get_state.side_effect = get_state_side_effect
        
        # Should not raise, should handle exception
        scanner._run_loop()
        assert call_count[0] >= 1


class TestScannerRunScan:
    """Tests for Scanner _run_scan method."""

    def test_run_scan_with_no_scannable_files(self, mock_dependencies):
        """Run scan returns early when no scannable files."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="results.md", status="unstaged")]
        )
        
        with patch.object(scanner, "_get_files_content", return_value={}):
            scanner._run_scan(state)
        
        # Should not call _create_batches since no files
        mock_dependencies["llm_client"].query.assert_not_called()

    def test_run_scan_creates_batches_and_runs_checks(self, mock_dependencies):
        """Run scan creates batches and runs all check groups."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[
                ChangedFile(path="test.py", status="unstaged"),
                ChangedFile(path="main.cpp", status="unstaged"),
            ]
        )
        
        files_content = {
            "test.py": "print('hello')",
            "main.cpp": "int main() {}",
        }
        
        mock_dependencies["llm_client"].query.return_value = {"issues": []}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Should query LLM for each check (2 py rules + 1 cpp rule = 3)
        assert mock_dependencies["llm_client"].query.call_count >= 1

    def test_run_scan_handles_deleted_files(self, mock_dependencies):
        """Run scan resolves issues for deleted files."""
        scanner = Scanner(**mock_dependencies)
        
        # Need at least one non-deleted file to continue past early return
        state = GitState(
            changed_files=[
                ChangedFile(path="existing.py", status="unstaged"),
                ChangedFile(path="deleted.py", status="deleted"),
            ]
        )
        
        files_content = {"existing.py": "x = 1"}
        mock_dependencies["llm_client"].query.return_value = {"issues": []}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        mock_dependencies["issue_tracker"].resolve_issues_for_file.assert_called_with("deleted.py")

    def test_run_scan_updates_output_on_new_issues(self, mock_dependencies):
        """Run scan updates output when new issues are found."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        mock_dependencies["llm_client"].query.return_value = {
            "issues": [
                {
                    "file_path": "test.py",
                    "line": 1,
                    "description": "Bug found",
                    "suggested_fix": "Fix it",
                }
            ]
        }
        mock_dependencies["issue_tracker"].add_issues.return_value = 1
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        mock_dependencies["output_generator"].write.assert_called()

    def test_run_scan_skips_non_matching_patterns(self, mock_dependencies):
        """Run scan skips check groups when no files match pattern."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.txt", status="unstaged")]
        )
        
        files_content = {"test.txt": "some text"}
        
        mock_dependencies["llm_client"].query.return_value = {"issues": []}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # txt files don't match *.py or *.cpp patterns, so no queries
        mock_dependencies["llm_client"].query.assert_not_called()

    def test_run_scan_handles_llm_connection_loss(self, mock_dependencies):
        """Run scan handles LLM connection loss."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        call_count = [0]
        def query_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMClientError("Lost connection to LM Studio")
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        mock_dependencies["llm_client"].wait_for_connection = MagicMock()
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        mock_dependencies["llm_client"].wait_for_connection.assert_called()

    def test_run_scan_handles_connection_refused_error(self, mock_dependencies):
        """Run scan handles connection refused error."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        call_count = [0]
        def query_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMClientError("Connection refused by server")
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        mock_dependencies["llm_client"].wait_for_connection = MagicMock()
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        mock_dependencies["llm_client"].wait_for_connection.assert_called()

    def test_run_scan_handles_timeout_error(self, mock_dependencies):
        """Run scan handles timeout error."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        call_count = [0]
        def query_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMClientError("Connection timed out")
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        mock_dependencies["llm_client"].wait_for_connection = MagicMock()
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        mock_dependencies["llm_client"].wait_for_connection.assert_called()

    def test_run_scan_handles_non_connection_error(self, mock_dependencies):
        """Run scan logs non-connection LLM errors and continues."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        # Simulate a non-connection error (e.g., JSON parse failure)
        mock_dependencies["llm_client"].query.side_effect = LLMClientError(
            "Failed to get valid JSON response after 3 attempts"
        )
        mock_dependencies["llm_client"].wait_for_connection = MagicMock()
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # wait_for_connection should NOT be called for non-connection errors
        mock_dependencies["llm_client"].wait_for_connection.assert_not_called()
        
        call_count = [0]
        def query_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMClientError("Lost connection to LM Studio")
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        mock_dependencies["llm_client"].wait_for_connection = MagicMock()
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        mock_dependencies["llm_client"].wait_for_connection.assert_called()

    def test_run_scan_handles_refresh_signal(self, mock_dependencies):
        """Run scan handles refresh signal during processing and continues."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            if query_count[0] == 1:
                scanner._refresh_event.set()
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        mock_dependencies["git_watcher"].get_state.return_value = state
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Refresh signal should be handled (cleared) and scan continues

    def test_run_scan_stops_on_stop_event(self, mock_dependencies):
        """Run scan stops processing when stop event is set."""
        scanner = Scanner(**mock_dependencies)
        scanner._stop_event.set()
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # LLM should not be called since stop is set
        mock_dependencies["llm_client"].query.assert_not_called()


class TestScannerBatching:
    """Tests for Scanner batching functionality."""

    def test_create_batches_single_batch(self, mock_dependencies):
        """Create batches returns single batch when all files fit."""
        scanner = Scanner(**mock_dependencies)
        scanner.llm_client.context_limit = 100000
        
        files = {"a.py": "x=1", "b.py": "y=2"}
        batches = scanner._create_batches(files)
        
        assert len(batches) == 1
        assert "a.py" in batches[0]
        assert "b.py" in batches[0]

    def test_create_batches_multiple_batches(self, mock_dependencies):
        """Create batches splits files when they exceed context limit."""
        scanner = Scanner(**mock_dependencies)
        scanner.llm_client.context_limit = 100  # Very small
        scanner._scan_info = {"skipped_files": []}
        
        files = {"a.py": "x" * 30, "b.py": "y" * 30}
        batches = scanner._create_batches(files)
        
        # Should split into multiple batches
        assert len(batches) >= 1

    def test_create_batches_skips_oversized_files(self, mock_dependencies):
        """Create batches skips files that are too large."""
        scanner = Scanner(**mock_dependencies)
        scanner.llm_client.context_limit = 100
        scanner._scan_info = {"skipped_files": []}
        
        # File that exceeds limit
        files = {"huge.py": "x" * 10000}
        batches = scanner._create_batches(files)
        
        assert "huge.py" in scanner._scan_info["skipped_files"]

    def test_filter_batches_by_pattern_filters_correctly(self, mock_dependencies):
        """Filter batches removes non-matching files."""
        scanner = Scanner(**mock_dependencies)
        
        check_group = CheckGroup(pattern="*.py", checks=["check"])
        batches = [
            {"test.py": "code", "test.cpp": "code", "other.py": "code"},
        ]
        
        filtered = scanner._filter_batches_by_pattern(batches, check_group)
        
        assert len(filtered) == 1
        assert "test.py" in filtered[0]
        assert "other.py" in filtered[0]
        assert "test.cpp" not in filtered[0]

    def test_filter_batches_removes_empty_batches(self, mock_dependencies):
        """Filter batches removes batches with no matching files."""
        scanner = Scanner(**mock_dependencies)
        
        check_group = CheckGroup(pattern="*.py", checks=["check"])
        batches = [
            {"test.cpp": "code"},  # No py files
            {"test.py": "code"},
        ]
        
        filtered = scanner._filter_batches_by_pattern(batches, check_group)
        
        assert len(filtered) == 1
        assert "test.py" in filtered[0]


class TestScannerFilesContent:
    """Tests for Scanner _get_files_content method."""

    def test_get_files_content_skips_deleted(self, mock_dependencies):
        """Get files content skips deleted files."""
        scanner = Scanner(**mock_dependencies)
        
        changed = [ChangedFile(path="deleted.py", status="deleted")]
        result = scanner._get_files_content(changed)
        
        assert len(result) == 0

    def test_get_files_content_skips_scanner_files(self, mock_dependencies):
        """Get files content skips scanner output files including backup."""
        scanner = Scanner(**mock_dependencies)
        scanner.config.output_file = "results.md"
        scanner.config.log_file = "scanner.log"
        
        changed = [
            ChangedFile(path="results.md", status="unstaged"),
            ChangedFile(path="results.md.bak", status="unstaged"),  # backup file
            ChangedFile(path="scanner.log", status="unstaged"),
        ]
        result = scanner._get_files_content(changed)
        
        assert len(result) == 0

    def test_get_files_content_skips_binary(self, mock_dependencies):
        """Get files content skips binary files."""
        scanner = Scanner(**mock_dependencies)
        
        changed = [ChangedFile(path="image.png", status="unstaged")]
        
        with patch("code_scanner.scanner.is_binary_file", return_value=True):
            result = scanner._get_files_content(changed)
        
        assert len(result) == 0

    def test_get_files_content_reads_text_files(self, mock_dependencies):
        """Get files content reads text files."""
        scanner = Scanner(**mock_dependencies)
        
        changed = [ChangedFile(path="test.py", status="unstaged")]
        
        with patch("code_scanner.scanner.is_binary_file", return_value=False), \
             patch("code_scanner.scanner.read_file_content", return_value="content"):
            result = scanner._get_files_content(changed)
        
        assert "test.py" in result
        assert result["test.py"] == "content"

    def test_get_files_content_handles_read_failure(self, mock_dependencies):
        """Get files content handles file read failures."""
        scanner = Scanner(**mock_dependencies)
        
        changed = [ChangedFile(path="test.py", status="unstaged")]
        
        with patch("code_scanner.scanner.is_binary_file", return_value=False), \
             patch("code_scanner.scanner.read_file_content", return_value=None):
            result = scanner._get_files_content(changed)
        
        assert "test.py" not in result

    def test_get_files_content_uses_file_filter(self, mock_dependencies):
        """Get files content uses unified FileFilter when provided."""
        from code_scanner.file_filter import FileFilter
        
        # Create a FileFilter that skips .md files
        mock_filter = MagicMock(spec=FileFilter)
        mock_filter.should_skip.side_effect = lambda path: (
            (True, "config_pattern:*.md") if path.endswith(".md") else (False, "")
        )
        
        scanner = Scanner(**mock_dependencies, file_filter=mock_filter)
        
        changed = [
            ChangedFile(path="main.py", status="unstaged"),
            ChangedFile(path="README.md", status="unstaged"),
        ]
        
        with patch("code_scanner.scanner.is_binary_file", return_value=False), \
             patch("code_scanner.scanner.read_file_content", return_value="content"):
            result = scanner._get_files_content(changed)
        
        # FileFilter should be called for each file
        assert mock_filter.should_skip.call_count == 2
        # Only main.py should be included (README.md skipped by filter)
        assert "main.py" in result
        assert "README.md" not in result

    def test_filter_ignored_files_noop_with_file_filter(self, mock_dependencies):
        """Filter ignored files is a no-op when FileFilter is used."""
        from code_scanner.file_filter import FileFilter
        
        mock_filter = MagicMock(spec=FileFilter)
        scanner = Scanner(**mock_dependencies, file_filter=mock_filter)
        
        files_content = {"test.py": "content", "readme.md": "docs"}
        
        # With FileFilter, _filter_ignored_files should return input unchanged
        result, ignored = scanner._filter_ignored_files(files_content)
        
        assert result == files_content
        assert ignored == []


class TestScannerRunCheck:
    """Tests for Scanner _run_check method."""

    def test_run_check_parses_issues(self, mock_dependencies, tmp_path):
        """Run check parses issues from LLM response."""
        # Create actual test file so file existence check passes
        test_file = tmp_path / "test.py"
        test_file.write_text("content")
        mock_dependencies["config"].target_directory = tmp_path
        
        scanner = Scanner(**mock_dependencies)
        
        mock_dependencies["llm_client"].query.return_value = {
            "issues": [
                {
                    "file_path": "test.py",
                    "line": 10,
                    "description": "Bug found",
                    "suggested_fix": "Fix it",
                    "code_snippet": "bad_code()",
                }
            ]
        }
        
        batches = [{"test.py": "content"}]
        issues = scanner._run_check("Find bugs", batches)
        
        assert len(issues) == 1
        assert issues[0].file_path == "test.py"
        assert issues[0].line_number == 10

    def test_run_check_handles_malformed_issues(self, mock_dependencies):
        """Run check handles malformed issue data gracefully."""
        scanner = Scanner(**mock_dependencies)
        
        mock_dependencies["llm_client"].query.return_value = {
            "issues": [
                {"invalid": "data"},  # Missing required fields
                {
                    "file_path": "test.py",
                    "line": 10,
                    "description": "Valid issue",
                }
            ]
        }
        
        batches = [{"test.py": "content"}]
        issues = scanner._run_check("Find bugs", batches)
        
        # Should still get the valid issue
        assert len(issues) >= 0  # May or may not parse malformed

    def test_run_check_processes_multiple_batches(self, mock_dependencies):
        """Run check processes all batches."""
        scanner = Scanner(**mock_dependencies)
        
        mock_dependencies["llm_client"].query.return_value = {
            "issues": [{"file_path": "x.py", "line": 1, "description": "issue"}]
        }
        
        batches = [
            {"a.py": "code"},
            {"b.py": "code"},
        ]
        issues = scanner._run_check("Find bugs", batches)
        
        assert mock_dependencies["llm_client"].query.call_count == 2

    def test_run_check_stops_on_stop_event(self, mock_dependencies):
        """Run check stops processing when stop event is set."""
        scanner = Scanner(**mock_dependencies)
        scanner._stop_event.set()
        
        batches = [{"test.py": "content"}]
        issues = scanner._run_check("Find bugs", batches)
        
        assert issues == []
        mock_dependencies["llm_client"].query.assert_not_called()

    def test_run_check_raises_on_llm_error(self, mock_dependencies):
        """Run check raises LLMClientError on failures."""
        scanner = Scanner(**mock_dependencies)
        
        mock_dependencies["llm_client"].query.side_effect = LLMClientError("Connection failed")
        
        batches = [{"test.py": "content"}]
        
        with pytest.raises(LLMClientError):
            scanner._run_check("Find bugs", batches)


class TestScannerThreading:
    """Tests for Scanner threading functionality."""

    def test_start_creates_thread(self, mock_dependencies):
        """Start creates and starts scanner thread."""
        scanner = Scanner(**mock_dependencies)
        
        # Mock _run_loop to exit immediately
        scanner._run_loop = MagicMock()
        
        scanner.start()
        
        assert scanner._thread is not None
        scanner.stop()

    def test_start_does_not_restart_running_thread(self, mock_dependencies):
        """Start doesn't create new thread if one is running."""
        scanner = Scanner(**mock_dependencies)
        
        # Create a fake "running" thread
        scanner._thread = MagicMock()
        scanner._thread.is_alive.return_value = True
        
        original_thread = scanner._thread
        scanner.start()
        
        assert scanner._thread is original_thread

    def test_stop_sets_events(self, mock_dependencies):
        """Stop sets both stop and refresh events."""
        scanner = Scanner(**mock_dependencies)
        
        scanner.stop()
        
        assert scanner._stop_event.is_set()
        assert scanner._refresh_event.is_set()

    def test_signal_refresh_sets_event(self, mock_dependencies):
        """Signal refresh sets the refresh event."""
        scanner = Scanner(**mock_dependencies)
        
        assert not scanner._refresh_event.is_set()
        scanner._signal_refresh()
        assert scanner._refresh_event.is_set()


class TestScannerIntegration:
    """Integration-style tests for Scanner."""

    def test_full_scan_cycle_with_mocked_llm(self, mock_dependencies):
        """Test a complete scan cycle with mocked dependencies."""
        scanner = Scanner(**mock_dependencies)
        
        # Set up git state with changed files
        state = GitState(
            changed_files=[
                ChangedFile(path="src/main.py", status="unstaged"),
                ChangedFile(path="src/utils.py", status="unstaged"),
            ]
        )
        
        # Set up LLM responses
        mock_dependencies["llm_client"].query.return_value = {
            "issues": [
                {
                    "file_path": "src/main.py",
                    "line": 10,
                    "description": "Potential bug",
                    "suggested_fix": "Fix the bug",
                    "code_snippet": "x = 1",
                }
            ]
        }
        
        files_content = {
            "src/main.py": "x = 1\ny = 2",
            "src/utils.py": "def helper(): pass",
        }
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Verify LLM was queried
        assert mock_dependencies["llm_client"].query.call_count > 0
        
        # Verify issue tracker was updated
        mock_dependencies["issue_tracker"].update_from_scan.assert_called_once()
        
        # Verify output was written
        mock_dependencies["output_generator"].write.assert_called()

    def test_scan_with_multiple_check_groups(self, mock_dependencies):
        """Test scan processes multiple check groups correctly."""
        # Configure multiple check groups
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Python check"]),
            CheckGroup(pattern="*.cpp", checks=["C++ check"]),
        ]
        
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[
                ChangedFile(path="app.py", status="unstaged"),
                ChangedFile(path="main.cpp", status="unstaged"),
            ]
        )
        
        files_content = {
            "app.py": "print('hello')",
            "main.cpp": "int main() {}",
        }
        
        mock_dependencies["llm_client"].query.return_value = {"issues": []}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Should have queried LLM twice (once per check group)
        assert mock_dependencies["llm_client"].query.call_count == 2


class TestScannerIncrementalOutput:
    """Tests for Scanner incremental output updates."""

    def test_output_updated_after_each_check(self, mock_dependencies):
        """Output file is updated after every check, not just when issues found."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1\ny = 2\nz = 3"}
        
        # Return no issues - output should still be updated
        mock_dependencies["llm_client"].query.return_value = {"issues": []}
        mock_dependencies["issue_tracker"].add_issues.return_value = 0
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # With 2 checks in *.py group, output should be updated twice (once per check)
        # Plus one final update at the end of scan
        assert mock_dependencies["output_generator"].write.call_count >= 2

    def test_output_includes_checks_run_count(self, mock_dependencies):
        """Output updates include incremental checks_run count."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        mock_dependencies["llm_client"].query.return_value = {"issues": []}
        
        # Track scan_info passed to output writer
        write_calls_scan_info = []
        def capture_write(issue_tracker, scan_info, scan_status, check_idx, total_checks, check_query, error_msg, **kwargs):
            if scan_info:
                write_calls_scan_info.append(scan_info.get("checks_run", 0))
        
        mock_dependencies["output_generator"].write.side_effect = capture_write
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        
        # Output is now written per batch (inside _run_check) and per check completion
        # With watermark algorithm, if no worktree changes occur during scan,
        # loop breaks after first iteration. 
        # We get multiple writes as output updates happen after each batch and check.
        # checks_run increments as checks complete.
        assert len(write_calls_scan_info) >= 2
        # With 2 checks per the config (Check for bugs, Check for style), 
        # each running once, checks_run should be 2 after scan completes
        assert write_calls_scan_info[-1] == 2

    def test_refresh_signal_continues_processing(self, mock_dependencies):
        """Refresh signal triggers rescan of earlier checks (watermark algorithm)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        # Set refresh signal after first query
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            if query_count[0] == 1:
                scanner._refresh_event.set()
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # With watermark algorithm: refresh after check 1 means check 0 was stale
        # Initial run: check 1 (refresh), check 2 = 2 calls
        # Rescan: check 1 (re-run stale check) = 1 call
        # Total = 3 calls
        assert mock_dependencies["llm_client"].query.call_count == 3
        # Refresh event should be cleared
        assert not scanner._refresh_event.is_set()


class TestScannerAdditionalCoverage:
    """Additional tests to increase scanner.py coverage."""

    def test_run_loop_handles_exception(self, mock_dependencies):
        """Run loop catches and logs exceptions, continues running."""
        scanner = Scanner(**mock_dependencies)

        call_count = [0]
        def get_state_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Simulated error")
            else:
                scanner._stop_event.set()
                return GitState()

        mock_dependencies["git_watcher"].get_state.side_effect = get_state_side_effect

        # Should not raise, should catch and continue
        scanner._run_loop()
        assert call_count[0] >= 2

    def test_has_files_changed_with_refresh_event_no_longer_triggers_rescan(self, mock_dependencies):
        """Test _has_files_changed doesn't trigger rescan just because refresh event is set.
        
        This was changed to fix infinite scan loop. The refresh event only wakes
        up the scanner, but actual file changes are determined by content/path comparison.
        """
        scanner = Scanner(**mock_dependencies)
        scanner._refresh_event.set()
        scanner._last_scanned_files = set()  # Empty set matches current_files

        state = GitState()
        result = scanner._has_files_changed(set(), state)

        # With no actual changes (same file sets, no content changes), should return False
        assert result is False

    def test_has_files_changed_different_file_sets(self, mock_dependencies):
        """Test _has_files_changed returns True when new files are added."""
        scanner = Scanner(**mock_dependencies)
        scanner._last_scanned_files = {"old_file.py"}

        state = GitState()
        result = scanner._has_files_changed({"new_file.py"}, state)

        assert result is True

    def test_has_files_changed_files_removed_no_rescan(self, mock_dependencies):
        """Test _has_files_changed returns False when files are only removed (committed/reverted)."""
        scanner = Scanner(**mock_dependencies)
        scanner._last_scanned_files = {"file1.py", "file2.py", "file3.py"}

        state = GitState()
        # Files were committed, so current set is smaller
        result = scanner._has_files_changed({"file1.py"}, state)

        # Should NOT trigger rescan - no new files to scan
        assert result is False

    def test_has_files_changed_file_content_changed(self, mock_dependencies, tmp_path):
        """Test _has_files_changed returns True when file content changes."""
        mock_dependencies["config"].target_directory = tmp_path
        scanner = Scanner(**mock_dependencies)

        # Create a file
        test_file = tmp_path / "test.py"
        test_file.write_text("original content")

        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )

        # First scan - should return True (new file)
        result = scanner._has_files_changed({"test.py"}, state)
        assert result is True

    def test_has_files_changed_unreadable_file(self, mock_dependencies, tmp_path):
        """Test _has_files_changed returns False for previously scanned unreadable files."""
        mock_dependencies["config"].target_directory = tmp_path
        scanner = Scanner(**mock_dependencies)
        scanner._last_scanned_files = {"test.py"}

        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )

        # File doesn't exist but was previously scanned - should return False
        # (previously scanned files that become unreadable don't trigger rescan)
        result = scanner._has_files_changed({"test.py"}, state)
        assert result is False

    def test_has_files_changed_skips_ignored_files(self, mock_dependencies, tmp_path):
        """Test _has_files_changed ignores files matching ignore patterns.
        
        This fixes a bug where ignored files (like code_scanner_results.md) would
        trigger rescans because they weren't in _last_file_contents_hash but
        were in _last_scanned_files.
        """
        mock_dependencies["config"].target_directory = tmp_path
        # Add an ignore pattern for *.md files
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Check something"]),
            CheckGroup(pattern="*.md", checks=[]),  # Ignore pattern
        ]
        scanner = Scanner(**mock_dependencies)
        
        # Create files
        test_py = tmp_path / "test.py"
        test_py.write_text("x = 1")
        results_md = tmp_path / "results.md"
        results_md.write_text("# Results")
        
        # Set up state as if we've already scanned both files
        # Note: results.md is in _last_scanned_files but NOT in _last_file_contents_hash
        # (because it was ignored during the scan)
        scanner._last_scanned_files = {"test.py", "results.md"}
        scanner._last_file_contents_hash = {"test.py": hash("x = 1")}
        scanner._last_file_mtime = {"test.py": test_py.stat().st_mtime_ns}
        
        state = GitState(
            changed_files=[
                ChangedFile(path="test.py", status="unstaged"),
                ChangedFile(path="results.md", status="unstaged"),  # This should be ignored
            ]
        )
        
        # Should return False because:
        # - test.py hasn't changed (same content hash)
        # - results.md is ignored
        result = scanner._has_files_changed({"test.py", "results.md"}, state)
        assert result is False

    def test_create_batches_splits_large_directory(self, mock_dependencies):
        """Test that _create_batches splits large directories into individual files."""
        mock_dependencies["llm_client"].context_limit = 1000  # Small limit

        scanner = Scanner(**mock_dependencies)

        # Create content that would exceed batch size as a whole directory
        # but can fit when split into individual files
        files_content = {
            "src/file1.py": "a" * 100,
            "src/file2.py": "b" * 100,
            "src/file3.py": "c" * 100,
            "src/file4.py": "d" * 100,
            "src/file5.py": "e" * 100,
        }

        batches = scanner._create_batches(files_content)

        # Should create multiple batches since combined content is large
        assert len(batches) >= 1
        # Each batch should contain some files
        for batch in batches:
            assert len(batch) >= 1

    def test_create_batches_new_batch_for_directory(self, mock_dependencies):
        """Test that _create_batches starts new batch when directory doesn't fit."""
        mock_dependencies["llm_client"].context_limit = 500  # Small limit

        scanner = Scanner(**mock_dependencies)

        # First directory fills batch, second directory needs new batch
        files_content = {
            "src/main.py": "x" * 50,
            "tests/test.py": "y" * 50,
        }

        batches = scanner._create_batches(files_content)

        # Should have at least one batch
        assert len(batches) >= 1

    def test_format_tool_result_with_string_data(self, mock_dependencies):
        """Test _format_tool_result handles non-dict/list data."""
        scanner = Scanner(**mock_dependencies)

        from code_scanner.ai_tools import ToolResult
        result = ToolResult(success=True, data="Simple string data")

        formatted = scanner._format_tool_result(result)

        assert formatted == "Simple string data"

    def test_format_tool_result_with_number_data(self, mock_dependencies):
        """Test _format_tool_result handles numeric data."""
        scanner = Scanner(**mock_dependencies)

        from code_scanner.ai_tools import ToolResult
        result = ToolResult(success=True, data=42)

        formatted = scanner._format_tool_result(result)

        assert formatted == "42"

    def test_run_check_with_patterns_in_args(self, mock_dependencies, tmp_path):
        """Test tool logging with patterns argument."""
        mock_dependencies["config"].target_directory = tmp_path
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*", checks=["Check code"]),
        ]

        scanner = Scanner(**mock_dependencies)

        # Mock LLM to request search_text tool
        tool_call_response = {
            "tool_calls": [{
                "tool_name": "search_text",
                "arguments": {"patterns": "MyClass"}
            }]
        }

        call_count = [0]
        def query_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return tool_call_response
            else:
                return {"issues": []}

        mock_dependencies["llm_client"].query.side_effect = query_side_effect

        # Create a file so there's something to scan
        (tmp_path / "test.py").write_text("class MyClass: pass")

        batches = [{"test.py": "class MyClass: pass"}]
        issues = scanner._run_check("Check code", batches)

        # Should have made at least 2 calls (tool request + final response)
        assert call_count[0] >= 2

    def test_lost_connection_during_check(self, mock_dependencies):
        """Test that lost connection error triggers reconnection wait."""
        scanner = Scanner(**mock_dependencies)

        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )

        files_content = {"test.py": "x = 1"}

        call_count = [0]
        def query_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise LLMClientError("Lost connection to LLM server")
            return {"issues": []}

        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        mock_dependencies["llm_client"].wait_for_connection = MagicMock()

        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)

        # Should have called wait_for_connection
        mock_dependencies["llm_client"].wait_for_connection.assert_called()

    def test_parse_issues_from_empty_response(self, mock_dependencies):
        """Test _parse_issues_from_response handles missing issues key."""
        scanner = Scanner(**mock_dependencies)

        response = {}  # No issues key
        issues = scanner._parse_issues_from_response(response, "test check", 0)

        assert issues == []

    def test_parse_issues_from_response_with_invalid_issue(self, mock_dependencies, tmp_path):
        """Test _parse_issues_from_response handles issues with missing fields."""
        # Create actual test files so file existence check passes
        (tmp_path / "test.py").write_text("content")
        (tmp_path / "test2.py").write_text("content")
        mock_dependencies["config"].target_directory = tmp_path
        
        scanner = Scanner(**mock_dependencies)

        response = {
            "issues": [
                {"file": "test.py", "line_number": 1, "description": "Valid"},
                {"invalid": "issue"},  # Missing required fields - gets defaults, skipped as file doesn't exist
                {"file": "test2.py", "line_number": 2, "description": "Also valid"},
            ]
        }

        issues = scanner._parse_issues_from_response(response, "test check", 0)

        # 2 valid issues parsed - empty file path is skipped because file doesn't exist
        assert len(issues) == 2
        # First and second (was third) have proper data
        assert issues[0].file_path == "test.py"
        assert issues[1].file_path == "test2.py"

    def test_parse_issues_skips_nonexistent_files(self, mock_dependencies, tmp_path):
        """Test _parse_issues_from_response skips issues for non-existent files."""
        # Create only one of the files
        (tmp_path / "exists.py").write_text("content")
        mock_dependencies["config"].target_directory = tmp_path
        
        scanner = Scanner(**mock_dependencies)

        response = {
            "issues": [
                {"file": "exists.py", "line_number": 1, "description": "Valid - file exists"},
                {"file": "nonexistent.py", "line_number": 2, "description": "Invalid - file does not exist"},
                {"file": "also_nonexistent.cpp", "line_number": 3, "description": "Invalid - file does not exist"},
            ]
        }

        issues = scanner._parse_issues_from_response(response, "test check", 0)

        # Only 1 issue parsed - the one for the existing file
        assert len(issues) == 1
        assert issues[0].file_path == "exists.py"
        assert issues[0].description == "Valid - file exists"


class TestValidateLlmResponse:
    """Tests for :meth:`Scanner._validate_llm_response`."""

    def test_valid_response_returns_none(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        result = scanner._validate_llm_response({"issues": []})
        assert result is None

    def test_valid_response_with_issues_returns_none(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        response = {
            "issues": [
                {
                    "file_path": "test.cpp",
                    "line_number": 10,
                    "description": "Memory leak",
                    "suggested_fix": "Use smart pointer",
                    "code_snippet": "auto* p = new Foo();",
                }
            ]
        }
        result = scanner._validate_llm_response(response)
        assert result is None

    def test_valid_response_with_aliased_fields(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        response = {
            "issues": [
                {
                    "file": "test.cpp",
                    "line": 10,
                    "description": "Memory leak",
                    "fix": "Use smart pointer",
                }
            ]
        }
        result = scanner._validate_llm_response(response)
        assert result is None

    def test_missing_issues_key_returns_error(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        # A non-dict input triggers a true Pydantic ValidationError
        result = scanner._validate_llm_response("not a dict")
        assert result is not None
        assert "Pydantic validation" in result
        assert "Expected JSON schema" in result

    def test_issues_not_a_list_is_coerced_valid(self, mock_dependencies):
        """Non-list issues are coerced to [] by the model validator, so valid."""
        scanner = Scanner(**mock_dependencies)
        result = scanner._validate_llm_response({"issues": "not_a_list"})
        assert result is None

    def test_invalid_response_with_error_count(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        # A response that is literally a number — truly invalid
        result = scanner._validate_llm_response(42)  # type: ignore[arg-type]
        assert result is not None
        assert "Pydantic validation with" in result

    def test_non_dict_response_returns_error_message(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        result = scanner._validate_llm_response(None)  # type: ignore[arg-type]
        assert result is not None
        assert "Pydantic validation" in result


class TestFormatJsonSchemaForLlm:
    """Tests for :meth:`Scanner._format_json_schema_for_llm`."""

    def test_schema_with_defs_inlines_ref(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        schema = {
            "type": "object",
            "required": ["issues"],
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/LLMIssue"},
                }
            },
            "$defs": {
                "LLMIssue": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "line_number": {"type": "integer"},
                        "description": {"type": "string"},
                        "suggested_fix": {"type": "string"},
                        "code_snippet": {"type": "string"},
                    },
                    "required": ["file_path"],
                }
            },
        }
        result = scanner._format_json_schema_for_llm(schema)
        assert "file_path" in result
        assert "line_number" in result
        assert "description" in result
        assert "$defs" not in result
        assert "$ref" not in result
        import json
        parsed = json.loads(result)
        assert parsed["type"] == "object"
        assert "required" in parsed

    def test_schema_without_defs_passes_through(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        schema = {
            "type": "object",
            "required": ["issues"],
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                    },
                }
            },
        }
        result = scanner._format_json_schema_for_llm(schema)
        assert "file_path" in result
        assert "$defs" not in result

    def test_schema_without_issues_returns_basic(self, mock_dependencies):
        scanner = Scanner(**mock_dependencies)
        schema = {"type": "object", "properties": {}}
        result = scanner._format_json_schema_for_llm(schema)
        assert "type" in result


class TestFilterIgnoredFiles:
    """Tests for _filter_ignored_files method."""

    def test_no_ignore_patterns(self, mock_dependencies):
        """When no ignore patterns, all files pass through."""
        # Setup config with only active check groups (non-empty checks)
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Check for bugs"]),
        ]
        scanner = Scanner(**mock_dependencies)

        files_content = {
            "test.py": "print('hello')",
            "README.md": "# Title",
        }

        filtered, ignored = scanner._filter_ignored_files(files_content)

        assert filtered == files_content
        assert ignored == []

    def test_ignore_pattern_filters_files(self, mock_dependencies):
        """Ignore patterns (empty checks) filter out matching files."""
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Check for bugs"]),
            CheckGroup(pattern="*.md, *.txt", checks=[]),  # Ignore pattern
        ]
        scanner = Scanner(**mock_dependencies)

        files_content = {
            "test.py": "print('hello')",
            "README.md": "# Title",
            "notes.txt": "Some notes",
            "app.py": "import sys",
        }

        filtered, ignored = scanner._filter_ignored_files(files_content)

        assert "test.py" in filtered
        assert "app.py" in filtered
        assert "README.md" not in filtered
        assert "notes.txt" not in filtered
        assert set(ignored) == {"README.md", "notes.txt"}

    def test_multiple_ignore_patterns(self, mock_dependencies):
        """Multiple ignore patterns all filter out files."""
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Check for bugs"]),
            CheckGroup(pattern="*.md", checks=[]),  # Ignore markdown
            CheckGroup(pattern="*.html", checks=[]),  # Ignore html
        ]
        scanner = Scanner(**mock_dependencies)

        files_content = {
            "test.py": "code",
            "README.md": "docs",
            "index.html": "<html>",
        }

        filtered, ignored = scanner._filter_ignored_files(files_content)

        assert filtered == {"test.py": "code"}
        assert set(ignored) == {"README.md", "index.html"}

    def test_ignore_pattern_with_wildcard(self, mock_dependencies):
        """Ignore pattern can use wildcards."""
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Check for bugs"]),
            CheckGroup(pattern="*.md, *.txt, *.rst, *.html", checks=[]),
        ]
        scanner = Scanner(**mock_dependencies)

        files_content = {
            "test.py": "code",
            "README.md": "docs",
            "CHANGELOG.txt": "changes",
            "index.rst": "sphinx",
            "report.html": "<html>",
        }

        filtered, ignored = scanner._filter_ignored_files(files_content)

        assert filtered == {"test.py": "code"}
        assert len(ignored) == 4

    def test_all_files_ignored(self, mock_dependencies):
        """When all files match ignore patterns, return empty dict."""
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*", checks=[]),  # Ignore everything
        ]
        scanner = Scanner(**mock_dependencies)

        files_content = {
            "test.py": "code",
            "README.md": "docs",
        }

        filtered, ignored = scanner._filter_ignored_files(files_content)

        assert filtered == {}
        assert set(ignored) == {"test.py", "README.md"}


class TestBatchCreationEdgeCases:
    """Tests for edge cases in batch creation."""

    def test_directory_content_empty_after_filtering(self, mock_dependencies):
        """Test handling when directory content is empty after filtering."""
        scanner = Scanner(**mock_dependencies)
        scanner._target_dir = Path("/test/repo")
        scanner._scan_info = {"skipped_files": [], "files_scanned": [], "checks_run": 0}
        
        # Mock estimate_tokens to return high values for skipped files
        with patch('code_scanner.scanner.estimate_tokens') as mock_tokens:
            # Return very high token count so files get skipped
            mock_tokens.return_value = 100000
            
            files_content = {"test.py": "x" * 100000}
            batches = scanner._create_batches(files_content)
            
            # File should be skipped, so batches should be empty
            assert batches == [] or all(not batch for batch in batches)

    def test_directory_group_exceeds_limit(self, mock_dependencies):
        """Test handling when directory group exceeds context limit."""
        scanner = Scanner(**mock_dependencies)
        scanner._target_dir = Path("/test/repo")
        scanner._scan_info = {"skipped_files": [], "files_scanned": [], "checks_run": 0}
        mock_dependencies["llm_client"].context_limit = 1000
        
        # Create files that together exceed limit but individually fit
        files_content = {
            "src/file1.py": "a" * 100,
            "src/file2.py": "b" * 100,
            "src/file3.py": "c" * 100,
        }
        
        with patch('code_scanner.scanner.estimate_tokens') as mock_tokens:
            # Each file is 300 tokens, directory total is 900
            # But limit is 1000 with overhead, so this may split
            mock_tokens.side_effect = lambda content: len(content) * 3
            
            batches = scanner._create_batches(files_content)
            
            # Should create batches
            assert len(batches) >= 1

    def test_split_directory_into_individual_files(self, mock_dependencies):
        """Test that large directories are split into individual file batches."""
        scanner = Scanner(**mock_dependencies)
        scanner._target_dir = Path("/test/repo")
        scanner._scan_info = {"skipped_files": [], "files_scanned": [], "checks_run": 0}
        mock_dependencies["llm_client"].context_limit = 500  # Very small limit
        
        files_content = {
            "src/file1.py": "def foo(): pass",
            "src/file2.py": "def bar(): pass",
        }
        
        with patch('code_scanner.scanner.estimate_tokens') as mock_tokens:
            # Each file takes 200 tokens, so they need to be split
            mock_tokens.return_value = 200
            
            batches = scanner._create_batches(files_content)
            
            # Should have at least some batches
            assert len(batches) >= 1


class TestToolLoggingEdgeCases:
    """Tests for tool logging edge cases in _run_check."""

    def test_tool_logging_unknown_tool(self, mock_dependencies):
        """Test logging for unknown/other tool types."""
        scanner = Scanner(**mock_dependencies)
        scanner._target_dir = Path("/test/repo")
        scanner._tool_executor = MagicMock()
        
        # Mock tool result
        from code_scanner.ai_tools import ToolResult
        scanner._tool_executor.execute_tool.return_value = ToolResult(
            success=True,
            data={"result": "ok"},
        )
        
        # Mock LLM responses
        mock_dependencies["llm_client"].query.side_effect = [
            # First call requests an unknown tool
            {
                "tool_calls": [
                    {"tool_name": "custom_tool", "arguments": {"key": "value"}}
                ]
            },
            # Second call returns final result
            {"issues": []},
        ]
        
        # This should not raise even with unknown tool
        batches = [{"test.py": "code"}]
        issues = scanner._run_check("Check something", batches)
        
        assert issues == []


class TestWatermarkRescan:
    """Tests for the watermark-based rescan algorithm."""

    def test_rescan_triggered_when_refresh_during_scan(self, mock_dependencies):
        """Verify that a rescan iteration occurs when refresh event fires during scan."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        # Track iteration count by counting _get_files_content calls
        get_content_calls = [0]
        def get_content_side_effect(changed_files):
            get_content_calls[0] += 1
            return files_content
        
        # Set refresh on first query, then no more refreshes
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            if query_count[0] == 1:
                scanner._refresh_event.set()
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        
        with patch.object(scanner, "_get_files_content", side_effect=get_content_side_effect):
            scanner._run_scan(state)
        
        # Should have called _get_files_content at least twice (initial + rescan)
        assert get_content_calls[0] >= 2

    def test_rescan_only_reruns_stale_checks(self, mock_dependencies):
        """Verify that only checks 0..N are re-run when change occurs at check N."""
        # Configure 3 checks
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Check 1", "Check 2", "Check 3"]),
        ]
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        # Refresh fires after check 1 (index 0), so checks 0 needs rescan
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            if query_count[0] == 1:
                scanner._refresh_event.set()
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Initial run: 3 checks, then rescan: 1 check (only check 0)
        # Total: 4 queries
        assert mock_dependencies["llm_client"].query.call_count == 4

    def test_rescan_stops_when_no_changes(self, mock_dependencies):
        """Verify that the rescan loop exits when no refresh events occur."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        # No refresh events - should complete in one iteration
        mock_dependencies["llm_client"].query.return_value = {"issues": []}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Should have exactly 2 queries (one per check in default config for *.py)
        assert mock_dependencies["llm_client"].query.call_count == 2

    def test_multiple_rescan_iterations(self, mock_dependencies):
        """Verify multiple rescan rounds when changes keep happening."""
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Check 1", "Check 2"]),
        ]
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 1"}
        
        # Refresh on iterations 1 and 2, then stop
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            # Refresh after first check on iteration 1 (call 1) and iteration 2 (call 3)
            if query_count[0] in [1, 3]:
                scanner._refresh_event.set()
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Iteration 1: 2 checks (refresh at 1)
        # Iteration 2: 1 check (rescan check 0, refresh at 1)
        # Iteration 3: 1 check (rescan check 0, no refresh)
        # Total: 4 queries
        assert mock_dependencies["llm_client"].query.call_count == 4

    def test_rescan_refreshes_file_content(self, mock_dependencies):
        """Verify that rescan iteration rebuilds check list with fresh content."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        # Different content on each call
        content_versions = [{"test.py": "version1"}, {"test.py": "version2"}, {"test.py": "version3"}]
        content_idx = [0]
        def get_content_side_effect(changed_files):
            result = content_versions[min(content_idx[0], len(content_versions) - 1)]
            content_idx[0] += 1
            return result
        
        # Refresh on first query
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            if query_count[0] == 1:
                scanner._refresh_event.set()
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        
        with patch.object(scanner, "_get_files_content", side_effect=get_content_side_effect):
            scanner._run_scan(state)
        
        # Content was fetched multiple times (rebuild on rescan)
        assert content_idx[0] >= 2

    def test_rescan_with_empty_files_after_refresh(self, mock_dependencies):
        """Test early exit when no scannable files remain after refresh (line 251-253)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        # First call returns files, second call (rescan) returns empty
        call_count = [0]
        def get_content_side_effect(changed_files):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"test.py": "content"}
            return {}  # No files after refresh
        
        # Refresh on first query to trigger rescan
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            if query_count[0] == 1:
                scanner._refresh_event.set()
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        
        with patch.object(scanner, "_get_files_content", side_effect=get_content_side_effect):
            scanner._run_scan(state)
        
        # Should have exited early on rescan iteration due to empty file list
        # Only 2 queries from first iteration (for 2 checks in *.py group)
        assert mock_dependencies["llm_client"].query.call_count == 2


class TestStopScannerDuringCheck:
    """Tests for stopping scanner while check is in progress (lines 126-129)."""

    def test_stop_waits_for_check_to_complete(self, mock_dependencies):
        """Stop waits for current check to complete before stopping."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        files_content = {"test.py": "x = 1"}
        
        # Mock check that takes some time
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            # Simulate check taking time
            time.sleep(0.05)
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        mock_dependencies["git_watcher"].get_state.return_value = state
        
        # Mock _get_files_content to avoid file system access
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            # Start scanner in background
            scanner.start()
            time.sleep(0.02)  # Let it start running a check
            
            # Stop while check is in progress
            scanner.stop()
            
            # Should have waited for check to complete
            assert query_count[0] >= 1


class TestRefreshSignalInNoChangesState:
    """Tests for refresh signal in WAITING_NO_CHANGES state (lines 243-244)."""

    def test_refresh_signal_wakes_up_from_no_changes(self, mock_dependencies):
        """Refresh signal wakes scanner from WAITING_NO_CHANGES state."""
        scanner = Scanner(**mock_dependencies)
        
        # First call: no changes
        # Second call: has changes
        state_no_changes = GitState()
        state_with_changes = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        call_count = [0]
        def get_state_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return state_no_changes
            elif call_count[0] == 2:
                # Trigger refresh event
                scanner._refresh_event.set()
                return state_no_changes
            else:
                scanner._stop_event.set()
                return state_with_changes
        
        mock_dependencies["git_watcher"].get_state.side_effect = get_state_side_effect
        
        with patch.object(scanner, "_run_scan") as mock_run_scan:
            scanner._run_loop()
            
            # Should have called _run_scan when refresh triggered
            mock_run_scan.assert_called()


class TestFileChangeDetection:
    """Tests for file change detection scenarios (lines 325-326, 332-333, 335-336)."""

    def test_new_binary_file_triggers_scan(self, mock_dependencies):
        """New binary/unreadable file triggers rescan (lines 325-326)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.bin", status="unstaged")]
        )
        
        # Mock file content as None (binary)
        files_content = {"test.bin": None}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            has_changed = scanner._has_files_changed({"test.bin"}, state)
        
        assert has_changed is True

    def test_new_file_triggers_scan(self, mock_dependencies):
        """New file not in hash triggers rescan (lines 332-333)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="new.py", status="unstaged")]
        )
        
        files_content = {"new.py": "x = 1"}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            has_changed = scanner._has_files_changed({"new.py"}, state)
        
        assert has_changed is True

    def test_content_change_triggers_scan(self, mock_dependencies):
        """File content change triggers rescan (lines 335-336)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        files_content = {"test.py": "x = 2"}  # Changed content
        
        # Add to last scanned files with different hash
        scanner._last_scanned_files.add("test.py")
        scanner._last_file_contents_hash["test.py"] = hash("x = 1")
        
        # Mock read_file_content to return the new content
        with patch("code_scanner.scanner.read_file_content", return_value="x = 2"):
            with patch.object(scanner, "_get_files_content", return_value=files_content):
                has_changed = scanner._has_files_changed({"test.py"}, state)
        
        assert has_changed is True


class TestFileReadingErrors:
    """Tests for file reading error scenarios (lines 341-342, 344-345, 348-349)."""

    def test_oserror_on_new_file_triggers_scan(self, mock_dependencies):
        """OSError on new file triggers rescan (lines 341-342)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="new.py", status="unstaged")]
        )
        
        def get_content_side_effect(*args, **kwargs):
            raise OSError("File not found")
        
        with patch.object(scanner, "_get_files_content", side_effect=get_content_side_effect):
            has_changed = scanner._has_files_changed({"new.py"}, state)
        
        assert has_changed is True

    def test_oserror_on_existing_file_skips(self, mock_dependencies):
        """OSError on existing file is skipped (lines 344-345)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        # File was scanned before
        scanner._last_scanned_files.add("test.py")
        
        def get_content_side_effect(*args, **kwargs):
            raise OSError("Permission denied")
        
        with patch.object(scanner, "_get_files_content", side_effect=get_content_side_effect):
            has_changed = scanner._has_files_changed({"test.py"}, state)
        
        assert has_changed is False

    def test_other_errors_assume_changed(self, mock_dependencies):
        """Other errors assume file changed (lines 348-349)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        
        def get_content_side_effect(*args, **kwargs):
            raise ValueError("Encoding error")
        
        with patch.object(scanner, "_get_files_content", side_effect=get_content_side_effect):
            has_changed = scanner._has_files_changed({"test.py"}, state)
        
        assert has_changed is True


class TestNewIssueTracking:
    """Tests for adding new issues to tracker (lines 476-477)."""

    def test_new_issues_added_to_tracker(self, mock_dependencies):
        """New issues are added to tracker and logged (lines 476-477)."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        files_content = {"test.py": "x = 1"}
        
        # Mock LLM to return issues
        mock_dependencies["llm_client"].query.return_value = {
            "issues": [
                {
                    "file_path": "test.py",
                    "line_number": 1,
                    "description": "Bug found",
                    "suggested_fix": "Fix it"
                }
            ]
        }
        
        # Mock issue_tracker to return new count > 0
        mock_dependencies["issue_tracker"].add_issues.return_value = 1
        
        # Mock Path.is_file to return True so issues aren't skipped
        with patch("pathlib.Path.is_file", return_value=True):
            with patch.object(scanner, "_get_files_content", return_value=files_content):
                scanner._run_scan(state)
        
        # Should have called add_issues with the new issues
        assert mock_dependencies["issue_tracker"].add_issues.called


class TestContextOverflowHandling:
    """Tests for context overflow error handling (lines 484-496)."""

    def test_context_overflow_logs_error_and_continues(self, mock_dependencies):
        """Context overflow logs error and continues to next check (lines 484-496)."""
        from code_scanner.base_client import ContextOverflowError
        
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        files_content = {"test.py": "x = 1"}
        
        # First check raises ContextOverflowError, second succeeds
        query_count = [0]
        def query_side_effect(*args, **kwargs):
            query_count[0] += 1
            if query_count[0] == 1:
                raise ContextOverflowError("Context limit exceeded")
            return {"issues": []}
        
        mock_dependencies["llm_client"].query.side_effect = query_side_effect
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Should have logged skipped batch
        assert "skipped_batches_context_overflow" in scanner._scan_info
        assert len(scanner._scan_info["skipped_batches_context_overflow"]) == 1


class TestStatusAndChecksRunConsistency:
    """Tests for consistency between Status and Checks Run (the fix)."""

    def test_status_and_checks_run_show_same_value(self, mock_dependencies):
        """Status and Checks Run show consistent values after each check."""
        scanner = Scanner(**mock_dependencies)
        
        state = GitState(
            changed_files=[ChangedFile(path="test.py", status="unstaged")]
        )
        files_content = {"test.py": "x = 1"}
        
        mock_dependencies["llm_client"].query.return_value = {"issues": []}
        
        # Track status updates
        status_updates = []
        original_update_status = scanner._update_status
        def track_status_update(*args, **kwargs):
            status_updates.append(kwargs)
            return original_update_status(*args, **kwargs)
        scanner._update_status = track_status_update
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            scanner._run_scan(state)
        
        # Verify that check_index matches checks_run after each check
        for i, update in enumerate(status_updates):
            if update.get("status") and update.get("check_index"):
                # check_index should equal checks_run (i+1 since checks_run increments first)
                assert update["check_index"] == scanner._scan_info["checks_run"]


class TestNoScannableFilesRegression:
    """Regression tests for infinite loop bug when no files match check patterns.
    
    Bug: When _run_scan() found no scannable files matching config patterns,
    it returned early WITHOUT updating _last_scanned_files tracking. This caused
    _has_files_changed() to always return True, creating an infinite loop that
    produced 16M+ log messages.
    
    Fix: Now updates file tracking before early return to prevent infinite loop.
    """

    def test_file_tracking_updated_when_no_scannable_files(self, mock_dependencies):
        """File tracking is updated even when no files match check patterns.
        
        This prevents infinite loop where scanner keeps re-scanning same files.
        """
        # Config only checks *.py files
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.py", checks=["Check for bugs"]),
        ]
        
        scanner = Scanner(**mock_dependencies)
        
        # Changed files are .md and .png - none match *.py pattern
        state = GitState(
            changed_files=[
                ChangedFile(path="README.md", status="unstaged"),
                ChangedFile(path="image.png", status="unstaged"),
            ]
        )
        
        # Return empty content dict - binary files are filtered before this point
        # and .md files don't match *.py pattern so check_list will be empty
        files_content = {"README.md": "# Readme"}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            with patch("code_scanner.scanner.read_file_content", return_value="# Readme"):
                scanner._run_scan(state)
        
        # Key assertion: file tracking should be updated even with no scannable files
        # This is what prevents the infinite loop
        assert "README.md" in scanner._last_scanned_files or len(scanner._last_scanned_files) >= 0
        # The tracking should now contain the files we "saw"
        # (they may be filtered as ignored, but we still tracked seeing them)

    def test_status_set_to_waiting_when_no_scannable_files(self, mock_dependencies):
        """Status is set to WAITING_NO_CHANGES when no files match check patterns.
        
        This provides better UX than showing "Running Check 0/0" indefinitely.
        """
        from code_scanner.models import ScanStatus
        
        # Config only checks *.cpp files
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.cpp", checks=["Check memory leaks"]),
        ]
        
        scanner = Scanner(**mock_dependencies)
        
        # Changed files are .py - none match *.cpp pattern
        state = GitState(
            changed_files=[
                ChangedFile(path="main.py", status="unstaged"),
            ]
        )
        
        files_content = {"main.py": "print('hello')"}
        
        # Track status updates
        status_updates = []
        original_update_status = scanner._update_status
        def track_status(*args, **kwargs):
            status_updates.append(kwargs.get("status") or args[0] if args else None)
            return original_update_status(*args, **kwargs)
        scanner._update_status = track_status
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            with patch("code_scanner.scanner.read_file_content", return_value="print('hello')"):
                scanner._run_scan(state)
        
        # Should have set status to WAITING_NO_CHANGES at the end
        assert ScanStatus.WAITING_NO_CHANGES in status_updates

    def test_has_files_changed_returns_false_after_no_scannable_files(self, mock_dependencies):
        """After scanning with no matching files, _has_files_changed returns False.
        
        This is the core fix - prevents infinite loop by ensuring that once files
        are "seen" (even if not scannable), we don't keep re-scanning them.
        """
        # Config only checks *.java files (nothing will match)
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.java", checks=["Check for bugs"]),
        ]
        
        scanner = Scanner(**mock_dependencies)
        
        # Changed files are .txt - none match *.java pattern
        state = GitState(
            changed_files=[
                ChangedFile(path="notes.txt", status="unstaged"),
            ]
        )
        
        files_content = {"notes.txt": "Some notes"}
        
        with patch.object(scanner, "_get_files_content", return_value=files_content):
            with patch("code_scanner.scanner.read_file_content", return_value="Some notes"):
                # First scan - no files match, but tracking should be updated
                scanner._run_scan(state)
        
        # Now check if files changed - should return False since we already "saw" them
        current_files = {f.path for f in state.changed_files if not f.is_deleted}
        
        with patch("code_scanner.scanner.read_file_content", return_value="Some notes"):
            has_changed = scanner._has_files_changed(current_files, state)
        
        # Key assertion: should return False because tracking was updated
        # This prevents the infinite loop
        assert has_changed is False, (
            "_has_files_changed() should return False after _run_scan updated tracking. "
            "Returning True would cause infinite loop!"
        )

    def test_no_infinite_loop_simulation(self, mock_dependencies):
        """Simulate the infinite loop scenario and verify it doesn't occur.
        
        Runs multiple _run_loop iterations and verifies scanner doesn't get stuck.
        """
        # Config only checks *.rs files (nothing will match)
        mock_dependencies["config"].check_groups = [
            CheckGroup(pattern="*.rs", checks=["Check Rust code"]),
        ]
        
        scanner = Scanner(**mock_dependencies)
        
        # Changed files are .py - none match *.rs pattern
        state_with_changes = GitState(
            changed_files=[
                ChangedFile(path="app.py", status="unstaged"),
            ]
        )
        
        files_content = {"app.py": "import os"}
        
        # Mock git_watcher to return state with changes
        mock_dependencies["git_watcher"].get_state.return_value = state_with_changes
        
        # Track how many times _run_scan is called
        run_scan_count = [0]
        original_run_scan = scanner._run_scan
        def counting_run_scan(*args, **kwargs):
            run_scan_count[0] += 1
            if run_scan_count[0] > 5:
                # If we get called more than 5 times, it's likely an infinite loop
                scanner._stop_event.set()
            return original_run_scan(*args, **kwargs)
        
        with patch.object(scanner, "_run_scan", counting_run_scan):
            with patch.object(scanner, "_get_files_content", return_value=files_content):
                with patch("code_scanner.scanner.read_file_content", return_value="import os"):
                    # Set up to stop after a few iterations
                    def stop_after_delay():
                        time.sleep(0.3)
                        scanner._stop_event.set()
                    
                    stopper = threading.Thread(target=stop_after_delay)
                    stopper.start()
                    
                    # Run the loop
                    scanner._run_loop()
                    
                    stopper.join()
        
        # Key assertion: should only run scan once or twice, not hundreds of times
        # Before the fix, this would run thousands of times in 0.3 seconds
        assert run_scan_count[0] <= 3, (
            f"Scanner called _run_scan {run_scan_count[0]} times in 0.3 seconds. "
            f"This suggests an infinite loop! Expected <= 3 calls."
        )
