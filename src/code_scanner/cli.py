"""CLI entry point and main application."""

import argparse
import atexit
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Optional

from . import __version__
from .config import Config, ConfigError, load_config
from .ctags_index import CtagsIndex, CtagsNotFoundError, CtagsError
from .ai_tools import RipgrepNotFoundError, verify_ripgrep
from .file_filter import FileFilter
from .git_watcher import GitWatcher, GitError
from .issue_tracker import IssueTracker
from .base_client import BaseLLMClient, LLMClientError
from .lmstudio_client import LMStudioClient
from .ollama_client import OllamaClient
from .output import OutputGenerator
from .scanner import Scanner
from .utils import setup_logging
from .project_manager import ProjectManager
from .llm_client_manager import LLMClientManager
from .models import Project, ScanStatus, ScanMode

logger = logging.getLogger(__name__)


class LockFileError(Exception):
    """Lock file related error."""

    pass


class Application:
    """Main application coordinator with multi-project support."""

    def __init__(self, projects: list[tuple[Path, Path, Optional[str]]], debug: bool = False, scan_mode: ScanMode = ScanMode.UNCOMMITTED):
        """Initialize the application.

        Args:
            projects: List of (target_directory, config_file, commit_hash) tuples.
            debug: Enable debug logging.
            scan_mode: Operation mode (uncommitted or branch).
        """
        self._project_configs = projects
        self._debug = debug
        self._scan_mode = scan_mode

        # Multi-project components
        self.project_manager = ProjectManager()
        self.llm_client_manager = LLMClientManager()

        # Single instance components (shared)
        self._stop_event = threading.Event()
        self._lock_acquired = False

        # Scanner (will be recreated for each project switch)
        self.scanner: Optional[Scanner] = None

    def run(self) -> int:
        """Run the application.

        Returns:
            Exit code (0 for success, non-zero for error).
        """
        try:
            self._setup()
            self._run_main_loop()
            return 0
        except (
            ConfigError,
            GitError,
            LLMClientError,
            LockFileError,
            CtagsNotFoundError,
            CtagsError,
            RipgrepNotFoundError,
        ) as e:
            logger.error(str(e))
            return 1
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            return 130  # Standard exit code for SIGINT
        except SystemExit as e:
            # User declined to overwrite or other sys.exit() call
            # Make sure cleanup runs
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            return 1
        finally:
            self._cleanup()

    def _setup(self) -> None:
        """Set up all projects and components."""
        # Get config directory (platform-specific)
        from .utils import get_config_dir

        config_dir = get_config_dir()

        # Print paths
        log_path = config_dir / "code_scanner.log"
        lock_path = config_dir / "code_scanner.lock"
        print(f"Log file: {log_path}")
        print(f"Lock file: {lock_path}")

        # Check and acquire lock
        self._acquire_lock()

        # Set up logging with project prefix support
        setup_logging(log_file=log_path, debug=self._debug, project_manager=self.project_manager)

        # Initialize all projects
        # First pass: detect duplicate directory names
        dir_name_counts: dict[str, list[Path]] = {}
        for target_dir, _, _ in self._project_configs:
            dir_name = target_dir.name
            if dir_name not in dir_name_counts:
                dir_name_counts[dir_name] = []
            dir_name_counts[dir_name].append(target_dir)

        # Determine which directory names need disambiguation
        duplicate_dir_names = {name for name, paths in dir_name_counts.items() if len(paths) > 1}

        existing_project_ids = set()
        for i, (target_dir, config_file, commit_hash) in enumerate(self._project_configs):
            # Generate meaningful project ID from directory name
            base_name = target_dir.name
            if base_name in duplicate_dir_names:
                # Use parent directory to disambiguate
                parent_name = target_dir.parent.name
                project_id = f"{parent_name}/{base_name}"
                # If still duplicate, add more parent levels
                if project_id in existing_project_ids:
                    grandparent_name = target_dir.parent.parent.name
                    project_id = f"{grandparent_name}/{parent_name}/{base_name}"
            else:
                project_id = base_name

            existing_project_ids.add(project_id)
            logger.info(f"Initializing project {project_id}: {target_dir}")

            # Load config
            config = load_config(
                target_directory=target_dir,
                config_file=config_file,
                commit_hash=commit_hash,
                scan_mode=self._scan_mode,
                debug=self._debug,
            )

            # Add to project manager
            project = self.project_manager.add_project(
                project_id=project_id,
                target_directory=target_dir,
                config_file=config_file,
                config=config,
            )

            # Backup existing output file for this project
            self._backup_existing_output(project.output_path)

            # Initialize components for this project
            self._initialize_project_components(project)

        # Determine initial active project
        active_project = self.project_manager.determine_active_project()
        if active_project:
            self.project_manager.switch_to_project(active_project, skip_cooldown=True)
            self._initialize_scanner(active_project)
            # Set all inactive projects to WAITING_OTHER_PROJECT
            all_projects = self.project_manager.get_all_projects()
            for project in all_projects:
                if project.project_id != active_project.project_id:
                    # Determine appropriate status based on changes
                    state = project.git_watcher.get_state(log_conflict=False, project_name=project.project_id)
                    if state.has_changes:
                        project.scan_status = ScanStatus.WAITING_OTHER_PROJECT
                        logger.info(
                            f"Setting inactive project {project.project_id} to WAITING_OTHER_PROJECT (has changes)"
                        )
                    else:
                        project.scan_status = ScanStatus.WAITING_NO_CHANGES
                        logger.info(
                            f"Setting inactive project {project.project_id} to WAITING_NO_CHANGES (no changes)"
                        )

                    if project.output_generator is not None:
                        project.output_generator.write(
                            project.issue_tracker,
                            {},
                            project.scan_status,
                            project.current_check_index,
                            project.total_checks,
                            project.current_check_query,
                            project.error_message,
                        )
                    else:
                        logger.warning(
                            f"Output generator is None for inactive project {project.project_id}"
                        )
        else:
            # No active project found, set all projects to WAITING_NO_CHANGES
            self.project_manager.set_all_projects_status(ScanStatus.WAITING_NO_CHANGES)

        # Log summary
        all_projects = self.project_manager.get_all_projects()
        logger.info(
            f"{'=' * 60}\n"
            f"Code Scanner starting\n"
            f"Monitoring {len(all_projects)} project(s)\n"
            f"Log file: {log_path}\n"
            f"Lock file: {lock_path}\n"
            f"{'=' * 60}"
        )
        for project in all_projects:
            unique_checks = {c for g in project.config.check_groups for c in g.checks}
            total_checks = len(unique_checks)
            logger.info(
                f"  {project.project_id}: {project.target_directory}\n"
                f"    Config: {project.config_file}\n"
                f"    Output: {project.output_path}\n"
                f"    Check groups: {len(project.config.check_groups)}, Total checks: {total_checks}"
            )

    def _initialize_project_components(self, project: Project) -> None:
        """Initialize components for a specific project.

        Args:
            project: The project to initialize.
        """
        config = project.config

        # Collect scanner output files to exclude from scanning and change detection
        scanner_files = {
            config.output_file,  # code_scanner_results.md
            f"{config.output_file}.bak",  # backup file
            config.log_file,  # code_scanner.log
        }

        # Collect config ignore patterns (check groups with empty checks list)
        config_ignore_patterns: list[str] = []
        for group in config.check_groups:
            if not group.checks:  # Empty checks = ignore pattern
                # Split pattern by comma to get individual patterns
                config_ignore_patterns.extend(p.strip() for p in group.pattern.split(","))

        # Create unified file filter for efficient filtering
        project.file_filter = FileFilter(
            repo_path=config.target_directory,
            scanner_files=scanner_files,
            config_ignore_patterns=config_ignore_patterns,
            load_gitignore=True,
        )

        # Create git watcher with cache TTL matching the main loop project-switch interval
        # to avoid excessive git status subprocess calls
        project.git_watcher = GitWatcher(
            config.target_directory,
            config.commit_hash,
            excluded_files=scanner_files,  # Keep for has_changes_since filtering
            file_filter=project.file_filter,  # Use for gitignore matching
            cache_ttl=5.0,  # Match the main loop interval to reduce CPU usage
            scan_mode=config.scan_mode,
        )
        project.git_watcher.connect()

        # Create issue tracker
        project.issue_tracker = IssueTracker()

        # Create output generator
        project.output_generator = OutputGenerator(project.output_path)

        # Create initial output file so user knows it's working
        project.output_generator.write(
            project.issue_tracker,
            {"status": "Scanning in progress..."},
            project.scan_status,
            project.current_check_index,
            project.total_checks,
            project.current_check_query,
            project.error_message,
        )

        # Create ctags index (will be generated when project becomes active)
        project.ctags_index = CtagsIndex(config.target_directory)

    def _initialize_scanner(self, project: Project) -> None:
        """Initialize scanner for a project.

        Args:
            project: The project to initialize scanner for.
        """
        # Switch LLM client if needed
        llm_client = self.llm_client_manager.switch_client(project.config.llm)

        # Connect if not already connected
        if llm_client and not llm_client.is_connected():
            llm_client.connect()
            logger.info(f"Connected to {llm_client.backend_name}")

        # Set context limit from config
        if llm_client:
            llm_client.set_context_limit(project.config.llm.context_limit)

        # Initialize ctags index for symbol navigation (async for faster startup)
        logger.info(f"Starting async ctags index generation for {project.project_id}...")
        project.ctags_index.generate_index_async()
        # Index will complete in background - tools will return limited results until ready

        # Verify ripgrep is installed (required for search_text tool)
        verify_ripgrep()

        # Create scanner
        self.scanner = Scanner(
            config=project.config,
            git_watcher=project.git_watcher,
            llm_client=llm_client,
            issue_tracker=project.issue_tracker,
            output_generator=project.output_generator,
            ctags_index=project.ctags_index,
            file_filter=project.file_filter,
            project=project,
        )

    def _switch_project(self, new_project: Project) -> None:
        """Switch to a different project.

        Args:
            new_project: The project to switch to.
        """
        logger.info(f"Switching to project: {new_project.project_id}")

        # Stop current scanner
        if self.scanner:
            self.scanner.stop()
            self.scanner = None

        # Switch project in project manager
        self.project_manager.switch_to_project(new_project)

        # Initialize scanner for new project
        self._initialize_scanner(new_project)

        # Start new scanner
        if self.scanner:
            self.scanner.start()

    def _check_and_switch_project(self) -> None:
        """Check if we should switch to a different project based on recent changes.

        Only switches if:
        1. New active project is different from current
        2. Cooldown period has elapsed (5 minutes)
        """
        active_project = self.project_manager.get_active_project()
        new_active_project = self.project_manager.determine_active_project()

        if new_active_project and new_active_project.project_id != active_project.project_id:
            # Check if cooldown period has elapsed
            if self.project_manager.can_switch_to_project(new_active_project.project_id):
                self._switch_project(new_active_project)
                logger.info(
                    f"Switched to project {new_active_project.project_id} after cooldown period"
                )
            else:
                logger.info(
                    f"Project {new_active_project.project_id} is more active but cooldown period not met, keeping current active project"
                )

    def _acquire_lock(self) -> None:
        """Acquire the lock file.

        Checks if the lock file exists and if the PID in it is still running.
        If the process is dead, removes the stale lock and acquires a new one.

        Raises:
            LockFileError: If another instance is already running.
        """
        from .utils import get_config_dir

        lock_path = get_config_dir() / "code_scanner.lock"

        if lock_path.exists():
            # Read PID from lock file
            try:
                pid_str = lock_path.read_text().strip()
                pid = int(pid_str)

                # Check if process is still running
                if self._is_process_running(pid):
                    raise LockFileError(
                        f"Another code-scanner instance is already running (PID: {pid}).\n"
                        f"Lock file: {lock_path}\n"
                        "Wait for it to finish or terminate it manually."
                    )
                else:
                    # Process is dead, remove stale lock
                    lock_path.unlink()
                    logger.info(f"Removed stale lock file (PID {pid} no longer running)")
            except (ValueError, IOError) as e:
                # Invalid lock file contents, remove it
                try:
                    lock_path.unlink()
                    logger.warning(f"Removed invalid lock file: {e}")
                except IOError:
                    raise LockFileError(f"Could not remove invalid lock file: {lock_path}")

        # Create lock file with PID
        try:
            with open(lock_path, "w") as f:
                f.write(f"{os.getpid()}\n")
            self._lock_acquired = True
            logger.debug(f"Acquired lock: {lock_path}")

            # Register atexit handler to ensure lock is released on any exit
            atexit.register(self._release_lock)
        except IOError as e:
            raise LockFileError(f"Could not create lock file: {e}")

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with the given PID is running.

        Args:
            pid: Process ID to check.

        Returns:
            True if the process is running, False otherwise.
        """
        try:
            # os.kill with signal 0 doesn't kill, just checks if process exists
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _release_lock(self) -> None:
        """Release the lock file."""
        if self._lock_acquired:
            from .utils import get_config_dir

            lock_path = get_config_dir() / "code_scanner.lock"
            try:
                if lock_path.exists():
                    lock_path.unlink()
                    logger.debug(f"Released lock: {lock_path}")
            except IOError as e:
                logger.warning(f"Could not remove lock file: {e}")
            self._lock_acquired = False

    def _backup_existing_output(self, output_path: Path) -> None:
        """Backup existing output file if it exists.

        Appends content to .bak file with timestamp prefix, then removes the original.
        The scanner starts fresh with an empty results file.

        Args:
            output_path: Path to the output file to backup.
        """
        if output_path.exists():
            backup_path = output_path.parent / f"{output_path.name}.bak"
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            try:
                content = output_path.read_text(encoding="utf-8")

                # Append to backup file with timestamp separator
                with open(backup_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n{'=' * 60}\n")
                    f.write(f"Backup created: {timestamp}\n")
                    f.write(f"{'=' * 60}\n\n")
                    f.write(content)

                logger.info(f"Backed up existing output to {backup_path}")

                # Remove original file
                output_path.unlink()
                logger.debug(f"Removed existing output file: {output_path}")

            except IOError as e:
                logger.warning(f"Could not backup output file: {e}")

    def _run_main_loop(self) -> None:
        """Run the main application loop."""
        # Set up signal handler for clean exit
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Start scanner (handles its own change detection - no separate git watcher needed)
        if self.scanner:
            self.scanner.start()

        # Wait for stop signal
        logger.info("Scanner running. Press Ctrl+C to stop.")
        while not self._stop_event.is_set():
            # Check if we should switch projects
            # Use longer interval (5 seconds) to avoid high CPU from frequent git status calls
            # Project switching doesn't need real-time responsiveness
            self._check_and_switch_project()
            time.sleep(5.0)

    def _signal_handler(self, signum: int, _frame: object) -> None:
        """Handle termination signals."""
        logger.info(f"Received signal {signum}, stopping...")
        self._stop_event.set()

    def _cleanup(self) -> None:
        """Clean up resources."""
        try:
            logger.info("Cleaning up...")
        except Exception:
            pass  # Logging may not be set up yet

        # Set all projects to NOT_RUNNING status
        self.project_manager.set_all_projects_status(ScanStatus.NOT_RUNNING)

        self._stop_event.set()

        if self.scanner:
            self.scanner.stop()

        self._release_lock()

        try:
            logger.info("Cleanup complete")
        except Exception:
            pass


def _clearer_error(self: argparse.ArgumentParser, message: str) -> NoReturn:
    """Replacement for ``ArgumentParser.error`` with actionable hints.

    Standard argparse prints a bare ``error: unrecognized arguments: --mode``
    which gives no hint whether the flag name is mistyped or the installed
    binary is simply stale (missing a newer flag). This handler keeps the
    concise one-line error but, for unrecognized-argument and invalid-choice
    errors, appends the list of known options and an explicit hint at the two
    likely causes (typo, or outdated install).
    """
    self.print_usage(sys.stderr)
    prog = self.prog

    msg_lower = message.lower()
    is_unrecognized = msg_lower.startswith("unrecognized arguments")
    is_invalid_choice = "invalid choice" in msg_lower

    sys.stderr.write(f"{prog}: error: {message}\n")

    if is_unrecognized or is_invalid_choice:
        # Collect the known optional flags (e.g. -c/--config, -m/--mode).
        known: list[str] = []
        for action in self._actions:  # noqa: SLF001 (argparse internals)
            for opt in action.option_strings:
                if opt in ("-h", "--help"):
                    continue
                if opt not in known:
                    known.append(opt)

        hint_lines = [
            "",
            "Known options: "
            + (", ".join(known) if known else "(none)")
            + ", and one or more project directories.",
        ]
        if is_unrecognized:
            hint_lines.append(
                "If the flag is correct, the installed code-scanner may be "
                "outdated. Reinstall from source, e.g.:\n"
                "  pipx install --force /path/to/code-scanner"
            )
        sys.stderr.write("\n".join(hint_lines) + "\n")

    self.exit(2)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="code-scanner",
        description="AI-driven code scanner for identifying issues in uncommitted changes",
    )
    # Replace the default error handler with one that gives actionable hints
    # for unrecognized-argument and invalid-choice errors.
    parser.error = _clearer_error.__get__(parser, argparse.ArgumentParser)

    parser.add_argument(
        "projects",
        nargs="+",
        type=str,
        help="Project directories and configs: <dir1> -c <config1> <dir2> -c <config2> ...",
    )

    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        action="append",  # Allow multiple -c flags
        help="Path to configuration file (can be specified multiple times)",
    )

    parser.add_argument(
        "--commit",
        type=str,
        default=None,
        action="append",  # Allow multiple --commit flags
        help="Git commit hash to compare against (can be specified multiple times)",
    )

    parser.add_argument(
        "-m",
        "--mode",
        type=str,
        default="uncommitted",
        choices=["uncommitted", "branch"],
        help="Operation mode: 'uncommitted' scans working tree changes (default), "
             "'branch' scans all changes in the current git branch",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug logging to console and log file (default: INFO level)",
    )

    return parser.parse_intermixed_args()


def parse_project_configs(args: argparse.Namespace) -> list[tuple[Path, Path, Optional[str]]]:
    """Parse project-config pairs from CLI arguments.

    Returns list of (target_directory, config_file, commit_hash) tuples.
    Supports both formats:
    1. <dir1> -c <config1> <dir2> -c <config2>
    2. <dir1> <dir2> -c <config1> -c <config2> (configs in order)

    Args:
        args: Parsed command line arguments.

    Returns:
        List of (target_directory, config_file, commit_hash) tuples.

    Raises:
        ConfigError: If validation fails.
    """
    projects = []
    args_list = args.projects

    # Separate directories and flags
    directories = []
    configs = args.config or []
    commits = args.commit or []

    # Extract directories (non-flag arguments)
    for arg in args_list:
        if not arg.startswith("-"):
            directories.append(Path(arg))

    # Validate counts
    if len(directories) == 0:
        raise ConfigError("At least one project directory must be specified")

    if len(configs) == 0:
        # Use default config locations
        configs = [d / "code_scanner_config.toml" for d in directories]
    elif len(configs) != len(directories):
        raise ConfigError(
            f"Number of configs ({len(configs)}) must match "
            f"number of directories ({len(directories)})"
        )

    # Pad commits list if needed
    while len(commits) < len(directories):
        commits.append(None)

    # Build project tuples
    for i, (directory, config) in enumerate(zip(directories, configs)):
        projects.append((directory, config, commits[i]))

    return projects


def main() -> int:
    """Main entry point.

    Returns:
        Exit code.
    """
    args = parse_args()

    try:
        projects = parse_project_configs(args)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    scan_mode = ScanMode(args.mode) if args.mode else ScanMode.UNCOMMITTED
    app = Application(projects, debug=args.debug, scan_mode=scan_mode)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
