"""Coverage-focused tests for git_watcher module."""

import os
import pytest
import tempfile
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_scanner.git_watcher import GitWatcher, GitError
from code_scanner.models import GitState, ChangedFile


@pytest.fixture
def temp_git_repo():
    """Create a temporary Git repository."""
    temp_dir = tempfile.mkdtemp()
    
    subprocess.run(["git", "init", "-q"], cwd=temp_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=temp_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=temp_dir, capture_output=True)
    
    readme = Path(temp_dir) / "README.md"
    readme.write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=temp_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial", "-q"], cwd=temp_dir, capture_output=True)
    
    yield Path(temp_dir)
    
    shutil.rmtree(temp_dir, ignore_errors=True)


class TestGitWatcherIsIgnored:
    """Tests for _is_ignored method."""

    def test_is_ignored_returns_false_for_tracked(self, temp_git_repo):
        """Test _is_ignored returns False for tracked files."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        result = watcher._is_ignored("README.md")
        assert result is False

    def test_is_ignored_returns_true_for_gitignored(self, temp_git_repo):
        """Test _is_ignored returns True for ignored files."""
        # Create .gitignore
        gitignore = temp_git_repo / ".gitignore"
        gitignore.write_text("*.log\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=temp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add gitignore", "-q"], cwd=temp_git_repo, capture_output=True)
        
        # Create ignored file
        log_file = temp_git_repo / "test.log"
        log_file.write_text("log content")
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        result = watcher._is_ignored("test.log")
        assert result is True

    def test_is_ignored_returns_false_when_not_connected(self, temp_git_repo):
        """Test _is_ignored returns False when not connected."""
        watcher = GitWatcher(temp_git_repo)
        # Don't connect
        
        result = watcher._is_ignored("any.txt")
        assert result is False


class TestGitWatcherGetState:
    """Tests for get_state method edge cases."""

    def test_get_state_not_connected_raises(self, temp_git_repo):
        """Test get_state raises error when not connected."""
        watcher = GitWatcher(temp_git_repo)
        
        with pytest.raises(GitError) as exc_info:
            watcher.get_state()
        
        assert "Not connected" in str(exc_info.value)

    def test_get_state_rebase_head_detected(self, temp_git_repo):
        """Test get_state detects REBASE_HEAD when unmerged entries exist."""
        self._create_conflict(temp_git_repo, "coverage_rebase.txt", merge=False)

        watcher = GitWatcher(temp_git_repo)
        watcher.connect()

        state = watcher.get_state()

        assert state.is_rebasing

    def test_get_state_rebase_apply_detected(self, temp_git_repo):
        """Test get_state detects rebase-apply directory when unmerged entries exist."""
        import subprocess

        # Create rebase-apply directory AND unmerged entries via real rebase conflict
        self._create_conflict(temp_git_repo, "coverage_rebase_apply.txt", merge=False)

        watcher = GitWatcher(temp_git_repo)
        watcher.connect()

        state = watcher.get_state()

        assert state.is_rebasing

    @staticmethod
    def _create_conflict(repo: Path, filename: str, merge: bool) -> None:
        """Create a real merge/rebase conflict with unmerged entries."""
        import subprocess

        def run(cmd, **kw):
            return subprocess.run(cmd, cwd=repo, capture_output=True, **kw)

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

    def test_get_state_handles_detached_head(self, temp_git_repo):
        """Test get_state handles detached HEAD state."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Get current commit
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True
        )
        commit = result.stdout.strip()
        
        # Detach HEAD
        subprocess.run(["git", "checkout", commit, "-q"], cwd=temp_git_repo, capture_output=True)
        
        state = watcher.get_state()
        
        # Should not crash in detached HEAD state
        assert state is not None


