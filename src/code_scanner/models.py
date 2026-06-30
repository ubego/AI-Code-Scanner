"""Data models for the code scanner."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .text_utils import normalize_whitespace as _normalize_whitespace
from .text_utils import similarity_ratio as _similarity_ratio

logger = logging.getLogger(__name__)

_MAX_STRING_LENGTH = 8192       # ~1000-2000 words; enough for descriptions/fixes, prevents LLM context bombs
_MAX_FILE_PATH_LENGTH = 1024    # Covers all real paths (most <256); POSIX PATH_MAX is 4096
_MAX_CODE_SNIPPET_LENGTH = 16384  # ~400 lines of code for issue context; 2x general string limit
_NONPRINTABLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_string(value: str, max_length: int) -> str:
    """Strip non-printable characters and enforce max length."""
    cleaned = _NONPRINTABLE_RE.sub("", value)
    if len(cleaned) > max_length:
        logger.warning("String value truncated from %d to %d chars", len(cleaned), max_length)
        return cleaned[:max_length]
    return cleaned


def sanitize_llm_response(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize an LLM response dict.

    Strips non-printable characters from all string values and enforces
    length limits to prevent the LLM from injecting malformed data.
    """
    if not isinstance(data, dict):
        return data

    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            sanitized[key] = _sanitize_string(value, _MAX_STRING_LENGTH)
        elif isinstance(value, dict):
            sanitized[key] = sanitize_llm_response(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_llm_response(item) if isinstance(item, dict) else
                _sanitize_string(item, _MAX_STRING_LENGTH) if isinstance(item, str) else
                item
                for item in value
            ]
        elif isinstance(value, (int, float, bool)) or value is None:
            sanitized[key] = value
        else:
            sanitized[key] = str(value)[:_MAX_STRING_LENGTH]
    return sanitized


def _build_known_tool_names() -> set[str]:
    """Extract known tool names from the tools schema."""
    try:
        from .tools_schema import AI_TOOLS_SCHEMA
        names: set[str] = set()
        for tool in AI_TOOLS_SCHEMA:
            if isinstance(tool, dict) and "function" in tool:
                name = tool["function"].get("name")
                if name:
                    names.add(name)
        return names
    except Exception:
        return set()


_KNOWN_TOOL_NAMES: set[str] = _build_known_tool_names()


# ---------------------------------------------------------------------------
# Pydantic models for LLM response validation
# ---------------------------------------------------------------------------

