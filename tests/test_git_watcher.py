"""Tests for Git watcher module."""

import pytest
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from code_scanner.git_watcher import GitWatcher, GitError
from code_scanner.models import FileStatus, ScanMode


class TestGitWatcher:
    """Tests for GitWatcher class."""

    def test_connect_to_valid_repo(self, git_repo: Path):
        """Test connecting to a valid Git repository."""
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        watcher.connect()

    def test_connect_to_non_repo_raises_error(self, temp_dir: Path):
        """Test that connecting to non-Git directory raises error."""
        watcher = GitWatcher(temp_dir)
        
        with pytest.raises(GitError) as exc_info:
            watcher.connect()
        
        assert "Not a Git repository" in str(exc_info.value)

    def test_get_state_no_changes(self, git_repo: Path):
        """Test getting state when there are no changes."""
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        assert not state.has_changes
        assert len(state.changed_files) == 0

    def test_get_state_with_unstaged_changes(self, git_repo: Path):
        """Test getting state with unstaged changes."""
        # Modify a file
        readme = git_repo / "README.md"
        readme.write_text("Modified content\n")
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        assert state.has_changes
        assert len(state.changed_files) == 1
        assert state.changed_files[0].path == "README.md"
        assert state.changed_files[0].status == FileStatus.UNSTAGED

    def test_get_state_with_staged_changes(self, git_repo: Path):
        """Test getting state with staged changes."""
        # Create and stage a new file
        new_file = git_repo / "new.txt"
        new_file.write_text("New file\n")
        subprocess.run(["git", "add", "new.txt"], cwd=git_repo, capture_output=True)
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        assert state.has_changes
        # The new file should show up as some kind of change (staged or untracked varies by git version)
        assert len(state.changed_files) >= 1
        assert any(f.path == "new.txt" for f in state.changed_files)

    def test_get_state_with_untracked_files(self, git_repo: Path):
        """Test getting state with untracked files."""
        # Create an untracked file
        new_file = git_repo / "untracked.txt"
        new_file.write_text("Untracked\n")
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        assert state.has_changes
        assert any(f.path == "untracked.txt" and f.status == FileStatus.UNTRACKED
                   for f in state.changed_files)

    def test_get_state_with_deleted_file(self, git_repo: Path):
        """Test getting state with deleted file."""
        # Delete the README
        readme = git_repo / "README.md"
        readme.unlink()
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        assert state.has_changes
        deleted_files = [f for f in state.changed_files if f.is_deleted]
        assert len(deleted_files) == 1

    def test_gitignore_respected(self, git_repo: Path):
        """Test that .gitignore patterns are respected."""
        # Create .gitignore
        gitignore = git_repo / ".gitignore"
        gitignore.write_text("*.log\nbuild/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add gitignore"],
            cwd=git_repo,
            capture_output=True,
        )
        
        # Create ignored files
        (git_repo / "test.log").write_text("log")
        build_dir = git_repo / "build"
        build_dir.mkdir()
        (build_dir / "output.txt").write_text("output")
        
        # Create non-ignored file
        (git_repo / "test.txt").write_text("test")
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        state = watcher.get_state()
        
        # Only test.txt should be detected
        paths = [f.path for f in state.changed_files]
        assert "test.txt" in paths
        assert "test.log" not in paths

    def test_has_changes_since(self, git_repo: Path):
        """Test detecting changes since last state."""
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        # Get initial state
        state1 = watcher.get_state()
        
        # No changes yet
        assert not watcher.has_changes_since(state1)
        
        # Make a change and stage it so git status sees it
        (git_repo / "new.txt").write_text("new")
        subprocess.run(["git", "add", "new.txt"], cwd=git_repo, capture_output=True)
        
        # Now there should be changes
        assert watcher.has_changes_since(state1)

    def test_has_changes_since_excludes_specified_files(self, git_repo: Path):
        """Test that excluded files don't trigger change detection."""
        # Create the output file (which should be excluded)
        output_file = git_repo / "code_scanner_results.md"
        output_file.write_text("initial content")
        
        # Create a normal file
        normal_file = git_repo / "code.py"
        normal_file.write_text("# code")
        
        # Create watcher with exclusion
        watcher = GitWatcher(
            git_repo,
            excluded_files={"code_scanner_results.md", "code_scanner_results.md.bak"}
        )
        watcher.connect()
        
        state1 = watcher.get_state()
        
        # Modify only the excluded file
        import time
        time.sleep(0.01)
        output_file.write_text("updated content")
        
        # Should NOT detect changes (excluded file modified)
        assert not watcher.has_changes_since(state1)
        
        # Now modify the normal file
        time.sleep(0.01)
        normal_file.write_text("# updated code")
        
        # Should detect changes (non-excluded file modified)
        assert watcher.has_changes_since(state1)

    def test_has_changes_since_mtime_detection(self, git_repo: Path):
        """Test that has_changes_since detects in-place file modifications via mtime."""
        # Create a file and stage it
        test_file = git_repo / "test.txt"
        test_file.write_text("original content")
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        # Get initial state (file is unstaged/untracked)
        state1 = watcher.get_state()
        assert len(state1.changed_files) > 0
        
        # No changes detected when state is same
        assert not watcher.has_changes_since(state1)
        
        # Wait a tiny bit and modify the file in-place
        import time
        time.sleep(0.01)  # Ensure mtime changes
        test_file.write_text("modified content")
        
        # Now mtime-based detection should see changes
        assert watcher.has_changes_since(state1)

    def test_has_changes_since_same_paths_no_mtime_change(self, git_repo: Path):
        """Test that has_changes_since returns False when paths same and mtime unchanged."""
        # Create a file
        test_file = git_repo / "test.txt"
        test_file.write_text("content")
        
        watcher = GitWatcher(git_repo)
        watcher.connect()
        
        # Get state twice without modifying file
        state1 = watcher.get_state()
        state2 = watcher.get_state()
        
        # Should not detect changes - paths same and mtime same
        assert not watcher.has_changes_since(state1)
        # state2 also works as base
        assert not watcher.has_changes_since(state2)

    def test_merge_in_progress_detected(self, git_repo: Path):
        """Test that merge in progress is detected."""
        import subprocess

        # Create a real merge conflict so git status shows unmerged entries
        (git_repo / "conflict.txt").write_text("version 1")
        subprocess.run(["git", "add", "conflict.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=git_repo, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", "merge_src"],
            cwd=git_repo, capture_output=True,
        )
        (git_repo / "conflict.txt").write_text("version 2")
        subprocess.run(["git", "add", "conflict.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "side"],
            cwd=git_repo, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "master"],
            cwd=git_repo, capture_output=True,
        )
        (git_repo / "conflict.txt").write_text("version 3")
        subprocess.run(["git", "add", "conflict.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main side"],
            cwd=git_repo, capture_output=True,
        )
        # Start merge that will conflict
        subprocess.run(
            ["git", "merge", "merge_src"],
            cwd=git_repo, capture_output=True,
        )

        watcher = GitWatcher(git_repo)
        watcher.connect()

        state = watcher.get_state()

        assert state.is_merging
        assert state.is_conflict_resolution_in_progress

    def test_rebase_in_progress_detected(self, git_repo: Path):
        """Test that rebase in progress is detected."""
        import subprocess

        # Create a real rebase conflict
        (git_repo / "rebase_file.txt").write_text("version 1")
        subprocess.run(["git", "add", "rebase_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=git_repo, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", "rebase_src"],
            cwd=git_repo, capture_output=True,
        )
        (git_repo / "rebase_file.txt").write_text("version 2")
        subprocess.run(["git", "add", "rebase_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "side"],
            cwd=git_repo, capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "master"],
            cwd=git_repo, capture_output=True,
        )
        (git_repo / "rebase_file.txt").write_text("version 3")
        subprocess.run(["git", "add", "rebase_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main side"],
            cwd=git_repo, capture_output=True,
        )
        # Start rebase that will conflict
        subprocess.run(
            ["git", "rebase", "rebase_src"],
            cwd=git_repo, capture_output=True,
        )

        watcher = GitWatcher(git_repo)
        watcher.connect()

        state = watcher.get_state()

        assert state.is_rebasing
        assert state.is_conflict_resolution_in_progress

    def test_invalid_commit_hash_raises_error(self, git_repo: Path):
        """Test that invalid commit hash raises error."""
        watcher = GitWatcher(git_repo, commit_hash="invalid_hash_12345")
        
        with pytest.raises(GitError) as exc_info:
            watcher.connect()
        
        assert "Invalid commit hash" in str(exc_info.value)


