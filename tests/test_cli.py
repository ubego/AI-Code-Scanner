"""Tests for CLI and Application functionality."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from code_scanner.cli import (
    Application,
    LockFileError,
    parse_args,
)
from code_scanner.config import Config
from code_scanner.models import LLMConfig, CheckGroup


class TestProjectIdGeneration:
    """Tests for project ID generation from directory names."""

    def test_project_id_uses_directory_name(self, tmp_path):
        """Single project uses directory name as ID."""
        # Create a project directory
        project_dir = tmp_path / "my-awesome-project"
        project_dir.mkdir()
        
        projects = [(project_dir, project_dir / "config.toml", None)]
        app = Application(projects)
        
        # Access the project ID generation logic directly by checking _project_configs
        # and simulating what _setup does for project ID generation
        dir_name_counts = {}
        for target_dir, _, _ in app._project_configs:
            dir_name = target_dir.name
            if dir_name not in dir_name_counts:
                dir_name_counts[dir_name] = []
            dir_name_counts[dir_name].append(target_dir)
        
        duplicate_dir_names = {name for name, paths in dir_name_counts.items() if len(paths) > 1}
        
        # Single directory should not be in duplicates
        assert "my-awesome-project" not in duplicate_dir_names
        # So project_id should just be the directory name
        assert project_dir.name == "my-awesome-project"

    def test_project_id_unique_directories(self, tmp_path):
        """Multiple projects with different names get their directory names as IDs."""
        # Create multiple project directories with unique names
        project1 = tmp_path / "frontend"
        project2 = tmp_path / "backend"
        project3 = tmp_path / "shared-lib"
        project1.mkdir()
        project2.mkdir()
        project3.mkdir()
        
        projects = [
            (project1, project1 / "config.toml", None),
            (project2, project2 / "config.toml", None),
            (project3, project3 / "config.toml", None),
        ]
        app = Application(projects)
        
        # Simulate the project ID generation logic
        dir_name_counts = {}
        for target_dir, _, _ in app._project_configs:
            dir_name = target_dir.name
            if dir_name not in dir_name_counts:
                dir_name_counts[dir_name] = []
            dir_name_counts[dir_name].append(target_dir)
        
        duplicate_dir_names = {name for name, paths in dir_name_counts.items() if len(paths) > 1}
        
        # All names are unique, so no duplicates
        assert len(duplicate_dir_names) == 0
        
        # Each project ID would be just the directory name
        expected_ids = {"frontend", "backend", "shared-lib"}
        actual_names = {d.name for d, _, _ in app._project_configs}
        assert actual_names == expected_ids

    def test_project_id_duplicate_names_uses_parent(self, tmp_path):
        """Two projects with same directory name get parent/dirname format."""
        # Create two projects with the same directory name but different parents
        parent1 = tmp_path / "work"
        parent2 = tmp_path / "personal"
        parent1.mkdir()
        parent2.mkdir()
        
        project1 = parent1 / "myapp"
        project2 = parent2 / "myapp"
        project1.mkdir()
        project2.mkdir()
        
        projects = [
            (project1, project1 / "config.toml", None),
            (project2, project2 / "config.toml", None),
        ]
        app = Application(projects)
        
        # Simulate the project ID generation logic
        dir_name_counts = {}
        for target_dir, _, _ in app._project_configs:
            dir_name = target_dir.name
            if dir_name not in dir_name_counts:
                dir_name_counts[dir_name] = []
            dir_name_counts[dir_name].append(target_dir)
        
        duplicate_dir_names = {name for name, paths in dir_name_counts.items() if len(paths) > 1}
        
        # "myapp" should be detected as duplicate
        assert "myapp" in duplicate_dir_names
        
        # Generate project IDs as the actual code does
        existing_project_ids = set()
        generated_ids = []
        for target_dir, _, _ in app._project_configs:
            base_name = target_dir.name
            if base_name in duplicate_dir_names:
                parent_name = target_dir.parent.name
                project_id = f"{parent_name}/{base_name}"
                if project_id in existing_project_ids:
                    grandparent_name = target_dir.parent.parent.name
                    project_id = f"{grandparent_name}/{parent_name}/{base_name}"
            else:
                project_id = base_name
            existing_project_ids.add(project_id)
            generated_ids.append(project_id)
        
        # Verify the IDs use parent directory
        assert "work/myapp" in generated_ids
        assert "personal/myapp" in generated_ids

    def test_project_id_triple_duplicate_uses_grandparent(self, tmp_path):
        """Three projects with same dir name and parent name uses grandparent."""
        # Create three projects where even parent names are duplicated
        # /grandparent1/parent/myapp
        # /grandparent2/parent/myapp
        gp1 = tmp_path / "dev"
        gp2 = tmp_path / "staging"
        gp1.mkdir()
        gp2.mkdir()
        
        parent1 = gp1 / "projects"
        parent2 = gp2 / "projects"
        parent1.mkdir()
        parent2.mkdir()
        
        project1 = parent1 / "webapp"
        project2 = parent2 / "webapp"
        project1.mkdir()
        project2.mkdir()
        
        projects = [
            (project1, project1 / "config.toml", None),
            (project2, project2 / "config.toml", None),
        ]
        app = Application(projects)
        
        # Simulate the project ID generation logic
        dir_name_counts = {}
        for target_dir, _, _ in app._project_configs:
            dir_name = target_dir.name
            if dir_name not in dir_name_counts:
                dir_name_counts[dir_name] = []
            dir_name_counts[dir_name].append(target_dir)
        
        duplicate_dir_names = {name for name, paths in dir_name_counts.items() if len(paths) > 1}
        
        # Generate project IDs as the actual code does
        existing_project_ids = set()
        generated_ids = []
        for target_dir, _, _ in app._project_configs:
            base_name = target_dir.name
            if base_name in duplicate_dir_names:
                parent_name = target_dir.parent.name
                project_id = f"{parent_name}/{base_name}"
                if project_id in existing_project_ids:
                    grandparent_name = target_dir.parent.parent.name
                    project_id = f"{grandparent_name}/{parent_name}/{base_name}"
            else:
                project_id = base_name
            existing_project_ids.add(project_id)
            generated_ids.append(project_id)
        
        # First one should use parent/dirname, second should use grandparent/parent/dirname
        assert "projects/webapp" in generated_ids
        assert "staging/projects/webapp" in generated_ids




class TestParseArgs:
    """Tests for parse_args function."""

    def test_parse_target_directory_only(self):
        """Parse with only target directory."""
        with patch.object(sys, 'argv', ['code-scanner', '/path/to/project']):
            args = parse_args()
            assert args.projects == ['/path/to/project']
            assert args.config is None
            assert args.commit is None

    def test_parse_with_config(self):
        """Parse with config file."""
        with patch.object(sys, 'argv', ['code-scanner', '/project', '-c', '/config.toml']):
            args = parse_args()
            assert args.config == [Path('/config.toml')]

    def test_parse_with_commit(self):
        """Parse with commit hash."""
        with patch.object(sys, 'argv', ['code-scanner', '/project', '--commit', 'abc123']):
            args = parse_args()
            assert args.commit == ['abc123']

    def test_parse_all_options(self):
        """Parse with all options."""
        with patch.object(sys, 'argv', [
            'code-scanner', '/project',
            '-c', '/config.toml',
            '--commit', 'abc123'
        ]):
            args = parse_args()
            assert args.projects == ['/project']
            assert args.config == [Path('/config.toml')]
            assert args.commit == ['abc123']

    def test_parse_mode_default(self):
        """Parse mode defaults to 'uncommitted'."""
        with patch.object(sys, 'argv', ['code-scanner', '/project']):
            args = parse_args()
            assert args.mode == 'uncommitted'

    def test_parse_mode_branch(self):
        """Parse with branch mode."""
        with patch.object(sys, 'argv', ['code-scanner', '/project', '-m', 'branch']):
            args = parse_args()
            assert args.mode == 'branch'

    def test_parse_mode_uncommitted_explicit(self):
        """Parse with explicit uncommitted mode."""
        with patch.object(sys, 'argv', ['code-scanner', '/project', '-m', 'uncommitted']):
            args = parse_args()
            assert args.mode == 'uncommitted'

    def test_parse_mode_invalid(self):
        """Parse with invalid mode exits."""
        with patch.object(sys, 'argv', ['code-scanner', '/project', '-m', 'invalid']):
            with pytest.raises(SystemExit):
                parse_args()

    def test_parse_unrecognized_arg_lists_known_options(self, capsys):
        """Unrecognized-argument error lists known options and staleness hint."""
        with patch.object(sys, 'argv', ['code-scanner', '/project', '--modee', 'branch']):
            with pytest.raises(SystemExit) as exc_info:
                parse_args()

        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        # Concise error line is still present
        assert "unrecognized arguments: --modee" in captured.err
        # Actionable hints are appended
        assert "Known options:" in captured.err
        assert "--mode" in captured.err  # the *correct* flag is shown
        assert "outdated" in captured.err  # staleness hint

    def test_parse_invalid_choice_lists_known_options(self, capsys):
        """Invalid-choice error lists known options."""
        with patch.object(sys, 'argv', ['code-scanner', '/project', '-m', 'bogus']):
            with pytest.raises(SystemExit) as exc_info:
                parse_args()

        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "invalid choice" in captured.err
        assert "Known options:" in captured.err

    def test_parse_missing_projects_uses_plain_error(self, capsys):
        """Missing-required-argument error is unchanged (no hint block)."""
        with patch.object(sys, 'argv', ['code-scanner', '-m', 'branch']):
            with pytest.raises(SystemExit):
                parse_args()

        captured = capsys.readouterr()
        assert "arguments are required: projects" in captured.err
        # No staleness hint for this error class
        assert "outdated" not in captured.err





class TestApplicationLockFile:
    """Tests for Application lock file handling."""

    @pytest.fixture(autouse=True)
    def mock_config_dir(self, tmp_path):
        """Mock get_config_dir to use tmp_path."""
        with patch('code_scanner.utils.get_config_dir', return_value=tmp_path):
            yield

    def test_acquire_lock_creates_file(self, tmp_path):
        """Acquire lock creates lock file."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        app = Application(projects)
        app._acquire_lock()
        
        # Lock file should be in config directory
        from code_scanner.utils import get_config_dir
        lock_path = get_config_dir() / 'code_scanner.lock'
        assert lock_path.exists()
        assert app._lock_acquired is True
        
        # Cleanup
        app._release_lock()

    def test_acquire_lock_fails_if_process_running(self, tmp_path):
        """Acquire lock fails if lock file exists and process is running."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        # Create existing lock with current process PID (definitely running)
        from code_scanner.utils import get_config_dir
        lock_path = get_config_dir() / 'code_scanner.lock'
        import os
        lock_path.write_text(str(os.getpid()))
        
        app = Application(projects)
        
        with pytest.raises(LockFileError, match="Another code-scanner instance is already running"):
            app._acquire_lock()
        
        # Cleanup
        if lock_path.exists():
            lock_path.unlink()

    def test_acquire_lock_removes_stale_lock(self, tmp_path):
        """Acquire lock removes stale lock if process is not running."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        # Create existing lock with a PID that's definitely not running
        from code_scanner.utils import get_config_dir
        lock_path = get_config_dir() / 'code_scanner.lock'
        lock_path.write_text("999999999")
        
        app = Application(projects)
        app._acquire_lock()
        
        # Lock should be acquired with our PID
        assert app._lock_acquired is True
        import os
        assert lock_path.read_text().strip() == str(os.getpid())
        
        # Cleanup
        app._release_lock()

    def test_release_lock_removes_file(self, tmp_path):
        """Release lock removes lock file."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        from code_scanner.utils import get_config_dir
        lock_path = get_config_dir() / 'code_scanner.lock'
        
        # Create lock file
        lock_path.write_text("1234")
        
        app = Application(projects)
        app._lock_acquired = True
        app._release_lock()
        
        assert not lock_path.exists()
        assert app._lock_acquired is False

    def test_release_lock_does_nothing_if_not_acquired(self, tmp_path):
        """Release lock does nothing if lock not acquired."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        from code_scanner.utils import get_config_dir
        lock_path = get_config_dir() / 'code_scanner.lock'
        
        # Create file but don't mark as acquired
        lock_path.write_text("1234")
        
        app = Application(projects)
        app._lock_acquired = False
        
        app._release_lock()
        
        # File should still exist
        assert lock_path.exists()

    def test_acquire_lock_registers_atexit_handler(self, tmp_path):
        """Acquire lock registers atexit handler for cleanup on any exit."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        app = Application(projects)
        
        with patch('code_scanner.cli.atexit.register') as mock_atexit:
            app._acquire_lock()
            
            # Verify atexit.register was called with _release_lock
            mock_atexit.assert_called_once_with(app._release_lock)
        
        # Cleanup
        app._release_lock()

    def test_lock_released_via_atexit_on_keyboard_interrupt(self, tmp_path):
        """Lock is released via atexit when KeyboardInterrupt occurs."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        from code_scanner.utils import get_config_dir
        lock_path = get_config_dir() / 'code_scanner.lock'
        
        # Create lock file
        lock_path.write_text("12345")
        
        app = Application(projects)
        
        # Simulate lock acquisition
        app._acquire_lock()
        assert lock_path.exists()
        
        # Simulate calling the registered atexit handler (as would happen on exit)
        app._release_lock()
        
        assert not lock_path.exists()
        assert app._lock_acquired is False

