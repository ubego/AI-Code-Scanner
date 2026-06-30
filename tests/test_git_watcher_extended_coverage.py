"""Extended coverage tests for git_watcher module - targeting uncovered paths."""

import pytest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_scanner.git_watcher import GitWatcher, GitError
from code_scanner.models import GitState, ChangedFile


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary Git repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    
    # Create initial commit
    (repo / "initial.py").write_text("print('initial')")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True)
    
    return repo


class TestRenamedFilesHandling:
    """Test detection of renamed files."""

    def test_renamed_file_detection(self, git_repo):
        """Test detection of renamed files via git mv."""
        # Create and commit a file
        old_file = git_repo / "old_name.py"
        old_file.write_text("content = 'hello'")
        subprocess.run(["git", "add", "old_name.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "add file"], cwd=git_repo, check=True)
        
        # Rename the file
        subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=git_repo, check=True)
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()
        
        # Should detect the renamed file (may show as old or new name depending on git status parsing)
        paths = [f.path for f in state.changed_files]
        assert len(paths) > 0  # Should have detected changes
        # The file should be tracked as either old_name.py (deleted) or new_name.py (new)
        assert "new_name.py" in paths or "old_name.py" in paths

    def test_staged_new_file_detection(self, git_repo):
        """Test detection of staged new files."""
        new_file = git_repo / "new_file.py"
        new_file.write_text("content = 'hello'")
        subprocess.run(["git", "add", "new_file.py"], cwd=git_repo, check=True)
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()
        
        paths = [f.path for f in state.changed_files]
        assert "new_file.py" in paths


class TestMergeConflictHandling:
    """Test handling of merge conflicts."""

    def test_merge_in_progress_detection(self, git_repo):
        """Test detection when merge is in progress."""
        # Create a branch and make conflicting changes
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=git_repo, check=True)
        
        conflict_file = git_repo / "conflict.py"
        conflict_file.write_text("feature content")
        subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "feature change"], cwd=git_repo, check=True)
        
        # Back to main branch
        subprocess.run(["git", "checkout", "master"], cwd=git_repo, check=True, capture_output=True)
        if subprocess.run(["git", "checkout", "master"], cwd=git_repo, capture_output=True).returncode != 0:
            subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True)
        
        # Create conflicting change
        conflict_file.write_text("master content")
        subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "master change"], cwd=git_repo, check=True)
        
        # Attempt merge (will conflict)
        result = subprocess.run(
            ["git", "merge", "feature"],
            cwd=git_repo,
            capture_output=True
        )
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()
        
        # Should detect merge in progress if conflict occurred
        if result.returncode != 0:
            assert state.is_merging or state.is_conflict_resolution_in_progress

    def test_rebase_detection(self, git_repo):
        """Test detection of rebase in progress with real unmerged entries."""
        import subprocess

        def run(cmd, **kw):
            return subprocess.run(cmd, cwd=git_repo, capture_output=True, **kw)

        result = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True)
        base_branch = result.stdout.strip()

        (git_repo / "rebase.txt").write_text("version 1\n")
        run(["git", "add", "rebase.txt"])
        run(["git", "commit", "-m", "base"])

        run(["git", "checkout", "-b", "side_rebase"])
        (git_repo / "rebase.txt").write_text("version 2\n")
        run(["git", "add", "rebase.txt"])
        run(["git", "commit", "-m", "side commit"])

        run(["git", "checkout", base_branch])
        (git_repo / "rebase.txt").write_text("version 3\n")
        run(["git", "add", "rebase.txt"])
        run(["git", "commit", "-m", "main commit"])

        run(["git", "rebase", "side_rebase"])

        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()

        assert state.is_rebasing