class LLMIssue(BaseModel):
    """Validates a single issue from an LLM response.

    Handles common field-name variants that LLMs produce:
      - ``file`` / ``file_path`` → ``file_path``
      - ``line_number`` / ``line`` → ``line_number``
      - ``suggested_fix`` / ``fix`` → ``suggested_fix``

    ``None`` values are coerced to safe defaults (empty string / 0).
    Non-printable characters are stripped and string lengths are capped.
    """

    file_path: str = ""
    line_number: int = Field(default=0, ge=0)
    description: str = ""
    suggested_fix: str = ""
    code_snippet: str = ""

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _resolve_aliases(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        if "file" in values and not values.get("file_path"):
            values["file_path"] = values.pop("file")

        if "line" in values and not values.get("line_number"):
            values["line_number"] = values.pop("line")

        if "fix" in values and not values.get("suggested_fix"):
            values["suggested_fix"] = values.pop("fix")

        return values

    @field_validator("file_path", "description", "suggested_fix", "code_snippet", mode="before")
    @classmethod
    def _coerce_none_to_empty_str(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    @field_validator("file_path", mode="after")
    @classmethod
    def _sanitize_file_path(cls, v: str) -> str:
        v = _sanitize_string(v, _MAX_FILE_PATH_LENGTH)
        v = v.replace("\\", "/")
        while v.startswith("/"):
            v = v[1:]
        while ".." in v:
            v = v.replace("../", "").replace("..\\", "")
        return v

    @field_validator("description", mode="after")
    @classmethod
    def _sanitize_description(cls, v: str) -> str:
        return _sanitize_string(v, _MAX_STRING_LENGTH)

    @field_validator("suggested_fix", mode="after")
    @classmethod
    def _sanitize_suggested_fix(cls, v: str) -> str:
        return _sanitize_string(v, _MAX_STRING_LENGTH)

    @field_validator("code_snippet", mode="after")
    @classmethod
    def _sanitize_code_snippet(cls, v: str) -> str:
        return _sanitize_string(v, _MAX_CODE_SNIPPET_LENGTH)

    @field_validator("line_number", mode="before")
    @classmethod
    def _coerce_line_number(cls, v: Any) -> int:
        if v is None:
            return 0
        try:
            result = int(v)
        except (ValueError, TypeError):
            return 0
        if result < 0:
            logger.warning("Negative line_number %d coerced to 0", result)
            return 0
        return result


class LLMScanResponse(BaseModel):
    """Validates the top-level LLM scan response: ``{"issues": [...]}``.

    If ``issues`` is missing or not a list the model defaults to an empty list.
    """

    issues: list[LLMIssue] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    @field_validator("issues", mode="before")
    @classmethod
    def _coerce_issues(cls, v: Any) -> list:
        if v is None:
            return []
        if not isinstance(v, list):
            logger.warning("LLM 'issues' field is not a list (type=%s), defaulting to empty", type(v).__name__)
            return []
        return v


class LLMToolCall(BaseModel):
    """Validates a single tool call from the LLM.

    Validates that the tool name is one of the known AI tools and that
    arguments are a proper dictionary.
    """

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "ignore"}

    @field_validator("tool_name", mode="after")
    @classmethod
    def _validate_tool_name(cls, v: str) -> str:
        if _KNOWN_TOOL_NAMES and v not in _KNOWN_TOOL_NAMES:
            logger.warning("Unknown tool name from LLM: '%s'. Known tools: %s", v, sorted(_KNOWN_TOOL_NAMES))
        return v

    @field_validator("arguments", mode="before")
    @classmethod
    def _coerce_arguments(cls, v: Any) -> dict[str, Any]:
        if v is None:
            return {}
        if not isinstance(v, dict):
            logger.warning("LLM tool call arguments is not a dict (type=%s), defaulting to empty", type(v).__name__)
            return {}
        return v


class LLMToolCallResponse(BaseModel):
    """Validates a tool-calling LLM response: ``{"tool_calls": [...]}``.

    Use ``is_tool_call(response_dict)`` to check before constructing.
    """

    tool_calls: list[LLMToolCall]

    model_config = {"extra": "ignore"}

    @staticmethod
    def is_tool_call(response: dict[str, Any]) -> bool:
        """Check whether a raw response dict represents a tool-call response."""
        return "tool_calls" in response

    @model_validator(mode="before")
    @classmethod
    def _sanitize_response(cls, values: Any) -> Any:
        if isinstance(values, dict):
            values = sanitize_llm_response(values)
        return values


class LLMToolResult(BaseModel):
    """Validates a tool execution result before sending back to the LLM.

    Ensures data is properly truncated and error/warning messages are
    safe for inclusion in LLM conversation context.
    """

    success: bool
    data: Any = None
    error: Optional[str] = None
    warning: Optional[str] = None

    model_config = {"extra": "ignore"}

    @field_validator("error", "warning", mode="after")
    @classmethod
    def _sanitize_message(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _sanitize_string(v, _MAX_STRING_LENGTH)

    @field_validator("data", mode="before")
    @classmethod
    def _sanitize_data(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str):
            return _sanitize_string(v, _MAX_CODE_SNIPPET_LENGTH)
        if isinstance(v, dict):
            return sanitize_llm_response(v)
        if isinstance(v, list):
            return [
                sanitize_llm_response(item) if isinstance(item, dict) else
                _sanitize_string(item, _MAX_CODE_SNIPPET_LENGTH) if isinstance(item, str) else
                item
                for item in v
            ]
        return v


class ScanMode(Enum):
    """Operation mode for code scanning."""

    UNCOMMITTED = "uncommitted"
    BRANCH = "branch"


class IssueStatus(Enum):
    """Status of a detected issue."""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class FileStatus(Enum):
    """Status of a file with uncommitted changes."""

    STAGED = "staged"
    UNSTAGED = "unstaged"
    UNTRACKED = "untracked"
    DELETED = "deleted"

    def __post_init__(self):
        """Convert string status to FileStatus enum for type safety."""
        if isinstance(self.status, str):
            # Map string values to enum values
            status_map = {
                "staged": FileStatus.STAGED,
                "unstaged": FileStatus.UNSTAGED,
                "untracked": FileStatus.UNTRACKED,
                "deleted": FileStatus.DELETED,
                "modified": FileStatus.UNSTAGED,  # Map 'modified' to 'unstaged' for backward compatibility
            }
            self.status = status_map.get(self.status, FileStatus.UNSTAGED)

    @property
    def is_deleted(self) -> bool:
        """Check if file is deleted."""
        return self.status == FileStatus.DELETED


class ScanStatus(Enum):
    """Status of the scan for a project."""

    INITIALIZING = "initializing"
    RUNNING = "running"
    WAITING_OTHER_PROJECT = "waiting_other_project"
    WAITING_NO_CHANGES = "waiting_no_changes"
    WAITING_MERGE_REBASE = "waiting_merge_rebase"
    NOT_RUNNING = "not_running"
    ERROR = "error"
    CONNECTION_LOST = "connection_lost"

    def get_display_text(self, check_index: int = 0, total_checks: int = 0,
                         check_query: str = "", error_message: str = "",
                         timestamp: Optional[datetime] = None,
                         inactive_since: Optional[datetime] = None,
                         active_since: Optional[datetime] = None) -> str:
        """Get the display text for this status.

        Args:
            check_index: Current check index (1-based) for RUNNING status.
            total_checks: Total number of checks for RUNNING status.
            check_query: Current check query for RUNNING status.
            error_message: Error message for ERROR or CONNECTION_LOST status.
            timestamp: Optional timestamp for WAITING statuses (when project became active/inactive).
            inactive_since: Optional timestamp for WAITING_OTHER_PROJECT status (when project became inactive).
            active_since: Optional timestamp for RUNNING status (when project became active).

        Returns:
            Formatted display text with icon and details.
        """
        icon = self.get_icon()

        if self == ScanStatus.RUNNING:
            if check_query:
                if active_since:
                    # Format: "January 25, 2026 at 9:05 PM" (local time, no timezone)
                    ts_str = active_since.strftime("%B %d, %Y at %I:%M %p")
                    return f"{icon} Running - Check {check_index}/{total_checks}: {check_query} (active since: {ts_str})"
                return f"{icon} Running - Check {check_index}/{total_checks}: {check_query}"
            return f"{icon} Running - Check {check_index}/{total_checks}"
        elif self == ScanStatus.WAITING_OTHER_PROJECT:
            if inactive_since:
                # Format: "January 25, 2026 at 9:05 PM" (local time, no timezone)
                ts_str = inactive_since.strftime("%B %d, %Y at %I:%M %p")
                return f"{icon} Waiting - Another project is currently being scanned (inactive since: {ts_str})"
            return f"{icon} Waiting - Another project is currently being scanned"
        elif self == ScanStatus.WAITING_NO_CHANGES:
            if timestamp:
                # Format: "January 25, 2026 at 9:05 PM" (local time, no timezone)
                ts_str = timestamp.strftime("%B %d, %Y at %I:%M %p")
                return f"{icon} Waiting - No uncommitted changes detected (since: {ts_str})"
            return f"{icon} Waiting - No uncommitted changes detected"
        elif self == ScanStatus.WAITING_MERGE_REBASE:
            return f"{icon} Waiting - Merge/rebase conflict resolution in progress"
        elif self == ScanStatus.NOT_RUNNING:
            return f"{icon} Not running"
        elif self == ScanStatus.ERROR:
            return f"{icon} Error - {error_message}"
        elif self == ScanStatus.CONNECTION_LOST:
            return f"{icon} Connection lost - Waiting for LLM server"
        elif self == ScanStatus.INITIALIZING:
            return f"{icon} Initializing"
        else:
            return f"{icon} {self.value}"

    def get_icon(self) -> str:
        """Get the icon for this status.

        Returns:
            Unicode icon character.
        """
        icons = {
            ScanStatus.INITIALIZING: "🔧",
            ScanStatus.RUNNING: "🔄",
            ScanStatus.WAITING_OTHER_PROJECT: "⏳",
            ScanStatus.WAITING_NO_CHANGES: "⏳",
            ScanStatus.WAITING_MERGE_REBASE: "⏳",
            ScanStatus.NOT_RUNNING: "⏹️",
            ScanStatus.ERROR: "❌",
            ScanStatus.CONNECTION_LOST: "🔌",
        }
        return icons.get(self, "")


@dataclass
class Issue:
    """Represents a single issue detected by the scanner."""

    file_path: str
    line_number: int
    description: str
    suggested_fix: str
    check_query: str
    timestamp: datetime
    status: IssueStatus = IssueStatus.OPEN
    code_snippet: str = ""

    def matches(self, other: "Issue", fuzzy_threshold: float = 0.8) -> bool:
        """Check if this issue matches another issue for deduplication.

        Issues match if they have the same file and similar code pattern/description.
        Line numbers are NOT used for matching as code can move.
        
        Uses fuzzy matching with Levenshtein distance for more robust comparison
        that handles minor code changes.

        Args:
            other: The other issue to compare against.
            fuzzy_threshold: Minimum similarity ratio (0.0 to 1.0) to consider a match.
                           Default 0.8 (80% similarity).

        Returns:
            True if issues match (should be deduplicated).
        """
        if self.file_path != other.file_path:
            return False

        # Normalize whitespace for comparison
        self_snippet = _normalize_whitespace(self.code_snippet)
        other_snippet = _normalize_whitespace(other.code_snippet)

        self_desc = _normalize_whitespace(self.description)
        other_desc = _normalize_whitespace(other.description)

        # Exact match first (fast path)
        if self_snippet == other_snippet or self_desc == other_desc:
            return True

        # Fuzzy match for code snippets using similarity ratio
        if self_snippet and other_snippet:
            snippet_similarity = _similarity_ratio(self_snippet, other_snippet)
            if snippet_similarity >= fuzzy_threshold:
                return True

        # Fuzzy match for descriptions
        if self_desc and other_desc:
            desc_similarity = _similarity_ratio(self_desc, other_desc)
            if desc_similarity >= fuzzy_threshold:
                return True

        return False

    @classmethod
    def from_llm_response(
        cls,
        data: "dict | LLMIssue",
        check_query: str,
        timestamp: Optional[datetime] = None,
    ) -> "Issue":
        """Create an Issue from LLM response data.

        Args:
            data: Either a raw dict from the LLM or a validated ``LLMIssue``.
                  Raw dicts are validated through ``LLMIssue`` first.
            check_query: The check query that produced this issue.
            timestamp: Optional timestamp; defaults to ``utcnow``.

        Returns:
            A new ``Issue`` instance.
        """
        if isinstance(data, LLMIssue):
            validated = data
        else:
            # Validate raw dict through Pydantic model
            validated = LLMIssue.model_validate(data)

        return cls(
            file_path=validated.file_path,
            line_number=validated.line_number,
            description=validated.description,
            suggested_fix=validated.suggested_fix,
            check_query=check_query,
            timestamp=timestamp or datetime.now(timezone.utc),
            code_snippet=validated.code_snippet,
        )


@dataclass
class ChangedFile:
    """Represents a file with uncommitted changes."""

    path: str
    status: FileStatus | str  # 'staged', 'unstaged', 'untracked', 'deleted'
    mtime_ns: Optional[int] = None  # Nanosecond-precision mtime for change detection

    def __post_init__(self):
        """Convert string status to FileStatus enum for type safety."""
        if isinstance(self.status, str):
            # Map string values to enum values
            status_map = {
                "staged": FileStatus.STAGED,
                "unstaged": FileStatus.UNSTAGED,
                "untracked": FileStatus.UNTRACKED,
                "deleted": FileStatus.DELETED,
                "modified": FileStatus.UNSTAGED,  # Map 'modified' to 'unstaged' for backward compatibility
            }
            self.status = status_map.get(self.status, FileStatus.UNSTAGED)

    @property
    def is_deleted(self) -> bool:
        """Check if file is deleted."""
        return self.status == FileStatus.DELETED


@dataclass
class GitState:
    """Current state of Git repository."""

    changed_files: list[ChangedFile] = field(default_factory=list)
    is_merging: bool = False
    is_rebasing: bool = False

    @property
    def is_conflict_resolution_in_progress(self) -> bool:
        """Check if merge/rebase conflict resolution is in progress."""
        return self.is_merging or self.is_rebasing

    @property
    def has_changes(self) -> bool:
        """Check if there are any uncommitted changes."""
        return len(self.changed_files) > 0


@dataclass
class LLMConfig:
    """Configuration for LLM backend connection.
    
    Supports both LM Studio and Ollama backends.
    The 'backend' field is required and must be explicitly set.
    """

    backend: str  # Required: "lm-studio" or "ollama"
    host: str  # Required: no default
    port: int  # Required: no default
    model: Optional[str] = None  # Required for Ollama, optional for LM Studio
    timeout: int = 120
    context_limit: Optional[int] = None  # Manual override for context window size

    # Valid backend values
    VALID_BACKENDS = ("lm-studio", "ollama")

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if self.backend not in self.VALID_BACKENDS:
            raise ValueError(
                f"Invalid backend '{self.backend}'. "
                f"Must be one of: {', '.join(self.VALID_BACKENDS)}"
            )
        
        if self.backend == "ollama" and not self.model:
            raise ValueError(
                "Ollama backend requires 'model' to be specified.\n"
                "Example: model = \"qwen3:4b\""
            )

    @property
    def base_url(self) -> str:
        """Get the base URL for LLM API."""
        if self.backend == "lm-studio":
            return f"http://{self.host}:{self.port}/v1"
        else:  # ollama
            return f"http://{self.host}:{self.port}"


@dataclass
class CheckGroup:
    """A group of checks that apply to files matching a pattern."""

    pattern: str  # Glob pattern like "*.cpp, *.h" or "*" for all files
    checks: list[str]  # List of checks to run

    def matches_file(self, file_path: str) -> bool:
        """Check if the file matches this check group's pattern.

        Supports:
        - File extension patterns: *.cpp, *.h
        - Wildcard: * matches all files
        - Directory patterns: /*dirname*/ matches files in directories containing 'dirname'

        Args:
            file_path: The file path to check.

        Returns:
            True if the file matches the pattern.
        """
        from fnmatch import fnmatch

        # Split patterns by comma and strip whitespace
        patterns = [p.strip() for p in self.pattern.split(",")]

        # Get just the filename for matching
        filename = file_path.split("/")[-1] if "/" in file_path else file_path

        # Check if any pattern matches
        for pattern in patterns:
            # Check for directory pattern: /*dirname*/
            if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 2:
                dir_pattern = pattern[1:-1]  # Remove leading and trailing /
                path_parts = file_path.replace("\\", "/").split("/")
                for part in path_parts[:-1]:  # Exclude the filename itself
                    if fnmatch(part, dir_pattern):
                        return True
            elif fnmatch(filename, pattern) or fnmatch(file_path, pattern):
                return True

        return False


@dataclass
class Project:
    """Represents a monitored project with all its components."""

    project_id: str  # Unique identifier (e.g., "my-project" or "parent/my-project")
    target_directory: Path
    config_file: Path
    config: "Config"
    git_watcher: Optional["GitWatcher"] = None
    issue_tracker: Optional["IssueTracker"] = None
    ctags_index: Optional["CtagsIndex"] = None
    output_generator: Optional["OutputGenerator"] = None
    file_filter: Optional["FileFilter"] = None

    # State tracking
    # Note: last_activity_time field removed (currently unused)
    is_active: bool = False
    last_scanned_files: set[str] = field(default_factory=set)
    last_file_contents_hash: dict[str, int] = field(default_factory=dict)
    scan_info: dict = field(default_factory=dict)  # Scan progress information (checks_run, total_checks, etc.)
    
    # Scan status tracking
    scan_status: ScanStatus = ScanStatus.INITIALIZING
    current_check_index: int = 0  # 1-based index of current check
    total_checks: int = 0  # Total number of checks in current scan
    current_check_query: str = ""  # Current check query being executed
    error_message: str = ""  # Error message for ERROR or CONNECTION_LOST status
    
    # New fields for project switching
    last_scan_time: Optional[datetime] = None  # When this project was last scanned
    last_switch_time: Optional[datetime] = None  # When scanner last switched to this project
    inactive_since: Optional[datetime] = None  # When this project became inactive

    @property
    def output_path(self) -> Path:
        """Get output file path for this project."""
        return self.target_directory / self.config.output_file