class TestGitWatcherStaleMarkers:
    """Tests that stale merge/rebase markers are ignored when no unmerged files exist."""

    def test_merge_head_without_unmerged_not_detected(self, git_repo: Path):
        """Stale MERGE_HEAD without unmerged entries should not be treated as merge."""
        (git_repo / ".git" / "MERGE_HEAD").write_text("abc123")

        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()

        assert not state.is_merging
        assert not state.is_conflict_resolution_in_progress

    def test_rebase_head_without_unmerged_not_detected(self, git_repo: Path):
        """Stale REBASE_HEAD without unmerged entries should not be treated as rebase."""
        (git_repo / ".git" / "REBASE_HEAD").write_text("abc123")

        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()

        assert not state.is_rebasing
        assert not state.is_conflict_resolution_in_progress

    def test_rebase_merge_dir_without_unmerged_not_detected(self, git_repo: Path):
        """Stale rebase-merge/ without unmerged entries should not be treated as rebase."""
        (git_repo / ".git" / "rebase-merge").mkdir()

        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()

        assert not state.is_rebasing
        assert not state.is_conflict_resolution_in_progress

    def test_rebase_apply_dir_without_unmerged_not_detected(self, git_repo: Path):
        """Stale rebase-apply/ without unmerged entries should not be treated as rebase."""
        (git_repo / ".git" / "rebase-apply").mkdir()

        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()

        assert not state.is_rebasing
        assert not state.is_conflict_resolution_in_progress

    def test_stale_markers_do_not_block_change_detection(self, git_repo: Path):
        """Stale markers should not prevent detection of real changed files."""
        (git_repo / ".git" / "MERGE_HEAD").write_text("abc123")
        (git_repo / "new_file.txt").write_text("new content")

        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()

        assert not state.is_merging
        assert state.has_changes
        paths = [f.path for f in state.changed_files]
        assert "new_file.txt" in paths


