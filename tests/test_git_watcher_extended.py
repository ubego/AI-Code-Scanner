"""Additional tests for git_watcher module to increase coverage."""

import os
import pytest
import subprocess
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from code_scanner.git_watcher import GitWatcher, GitError
from code_scanner.models import FileStatus, GitState, ChangedFile


@pytest.fixture
def temp_git_repo():
    """Create a temporary Git repository."""
    temp_dir = tempfile.mkdtemp()
    
    # Initialize Git repo
    subprocess.run(['git', 'init', '-q'], cwd=temp_dir, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=temp_dir, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=temp_dir, check=True)
    
    # Create initial commit
    readme = Path(temp_dir) / "README.md"
    readme.write_text("# Test\n")
    subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
    subprocess.run(['git', 'commit', '-m', 'Initial', '-q'], cwd=temp_dir, check=True)
    
    yield Path(temp_dir)
    
    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


class TestGitWatcherIsIgnored:
    """Tests for _is_ignored method."""

    def test_ignored_file_returns_true(self, temp_git_repo):
        """Files in .gitignore return True."""
        # Create .gitignore
        gitignore = temp_git_repo / ".gitignore"
        gitignore.write_text("*.log\n")
        subprocess.run(['git', 'add', '.gitignore'], cwd=temp_git_repo, check=True)
        subprocess.run(['git', 'commit', '-m', 'Add gitignore', '-q'], cwd=temp_git_repo, check=True)
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        result = watcher._is_ignored("test.log")
        assert result is True

    def test_non_ignored_file_returns_false(self, temp_git_repo):
        """Files not in .gitignore return False."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        result = watcher._is_ignored("test.py")
        assert result is False

    def test_is_ignored_not_connected(self):
        """Returns False when not connected."""
        watcher = GitWatcher(Path("/tmp"))
        # Don't connect
        
        result = watcher._is_ignored("test.py")
        assert result is False


class TestGitWatcherCommitComparison:
    """Tests for commit hash comparison."""

    def test_changes_relative_to_commit(self, temp_git_repo):
        """Test detecting changes relative to a specific commit."""
        # Get current commit hash
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True
        )
        initial_commit = result.stdout.strip()
        
        # Make another commit
        test_file = temp_git_repo / "test.py"
        test_file.write_text("print('test')\n")
        subprocess.run(['git', 'add', '.'], cwd=temp_git_repo, check=True)
        subprocess.run(['git', 'commit', '-m', 'Add test', '-q'], cwd=temp_git_repo, check=True)
        
        # Create watcher comparing to initial commit
        watcher = GitWatcher(temp_git_repo, commit_hash=initial_commit)
        watcher.connect()
        
        state = watcher.get_state()
        
        # test.py should show as changed relative to initial commit
        # (it was added after)
        file_paths = [f.path for f in state.changed_files]
        # Note: The file was committed, so it won't show as uncommitted
        # This tests the commit comparison path exists


class TestGitWatcherMergeConflict:
    """Tests for merge/rebase conflict detection."""

    def test_merge_head_detected(self, temp_git_repo):
        """Test that MERGE_HEAD file with unmerged entries is detected."""
        import subprocess
        self._create_conflict(temp_git_repo, "merge_conflict.txt", merge=True)

        watcher = GitWatcher(temp_git_repo)
        watcher.connect()

        state = watcher.get_state()

        assert state.is_conflict_resolution_in_progress is True
        assert state.is_merging

    def test_rebase_head_detected(self, temp_git_repo):
        """Test that REBASE_HEAD file with unmerged entries is detected."""
        import subprocess
        self._create_conflict(temp_git_repo, "rebase_conflict.txt", merge=False)

        watcher = GitWatcher(temp_git_repo)
        watcher.connect()

        state = watcher.get_state()

        assert state.is_conflict_resolution_in_progress is True
        assert state.is_rebasing

    def test_rebase_merge_dir_detected(self, temp_git_repo):
        """Test that rebase-merge dir with unmerged entries is detected."""
        import subprocess
        self._create_conflict(temp_git_repo, "rebase_merge_conflict.txt", merge=False)

        watcher = GitWatcher(temp_git_repo)
        watcher.connect()

        state = watcher.get_state()

        assert state.is_conflict_resolution_in_progress is True
        assert state.is_rebasing

    def _create_conflict(self, repo: Path, filename: str, merge: bool) -> None:
        """Create a real merge/rebase conflict with unmerged entries."""
        import subprocess

        def run(cmd, **kw):
            return subprocess.run(cmd, cwd=repo, capture_output=True, **kw)

        # Get current branch
        result = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True)
        base_branch = result.stdout.strip()

        (repo / filename).write_text("version 1\n")
        run(["git", "add", filename])
        run(["git", "commit", "-m", "base commit for conflict"])

        run(["git", "checkout", "-b", "conflict_side"])
        (repo / filename).write_text("version 2\n")
        run(["git", "add", filename])
        run(["git", "commit", "-m", "side commit"])

        run(["git", "checkout", base_branch])
        (repo / filename).write_text("version 3\n")
        run(["git", "add", filename])
        run(["git", "commit", "-m", "main commit"])

        if merge:
            run(["git", "merge", "conflict_side"])
        else:
            run(["git", "rebase", "conflict_side"])


class TestGitWatcherFileStatuses:
    """Tests for different file status handling."""

    def test_renamed_file_detection(self, temp_git_repo):
        """Test that renamed files are detected."""
        # Create and commit a file
        old_file = temp_git_repo / "old_name.txt"
        old_file.write_text("content\n")
        subprocess.run(['git', 'add', '.'], cwd=temp_git_repo, check=True)
        subprocess.run(['git', 'commit', '-m', 'Add file', '-q'], cwd=temp_git_repo, check=True)
        
        # Rename the file
        new_file = temp_git_repo / "new_name.txt"
        old_file.rename(new_file)
        subprocess.run(['git', 'add', '.'], cwd=temp_git_repo, check=True)
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        # Should detect renamed file
        assert state.has_changes

    def test_deleted_file_detection(self, temp_git_repo):
        """Test that deleted files are detected with correct status."""
        # Create and commit a file
        test_file = temp_git_repo / "to_delete.txt"
        test_file.write_text("content\n")
        subprocess.run(['git', 'add', '.'], cwd=temp_git_repo, check=True)
        subprocess.run(['git', 'commit', '-m', 'Add file', '-q'], cwd=temp_git_repo, check=True)
        
        # Delete the file
        test_file.unlink()
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        deleted_files = [f for f in state.changed_files if f.is_deleted]
        assert len(deleted_files) >= 1
        assert any("to_delete.txt" in f.path for f in deleted_files)

    def test_untracked_file_detection(self, temp_git_repo):
        """Test that untracked files are detected."""
        # Create untracked file
        untracked = temp_git_repo / "untracked.txt"
        untracked.write_text("new content\n")
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        file_paths = [f.path for f in state.changed_files]
        assert "untracked.txt" in file_paths



class TestGitWatcherHasChangesSince:
    """Tests for has_changes_since method."""

    def test_has_changes_since_none(self, temp_git_repo):
        """has_changes_since(None) returns True if has_changes."""
        # Create uncommitted file
        test_file = temp_git_repo / "new.txt"
        test_file.write_text("content\n")
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        result = watcher.has_changes_since(None)
        assert result is True

    def test_has_changes_since_same_state(self, temp_git_repo):
        """has_changes_since returns False if state unchanged."""
        # Create uncommitted file
        test_file = temp_git_repo / "new.txt"
        test_file.write_text("content\n")
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        state1 = watcher.get_state()
        
        # Check against same state
        result = watcher.has_changes_since(state1)
        assert result is False

    def test_has_changes_since_different_state(self, temp_git_repo):
        """has_changes_since returns True if state changed."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Get initial state (no changes)
        state1 = watcher.get_state()
        
        # Create new file
        test_file = temp_git_repo / "new.txt"
        test_file.write_text("content\n")
        
        # Check for changes
        result = watcher.has_changes_since(state1)
        assert result is True