class TestGitWatcherGetChangedFiles:
    """Tests for _get_changed_files method."""

    def test_get_changed_files_handles_renamed(self, temp_git_repo):
        """Test handling of renamed files."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Create and commit a file
        old_file = temp_git_repo / "old_name.txt"
        old_file.write_text("content")
        subprocess.run(["git", "add", "old_name.txt"], cwd=temp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add file", "-q"], cwd=temp_git_repo, capture_output=True)
        
        # Rename the file
        new_file = temp_git_repo / "new_name.txt"
        subprocess.run(["git", "mv", "old_name.txt", "new_name.txt"], cwd=temp_git_repo, capture_output=True)
        
        state = watcher.get_state()
        
        # Should detect the rename
        assert state.has_changes

    def test_get_changed_files_skips_directories(self, temp_git_repo):
        """Test that directories (submodules) are skipped."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Create a directory that looks like an untracked item
        subdir = temp_git_repo / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")
        
        state = watcher.get_state()
        
        # Should have the file but not the directory itself
        paths = [f.path for f in state.changed_files]
        assert "subdir" not in paths

    def test_get_changed_files_with_commit_hash(self, temp_git_repo):
        """Test comparing against a specific commit."""
        # Get initial commit
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True
        )
        initial_commit = result.stdout.strip()
        
        # Make a new commit
        new_file = temp_git_repo / "new.txt"
        new_file.write_text("new content")
        subprocess.run(["git", "add", "new.txt"], cwd=temp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add new", "-q"], cwd=temp_git_repo, capture_output=True)
        
        # Create watcher comparing against initial commit
        watcher = GitWatcher(temp_git_repo, commit_hash=initial_commit)
        watcher.connect()
        
        state = watcher.get_state()
        
        # Should detect the new file as changed since initial commit
        paths = [f.path for f in state.changed_files]
        assert "new.txt" in paths


class TestGitWatcherHasChangesSince:
    """Tests for has_changes_since method."""

    def test_has_changes_since_none_returns_has_changes(self, temp_git_repo):
        """Test has_changes_since with None returns current has_changes."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Create uncommitted file
        (temp_git_repo / "new.txt").write_text("content")
        
        result = watcher.has_changes_since(None)
        
        assert result is True

    def test_has_changes_since_same_state_returns_false(self, temp_git_repo):
        """Test has_changes_since with same state returns False."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Create a change
        (temp_git_repo / "new.txt").write_text("content")
        
        state1 = watcher.get_state()
        
        # No new changes
        result = watcher.has_changes_since(state1)
        
        assert result is False

    def test_has_changes_since_new_file_returns_true(self, temp_git_repo):
        """Test has_changes_since detects new files."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        state1 = watcher.get_state()
        
        # Create a new file and stage it so git status sees it
        (temp_git_repo / "new.txt").write_text("content")
        subprocess.run(["git", "add", "new.txt"], cwd=temp_git_repo, capture_output=True)
        
        result = watcher.has_changes_since(state1)
        
        assert result is True


class TestGitWatcherConnect:
    """Tests for connect method."""

    def test_connect_valid_repo(self, temp_git_repo):
        """Test connecting to valid repository."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()

    def test_connect_invalid_path_raises(self):
        """Test connecting to non-repo raises error."""
        temp_dir = tempfile.mkdtemp()
        
        try:
            watcher = GitWatcher(Path(temp_dir))
            
            with pytest.raises(GitError) as exc_info:
                watcher.connect()
            
            assert "Not a Git repository" in str(exc_info.value)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_connect_with_valid_commit_hash(self, temp_git_repo):
        """Test connecting with valid commit hash."""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True
        )
        commit = result.stdout.strip()
        
        watcher = GitWatcher(temp_git_repo, commit_hash=commit)
        watcher.connect()

    def test_connect_with_invalid_commit_hash_raises(self, temp_git_repo):
        """Test connecting with invalid commit hash raises error."""
        watcher = GitWatcher(temp_git_repo, commit_hash="invalid123456")
        
        with pytest.raises(GitError) as exc_info:
            watcher.connect()
        
        assert "Invalid commit hash" in str(exc_info.value)