class TestGitWatcherBranchMode:
    """Tests for branch mode operation."""

    def test_branch_mode_default_is_uncommitted(self, git_repo: Path):
        """Test that default scan mode is UNCOMMITTED."""
        watcher = GitWatcher(git_repo)
        watcher.connect()
        state = watcher.get_state()
        assert not state.has_changes

    def test_branch_mode_explicit_uncommitted(self, git_repo: Path):
        """Test explicit uncommitted mode works same as default."""
        watcher = GitWatcher(git_repo, scan_mode=ScanMode.UNCOMMITTED)
        watcher.connect()
        state = watcher.get_state()
        assert not state.has_changes

    def test_branch_mode_no_main_branch(self, git_repo: Path):
        """Test branch mode when there's no main/master branch falls back gracefully."""
        import subprocess
        subprocess.run(
            ["git", "branch", "-m", "develop"], cwd=git_repo, capture_output=True
        )
        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH)
        watcher.connect()
        state = watcher.get_state()
        assert not state.has_changes

    def test_branch_mode_detects_branch_changes(self, git_repo: Path):
        """Test branch mode detects changes in the branch compared to main."""
        # Create a 'main' branch first with an initial commit
        import subprocess
        subprocess.run(
            ["git", "branch", "-m", "main"], cwd=git_repo, capture_output=True
        )

        # Make another commit on main
        (git_repo / "on_main.txt").write_text("main file")
        subprocess.run(["git", "add", "on_main.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Commit on main"],
            cwd=git_repo,
            capture_output=True,
        )

        # Create a feature branch from main and make commits
        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=git_repo, capture_output=True
        )
        (git_repo / "feature_file.txt").write_text("feature content")
        subprocess.run(["git", "add", "feature_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Feature commit"],
            cwd=git_repo,
            capture_output=True,
        )

        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH)
        watcher.connect()

        state = watcher.get_state()
        paths = [f.path for f in state.changed_files]
        assert "feature_file.txt" in paths

    def test_branch_mode_combined_with_uncommitted(self, git_repo: Path):
        """Test branch mode picks up both committed branch changes and uncommitted changes."""
        import subprocess
        subprocess.run(
            ["git", "branch", "-m", "main"], cwd=git_repo, capture_output=True
        )

        # Commit on main
        (git_repo / "main.txt").write_text("main")
        subprocess.run(["git", "add", "main.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main commit"],
            cwd=git_repo,
            capture_output=True,
        )

        # Feature branch with a commit
        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=git_repo, capture_output=True
        )
        (git_repo / "committed.txt").write_text("committed")
        subprocess.run(["git", "add", "committed.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Feature commit"],
            cwd=git_repo,
            capture_output=True,
        )

        # Add an uncommitted change
        (git_repo / "uncommitted.txt").write_text("uncommitted")

        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH)
        watcher.connect()

        state = watcher.get_state()
        paths = [f.path for f in state.changed_files]
        assert "committed.txt" in paths
        assert "uncommitted.txt" in paths

    def test_branch_mode_with_master_base(self, git_repo: Path):
        """Test branch mode works with 'master' as base when 'main' doesn't exist."""
        import subprocess
        subprocess.run(
            ["git", "branch", "-m", "master"], cwd=git_repo, capture_output=True
        )

        (git_repo / "master_file.txt").write_text("master")
        subprocess.run(["git", "add", "master_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "master commit"],
            cwd=git_repo,
            capture_output=True,
        )

        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=git_repo, capture_output=True
        )
        (git_repo / "branch_file.txt").write_text("branch")
        subprocess.run(["git", "add", "branch_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Branch commit"],
            cwd=git_repo,
            capture_output=True,
        )

        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH)
        watcher.connect()

        state = watcher.get_state()
        paths = [f.path for f in state.changed_files]
        assert "branch_file.txt" in paths
        assert "master_file.txt" not in paths  # not changed on feature branch

    def test_branch_mode_resolve_base_cached(self, git_repo: Path):
        """Test that branch base resolution is cached."""
        import subprocess
        subprocess.run(
            ["git", "branch", "-m", "main"], cwd=git_repo, capture_output=True
        )

        (git_repo / "base_file.txt").write_text("base")
        subprocess.run(["git", "add", "base_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "base commit"],
            cwd=git_repo,
            capture_output=True,
        )

        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=git_repo, capture_output=True
        )

        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH)
        watcher.connect()

        assert watcher._branch_base is None
        base1 = watcher._resolve_branch_base()
        assert base1 is not None
        base2 = watcher._resolve_branch_base()
        assert base1 == base2
        assert watcher._branch_base is not None

    def test_branch_mode_on_main_scans_entire_codebase(self, git_repo: Path):
        """Test branch mode on 'main' branch compares against root of history."""
        import subprocess

        subprocess.run(
            ["git", "branch", "-m", "main"], cwd=git_repo, capture_output=True
        )

        (git_repo / "file_a.txt").write_text("content a")
        subprocess.run(["git", "add", "file_a.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add file_a"],
            cwd=git_repo,
            capture_output=True,
        )

        (git_repo / "file_b.txt").write_text("content b")
        subprocess.run(["git", "add", "file_b.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add file_b"],
            cwd=git_repo,
            capture_output=True,
        )

        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH)
        watcher.connect()

        base = watcher._resolve_branch_base()
        assert base is not None
        assert len(base) == 40

        state = watcher.get_state()
        paths = {f.path for f in state.changed_files}
        assert "README.md" in paths
        assert "file_a.txt" in paths
        assert "file_b.txt" in paths

    def test_branch_mode_on_master_scans_entire_codebase(self, git_repo: Path):
        """Test branch mode on 'master' branch compares against root of history."""
        import subprocess

        subprocess.run(
            ["git", "branch", "-m", "master"], cwd=git_repo, capture_output=True
        )

        (git_repo / "only_file.txt").write_text("content")
        subprocess.run(["git", "add", "only_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add only_file"],
            cwd=git_repo,
            capture_output=True,
        )

        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH)
        watcher.connect()

        base = watcher._resolve_branch_base()
        assert base is not None
        assert len(base) == 40

        state = watcher.get_state()
        paths = {f.path for f in state.changed_files}
        assert "README.md" in paths
        assert "only_file.txt" in paths

    def test_branch_mode_custom_base_branch(self, git_repo: Path):
        """Test branch mode with a custom base branch name via CLI/config."""
        import subprocess

        subprocess.run(
            ["git", "branch", "-m", "develop"], cwd=git_repo, capture_output=True
        )

        (git_repo / "develop_file.txt").write_text("develop")
        subprocess.run(["git", "add", "develop_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "develop commit"],
            cwd=git_repo,
            capture_output=True,
        )

        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=git_repo, capture_output=True
        )
        (git_repo / "feature_file.txt").write_text("feature")
        subprocess.run(["git", "add", "feature_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Feature commit"],
            cwd=git_repo,
            capture_output=True,
        )

        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH, base_branch="develop")
        watcher.connect()

        state = watcher.get_state()
        paths = [f.path for f in state.changed_files]
        assert "feature_file.txt" in paths
        assert "develop_file.txt" not in paths

    def test_branch_mode_custom_base_branch_not_found(self, git_repo: Path):
        """Test that custom base branch is tried first and falls back gracefully."""
        import subprocess

        subprocess.run(
            ["git", "branch", "-m", "main"], cwd=git_repo, capture_output=True
        )

        (git_repo / "main_file.txt").write_text("main")
        subprocess.run(["git", "add", "main_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main commit"],
            cwd=git_repo,
            capture_output=True,
        )

        subprocess.run(
            ["git", "checkout", "-b", "feature"], cwd=git_repo, capture_output=True
        )
        (git_repo / "feature_file.txt").write_text("feature")
        subprocess.run(["git", "add", "feature_file.txt"], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Feature commit"],
            cwd=git_repo,
            capture_output=True,
        )

        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH, base_branch="nonexistent")
        watcher.connect()

        state = watcher.get_state()
        paths = [f.path for f in state.changed_files]
        assert "feature_file.txt" in paths

    def test_branch_mode_detect_remote_default(self, git_repo: Path):
        """Test _detect_remote_default_branch reads origin/HEAD symref."""
        watcher = GitWatcher(git_repo, scan_mode=ScanMode.BRANCH)
        watcher.connect()

        result = watcher._detect_remote_default_branch()
        assert result is None
