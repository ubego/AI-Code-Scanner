"""Git integration for monitoring file changes."""

import logging
from pathlib import Path
from typing import Optional

from git import Repo, InvalidGitRepositoryError, GitCommandError

from .file_filter import FileFilter
from .models import ChangedFile, FileStatus, GitState, ScanMode

logger = logging.getLogger(__name__)


class GitError(Exception):
    """Git-related error."""

    pass


class GitWatcher:
    """Monitors a Git repository for uncommitted changes."""

    def __init__(
        self,
        repo_path: Path,
        commit_hash: Optional[str] = None,
        excluded_files: Optional[set[str]] = None,
        file_filter: Optional[FileFilter] = None,
        cache_ttl: float = 1.0,
        scan_mode: ScanMode = ScanMode.UNCOMMITTED,
        base_branch: Optional[str] = None,
    ):
        """Initialize the Git watcher.

        Args:
            repo_path: Path to the Git repository.
            commit_hash: Optional commit hash to compare against.
                        If None, compares against HEAD.
            excluded_files: Optional set of file paths to exclude from change detection.
                           These files (like scanner output files) will not trigger rescans.
            file_filter: Optional unified FileFilter for all exclusion rules.
                        If provided, replaces subprocess-based gitignore checking.
            cache_ttl: Time-to-live for git status cache in seconds. Default 1.0.
            scan_mode: Operation mode. UNCOMMITTED scans working tree changes,
                      BRANCH scans all changes in the current branch.
            base_branch: Optional custom base branch name for branch mode.
                        If not set, tries 'main', 'master', then auto-detects
                        from the remote default branch.

        Raises:
            GitError: If path is not a valid Git repository.
        """
        self.repo_path = repo_path.resolve()
        self.commit_hash = commit_hash
        self.excluded_files = excluded_files or set()
        self._repo: Optional[Repo] = None
        self._last_state: Optional[GitState] = None
        self._file_filter = file_filter
        self._scan_mode = scan_mode
        self._base_branch = base_branch
        # Git status cache to reduce subprocess calls
        self._cache_ttl = cache_ttl
        self._cached_state: Optional[GitState] = None
        self._cache_time: float = 0.0
        # Branch mode: cached merge-base commit hash
        self._branch_base: Optional[str] = None
        # Merge/rebase suppression: only log once per conflict episode
        self._conflict_logged: bool = False

    def connect(self) -> None:
        """Connect to the Git repository.

        Raises:
            GitError: If repository is invalid.
        """
        try:
            self._repo = Repo(self.repo_path)
            logger.info(f"Connected to Git repository: {self.repo_path}")

            # Validate commit hash if provided
            if self.commit_hash:
                try:
                    self._repo.commit(self.commit_hash)
                    logger.info(f"Using base commit: {self.commit_hash}")
                except Exception:
                    raise GitError(f"Invalid commit hash: {self.commit_hash}")

        except InvalidGitRepositoryError:
            raise GitError(
                f"Not a Git repository: {self.repo_path}\n"
                "Please run 'git init' or choose a directory that is a Git repository."
            )

    def get_state(
        self,
        force_refresh: bool = False,
        log_conflict: bool = True,
        project_name: Optional[str] = None,
    ) -> GitState:
        """Get the current Git state with caching.

        Uses a TTL-based cache to reduce subprocess calls to git status.

        Args:
            force_refresh: If True, bypass cache and fetch fresh state.
            log_conflict: If True (default), log a warning when merge/rebase
                          is detected. Set to False to suppress logging when
                          checking non-active projects.
            project_name: Optional project identifier for contextual logging.

        Returns:
            Current GitState with changed files and merge/rebase status.

        Raises:
            GitError: If repository is not connected.
        """
        if self._repo is None:
            raise GitError("Not connected to repository")

        # Check cache (skip for merge/rebase detection which is fast)
        import time
        now = time.time()
        if not force_refresh and self._cached_state is not None:
            if (now - self._cache_time) < self._cache_ttl:
                logger.debug(f"Using cached git state (age: {now - self._cache_time:.2f}s)")
                return self._cached_state

        state = GitState()

        # Get changed files via porcelain v2 – also inspect for unmerged entries
        raw_status_output: str = ""
        try:
            raw_status_output = self._repo.git.status(
                "--porcelain=v2", "--untracked-files=all"
            )
        except GitCommandError:
            pass

        has_unmerged = self._has_unmerged_entries(raw_status_output)

        # Merge/rebase markers on disk are only trusted when
        # porcelain-v2 confirms unmerged entries are present.
        # Stale markers (leftover after abort, worktree issues) won't have
        # unmerged entries and should not block scanning.
        git_dir = Path(self._repo.git_dir)
        if has_unmerged:
            state.is_merging = (git_dir / "MERGE_HEAD").exists()
            state.is_rebasing = (
                (git_dir / "REBASE_HEAD").exists()
                or (git_dir / "rebase-merge").exists()
                or (git_dir / "rebase-apply").exists()
            )

        if state.is_conflict_resolution_in_progress:
            if log_conflict and not self._conflict_logged:
                prefix = f"[{project_name}] " if project_name else ""
                logger.info("%sMerge/rebase in progress (unmerged files detected), skipping change detection", prefix)
                self._conflict_logged = True
            return state
        else:
            self._conflict_logged = False

        state.changed_files = self._get_changed_files_from_output(raw_status_output)

        # Update cache
        self._cached_state = state
        self._cache_time = now
        logger.debug(f"Refreshed git state cache with {len(state.changed_files)} changed files")

        return state

    @staticmethod
    def _has_unmerged_entries(status_output: str) -> bool:
        """Check porcelain-v2 output for unmerged ('u') entries."""
        if not status_output:
            return False
        for line in status_output.splitlines():
            if line.startswith("u "):
                return True
        return False

    def invalidate_cache(self) -> None:
        """Invalidate the git status cache, forcing next get_state() to refresh."""
        self._cached_state = None
        self._cache_time = 0.0

    def _resolve_branch_base(self) -> Optional[str]:
        """Resolve the base branch and return the merge-base commit hash.

        Resolution order:
        1. If the current branch *is* 'main' or 'master', compare against the
           empty tree (root of history) so the entire codebase is scanned.
        2. Try configured ``base_branch`` (via CLI or config).
        3. Standard names: 'main', then 'master'.
        4. Auto-detection: queries the remote default branch.

        Returns:
            Merge-base commit hash, empty-tree SHA, or None.
        """
        if self._repo is None:
            return None

        if self._branch_base is not None:
            return self._branch_base

        try:
            current_branch = self._repo.active_branch.name
        except (TypeError, ValueError):
            current_branch = None

        if current_branch in ("main", "master"):
            logger.info("[%s] Branch mode: on '%s' branch, comparing against root of history", self.repo_path, current_branch)
            import subprocess as _sp
            try:
                result = _sp.run(
                    ["git", "hash-object", "-t", "tree", "-w", "--stdin"],
                    cwd=str(self.repo_path),
                    input="",
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    self._branch_base = result.stdout.strip()
                else:
                    self._branch_base = "4b825dc642cb6eb9a060e54bf8998a0d8475a4f9"
            except Exception:
                self._branch_base = "4b825dc642cb6eb9a060e54bf8998a0d8475a4f9"
            return self._branch_base

        candidates: list[str] = []

        if self._base_branch:
            candidates.append(self._base_branch)

        candidates.extend(("main", "master"))

        detected = self._detect_remote_default_branch()
        if detected and detected not in candidates:
            candidates.append(detected)

        for base_name in candidates:
            try:
                merge_base = self._repo.git.merge_base(base_name, "HEAD")
                if merge_base:
                    self._branch_base = merge_base.strip()
                    logger.info("[%s] Branch mode: using base branch '%s', merge-base: %s", self.repo_path, base_name, self._branch_base[:8])
                    return self._branch_base
            except GitCommandError:
                continue

        logger.warning("[%s] Branch mode enabled but could not find 'main' or 'master' branch", self.repo_path)
        return None

    def _detect_remote_default_branch(self) -> Optional[str]:
        """Detect the default branch from the remote tracking ref.

        Tries ``refs/remotes/origin/HEAD`` to determine the remote default
        branch name (e.g. 'origin/main' → 'develop').

        Returns:
            Short branch name (e.g. 'develop') or None.
        """
        if self._repo is None:
            return None

        try:
            ref = self._repo.git.symbolic_ref("refs/remotes/origin/HEAD")
            ref = ref.strip()
            # refs/remotes/origin/<name> → extract <name>
            if "/" in ref:
                return ref.rsplit("/", 1)[-1]
            return ref
        except GitCommandError:
            return None

    def _get_changed_files(self) -> list[ChangedFile]:
        """Get list of files with uncommitted changes.

        Uses git status --porcelain=v2 for robust handling of submodules and edge cases.

        Returns:
            List of ChangedFile objects.

        Raises:
            GitError: If not connected to repository.
        """
        if self._repo is None:
            raise GitError("Not connected to repository")

        try:
            status_output = self._repo.git.status("--porcelain=v2", "--untracked-files=all")
        except GitCommandError as e:
            logger.warning(f"Git command error: {e}")
            return []

        return self._get_changed_files_from_output(status_output)

    def _get_changed_files_from_output(self, status_output: str) -> list[ChangedFile]:
        """Parse changed files from porcelain-v2 output string.

        Args:
            status_output: Raw output from ``git status --porcelain=v2``.

        Returns:
            List of ChangedFile objects.
        """
        if self._repo is None:
            raise GitError("Not connected to repository")

        changed_files: list[ChangedFile] = []
        seen_paths: set[str] = set()

        for line in status_output.splitlines():
            if not line:
                continue

            parts = line.split(" ")
            entry_type = parts[0]

            path = ""
            xy = ""

            if entry_type == "1":
                xy = parts[1]
                path = " ".join(parts[8:])

            elif entry_type == "2":
                xy = parts[1]
                path_portion = " ".join(parts[9:])
                if "\t" in path_portion:
                    path = path_portion.split("\t")[1]
                else:
                    path = path_portion
                if path.startswith('"') and path.endswith('"'):
                    path = path[1:-1]

            elif entry_type == "?":
                xy = "??"
                path = " ".join(parts[1:])

            elif entry_type == "u":
                xy = parts[1]
                path = " ".join(parts[10:])

            else:
                continue

            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]

            if not path or path in seen_paths:
                continue

            full_path = self.repo_path / path
            if full_path.is_dir():
                continue

            index_status = xy[0]
            work_tree_status = xy[1]

            if index_status == "D" or work_tree_status == "D":
                status = FileStatus.DELETED
            elif xy == "??" or (index_status == "?" and work_tree_status == "?"):
                status = FileStatus.UNTRACKED
            elif index_status != "." and index_status != "?":
                status = FileStatus.STAGED
            else:
                status = FileStatus.UNSTAGED

            if not self._is_ignored(path) and path not in self.excluded_files:
                mtime_ns = None
                if status != "deleted":
                    try:
                        mtime_ns = (self.repo_path / path).stat().st_mtime_ns
                        logger.debug(f"File {path}: mtime_ns={mtime_ns}")
                    except OSError as e:
                        logger.debug(f"Could not stat file {path}: {e}")
                changed_files.append(ChangedFile(path=path, status=status, mtime_ns=mtime_ns))
                seen_paths.add(path)
            elif path in self.excluded_files:
                logger.debug(f"Skipping excluded file: {path}")

        if self.commit_hash:
            try:
                diff_output = self._repo.git.diff(
                    "--name-status", self.commit_hash, "--"
                )
                for line in diff_output.splitlines():
                    if not line:
                        continue
                    parts = line.split("\t", 1)
                    if len(parts) < 2:
                        continue
                    status_char, path = parts[0], parts[1]

                    if "\t" in path:
                        path = path.split("\t")[1]

                    if path in seen_paths:
                        continue

                    if status_char == "D":
                        status = FileStatus.DELETED
                    else:
                        status = FileStatus.STAGED

                    if not self._is_ignored(path) and path not in self.excluded_files:
                        mtime_ns = None
                        if status != "deleted":
                            try:
                                mtime_ns = (self.repo_path / path).stat().st_mtime_ns
                            except OSError:
                                pass
                        changed_files.append(ChangedFile(path=path, status=status, mtime_ns=mtime_ns))
                        seen_paths.add(path)
                    elif path in self.excluded_files:
                        logger.debug(f"Skipping excluded file in commit diff: {path}")
            except GitCommandError as e:
                logger.warning(f"Git diff error: {e}")

        if self._scan_mode == ScanMode.BRANCH:
            branch_base = self._resolve_branch_base()
            if branch_base:
                try:
                    diff_output = self._repo.git.diff(
                        "--name-status", branch_base, "--"
                    )
                    for line in diff_output.splitlines():
                        if not line:
                            continue
                        parts = line.split("\t", 1)
                        if len(parts) < 2:
                            continue
                        status_char, path = parts[0], parts[1]

                        if "\t" in path:
                            path = path.split("\t")[1]

                        if path in seen_paths:
                            continue

                        if status_char == "D":
                            status = FileStatus.DELETED
                        else:
                            status = FileStatus.STAGED

                        if not self._is_ignored(path) and path not in self.excluded_files:
                            mtime_ns = None
                            if status != "deleted":
                                try:
                                    mtime_ns = (self.repo_path / path).stat().st_mtime_ns
                                except OSError:
                                    pass
                            changed_files.append(ChangedFile(path=path, status=status, mtime_ns=mtime_ns))
                            seen_paths.add(path)
                        elif path in self.excluded_files:
                            logger.debug(f"Skipping excluded file in branch diff: {path}")
                except GitCommandError as e:
                    logger.warning(f"Git branch diff error: {e}")

        changed_files.sort(key=lambda f: f.path)
        return changed_files

    def _is_ignored(self, path: str) -> bool:
        """Check if a path is ignored by .gitignore.

        Uses FileFilter for in-memory matching if available,
        falls back to git check-ignore subprocess otherwise.

        Args:
            path: Relative path to check.

        Returns:
            True if path should be ignored.
        """
        # Use unified file filter if available (fast, in-memory)
        if self._file_filter is not None:
            return self._file_filter.is_gitignored(path)
        
        # Fallback to subprocess (slow, but accurate)
        if self._repo is None:
            return False

        try:
            # Use git check-ignore command
            self._repo.git.check_ignore(path)
            return True
        except GitCommandError:
            # Non-zero exit means not ignored
            return False

    def has_changes_since(self, last_state: Optional[GitState]) -> bool:
        """Check if there are changes since the last state.

        Compares both file paths AND modification times to detect actual changes,
        not just git status fluctuations. Excludes files in self.excluded_files
        from triggering change detection (e.g., scanner output files).

        Args:
            last_state: Previous GitState to compare against.

        Returns:
            True if there are new changes.
        """
        # Force refresh to get latest state, not cached
        current_state = self.get_state(force_refresh=True)

        if last_state is None:
            # Filter out excluded files when checking for changes
            non_excluded_files = [
                f for f in current_state.changed_files
                if f.path not in self.excluded_files
            ]
            has_changes = len(non_excluded_files) > 0
            if has_changes:
                logger.debug(
                    f"Initial state has {len(non_excluded_files)} changed files: "
                    f"{[f.path for f in non_excluded_files[:10]]}"
                    f"{'...' if len(non_excluded_files) > 10 else ''}"
                )
            return has_changes

        # Compare file lists by path, excluding scanner output files
        current_paths = {
            f.path for f in current_state.changed_files
            if f.path not in self.excluded_files
        }
        last_paths = {
            f.path for f in last_state.changed_files
            if f.path not in self.excluded_files
        }

        # If paths differ, there are definitely changes
        if current_paths != last_paths:
            added = current_paths - last_paths
            removed = last_paths - current_paths
            if added:
                logger.info(f"New changed files detected: {list(added)}")
            if removed:
                logger.info(f"Files no longer changed: {list(removed)}")
            return True

        # Paths are same - check if any file's modification time changed
        # This catches in-place edits that don't change git status paths
        # Build lookup dict for O(n) instead of O(n²) comparison
        last_mtime_map = {f.path: f.mtime_ns for f in last_state.changed_files}
        
        for changed_file in current_state.changed_files:
            # Skip excluded files (e.g., scanner output files)
            if changed_file.path in self.excluded_files:
                logger.debug(f"Skipping excluded file in mtime check: {changed_file.path}")
                continue
            if changed_file.is_deleted:
                continue
            
            file_path = self.repo_path / changed_file.path
            try:
                current_mtime_ns = file_path.stat().st_mtime_ns
            except OSError:
                # Can't stat file (doesn't exist, encoding issue, etc.) - skip it
                # This can happen with files that have special characters in names
                # or files that were deleted but git status still shows them
                logger.debug(f"Cannot stat file in mtime check: {changed_file.path}")
                continue
            
            # O(1) lookup instead of nested loop
            last_mtime = last_mtime_map.get(changed_file.path)
            if last_mtime is not None and current_mtime_ns > last_mtime:
                logger.info(f"File modified since last check: {changed_file.path}")
                return True

        logger.debug("No mtime changes detected in has_changes_since")
        return False
