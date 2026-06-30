"""Project manager for multi-project support."""

import threading
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, timedelta

from .models import Project, ScanStatus
from .utils import logger


class ProjectManager:
    """Manages multiple projects and handles project switching."""
    
    # Constants
    COOLDOWN_SECONDS = 300  # 5 minutes in seconds
    
    def __init__(self):
        """Initialize the project manager."""
        self._projects: dict[str, Project] = {}
        self._active_project_id: Optional[str] = None
        self._previous_active_project_id: Optional[str] = None
        self._lock = threading.RLock()
        self._last_switch_time: Optional[datetime] = None  # Global cooldown tracking

    def _update_project_status(
        self,
        project: Project,
        status: ScanStatus,
        error_message: str = "",
        inactive_since: Optional[datetime] = None,
        active_since: Optional[datetime] = None,
    ) -> None:
        """Update project status and sync to output file.
        
        DRY helper to consolidate repeated status update patterns.
        
        Args:
            project: The project to update.
            status: The new scan status.
            error_message: Optional error message.
            inactive_since: Optional timestamp for WAITING_OTHER_PROJECT status.
            active_since: Optional timestamp for RUNNING status.
        """
        project.scan_status = status
        project.error_message = error_message
        if inactive_since is not None:
            project.inactive_since = inactive_since
        
        if project.output_generator is not None:
            project.output_generator.write(
                project.issue_tracker,
                {},
                project.scan_status,
                project.current_check_index,
                project.total_checks,
                project.current_check_query,
                project.error_message,
                inactive_since=project.inactive_since,
                active_since=active_since,
            )
        else:
            logger.warning(f"Output generator is None for project {project.project_id}")
    
    def add_project(
        self,
        project_id: str,
        target_directory: Path,
        config_file: Path,
        config: "Config",
    ) -> Project:
        """Add a new project to monitor.

        Args:
            project_id: Unique identifier for the project.
            target_directory: Path to the project directory.
            config_file: Path to the configuration file.
            config: Configuration object.

        Returns:
            The created Project object.
        """
        project = Project(
            project_id=project_id,
            target_directory=target_directory,
            config_file=config_file,
            config=config,
        )
        with self._lock:
            self._projects[project_id] = project
        return project
    
    @staticmethod
    def _ns_to_datetime(ns: float) -> str:
        if ns == 0.0:
            return "N/A"
        dt = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def determine_active_project(self) -> Optional[Project]:
        """Determine which project should be active based on most recent changes.
        
        Filters out projects that were already scanned after their last file changes.

        Returns:
            The project that should be active, or None if no projects exist.
        """
        with self._lock:
            if not self._projects:
                return None

            if len(self._projects) == 1:
                logger.debug(f"Only one project, returning: {list(self._projects.keys())[0]}")
                return next(iter(self._projects.values()))

            # Get git state for all projects (suppress merge/rebase logging)
            project_activity: dict[str, float] = {}
            for project_id, project in self._projects.items():
                if project.git_watcher is None:
                    logger.debug(f"Project {project_id} has no git watcher, skipping")
                    continue

                state = project.git_watcher.get_state(log_conflict=False, project_name=project_id)
                logger.debug(f"Project {project_id}: has_changes={state.has_changes}, is_merging={state.is_merging}, is_rebasing={state.is_rebasing}, changed_files={len(state.changed_files)}")
                if state.is_conflict_resolution_in_progress:
                    logger.debug(f"Project {project_id}: in merge/rebase, skipping")
                    continue
                if state.has_changes:
                    # Find max mtime among changed files
                    max_mtime = max(
                        (f.mtime_ns for f in state.changed_files if f.mtime_ns is not None),
                        default=0.0
                    )
                    project_activity[project_id] = max_mtime
                    logger.debug(f"Project {project_id}: max_mtime={self._ns_to_datetime(max_mtime)}, changed_files_with_mtime={sum(1 for f in state.changed_files if f.mtime_ns is not None)}")

            # Find project with highest activity, filtering out already-scanned projects
            if not project_activity:
                # No changes in any project, keep current active
                logger.debug("No projects with changes, keeping current active project")
                return self.get_active_project()

            eligible_projects = []
            current_time = datetime.now(timezone.utc).timestamp()
            
            for project_id, project in self._projects.items():
                if project.git_watcher is None:
                    logger.debug(f"Project {project_id} has no git watcher, skipping")
                    continue

                state = project.git_watcher.get_state(log_conflict=False, project_name=project_id)
                if state.is_conflict_resolution_in_progress:
                    logger.debug(f"Project {project_id}: in merge/rebase, skipping")
                    continue
                if state.has_changes:
                    # Check if this project has new changes since last scan
                    if project.last_scan_time is not None:
                        # Project was scanned before, check if file changes occurred after
                        has_new_changes = any(
                            f.mtime_ns is not None and (f.mtime_ns / 1e9) > project.last_scan_time.timestamp()
                            for f in state.changed_files
                        )
                    else:
                        # Project not scanned yet, consider it eligible
                        has_new_changes = True
                    
                    if has_new_changes:
                        # Find max mtime among changed files
                        max_mtime = max(
                            (f.mtime_ns for f in state.changed_files if f.mtime_ns is not None),
                            default=0.0
                        )
                        project_activity[project_id] = max_mtime
                        eligible_projects.append(project_id)
                        logger.debug(f"Project {project_id}: max_mtime={self._ns_to_datetime(max_mtime)}, eligible (has new changes since last scan)")
                    else:
                        logger.debug(f"Project {project_id}: no changes or already scanned, not eligible")

            if not eligible_projects:
                # No eligible projects, keep current active
                logger.debug("No eligible projects with new changes, keeping current active project")
                return self.get_active_project()

            most_active_id = max(eligible_projects, key=project_activity.get)
            readable_activity = {pid: self._ns_to_datetime(ts) for pid, ts in project_activity.items()}
            logger.info(f"Project activity comparison (filtered): {readable_activity}")
            logger.info(f"Selected most active project: {most_active_id} (mtime={self._ns_to_datetime(project_activity[most_active_id])})")
            return self._projects[most_active_id]
    
    def switch_to_project(self, project: Project, skip_cooldown: bool = False) -> None:
        """Switch to a different project.

        This is non-blocking - waits for current check to complete.

        Args:
            project: The project to switch to.
            skip_cooldown: If True, don't update _last_switch_time (for initial selection).
        """
        with self._lock:
            if self._active_project_id == project.project_id:
                return  # Already active

            previous_project = self.get_active_project()
            self._previous_active_project_id = self._active_project_id
            self._active_project_id = project.project_id

            # Update global last switch time (unless skipped for initial selection)
            if not skip_cooldown:
                self._last_switch_time = datetime.now(timezone.utc)

            # Update project states
            for p in self._projects.values():
                p.is_active = (p.project_id == project.project_id)
                if p.is_active:
                    p.scan_status = ScanStatus.RUNNING

            logger.info(f"Switched to active project: {project.project_id} ({project.target_directory})")

            # Update status for previous project (if exists)
            if previous_project is not None:
                # Determine new status based on whether it still has changes
                # (e.g. if we switched away due to cooldown but it wasn't finished, 
                # or if it finished scanning)
                new_status = ScanStatus.WAITING_NO_CHANGES
                
                if previous_project.git_watcher:
                    state = previous_project.git_watcher.get_state(log_conflict=False, project_name=previous_project.project_id)
                    if state.has_changes:
                        new_status = ScanStatus.WAITING_OTHER_PROJECT
                
                logger.info(f"Setting previous project {previous_project.project_id} to {new_status.value}")
                logger.debug(f"Updating output file for previous project {previous_project.project_id}")
                self._update_project_status(
                    previous_project,
                    new_status,
                    inactive_since=datetime.now(timezone.utc),
                )

    def get_active_project(self) -> Optional[Project]:
        """Get currently active project.

        Returns:
            The active project, or None if no project is active.
        """
        with self._lock:
            return self._projects.get(self._active_project_id) if self._active_project_id else None

    def get_previous_active_project(self) -> Optional[Project]:
        """Get previously active project.

        Returns:
            The previously active project, or None if there was no previous project.
        """
        with self._lock:
            return self._projects.get(self._previous_active_project_id) if self._previous_active_project_id else None

    def get_all_projects(self) -> list[Project]:
        """Get all projects.

        Returns:
            List of all projects.
        """
        with self._lock:
            return list(self._projects.values())

    def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Get a project by its ID.

        Args:
            project_id: The project ID.

        Returns:
            The project, or None if not found.
        """
        with self._lock:
            return self._projects.get(project_id)

    def get_project_by_directory(self, directory: Path) -> Optional[Project]:
        """Get a project by its target directory.

        Args:
            directory: The target directory path.

        Returns:
            The project, or None if not found.
        """
        with self._lock:
            for project in self._projects.values():
                if project.target_directory == directory:
                    return project
            return None

    def set_all_projects_status(self, status: ScanStatus, error_message: str = "") -> None:
        """Set the scan status for all projects.

        Args:
            status: The scan status to set for all projects.
            error_message: Optional error message for ERROR or CONNECTION_LOST status.
        """
        with self._lock:
            for project in self._projects.values():
                logger.debug(f"Setting project {project.project_id} to status: {status.value}")
                self._update_project_status(project, status, error_message=error_message)
        logger.info(f"Set all projects to status: {status.value}")

    def update_project_last_scan_time(self, project_id: str, scan_time: datetime) -> None:
        """Update the last scan time for a project.

        Args:
            project_id: The project ID.
            scan_time: When the project was last scanned.
        """
        with self._lock:
            project = self._projects.get(project_id)
            if project is not None:
                project.last_scan_time = scan_time
                logger.debug(f"Updated last scan time for project {project_id}: {scan_time}")

    def can_switch_to_project(self, project_id: str) -> bool:
        """Check if switching to a project is allowed based on global cooldown.

        Args:
            project_id: The project ID to switch to.

        Returns:
            True if switching is allowed (cooldown period met), False otherwise.
        """
        with self._lock:
            if self._last_switch_time is not None:
                time_since_last_switch = (datetime.now(timezone.utc) - self._last_switch_time).total_seconds()
                return time_since_last_switch >= self.COOLDOWN_SECONDS
            return True  # No switch time recorded, allow switch