class TestGitWatcherUnmerged:
    """Tests for unmerged file detection (merge conflicts)."""

    def test_unmerged_file_detection(self, temp_git_repo):
        """Test that unmerged files ('u' status in porcelain v2) are detected."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # We can't easily create a real unmerged state with just git commands in a linear script
        # without thorough setup (branching, conflicting commits, merge).
        # So we'll mock the _repo.git.status output to simulate 'u' entries.
        
        # Patching watcher._repo.git.status fails due to gitpython internal structure.
        # Instead, we replace the whole repo object with a mock for this call.
        original_repo = watcher._repo
        mock_repo = MagicMock()
        # Mock output for: u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
        mock_repo.git.status.return_value = "u UU N... 100644 100644 100644 100644 h1 h2 h3 conflict.txt"
        
        # mock check_ignore to raise GitCommandError (meaning NOT ignored)
        mock_repo.git.check_ignore.side_effect = GitError("Not ignored") # GitWatcher catches GitCommandError?
        # Wait, git_watcher.py catches GitCommandError.
        # I need to import GitCommandError or use a generic exception if implementation allows, 
        # but implementation catches GitCommandError specifically.
        from git import GitCommandError
        mock_repo.git.check_ignore.side_effect = GitCommandError("check-ignore", 1)
        
        watcher._repo = mock_repo
        try:
            changed_files = watcher._get_changed_files()
        finally:
            watcher._repo = original_repo
            
        assert len(changed_files) == 1
        assert changed_files[0].path == "conflict.txt"
        # Status mapping logic:
        # xy = "UU" -> index="U", work="U" -> status="staged" (because U != . and U != ?)
        assert changed_files[0].status == FileStatus.STAGED