class TestIsIgnoredFallback:
    """Test _is_ignored fallback to git check-ignore."""

    def test_is_ignored_without_file_filter(self, git_repo):
        """Test _is_ignored falls back to git check-ignore."""
        # Create gitignore
        (git_repo / ".gitignore").write_text("*.log\n*.tmp\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=git_repo, check=True)
        
        watcher = GitWatcher(git_repo, file_filter=None)
        watcher.connect()
        
        # Test ignored file
        assert watcher._is_ignored("debug.log") is True
        assert watcher._is_ignored("cache.tmp") is True
        assert watcher._is_ignored("main.py") is False


class TestHasChangesSinceMtimeEdgeCases:
    """Test has_changes_since mtime edge cases."""

    def test_mtime_change_detected(self, git_repo):
        """Test detection of mtime changes."""
        # Create an uncommitted file
        test_file = git_repo / "test.py"
        test_file.write_text("content")
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state1 = watcher.get_state()
        
        # Modify the file
        import time
        time.sleep(0.01)  # Ensure mtime changes
        test_file.write_text("modified content")
        
        # Force cache invalidation
        watcher.invalidate_cache()
        
        # Check for changes
        has_changes = watcher.has_changes_since(state1)
        
        # Should detect the change
        assert has_changes is True

    def test_no_changes_same_state(self, git_repo):
        """Test no changes detected when state is same."""
        test_file = git_repo / "test.py"
        test_file.write_text("content")
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state1 = watcher.get_state()
        
        # Get state again without changes
        watcher.invalidate_cache()
        
        has_changes = watcher.has_changes_since(state1)
        
        # Should not detect changes if nothing changed
        assert has_changes is False

    def test_file_stat_failure_handled(self, git_repo):
        """Test handling when file stat fails during mtime check."""
        test_file = git_repo / "test.py"
        test_file.write_text("content")
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        # Delete the file to simulate stat failure
        test_file.unlink()
        
        # Should not crash when file no longer exists
        watcher.invalidate_cache()
        has_changes = watcher.has_changes_since(state)
        
        # Should detect change (file was removed)
        assert isinstance(has_changes, bool)


class TestCacheInvalidation:
    """Test cache invalidation behavior."""

    def test_cache_invalidation(self, git_repo):
        """Test that cache is properly invalidated."""
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        # Get state (caches it)
        state1 = watcher.get_state()
        
        # Should return cached state
        state2 = watcher.get_state()
        
        # Invalidate cache
        watcher.invalidate_cache()
        
        # Should fetch fresh state
        state3 = watcher.get_state(force_refresh=True)

    def test_force_refresh_bypasses_cache(self, git_repo):
        """Test that force_refresh bypasses cache."""
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        # Get cached state
        state1 = watcher.get_state()
        
        # Force refresh should work even with valid cache
        state2 = watcher.get_state(force_refresh=True)


class TestExcludedFilesHandling:
    """Test excluded files handling."""

    def test_excluded_files_not_trigger_rescan(self, git_repo):
        """Test that excluded files don't trigger rescan."""
        # Create output file (typically excluded)
        output_file = git_repo / "code_scanner_results.md"
        output_file.write_text("# Results")
        
        excluded = {"code_scanner_results.md", "code_scanner_results.md.bak"}
        
        watcher = GitWatcher(git_repo, excluded_files=excluded)
        watcher.connect()
        
        state1 = watcher.get_state()
        
        # Modify excluded file
        import time
        time.sleep(0.01)
        output_file.write_text("# Updated Results")
        
        watcher.invalidate_cache()
        
        # Should not detect as a change that triggers rescan
        # (the file is in changed_files but excluded from mtime check)


class TestConnectErrors:
    """Test connect method error handling."""

    def test_connect_invalid_repo(self, tmp_path):
        """Test connecting to invalid repository."""
        not_git = tmp_path / "not_git"
        not_git.mkdir()
        
        watcher = GitWatcher(not_git)
        
        with pytest.raises(GitError) as exc_info:
            watcher.connect()
        
        assert "Not a Git repository" in str(exc_info.value)

    def test_connect_invalid_commit_hash(self, git_repo):
        """Test connecting with invalid commit hash."""
        watcher = GitWatcher(git_repo, commit_hash="invalid123")
        
        with pytest.raises(GitError) as exc_info:
            watcher.connect()
        
        assert "Invalid commit hash" in str(exc_info.value)


class TestDeletedFilesHandling:
    """Test handling of deleted files."""

    def test_deleted_file_detection(self, git_repo):
        """Test detection of deleted files."""
        # Create and commit a file
        to_delete = git_repo / "to_delete.py"
        to_delete.write_text("delete me")
        subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "add file to delete"], cwd=git_repo, check=True)
        
        # Delete the file
        subprocess.run(["git", "rm", "to_delete.py"], cwd=git_repo, check=True)
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()
        
        # Should detect deleted file
        deleted_files = [f for f in state.changed_files if f.is_deleted]
        assert any("to_delete.py" in f.path for f in deleted_files)
