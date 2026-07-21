"""AI Scanner thread - executes checks against code."""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import ValidationError

from .config import Config
from .ctags_index import CtagsIndex
from .file_filter import FileFilter
from .git_watcher import GitWatcher
from .issue_tracker import IssueTracker
from .base_client import BaseLLMClient, LLMClientError, ContextOverflowError, SYSTEM_PROMPT_TEMPLATE, build_user_prompt
from .models import (
    Issue, GitState, ChangedFile, CheckGroup, ScanStatus, Project,
    LLMScanResponse, LLMToolCallResponse, LLMToolResult,
    sanitize_llm_response,
)
from .output import OutputGenerator
from .utils import (
    estimate_tokens,
    read_file_content,
    is_binary_file,
    group_files_by_directory,
)
from .ai_tools import AIToolExecutor, AI_TOOLS_SCHEMA

logger = logging.getLogger(__name__)

# Hardcoded fraction of the context window filled with file content per LLM
# call. Lower = smaller batches = faster input prefill on weak GPUs (more
# calls). Not user-configurable by design.
DEFAULT_CONTEXT_USAGE_RATIO = 0.2


class Scanner:
    """AI Scanner that executes checks against code changes."""

    def __init__(
        self,
        config: Config,
        git_watcher: GitWatcher,
        llm_client: BaseLLMClient,
        issue_tracker: IssueTracker,
        output_generator: OutputGenerator,
        ctags_index: CtagsIndex,
        file_filter: Optional[FileFilter] = None,
        project: Optional[Project] = None,
    ):
        """Initialize the scanner.

        Args:
            config: Application configuration.
            git_watcher: Git watcher instance.
            llm_client: LLM client instance.
            issue_tracker: Issue tracker instance.
            output_generator: Output generator instance.
            ctags_index: Ctags index for symbol navigation.
            file_filter: Optional unified file filter for efficient filtering.
            project: Optional project reference for status tracking.
        """
        self.config = config
        self.git_watcher = git_watcher
        self.llm_client = llm_client
        self.issue_tracker = issue_tracker
        self.output_generator = output_generator
        self.ctags_index = ctags_index
        self._file_filter = file_filter
        self._project = project

        # Initialize AI tool executor for context expansion
        self._tool_executor = None

        # Threading controls
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()  # Signals to refresh file contents
        self._check_in_progress_event = threading.Event()  # Tracks when a check is in progress
        self._thread: Optional[threading.Thread] = None

        # State - restore from project if available
        if self._project and self._project.scan_info:
            self._scan_info = self._project.scan_info.copy()
        else:
            self._scan_info = {}
        
        if self._project and self._project.last_scanned_files:
            self._last_scanned_files = self._project.last_scanned_files.copy()
        else:
            self._last_scanned_files = set()
        
        if self._project and self._project.last_file_contents_hash:
            self._last_file_contents_hash = self._project.last_file_contents_hash.copy()
        else:
            self._last_file_contents_hash = {}

    @property
    def tool_executor(self) -> AIToolExecutor:
        """Lazy initialization of the tool executor.
        
        This prevents accessing llm_client.context_limit before the client
        is connected (which would raise an error).
        """
        if self._tool_executor is None:
            self._tool_executor = AIToolExecutor(
                target_directory=self.config.target_directory,
                context_limit=self.llm_client.context_limit,
                ctags_index=self.ctags_index,
            )
        return self._tool_executor

    def start(self) -> None:
        """Start the scanner thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Scanner already running")
            return

        self._stop_event.clear()
        self._refresh_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Scanner thread started")

    def stop(self) -> None:
        """Stop the scanner thread.
        
        Waits for the current check to complete before stopping.
        """
        logger.info("Stopping scanner thread...")
        self._stop_event.set()
        self._refresh_event.set()  # Wake up if waiting
        
        # Wait for current check to complete (max 30 seconds)
        if self._check_in_progress_event.is_set():
            logger.info("Waiting for current check to complete...")
            self._check_in_progress_event.wait(timeout=30)
            if self._check_in_progress_event.is_set():
                logger.warning("Timeout waiting for check to complete, forcing stop")
        
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Scanner thread stopped")

    def _update_status(self, status: ScanStatus, check_index: int = 0, 
                     total_checks: int = 0, check_query: str = "", 
                     error_message: str = "") -> None:
        """Update the project's scan status and write to output file.

        Args:
            status: The new scan status.
            check_index: Current check index (1-based) for RUNNING status.
            total_checks: Total number of checks for RUNNING status.
            check_query: Current check query for RUNNING status.
            error_message: Error message for ERROR or CONNECTION_LOST status.
        """
        if self._project is None:
            return

        self._project.scan_status = status
        self._project.current_check_index = check_index
        self._project.total_checks = total_checks
        self._project.current_check_query = check_query
        self._project.error_message = error_message

        # Update output file with current status
        self._update_output_with_status()

    def _update_output_with_status(self) -> None:
        """Update the output file with current project status."""
        if self._project is None:
            return

        # Ensure scan_status is never None - use default if not set
        # This prevents the status line from disappearing from the output file
        scan_status = self._project.scan_status
        if scan_status is None:
            scan_status = ScanStatus.INITIALIZING

        self.output_generator.write(
            self.issue_tracker,
            self._scan_info,
            scan_status,
            self._project.current_check_index,
            self._project.total_checks,
            self._project.current_check_query,
            self._project.error_message,
        )

    def _signal_refresh(self) -> None:
        """Signal the scanner to refresh file contents for the current check.
        
        ⚠️ TEST ONLY: This method is intended for unit tests to simulate
        file system change signals. Production code should rely on the
        git watcher's file system monitoring.
        
        When files change, the scanner will continue from its current position
        with refreshed file contents, rather than restarting from the beginning.
        """
        logger.info("Refresh signal received - worktree changes detected by git watcher")
        self._refresh_event.set()

    def _run_loop(self) -> None:
        """Main scanner loop."""
        logger.info("Scanner loop started")

        # Set initial status
        self._update_status(ScanStatus.INITIALIZING)

        while not self._stop_event.is_set():
            try:
                # Get current git state
                git_state = self.git_watcher.get_state(
                    project_name=self._project.project_id if self._project else None
                )

                # Wait if merge/rebase in progress
                if git_state.is_conflict_resolution_in_progress:
                    project_label = f"[{self._project.project_id}] " if self._project else ""
                    logger.info("%sWaiting for merge/rebase to complete...", project_label)
                    self._update_status(ScanStatus.WAITING_MERGE_REBASE)
                    time.sleep(self.config.git_poll_interval)
                    continue

                # Wait if no changes
                if not git_state.has_changes:
                    logger.debug("No changes detected, waiting...")
                    self._update_status(ScanStatus.WAITING_NO_CHANGES)
                    # Clear tracking since files were committed/reverted
                    self._last_scanned_files.clear()
                    self._last_file_contents_hash.clear()
                    # Wait for refresh signal or timeout
                    was_signaled = self._refresh_event.wait(timeout=self.config.git_poll_interval)
                    self._refresh_event.clear()
                    if was_signaled:
                        logger.debug("Woke up from refresh signal (no changes state)")
                    continue

                # Check if files have actually changed since last scan
                current_files = {f.path for f in git_state.changed_files if not f.is_deleted}
                logger.debug(f"Checking if files changed: git_state.has_changes={git_state.has_changes}, "
                            f"current_files={len(current_files)}")
                has_changed = self._has_files_changed(current_files, git_state)
                logger.debug(f"_has_files_changed returned: {has_changed}")
                if has_changed:
                    # Clear refresh event before scan - any signals during scan will set it again
                    self._refresh_event.clear()
                    # Run scan
                    self._run_scan(git_state)
                else:
                    # No new changes - wait for refresh signal or timeout
                    logger.debug("No new file changes since last scan, waiting...")
                    self._update_status(ScanStatus.WAITING_NO_CHANGES)
                    was_signaled = self._refresh_event.wait(timeout=self.config.git_poll_interval)
                    self._refresh_event.clear()
                    if was_signaled:
                        logger.debug("Woke up from refresh signal (no content changes state)")

            except Exception as e:
                logger.error(f"Scanner error: {e}", exc_info=True)
                self._update_status(ScanStatus.ERROR, error_message=str(e))
                time.sleep(5)  # Brief pause before retrying

        logger.info("Scanner loop ended")

    def _is_file_ignored(self, file_path: str) -> bool:
        """Check if a file should be ignored from scanning.
        
        Uses the unified FileFilter if available (which includes config ignore
        patterns AND gitignore patterns), otherwise falls back to checking
        config ignore patterns only.
        
        Args:
            file_path: Relative path to the file.
            
        Returns:
            True if the file should be ignored from scanning.
        """
        # Use unified FileFilter if available (includes gitignore + config patterns)
        if self._file_filter is not None:
            should_skip, _ = self._file_filter.should_skip(file_path)
            return should_skip
        
        # Fallback: check config ignore patterns only
        for pattern_group in self.config.check_groups:
            if not pattern_group.checks and pattern_group.matches_file(file_path):
                return True
        return False

    def _has_files_changed(self, current_files: set[str], git_state: GitState) -> bool:
        """Check if files have changed since the last scan.
        
        Args:
            current_files: Set of current file paths (non-deleted).
            git_state: Current Git state.
            
        Returns:
            True if files have changed and need rescanning.
        """
        # Note: _refresh_event being set just means git watcher detected something,
        # but we still need to check if files ACTUALLY changed content-wise
        
        # Pre-compute ignored files set once to avoid repeated pattern matching
        # This reduces O(n*m) to O(n) where n=files, m=patterns
        ignored_files = {f for f in current_files if self._is_file_ignored(f)}
        
        # Filter out ignored files from current set to match _last_scanned_files
        # (_last_scanned_files only contains non-ignored files after scan completes)
        current_non_ignored = current_files - ignored_files
        
        logger.debug(f"_has_files_changed: current_files={len(current_files)}, current_non_ignored={len(current_non_ignored)}, "
                    f"last_scanned={len(self._last_scanned_files)}")
        
        # Check for new files added (files committed/removed don't require rescan)
        added_files = current_non_ignored - self._last_scanned_files
        if added_files:
            logger.debug(f"New files added to worktree: {list(added_files)[:5]}{'...' if len(added_files) > 5 else ''}")
            return True
        
        # Log removed files but don't trigger rescan
        removed_files = self._last_scanned_files - current_non_ignored
        if removed_files:
            logger.debug(f"Files removed from worktree (committed/reverted): {list(removed_files)[:5]}{'...' if len(removed_files) > 5 else ''}")
        
        # Check if any file contents have changed by comparing hashes
        for changed_file in git_state.changed_files:
            if changed_file.is_deleted:
                continue
            
            # Skip ignored files - they don't affect scan results
            # Skip early to avoid unnecessary hash checks and file reading
            # Use pre-computed set instead of calling _is_file_ignored again
            if changed_file.path in ignored_files:
                continue
            
            file_path = self.config.target_directory / changed_file.path
            try:
                # Use helper for safe file reading (handles encoding issues)
                content = read_file_content(file_path)
                if content is None:
                    # Binary or unreadable file - check if it's new
                    if changed_file.path not in self._last_scanned_files:
                        logger.debug(f"New binary/unreadable file detected: {changed_file.path}")
                        return True
                    continue
                
                content_hash = hash(content)
                
                if changed_file.path not in self._last_file_contents_hash:
                    logger.debug(f"New file detected: {changed_file.path}")
                    return True
                if self._last_file_contents_hash[changed_file.path] != content_hash:
                    logger.debug(f"File content changed: {changed_file.path}")
                    return True
            except OSError as e:
                # File system error - file doesn't exist or can't be accessed
                # Check if it's a new file (not in _last_scanned_files)
                if changed_file.path not in self._last_scanned_files:
                    logger.debug(f"New file detected (OSError): {changed_file.path}")
                    return True
                # Existing file that can't be read - skip it (don't assume it changed)
                logger.debug(f"Cannot read existing file {changed_file.path}, skipping: {e}")
                continue
            except Exception as e:
                # Other errors (encoding, etc.) - assume it changed
                logger.debug(f"Cannot read file {changed_file.path}, assuming changed: {e}")
                return True
        
        logger.info("Refresh event processed: no actual file content changes detected")
        logger.debug(f"_has_files_changed returning False")
        return False

    def _run_scan(self, git_state: GitState) -> None:
        """Run a full scan cycle with watermark-based rescanning.

        When worktree changes during scanning, checks after the change point already
        use fresh content. Only checks before the change point need re-running.
        This loops until all checks complete on an unchanged worktree snapshot.

        Args:
            git_state: Current Git state with changed files.
        """
        # Clear file cache since we're starting a new scan with potentially changed files
        self.tool_executor.clear_file_cache()
        
        # Cache for file content to avoid redundant reads (build_check_list + post-scan)
        cached_files_content: dict[str, str] | None = None
        cached_content_hashes: dict[str, int] = {}
        
        # Set running status
        self._update_status(ScanStatus.RUNNING)
        
        # Log changed files at the start of scan cycle
        changed_file_paths = [f.path for f in git_state.changed_files if not f.is_deleted]
        logger.info(f"Starting scan with {len(changed_file_paths)} changed files")
        if changed_file_paths:
            # Log first 20 files, with indicator if there are more
            files_to_log = changed_file_paths[:20]
            logger.info(f"Changed files: {files_to_log}{'...' if len(changed_file_paths) > 20 else ''}")

        # Build flat list of (check_group, check, filtered_batches) for index-based iteration
        # This needs to be rebuilt each iteration to get fresh file content
        def build_check_list() -> list[tuple[CheckGroup, str, list[dict[str, str]]]]:
            """Build list of checks with their filtered batches using fresh file content."""
            nonlocal cached_files_content, cached_content_hashes
            
            files_content = self._get_files_content(git_state.changed_files)
            if not files_content:
                return []

            # Filter out files matching ignore patterns
            filtered_content, ignored = self._filter_ignored_files(files_content)
            if ignored:
                logger.debug(f"Ignoring {len(ignored)} file(s) matching ignore patterns")
            
            if not filtered_content:
                # No files to process – clear caches to avoid stale data
                cached_files_content = None
                cached_content_hashes = {}
                return []
            
            # Cache for reuse after scan completes (avoid redundant reads)
            cached_files_content = filtered_content
            # Pre-compute hashes to avoid duplicate hash() calls
            cached_content_hashes = {path: hash(content) for path, content in filtered_content.items()}

            # Update scan info - preserve checks_run count across iterations
            existing_checks_run = self._scan_info.get("checks_run", 0) if self._scan_info else 0
            existing_total_checks = self._scan_info.get("total_checks", 0) if self._scan_info else 0
            self._scan_info = {
                "files_scanned": list(filtered_content.keys()),
                "skipped_files": ignored,
                "checks_run": existing_checks_run,
                "total_checks": existing_total_checks,
            }

            batches = self._create_batches(filtered_content)
            
            check_list: list[tuple[CheckGroup, str, list[dict[str, str]]]] = []
            for check_group in self.config.check_groups:
                if not check_group.checks:
                    continue  # Skip ignore patterns
                
                filtered_batches = self._filter_batches_by_pattern(batches, check_group)
                if not filtered_batches:
                    continue  # No files match this pattern
                
                for check in check_group.checks:
                    check_list.append((check_group, check, filtered_batches))
            
            return check_list

        # Initial build
        check_list = build_check_list()
        if not check_list:
            logger.info("No scannable files or checks found")
            # Still update file tracking to prevent infinite loop!
            # Without this, _has_files_changed() keeps returning True
            all_changed_paths = {f.path for f in git_state.changed_files if not f.is_deleted}
            all_changed_non_ignored = {f for f in all_changed_paths if not self._is_file_ignored(f)}
            self._last_scanned_files = all_changed_non_ignored
            # Update hash tracking - read files and compute hashes
            self._last_file_contents_hash = {}
            for file_path in all_changed_non_ignored:
                try:
                    content = read_file_content(self.config.target_directory / file_path)
                    if content is not None:
                        self._last_file_contents_hash[file_path] = hash(content)
                except Exception:
                    pass  # Skip files that can't be read
            # Update status to show we're waiting for changes, not stuck
            self._update_status(ScanStatus.WAITING_NO_CHANGES)
            return

        total_checks = len(check_list)
        run_until = total_checks  # First pass: run all checks
        all_issues: list[Issue] = []
        iteration = 0

        # Initialize checks_run counter for new scan cycle
        self._scan_info["checks_run"] = 0
        
        # Store total_checks in scan_info for output
        self._scan_info["total_checks"] = total_checks

        # Update output immediately with total checks count
        self._update_status(ScanStatus.RUNNING, 0, total_checks)

        logger.info(f"Created {total_checks} check(s) to run")

        # Watermark loop: run checks until no changes occur during the run
        while run_until > 0:
            iteration += 1
            if iteration > 1:
                logger.info(f"Rescan iteration {iteration}: running checks 1-{run_until} of {total_checks}")
                # Rebuild check list with fresh file content
                check_list = build_check_list()
                if not check_list:
                    logger.info("No scannable files after refresh")
                    break
                
                # Update status for rescan
                self._update_status(ScanStatus.RUNNING, self._scan_info["checks_run"], total_checks)

            last_change_at: int | None = None

            for check_idx in range(run_until):
                if self._stop_event.is_set():
                    break

                check_group, check, filtered_batches = check_list[check_idx]
                logger.info(f"Running check {check_idx + 1}/{total_checks}: {check[:50]}...")

                # Update running status with current check info BEFORE running it
                # This ensures user sees what check is currently running
                self._update_status(ScanStatus.RUNNING, self._scan_info["checks_run"], total_checks, check)

                # Signal that a check is in progress
                self._check_in_progress_event.set()

                try:
                    # Run check against filtered batches
                    # Note: filtered_batches is already filtered by check_group.pattern in build_check_list()
                    check_batches = filtered_batches

                    check_issues = self._run_check(check, check_batches)
                    all_issues.extend(check_issues)
                    self._scan_info["checks_run"] += 1
                    
                    # Update status again after completion (incremented count)
                    self._update_status(ScanStatus.RUNNING, self._scan_info["checks_run"], total_checks, check)

                    # Immediately add new issues to tracker
                    if check_issues:
                        new_count = self.issue_tracker.add_issues(check_issues)
                        if new_count > 0:
                            logger.info(f"Added {new_count} new issue(s) to tracker")
                except ContextOverflowError as e:
                    # Context overflow despite dynamic token tracking - this indicates
                    # our token estimation or limits are miscalculated. Log as ERROR.
                    logger.error(
                        f"UNEXPECTED context overflow during check - limits may be miscalculated! "
                        f"Check: {check[:50]}, Error: {e}"
                    )
                    skipped_key = "skipped_batches_context_overflow"
                    if skipped_key not in self._scan_info:
                        self._scan_info[skipped_key] = []
                    self._scan_info[skipped_key].append({
                        "check": check[:50],
                        "batch": batch_idx + 1 if 'batch_idx' in dir() else 0,
                        "error": "limits_miscalculated",
                    })
                    continue  # Skip to next check
                except LLMClientError as e:
                    error_msg = str(e).lower()
                    # A genuine timeout means the model is too slow for this
                    # check/batch. Rather than aborting the whole scan (which
                    # discards all progress and restarts from check 1), skip
                    # this single check and move on so the scan can advance.
                    is_timeout = "timed out" in error_msg
                    # Check for any connection-related error (lost connection, connection refused, etc.)
                    is_connection_error = any(
                        phrase in error_msg for phrase in [
                            "lost connection",
                            "connection refused",
                            "connection reset",
                            "connection error",
                            "not connected",
                            "urlopen error",
                            "network",
                        ]
                    )

                    if is_timeout:
                        logger.warning(f"LLM timed out on check, skipping to next check: {check[:50]}")
                        skipped_key = "skipped_checks_timeout"
                        if skipped_key not in self._scan_info:
                            self._scan_info[skipped_key] = []
                        self._scan_info[skipped_key].append({"check": check[:50]})
                        # Count as processed so the run advances instead of stalling at 0
                        self._scan_info["checks_run"] += 1
                        self._update_status(
                            ScanStatus.RUNNING, self._scan_info["checks_run"], total_checks, check
                        )
                        continue  # Skip to next check
                    elif is_connection_error:
                        logger.warning(f"LLM connection error: {e}")
                        logger.info("Waiting for LLM connection to be restored...")
                        self._update_status(ScanStatus.CONNECTION_LOST, error_message=str(e))
                        self.llm_client.wait_for_connection(self.config.llm_retry_interval)
                        logger.info("LLM connection restored, retrying check...")
                        # Retry by not incrementing - watermark ensures check will run again
                        continue
                    else:
                        # Non-connection error (e.g., malformed response after retries)
                        # Log but continue to next check
                        logger.error(f"LLM error during check (skipping): {e}")
                        self._update_status(ScanStatus.ERROR, error_message=str(e))
                finally:
                    # Clear the check-in-progress event
                    self._check_in_progress_event.clear()

                # Track worktree changes - update watermark only if content actually changed
                if self._refresh_event.is_set():
                    self._refresh_event.clear()
                    # Verify content actually changed before triggering rescan
                    # The git watcher uses mtime which can have false positives
                    current_files = {f.path for f in git_state.changed_files if not f.is_deleted}
                    if self._has_files_changed(current_files, git_state):
                        last_change_at = check_idx
                        logger.info(f"Worktree changed at check {check_idx + 1}, will rescan checks 1-{check_idx + 1}")
                    else:
                        logger.debug(f"Refresh event received at check {check_idx + 1}, but no actual content changes detected")

            if self._stop_event.is_set():
                break

            if last_change_at is None:
                # No changes during this run - all checks are on fresh content
                logger.info(f"Scan iteration {iteration} complete with no worktree changes")
                break
            else:
                # Re-run checks 0..last_change_at (they used stale content)
                run_until = last_change_at + 1

        # Handle deleted files - resolve their issues
        deleted_files = [f.path for f in git_state.changed_files if f.is_deleted]
        for deleted_file in deleted_files:
            self.issue_tracker.resolve_issues_for_file(deleted_file)

        # Log total issues found in this scan
        logger.info(f"Scan found {len(all_issues)} total issue(s) across all checks")

        # Determine which files have actually changed content since last scan
        # Only resolve issues for files with changed content (LLM results are non-deterministic)
        # Use cached content from build_check_list to avoid redundant file reads
        files_content = cached_files_content or {}
        
        actually_changed_files: list[str] = []
        for file_path in files_content:
            # Use pre-computed hashes to avoid duplicate hash() calls
            current_hash = cached_content_hashes.get(file_path) or hash(files_content[file_path])
            previous_hash = self._last_file_contents_hash.get(file_path)
            if previous_hash is None or current_hash != previous_hash:
                # File is new or content changed - issues can be resolved if not re-reported
                actually_changed_files.append(file_path)
            # If content unchanged, don't resolve issues even if LLM didn't re-report them
        
        if actually_changed_files:
            logger.debug(f"Files with changed content: {actually_changed_files[:10]}{'...' if len(actually_changed_files) > 10 else ''}")

        # Update issue tracker with scan results - only for files that actually changed.
        # Pass current file content so resolution is content-based: an issue whose
        # code snippet is still present is kept OPEN even if the LLM didn't re-report it.
        new_count, resolved_count = self.issue_tracker.update_from_scan(
            all_issues, actually_changed_files, files_content=cached_files_content or {}
        )
        logger.info(f"Scan complete: {new_count} new issues, {resolved_count} resolved")

        # Write output
        self._update_output_with_status()
        logger.info(f"Output file updated with {self.issue_tracker.get_stats()['total']} total issues")

        # Track scanned files and their content hashes to avoid rescanning unchanged files
        # Store only non-ignored files to match what's in _last_file_contents_hash
        all_changed_paths = {f.path for f in git_state.changed_files if not f.is_deleted}
        all_changed_non_ignored = {f for f in all_changed_paths if not self._is_file_ignored(f)}
        self._last_scanned_files = all_changed_non_ignored
        
        # Use pre-computed hashes instead of recomputing
        self._last_file_contents_hash = cached_content_hashes.copy()
        
        # Save scan state to project for persistence across project switches
        if self._project is not None:
            self._project.scan_info = self._scan_info.copy()
            self._project.last_scanned_files = self._last_scanned_files.copy()
            self._project.last_file_contents_hash = self._last_file_contents_hash.copy()
            logger.debug(f"Saved scan state to project: scan_info={self._scan_info}, "
                        f"last_scanned_files={len(self._last_scanned_files)}, "
                        f"last_file_contents_hash={len(self._last_file_contents_hash)}")
        
        logger.info("Scan cycle complete. Waiting for new file changes...")
        
        # Update project's last scan time for redundant scan prevention
        if self._project and hasattr(self._project, 'last_scan_time'):
            self._project.last_scan_time = datetime.now(timezone.utc)
        
        # Notify application of scan completion for potential project switch
        if self._project and hasattr(self._project, 'scan_completed_callback'):
            self._project.scan_completed_callback()

    def _filter_batches_by_pattern(
        self,
        batches: list[dict[str, str]],
        check_group: CheckGroup,
    ) -> list[dict[str, str]]:
        """Filter batches to only include files matching the check group's pattern.

        Args:
            batches: List of file batches.
            check_group: The check group with pattern to filter by.

        Returns:
            Filtered batches with only matching files.
        """
        filtered_batches: list[dict[str, str]] = []

        for batch in batches:
            filtered_batch = {
                file_path: content
                for file_path, content in batch.items()
                if check_group.matches_file(file_path)
            }
            if filtered_batch:
                filtered_batches.append(filtered_batch)

        return filtered_batches

    def _get_files_content(
        self,
        changed_files: list[ChangedFile],
    ) -> dict[str, str]:
        """Get content of changed files.

        Uses FileFilter for unified filtering if available,
        otherwise falls back to manual scanner_files check.

        Args:
            changed_files: List of changed files.

        Returns:
            Dictionary mapping file paths to content.
        """
        files_content: dict[str, str] = {}

        for file_info in changed_files:
            if file_info.is_deleted:
                continue

            # Use unified filter if available
            if self._file_filter is not None:
                should_skip, reason = self._file_filter.should_skip(file_info.path)
                if should_skip:
                    logger.debug(f"Skipping file (reason: {reason}): {file_info.path}")
                    continue
            else:
                # Fallback: Files generated by code-scanner that should never be scanned
                scanner_files = {
                    self.config.output_file,  # code_scanner_results.md
                    f"{self.config.output_file}.bak",  # code_scanner_results.md.bak
                    self.config.log_file,     # code_scanner.log
                }
                if file_info.path in scanner_files:
                    logger.debug(f"Skipping scanner output file: {file_info.path}")
                    continue

            file_path = self.config.target_directory / file_info.path

            if is_binary_file(file_path):
                logger.debug(f"Skipping binary file: {file_info.path}")
                continue

            content = read_file_content(file_path)
            if content is not None:
                files_content[file_info.path] = content
            else:
                logger.warning(f"Could not read file: {file_info.path}")

        return files_content

    def _filter_ignored_files(
        self,
        files_content: dict[str, str],
    ) -> tuple[dict[str, str], list[str]]:
        """Filter out files matching ignore patterns.

        If FileFilter is available, this is a no-op since filtering was
        already done in _get_files_content. Otherwise, applies config
        ignore patterns (check groups with empty checks list).

        Args:
            files_content: Dictionary mapping file paths to content.

        Returns:
            Tuple of (filtered files_content, list of ignored file paths).
        """
        # If unified filter was used, files are already filtered
        if self._file_filter is not None:
            return files_content, []
        
        # Fallback: Get ignore patterns (check groups with empty checks)
        ignore_patterns = [
            group for group in self.config.check_groups
            if not group.checks  # Empty checks = ignore pattern
        ]

        if not ignore_patterns:
            return files_content, []

        filtered_content: dict[str, str] = {}
        ignored_files: list[str] = []

        for file_path, content in files_content.items():
            # Check if file matches any ignore pattern
            should_ignore = False
            for pattern_group in ignore_patterns:
                if pattern_group.matches_file(file_path):
                    should_ignore = True
                    logger.debug(f"File '{file_path}' matches ignore pattern '{pattern_group.pattern}'")
                    break

            if should_ignore:
                ignored_files.append(file_path)
            else:
                filtered_content[file_path] = content

        return filtered_content, ignored_files

    def _create_batches(
        self,
        files_content: dict[str, str],
    ) -> list[dict[str, str]]:
        """Create batches of files based on context limit.
        
        Args:
            files_content: Dictionary of file paths to content.

        Returns:
            List of batches, each a dict of file paths to content.
        """
        context_limit = self.llm_client.context_limit

        # Reserve tokens for prompt overhead, tool calling, and response.
        # Hardcoded ratio of the context window filled with file content per
        # call. A smaller value yields smaller batches, which speeds up input
        # prefill on weak GPUs at the cost of more calls.
        available_tokens = int(context_limit * DEFAULT_CONTEXT_USAGE_RATIO)
        
        # Pre-compute token counts to avoid repeated estimation
        file_tokens = {path: estimate_tokens(content) for path, content in files_content.items()}
        
        # Try all files together first
        total_tokens = sum(file_tokens.values())
        if total_tokens <= available_tokens:
            return [files_content]

        # Group by directory hierarchy
        batches: list[dict[str, str]] = []
        groups = group_files_by_directory(list(files_content.keys()))
        
        # Ensure skipped_files list exists
        if "skipped_files" not in self._scan_info:
            self._scan_info["skipped_files"] = []

        current_batch: dict[str, str] = {}
        current_tokens = 0

        for dir_path, file_paths in groups.items():
            dir_content: dict[str, str] = {}
            dir_tokens = 0

            for file_path in file_paths:
                content = files_content[file_path]
                tokens = file_tokens[file_path]

                # Skip files that alone exceed the limit
                if tokens > available_tokens:
                    logger.warning(
                        f"Skipping oversized file: {file_path} "
                        f"({tokens} tokens > {available_tokens} available)"
                    )
                    self._scan_info["skipped_files"].append(file_path)
                    continue

                dir_content[file_path] = content
                dir_tokens += tokens

            if not dir_content:
                continue

            # Check if directory group fits in current batch
            if current_tokens + dir_tokens <= available_tokens:
                current_batch.update(dir_content)
                current_tokens += dir_tokens
            else:
                # Try to fit individual files
                if dir_tokens <= available_tokens:
                    # Start new batch with this directory
                    if current_batch:
                        batches.append(current_batch)
                    current_batch = dir_content
                    current_tokens = dir_tokens
                else:
                    # Split directory into individual files
                    if current_batch:
                        batches.append(current_batch)
                    current_batch = {}
                    current_tokens = 0

                for file_path, content in dir_content.items():
                    tokens = file_tokens[file_path]
                    if current_tokens + tokens <= available_tokens:
                        current_batch[file_path] = content
                        current_tokens += tokens
                    else:
                        if current_batch:
                            batches.append(current_batch)
                        current_batch = {file_path: content}
                        current_tokens = tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    def _parse_issues_from_response(
        self,
        response: dict[str, Any],
        check_query: str,
        batch_idx: int,
    ) -> list[Issue]:
        """Parse issues from LLM response.

        Validates the response through ``LLMScanResponse`` Pydantic model,
        then checks that file paths exist before including issues.
        Issues with non-existent file paths are logged and skipped.

        Args:
            response: Raw LLM response dictionary.
            check_query: The check query that was run.
            batch_idx: Batch index for logging.

        Returns:
            List of parsed issues.
        """
        # Sanitize then validate the full response through Pydantic
        try:
            sanitized_response = sanitize_llm_response(response)
            validated = LLMScanResponse.model_validate(sanitized_response)
        except Exception as e:
            logger.warning(f"LLM response validation failed: {e}")
            return []

        parsed_issues: list[Issue] = []
        skipped_count = 0

        for llm_issue in validated.issues:
            try:
                issue = Issue.from_llm_response(
                    data=llm_issue,
                    check_query=check_query,
                    timestamp=datetime.now(timezone.utc),
                )
                
                # Validate that file path is non-empty and file exists
                if not issue.file_path or not issue.file_path.strip():
                    logger.warning(
                        f"Skipping issue with empty file path "
                        f"(LLM hallucination or malformed response)"
                    )
                    skipped_count += 1
                    continue
                
                file_path = self.config.target_directory / issue.file_path
                if not file_path.is_file():
                    logger.warning(
                        f"Skipping issue for non-existent file: {issue.file_path} "
                        f"(LLM hallucination or stale reference)"
                    )
                    skipped_count += 1
                    continue
                
                parsed_issues.append(issue)
                logger.debug(f"Parsed issue: {issue.file_path}:{issue.line_number}")
            except Exception as e:
                logger.warning(f"Failed to parse issue: {e}, data: {llm_issue}")

        if skipped_count > 0:
            logger.info(f"Skipped {skipped_count} issue(s) with invalid or non-existent file paths")
        
        return parsed_issues

    def _validate_llm_response(self, response: dict[str, Any]) -> Optional[str]:
        """Validate that an LLM response matches the expected scan-response schema.

        Uses Pydantic to validate the response.  On failure the method returns
        a structured error message that includes the concrete JSON schema the
        LLM must follow, so it can correct its mistake in a follow-up query.

        Args:
            response: Raw LLM response dictionary.

        Returns:
            ``None`` if the response is valid, otherwise an error message string
            suitable for asking the LLM to correct its output.
        """
        try:
            sanitized_response = sanitize_llm_response(response)
            LLMScanResponse.model_validate(sanitized_response)
            return None
        except ValidationError as e:
            error_count = e.error_count()
            error_lines: list[str] = []
            for err in e.errors(include_url=False):
                loc = " → ".join(str(part) for part in err["loc"]) if err["loc"] else "(root)"
                error_lines.append(f"  Field [{loc}]: {err['msg']} (type={err['type']})")
            error_summary = "\n".join(error_lines[:10])
            if error_count > 10:
                error_summary += f"\n  ... and {error_count - 10} more error(s)"
            schema_json = LLMScanResponse.model_json_schema()
            schema_str = self._format_json_schema_for_llm(schema_json)
            return (
                f"Your response failed Pydantic validation with {error_count} error(s):\n"
                f"{error_summary}\n\n"
                f"Expected JSON schema:\n"
                f"{schema_str}\n\n"
                f"Do NOT include markdown fences, explanations, or extra text. "
                f"Return ONLY the raw JSON object."
            )
        except Exception as exc:
            error_detail = str(exc)[:512]
            return (
                f"Response validation failed: {error_detail}\n\n"
                f"Please reformat your response as valid JSON matching "
                f'this exact structure:\n'
                f'{{"issues": [{{"file_path": "path/to/file", "line_number": N, '
                f'"description": "...", "suggested_fix": "...", '
                f'"code_snippet": "..."}}]}}\n\n'
                f"If you found no issues, return: {{\"issues\": []}}"
            )

    def _format_json_schema_for_llm(self, schema: dict[str, Any]) -> str:
        """Render a compact JSON-schema summary suitable for an LLM prompt.

        Flattens deeply nested ``$defs`` / ``anyOf`` / ``allOf`` where possible
        so the LLM can see the expected field names and types at a glance.
        """
        import json

        # Build a simplified schema summary
        simplified: dict[str, Any] = {
            "type": schema.get("type", "object"),
            "required": schema.get("required", []),
        }

        properties = schema.get("properties", {})
        if "issues" in properties:
            issue_props = (properties["issues"]
                           .get("items", {})
                           .get("$ref", ""))
            if issue_props and "$defs" in schema:
                ref_key = issue_props.split("/")[-1]
                def_entry = schema["$defs"].get(ref_key, {})
                simplified["properties"] = {
                    "issues": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": def_entry.get("properties", {}),
                            "required": def_entry.get("required", []),
                        },
                    }
                }
            else:
                simplified["properties"] = properties

        return json.dumps(simplified, indent=2)

    def _run_check(
        self,
        check_query: str,
        batches: list[dict[str, str]],
    ) -> list[Issue]:
        """Run a single check against all batches with AI tool support.

        The LLM can request additional context via tools. This method handles
        iterative tool calling until LLM provides a final answer.

        Args:
            check_query: The check query to run.
            batches: List of file batches.

        Returns:
            List of issues found.
        """
        all_issues: list[Issue] = []

        for batch_idx, batch in enumerate(batches):
            if self._stop_event.is_set():
                break

            logger.debug(f"Processing batch {batch_idx + 1}/{len(batches)}")

            # Run check with tool support (may involve multiple rounds)
            batch_issues = self._run_check_with_tools(
                check_query=check_query,
                batch=batch,
                batch_idx=batch_idx,
            )
            all_issues.extend(batch_issues)

            # Immediately add batch issues to tracker and update output
            if batch_issues:
                new_count = self.issue_tracker.add_issues(batch_issues)
                if new_count > 0:
                    logger.info(f"Added {new_count} new issue(s) from batch {batch_idx + 1}")

            # Update output after each batch for immediate feedback (includes status)
            self._update_output_with_status()
            logger.info(f"Output updated after batch {batch_idx + 1}/{len(batches)}")

        return all_issues

    def _run_check_with_tools(
        self,
        check_query: str,
        batch: dict[str, str],
        batch_idx: int,
    ) -> list[Issue]:
        """Run a check with iterative tool calling support.

        This method handles the conversation loop:
        1. Send initial query with tools available
        2. If LLM requests tools, execute them
        3. Send tool results back to LLM
        4. Repeat until LLM provides final answer

        Args:
            check_query: The check query to run.
            batch: File batch content.
            batch_idx: Batch index for logging.

        Returns:
            List of issues found.
        """
        # Build initial user prompt
        user_prompt = build_user_prompt(check_query, batch)
        logger.info(f"Sending query to LLM: {check_query}")
        logger.debug(f"User prompt length: {len(user_prompt)} chars")

        # Conversation history for multi-turn interactions
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE},
            {"role": "user", "content": user_prompt},
        ]

        # Dynamic token tracking to prevent context overflow
        context_limit = self.llm_client.context_limit
        context_safety_threshold = 0.85  # Stop tool calls at 85% of limit
        max_context_tokens = int(context_limit * context_safety_threshold)
        
        # Track accumulated tokens
        accumulated_tokens = (
            estimate_tokens(SYSTEM_PROMPT_TEMPLATE) + 
            estimate_tokens(user_prompt)
        )
        logger.debug(f"Initial context usage: {accumulated_tokens} tokens ({accumulated_tokens * 100 // context_limit}% of {context_limit})")

        # Calculate dynamic iteration limit based on available context
        # Estimate average tokens per tool call (conservative estimate)
        avg_tool_result_tokens = 500  # Typical tool result size
        remaining_tokens = max_context_tokens - accumulated_tokens
        estimated_possible_iterations = max(1, remaining_tokens // avg_tool_result_tokens)
        
        # Cap at 50 to prevent endless loops, but use context-based limit if smaller
        max_tool_iterations = min(estimated_possible_iterations, 50)
        MAX_MALFORMED_TOOL_RESPONSES = 3
        logger.debug(f"Max tool iterations set to {max_tool_iterations} (based on context: {estimated_possible_iterations}, capped at 50)")

        iteration = 0
        malformed_count = 0
        validation_retries = 0
        MAX_VALIDATION_RETRIES = 3

        while iteration < max_tool_iterations:
            iteration += 1

            try:
                # Query LLM with tools available
                response = self.llm_client.query(
                    system_prompt=messages[0]["content"],
                    user_prompt=messages[-1]["content"],
                    max_retries=self.config.max_llm_retries,
                    tools=AI_TOOLS_SCHEMA,
                )

                # Check if LLM wants to use tools
                if LLMToolCallResponse.is_tool_call(response):
                    # Validate tool call format through Pydantic
                    try:
                        validated_tc = LLMToolCallResponse.model_validate(response)
                    except Exception as e:
                        logger.warning(
                            f"LLM returned malformed tool call response (iteration {iteration}): {e}. "
                            "Asking LLM to correct."
                        )
                        malformed_count += 1
                        if malformed_count > MAX_MALFORMED_TOOL_RESPONSES:
                            logger.warning(
                                f"Too many malformed tool call responses ({malformed_count}). "
                                "Forcing LLM to finalize without tools."
                            )
                            messages.append({
                                "role": "user",
                                "content": (
                                    "You have repeatedly sent malformed tool call requests. "
                                    "Do NOT request any more tools. "
                                    "Provide your FINAL analysis now with issues found."
                                ),
                            })
                            break
                        messages.append({
                            "role": "user",
                            "content": (
                                f"Your previous tool call request was malformed and could not be processed: {e}\n\n"
                                "Please correct the format and try again. "
                                "Each tool call must have 'tool_name' (string) and 'arguments' (object) fields."
                            ),
                        })
                        continue

                    tool_calls = [tc.model_dump() for tc in validated_tc.tool_calls]

                    if not tool_calls:
                        logger.warning(
                            f"LLM returned empty tool_calls list (iteration {iteration}). "
                            "Asking LLM to either call tools or provide final answer."
                        )
                        messages.append({
                            "role": "user",
                            "content": (
                                "You indicated you want to use tools but provided an empty tool_calls list. "
                                "Either request valid tool calls or provide your final analysis with issues found."
                            ),
                        })
                        continue
                    tool_names = [tc["tool_name"] for tc in tool_calls]
                    logger.info(f"LLM requested {len(tool_calls)} tool call(s): {', '.join(tool_names)} (iteration {iteration})")

                    # Execute all requested tools
                    tool_results = []
                    for tool_call in tool_calls:
                        tool_name = tool_call["tool_name"]
                        arguments = tool_call["arguments"]

                        # Log tool execution with compact argument summary
                        args_summary = self._format_tool_args_for_log(tool_name, arguments)
                        logger.info(f"  → {tool_name}: {args_summary}")

                        logger.debug(f"Executing tool: {tool_name} with args: {arguments}")
                        result = self.tool_executor.execute_tool(tool_name, arguments)

                        # Validate tool result through Pydantic before sending to LLM
                        try:
                            validated_result = LLMToolResult(
                                success=result.success,
                                data=result.data,
                                error=result.error,
                                warning=result.warning,
                            )
                        except Exception as e:
                            logger.warning(f"Tool result validation failed for {tool_name}: {e}")
                            validated_result = LLMToolResult(
                                success=False,
                                data=None,
                                error=f"Tool result validation error: {e}",
                            )

                        # Format tool result for LLM
                        if validated_result.success:
                            result_msg = f"Tool {tool_name} succeeded:\n{self._format_tool_result(result)}"
                            if validated_result.warning:
                                logger.info(f"  ✓ {tool_name} completed with warning: {validated_result.warning[:100]}...")
                                result_msg = f"{validated_result.warning}\n\n{result_msg}"
                            else:
                                logger.info(f"  ✓ {tool_name} completed successfully")
                        else:
                            logger.info(f"  ✗ {tool_name} failed: {validated_result.error}")
                            result_msg = f"Tool {tool_name} failed: {validated_result.error}"

                        tool_results.append(result_msg)

                    # Add tool results to conversation
                    tool_results_message = "\n\n".join(tool_results)
                    
                    # Check if adding tool results would exceed context threshold
                    tool_results_tokens = estimate_tokens(tool_results_message)
                    new_total = accumulated_tokens + tool_results_tokens
                    context_usage_pct = new_total * 100 // context_limit
                    
                    logger.debug(f"Context usage after tools: {new_total} tokens ({context_usage_pct}% of {context_limit})")
                    
                    if new_total > max_context_tokens:
                        # Approaching context limit - ask LLM to finalize with current info
                        logger.warning(
                            f"Context approaching limit ({context_usage_pct}% used, threshold {int(context_safety_threshold * 100)}%). "
                            f"Requesting LLM to finalize analysis without further tool calls."
                        )
                        messages.append({
                            "role": "user",
                            "content": (
                                f"Tool results:\n\n{tool_results_message}\n\n"
                                "IMPORTANT: Context limit reached. Do NOT request more tools. "
                                "Provide your FINAL analysis now with issues found based on available information."
                            ),
                        })
                        break  # Exit tool calling loop
                    else:
                        messages.append({
                            "role": "user",
                            "content": f"Tool results:\n\n{tool_results_message}\n\n",
                        })

                # Check if LLM provided final answer (no more tool calls)
                if not LLMToolCallResponse.is_tool_call(response):
                    # Validate the response before accepting as final
                    validation_error = self._validate_llm_response(response)
                    if validation_error is None:
                        logger.debug(f"LLM provided valid final answer (iteration {iteration})")
                        break
                    else:
                        logger.warning(
                            f"LLM response validation failed (iteration {iteration}, "
                            f"attempt {validation_retries + 1}/{MAX_VALIDATION_RETRIES}): "
                            f"{str(validation_error)[:200]}"
                        )
                        validation_retries += 1
                        if validation_retries >= MAX_VALIDATION_RETRIES:
                            logger.warning(
                                f"Max validation retries ({MAX_VALIDATION_RETRIES}) reached, "
                                "accepting empty result"
                            )
                            response = {"issues": []}
                            break
                        last_context = messages[-1]["content"]
                        correction_prompt = (
                            f"{last_context}\n\n"
                            f"---\n\n"
                            f"{validation_error}"
                        )
                        messages.append({"role": "user", "content": correction_prompt})
                        continue

            except LLMClientError as e:
                # LLM error - log and re-raise
                logger.error(f"LLM client error: {e}")
                raise

        # Validate response is a dictionary before parsing
        if not isinstance(response, dict):
            logger.warning(
                f"Unexpected LLM response type: {type(response).__name__}. Expected dict."
            )
            return []
        
        # Parse issues from LLM response
        parsed_issues = self._parse_issues_from_response(
            response=response,
            check_query=check_query,
            batch_idx=batch_idx,
        )

        return parsed_issues

    def _format_tool_args_for_log(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Format tool arguments for compact logging.

        Args:
            tool_name: Name of the tool.
            arguments: Tool arguments.

        Returns:
            Compact string representation of arguments.
        """
        if not arguments:
            return "(no args)"
        elif tool_name == "search_text":
            patterns = arguments.get('patterns', [])
            if isinstance(patterns, list) and patterns:
                # Show first 3 patterns
                pattern_str = ', '.join(str(p) for p in patterns[:3])
                if len(patterns) > 3:
                    pattern_str += "..."
                return f"patterns: {pattern_str}"
            else:
                query = arguments.get('query', '')
                return f"query: {query[:50]}..."
        elif tool_name == "get_file":
            return f"file: {arguments.get('file_path', '')[:50]}..."
        elif tool_name == "read_file":
            file_path = arguments.get('file_path', '')
            start_line = arguments.get('start_line')
            end_line = arguments.get('end_line')
            if start_line is not None and end_line is not None:
                return f"{file_path[:50]}... (lines {start_line}-{end_line})"
            elif start_line is not None:
                return f"{file_path[:50]}... (from line {start_line})"
            else:
                return f"{file_path[:50]}..."
        else:
            return f"{len(arguments)} arg(s)"

    def _format_tool_result(self, result: Any) -> str:
        """Format tool result for logging.

        Args:
            result: Tool result.

        Returns:
            Formatted result string.
        """
        # Import ToolResult to check type
        from .ai_tools import ToolResult

        # Check if error attribute exists and is not None
        if isinstance(result, ToolResult) and result.error is not None:
            return f"{result.error}"
        elif hasattr(result, 'data') and result.data is not None:
            # Handle different data types
            if isinstance(result.data, str):
                # If it's a string, return it as-is
                return result.data
            elif isinstance(result.data, dict):
                # If it's a dict, convert to string representation
                return str(result.data)
            elif isinstance(result.data, list):
                # If it's a list, convert to string representation
                return str(result.data)
            else:
                # For other types (int, float, etc.), convert to string
                return str(result.data)
        else:
            return "no output"