class TestGitWatcherIsIgnoredNoRepo:
    """Tests for _is_ignored when not connected."""

    def test_is_ignored_returns_false_when_not_connected(self, temp_git_repo):
        """Test _is_ignored returns False when repo is None."""
        watcher = GitWatcher(temp_git_repo)
        # Don't call connect(), so _repo is None
        
        result = watcher._is_ignored("anyfile.txt")
        assert result is False


class TestHasChangesSinceEdgeCases:
    """Additional tests for has_changes_since method."""

    def test_has_changes_since_identical_states(self, temp_git_repo):
        """Test has_changes_since returns False for identical states."""
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        result = watcher.has_changes_since(state)
        assert result is False

    def test_has_changes_since_none_with_changes(self, temp_git_repo):
        """Test has_changes_since with None and changes present."""
        # Create a new untracked file
        new_file = temp_git_repo / "new_file.txt"
        new_file.write_text("content")
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        result = watcher.has_changes_since(None)
        assert result is True

    def test_has_changes_since_oserror_on_stat(self, temp_git_repo):
        """Test has_changes_since returns True when file stat fails during mtime check."""
        from unittest.mock import patch, MagicMock
        
        # Create a file
        new_file = temp_git_repo / "test_file.txt"
        new_file.write_text("content")
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Get state with the file (has mtime_ns)
        state = watcher.get_state()
        assert len(state.changed_files) > 0
        
        # Create a fake current_state that will be returned by get_state 
        # but has a file where stat will fail
        fake_changed_file = ChangedFile(
            path="test_file.txt",
            status="unstaged", 
            mtime_ns=new_file.stat().st_mtime_ns
        )
        fake_state = GitState(changed_files=[fake_changed_file])
        
        # Delete the file so stat will fail during mtime check
        new_file.unlink()
        
        with patch.object(watcher, 'get_state', return_value=fake_state):
            # has_changes_since should return False - OSError is skipped (file doesn't exist)
            # This prevents false positives from files with encoding issues or missing files
            result = watcher.has_changes_since(state)
        
        assert result is False

    def test_has_changes_since_deleted_file_skipped(self, temp_git_repo):
        """Test has_changes_since skips deleted files in mtime check."""
        # Create and commit a file
        test_file = temp_git_repo / "to_delete.txt"
        test_file.write_text("content")
        subprocess.run(["git", "add", "to_delete.txt"], cwd=temp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add file", "-q"], cwd=temp_git_repo, capture_output=True)
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Delete the file and stage the deletion
        subprocess.run(["git", "rm", "to_delete.txt"], cwd=temp_git_repo, capture_output=True)
        
        state1 = watcher.get_state()
        
        # Should have a deleted file
        deleted_files = [f for f in state1.changed_files if f.is_deleted]
        assert len(deleted_files) >= 1
        
        # has_changes_since should work without errors
        result = watcher.has_changes_since(state1)
        assert result is False

    def test_has_changes_since_none_mtime(self, temp_git_repo):
        """Test has_changes_since handles None mtime_ns."""
        # Create a file and stage it
        new_file = temp_git_repo / "test_file.txt"
        new_file.write_text("content")
        subprocess.run(["git", "add", "test_file.txt"], cwd=temp_git_repo, capture_output=True)
        
        watcher = GitWatcher(temp_git_repo)
        watcher.connect()
        
        # Get initial state
        state = watcher.get_state()
        
        # Manually set None mtime_ns
        if state.changed_files:
            state.changed_files[0] = ChangedFile(
                path=state.changed_files[0].path, 
                status="staged", 
                mtime_ns=None
            )
        
        # Should not crash and return False (None mtime_ns means we can't compare mtime)
        result = watcher.has_changes_since(state)
        assert result is False

class TestGitWatcherCaching:
    """Tests for git status caching."""

    def test_cache_returns_cached_state(self, temp_git_repo):
        """Test that cache returns cached state within TTL."""
        watcher = GitWatcher(temp_git_repo, cache_ttl=10.0)  # Long TTL
        watcher.connect()
        
        # Create a file and stage it
        (temp_git_repo / "test.txt").write_text("content")
        subprocess.run(["git", "add", "test.txt"], cwd=temp_git_repo, capture_output=True)
        
        # First call - populates cache
        state1 = watcher.get_state()
        
        # Second call should return cached state (same object)
        state2 = watcher.get_state()
        
        assert state1 is state2

    def test_force_refresh_bypasses_cache(self, temp_git_repo):
        """Test that force_refresh=True bypasses cache."""
        watcher = GitWatcher(temp_git_repo, cache_ttl=10.0)  # Long TTL
        watcher.connect()
        
        # First call
        state1 = watcher.get_state()
        
        # Force refresh - should get new state object
        state2 = watcher.get_state(force_refresh=True)
        
        assert state1 is not state2

    def test_invalidate_cache_clears_cache(self, temp_git_repo):
        """Test that invalidate_cache clears the cache."""
        watcher = GitWatcher(temp_git_repo, cache_ttl=10.0)
        watcher.connect()
        
        # Populate cache
        state1 = watcher.get_state()
        
        # Invalidate
        watcher.invalidate_cache()
        
        # Next call should get fresh state
        state2 = watcher.get_state()
        
        assert state1 is not state2

    def test_cache_expires_after_ttl(self, temp_git_repo):
        """Test that cache expires after TTL."""
        import time
        
        watcher = GitWatcher(temp_git_repo, cache_ttl=0.1)  # Very short TTL
        watcher.connect()
        
        # First call
        state1 = watcher.get_state()
        
        # Wait for TTL to expire
        time.sleep(0.15)
        
        # Should get new state
        state2 = watcher.get_state()
        
        assert state1 is not state2


class TestGitWatcherFileFilter:
    """Tests for FileFilter integration."""

    def test_uses_file_filter_for_gitignore(self, temp_git_repo):
        """Test that watcher uses FileFilter for gitignore checking."""
        from code_scanner.file_filter import FileFilter
        
        # Create gitignore
        gitignore = temp_git_repo / ".gitignore"
        gitignore.write_text("*.log\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=temp_git_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add gitignore", "-q"], cwd=temp_git_repo, capture_output=True)
        
        # Create file filter
        file_filter = FileFilter(
            repo_path=temp_git_repo,
            scanner_files=set()
        )
        
        watcher = GitWatcher(temp_git_repo, file_filter=file_filter)
        watcher.connect()
        
        # Create ignored file
        (temp_git_repo / "test.log").write_text("log")
        
        state = watcher.get_state()
        
        # Ignored file should not be in changed files
        paths = [f.path for f in state.changed_files]
        assert "test.log" not in paths


class TestGitWatcherUnmergedFiles:
    """Tests for handling unmerged files."""

    def test_handles_unmerged_files_during_merge_conflict(self, temp_git_repo):
        """Test handling of unmerged files — merge conflicts detected via state flags."""
        import subprocess

        def run(cmd, **kw):
            return subprocess.run(cmd, cwd=temp_git_repo, capture_output=True, **kw)

        result = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True)
        base_branch = result.stdout.strip()

        (temp_git_repo / "conflict.txt").write_text("version 1\n")
        run(["git", "add", "conflict.txt"])
        run(["git", "commit", "-m", "base"])

        run(["git", "checkout", "-b", "side"])
        (temp_git_repo / "conflict.txt").write_text("version 2\n")
        run(["git", "add", "conflict.txt"])
        run(["git", "commit", "-m", "side commit"])

        run(["git", "checkout", base_branch])
        (temp_git_repo / "conflict.txt").write_text("version 3\n")
        run(["git", "add", "conflict.txt"])
        run(["git", "commit", "-m", "main commit"])

        run(["git", "merge", "side"])

        watcher = GitWatcher(temp_git_repo)
        watcher.connect()

        state = watcher.get_state()

        assert state.is_merging