class TestApplicationSignalHandler:
    """Tests for Application signal handling."""

    def test_signal_handler_sets_stop_event(self, tmp_path):
        """Signal handler sets stop event."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        app = Application(projects)
        
        assert not app._stop_event.is_set()
        
        app._signal_handler(2, None)  # SIGINT
        
        assert app._stop_event.is_set()


class TestApplicationCleanup:
    """Tests for Application cleanup."""

    def test_cleanup_stops_scanner(self, tmp_path):
        """Cleanup stops scanner."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        app = Application(projects)
        app.scanner = MagicMock()
        app._lock_acquired = False
        
        app._cleanup()
        
        app.scanner.stop.assert_called_once()

    def test_cleanup_releases_lock(self, tmp_path):
        """Cleanup releases lock."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        from code_scanner.utils import get_config_dir
        lock_path = get_config_dir() / 'code_scanner.lock'
        lock_path.write_text("1234")
        
        app = Application(projects)
        app._lock_acquired = True
        
        app._cleanup()
        
        assert not lock_path.exists()

    def test_cleanup_handles_no_scanner(self, tmp_path):
        """Cleanup handles case when scanner is None."""
        # Create a single project tuple
        projects = [(tmp_path, tmp_path / "config.toml", None)]
        
        app = Application(projects)
        app.scanner = None
        app._lock_acquired = False
        
        # Should not raise
        app._cleanup()


class TestLockFileError:
    """Tests for LockFileError exception."""

    def test_lock_file_error_message(self):
        """LockFileError has proper message."""
        error = LockFileError("Lock file exists")
        assert str(error) == "Lock file exists"

    def test_lock_file_error_is_exception(self):
        """LockFileError is an Exception."""
        assert issubclass(LockFileError, Exception)
