# Product Requirements Document: Local AI-Driven Code Scanner

## 1. Business Requirements

The primary objective of this project is to implement a software program that **scans a target source code directory** using a separate application to identify potential issues or answer specific user-defined questions.

*   **Core Value Proposition:** Provide developers with an automated, **language-agnostic** background scanner that identifies "undefined behavior," code style inconsistencies, optimization opportunities, and architectural violations (e.g., broken MVC patterns).
*   **Quality Assurance:** The codebase maintains comprehensive test coverage with 1208 unit tests ensuring reliability and maintainability.
*   **Target Scope:** The application focuses on **uncommitted changes** in a Git branch by default, ensuring immediate feedback for developer before code is finalized.
*   **Directory Scope:** The scanner can monitor **multiple directories (projects) simultaneously** with automatic project switching based on most recent changes. Each directory is scanned **recursively** (all subdirectories).
*   **Git Requirement:** The target directory **must be a Git repository**. The scanner will fail with an error if Git is not initialized.
*   **Binary File Handling:** Binary files (images, compiled objects, etc.) are **silently skipped** during scanning. The change detection system correctly tracks these files to prevent infinite rescan loops.
*   **Privacy and Efficiency:** By utilizing a **local AI model**, the application ensures that source code does not leave the local environment while providing the intelligence of a Large Language Model (LLM).
*   **MVP Philosophy:** The initial delivery will be an **MVP (Minimum Viable Product)**, focusing on core functionality without excessive configuration or customization.
*   **Cross-Platform:** The scanner must be **cross-platform**, supporting Windows, macOS, and Linux.
    *   **Configuration Directory:** Platform-specific locations are used for configuration files:
        *   Windows: `%APPDATA%\code-scanner` (Roaming AppData)
        *   macOS: `~/Library/Application Support/code-scanner`
        *   Linux/Unix: `~/.code-scanner`
    *   **Log and Lock Files:** The log file (`code_scanner.log`) and lock file (`code_scanner.lock`) are stored in the platform-specific configuration directory.
*   **Prerequisites:** The following tools must be installed:
    *   **Python 3.11 or higher** - Required runtime
    *   **Git** - For tracking file changes
    *   **Universal Ctags** - For symbol indexing and navigation
    *   **ripgrep** - For fast code search (used by AI tools)
*   **Uninteractive Daemon Mode:** The scanner is designed for **fully uninteractive daemon operation**. No interactive prompts are used—all configuration must be provided via a config file. This enables running as a system service or background process.
*   **Continuous Scanning by Default:** The scanner runs in continuous monitoring mode automatically—there is no separate "watch mode" flag. Once started, it monitors for changes and scans indefinitely until manually stopped (`Ctrl+C`).
*   **Passive Operation:** The scanner operates as a **passive background tool** that only reports issues to a log file. It does **not** modify any source files in the target directory.
*   **Success Criteria:** 
    *   Ability to accurately identify issues based on user-provided queries in a configuration file.
    *   Successful integration with local LLM servers (**LM Studio** and **Ollama**).
    *   Automated re-scanning triggered by Git changes.

---

## 2. Functional Requirements

### 2.1 Git Integration and Change Detection

*   **Default Behavior:** The scanner must monitor the target directory and identify **files with uncommitted changes**.
*   **Change Scope:** Uncommitted changes include:
    *   **Staged files** (added to index with `git add`)
    *   **Unstaged files** (modified but not staged)
    *   **Untracked files** (new files not yet added to Git)
*   **Gitignore Respect:** Files matching patterns in `.gitignore` are **excluded** from scanning, even if they appear as untracked.
*   **Deleted Files:** When a file is deleted (uncommitted deletion), scanner must **trigger resolution** of any open issues associated with that file. Resolution occurs during the **next full scan cycle** (not immediately upon detection).
*   **Whole File Analysis:** When a file is modified, the scanner analyzes the **entire file content**, not just the diff/changed lines, to ensure full context is available for AI.
*   **Specific Commit Analysis:** Users must have the option to scan changes **relative to a specific commit hash** (similar to `git reset --soft <hash>`). This allows scanning cumulative changes against a parent branch. After the initial scan, the application continues to monitor for new changes relative to that base.
*   **Untracked Files:** Untracked files are **still included** in commit-relative mode, regardless of the specified commit.
*   **Rebase/Merge Conflict Handling:** If a rebase or merge with conflict resolution is in progress, the scanner must **wait for completion** before launching new scans on that project.
    *   **Detection:** Merge/rebase state is detected by checking `git status --porcelain=v2` output for unmerged (`u`) entries, validated against on-disk marker files (`.git/MERGE_HEAD`, `.git/REBASE_HEAD`, `rebase-merge/`, `rebase-apply/`). Both conditions must be true to avoid false positives from stale marker files.
    *   **Active Project Only:** Merge/rebase detection is only performed for the **currently active project** being scanned. Non-active projects are checked for file changes only (merge/rebase state is ignored during activity comparison).
    *   **Logging:** When merge/rebase is detected, the scanner logs the event with the **project name** (directory name) for clear identification. Repeated log messages are suppressed while the conflict persists.
    *   **Multi-Project Behavior:** In multi-project mode, when the active project enters merge/rebase state, the scanner **temporarily switches to another eligible project** with uncommitted changes. The original project is revisited once the conflict is resolved (merge/rebase markers cleared).
    *   **Single-Project Behavior:** In single-project mode, the scanner polls for conflict resolution and resumes scanning automatically once the merge/rebase is complete.
*   **Monitoring Loop:** The application will run in a continuous loop, polling every **30 seconds** for new updates when idle. When files change during a scan, the scanner uses a **watermark algorithm** for efficient rescanning:
    1.  Each check is executed with the **latest file content** fetched at scan time.
    2.  If files change at check index N, checks N+1 onwards already used fresh content.
    3.  After completing the cycle, only checks 0..N (the "stale" checks) are re-run.
    4.  This repeats until a full cycle completes with no file changes.
    5.  This ensures **all checks run on a consistent worktree snapshot** without redundant work.
*   **Scan Completion Behavior:** After completing all checks in a scan cycle with no mid-scan changes, the scanner **waits for new file changes** before starting another scan. It tracks file modification times and content hashes to detect actual changes. Binary or unreadable files are tracked by path to ensure they don't trigger false rescans simply because their content wasn't hashed. This ensures a stable idle state without endless scanning loops.
*   **Scanner Output File Exclusion:** The scanner's own output files (`code_scanner_results.md` and `code_scanner_results.md.bak`) and log file (`code_scanner.log`) are **automatically excluded from change detection**. This prevents infinite rescan loops that would otherwise occur because the scanner writes to these files during each scan cycle.
*   **Startup Behavior:** If no uncommitted changes exist at startup, the application must **enter a wait state immediately** and poll for changes. It should not exit.
*   **Change Detection Integration:** File change detection via Git is integrated into the scanner's main loop with efficient caching to minimize git subprocess calls. Git status results are cached with a configurable TTL (default 5 seconds) to prevent redundant git operations and reduce CPU usage during idle periods.
*   **Change Detection Logging:** When changes are detected, the scanner logs which specific files triggered rescan. This includes:
    *   **New/removed files:** Files added to or removed from the changed files set.
    *   **Modified files:** Files whose content was modified in-place (detected via modification time).
    *   **Scan startup:** List of all changed files at the beginning of each scan cycle.

### 2.2 Query and Analysis Engine

*   **Configuration Input:** The scanner will take a **TOML configuration file** containing user-defined prompts organized into check groups. The configuration file is **read once at startup** (no hot-reload support).
*   **Config File Location:** The TOML config file is specified via **CLI argument**, or defaults to `code_scanner_config.toml` in the **target directory** if not provided.
*   **Missing Config File:** If no config file is found (not provided and not in script directory), **fail with error**.
*   **Empty Checks List:** If a config file exists but contains no checks, **fail with error**.
*   **Strict Configuration Validation:** The scanner validates configuration files strictly:
    *   **Supported Sections:** Only `[llm]` and `[[checks]]` sections are allowed. Any other top-level section (e.g., `[scan]`, `[output]`) causes an immediate error.
    *   **Supported LLM Parameters:** Only `backend`, `host`, `port`, `model`, `timeout`, `context_limit` are allowed in `[llm]`. Unknown parameters cause an error.
    *   **Supported Check Parameters:** Only `pattern` and `checks` are allowed in `[[checks]]`. Unknown parameters (e.g., `name`, `query`) cause an error.
    *   **Error Messages:** Validation errors list unsupported parameters and show supported alternatives.
*   **Check Groups Structure:** Checks are organized into **groups**, each with a file pattern and list of check items:
    *   **Pattern:** Glob pattern to match files (e.g., `"*.cpp, *.h"` for C++ files, `"*"` for all files).
    *   **Checks:** List of prompt strings to run against matching files.
*   **Legacy Support:** Simple list of strings format is still supported (converted to single group with `"*"` pattern).
*   **Ignore Patterns:** Check groups with an **empty checks list** define ignore patterns. Files matching these patterns are **excluded from all scanning**:
    *   **File patterns:** Match by extension (e.g., `*.md, *.txt, *.json`)
    *   **Directory patterns:** Match files in directories using `/*dirname*/` syntax (e.g., `/*tests*/`, `/*3rdparty*/`, `/*build*/`)
    *   Example: `[[checks]]\npattern = "*.md, *.txt, /*tests*/, /*vendor*/"\nchecks = []`
    *   Directory patterns support wildcards (e.g., `/*cmake-build-*/` matches `cmake-build-debug`, `cmake-build-release`)
    *   Files matching ignore patterns are silently skipped, reducing noise and improving performance.
    *   Useful for excluding documentation, test directories, third-party code, and build artifacts.
*   **Unified Filtering:** Ignore patterns are combined with gitignore patterns in a unified `FileFilter` component, ensuring consistent filtering throughout the scan lifecycle (change detection, file iteration, and issue resolution).
*   **Sequential Processing:** Queries must be executed **one by one** against identified code changes in an **AI scanning thread**.
*   **Pattern-Based Filtering:** For each check group, only files matching the group's pattern are included in the analysis batches.
*   **Aggregated Context:** Each query is sent to the AI with the **entire content of all matching modified files** as context, not file-by-file.
*   **Context Overflow Strategy:** If a combined content of all modified files exceeds the AI model's context window:
    1.  **Group by directory hierarchy:** Batch files from the same directory together, considering the **full directory hierarchy** (e.g., `src/utils/helpers/` first, then `src/utils/`, then `src/`).
    2.  **Deterministic Batching:** Within each directory group, files are sorted alphabetically, and groups are processed deepest-first using OS-agnostic path depth calculation. This ensures consistent LLM context across different runs and operating systems.
    3.  **File-by-file fallback:** If a directory group still exceeds the limit, process files individually.
    4.  **Skip oversized files:** If a single file exceeds the context limit, skip it and log a warning.
    5.  **Merged Results:** When a check runs across multiple batches, all issues from all batches are **merged into a single result set**.
*   **Dynamic Token Tracking:** To prevent context overflow during multi-turn tool calling:
    1.  **Batch Size:** Uses **55% of context limit** for file content, leaving 45% for system prompt, tool iterations, and response.
    2.  **Runtime Tracking:** Scanner tracks accumulated tokens during tool call iterations.
    3.  **Early Termination:** At **85% context usage**, tool calling stops and LLM is instructed to finalize with available information.
    4.  **Fallback:** If overflow still occurs despite tracking, log **ERROR** (indicates limit miscalculation) and skip the batch.
*   **Token Estimation:** Use a **simple character/word ratio** approximation to estimate token count before sending to LLM.
*   **Continuous Loop:** Once all checks in a list are completed, the scanner **restarts from the beginning** of the check list and continues indefinitely.
*   **AI Interaction:** Each query will be sent to a local AI model.
*   **Context Limit Configuration:** The AI model's context window size is configured in the TOML config:
    *   **Required Parameter:** The `context_limit` parameter is **required** in the `[llm]` section. Missing context_limit is a configuration error that causes immediate failure.
    *   **LM Studio:** The scanner queries context limit from LM Studio API for validation but uses the config value.
    *   **Ollama:** The scanner queries context limit via `/api/show` endpoint for validation.
    *   **Context Limit Validation (Ollama):** When using Ollama, if the config `context_limit` exceeds the model's actual limit (from `/api/show`), **fail with error**. If the config value is less than or equal to the model's limit, log a warning and continue with config value.
    *   **Recommended Values:** Common values are 4096 (small models), 8192 (medium), 16384 (recommended minimum), 32768 (large), 131072 (very large).
*   **AI Configuration:** Connection settings (host, port, model) must be specified in the TOML config `[llm]` section. No default ports are assumed.
*   **LM Studio Client:** Use the **Python client library for LM Studio** (OpenAI-compatible API client).
*   **Ollama Client:** Use the **native Ollama `/api/chat` endpoint** for message-based interactions with system/user role separation.
*   **Model Selection:**
    *   **LM Studio:** Use the **first/default model** available. No explicit model selection required.
    *   **Ollama:** Model specification is **required** in config (e.g., `model = "qwen3:4b"`).
*   **Client Architecture:** Both `LMStudioClient` and `OllamaClient` implement a common **abstract base class** (`BaseLLMClient`) to ensure interchangeable usage by the Scanner. Shared logic is consolidated in the base class:
    *   **Concrete Properties:** `context_limit` and `model_id` properties with connected-checks are shared.
    *   **Concrete Methods:** `strip_markdown_fences()`, `wait_for_connection(max_attempts)`, `set_context_limit()`, and `_parse_and_fix_json_response()` live in the base class — eliminating duplication.
    *   **Shared Constants:** `JSON_FIX_SYSTEM_PROMPT` used by both clients for malformed response recovery.
    *   **Streaming Accumulator:** `StreamAccumulator` wraps `StreamValidator` for shared content accumulation with validation during streaming.
    *   **`wait_for_connection` Guard:** `max_attempts` parameter (default 0 = unlimited) prevents infinite blocking when the LLM backend is permanently unavailable.
*   **Prompt Format:** Use an optimized prompt structure that is well-understood by LLMs (system prompt with instructions, user prompt with code context).
*   **Response Format:** The scanner must request a **structured JSON response** from the LLM with a fixed schema.
*   **Strict Prompt Instructions:** The system prompt must explicitly forbid markdown code fences, explanations, and any text outside of JSON object.
*   **Markdown Fence Stripping:** If the LLM wraps JSON in markdown fences (` ```json ... ``` `), the scanner must **automatically strip them** before parsing.
*   **JSON Enforcement:** Use API parameter `response_format={ "type": "json_object" }` to guarantee valid JSON output.
*   **Response is an **array of issues** (multiple issues per query are supported).
*   Each issue contains: file, line number, description, suggested fix.
*   **No issues found:** Return an empty array `[]`.
*   **File Path Validation:** Issues with empty file paths or file paths that don't exist in the target directory are silently discarded. Before that, file paths are sanitized: directory traversal (`..`) removed, backslashes normalized to forward slashes, leading slashes stripped, null bytes removed, and paths capped at 1024 chars. Field aliases (`file` → `file_path`, `line` → `line_number`, `fix` → `suggested_fix`) are resolved for common LLM naming variants. Negative line numbers are coerced to 0.
*   **Reasoning Effort:** The scanner must set **`reasoning_effort = "high"`** in API requests to maximize analysis quality.
*   **Malformed Response Handling:** If the LLM returns invalid JSON or doesn't follow the schema:
    *   **Reformat Request:** First, ask the LLM to **reformat its own response** into valid JSON. Uses a shared `JSON_FIX_SYSTEM_PROMPT` to request extraction without markdown fences.
    *   **Retry on failure:** If reformatting fails, retry the original query (no delay/backoff).
    *   **Maximum 3 retries** before skipping the query and logging an error.
    *   Log all retry attempts with attempt count (e.g., "attempt 1/3") to system log.
    *   Common causes: model timeout, context overflow, or model returning explanation text instead of JSON.
*   **Streaming Response Validation:** Both LLM backends use **streaming API requests** with real-time chunk validation:
    *   **Runaway Generation Detection:** A `StreamValidator` monitors streaming chunks as they arrive from the LLM backend. If the LLM generates the same content repeatedly (content loop), stalls on empty chunks, or exceeds the **512KB response size cap**, generation is aborted and the query is retried.
    *   **Repeat Pattern Detection:** Detects when the last 200-character window repeats 10 consecutive times — indicating the LLM is stuck in a generation loop.
    *   **Stalled Stream Detection:** 20 consecutive empty chunks trigger abort — catches models that connect but never produce output.
    *   **Response Size Limit:** Hard cap at 512KB prevents memory exhaustion from excessively large LLM responses.
    *   Both protections count toward the `max_retries` limit (default 3).
*   **Response Content Sanitization:** All LLM responses are sanitized before processing:
    *   **Non-printable Character Stripping:** Null bytes and other control characters (`\\x00-\\x08`, `\\x0b`, `\\x0c`, `\\x0e-\\x1f`) are removed from all string values recursively through the response dict.
    *   **String Length Limits:** Descriptions capped at 8192 chars, file paths at 1024 chars, code snippets at 16384 chars.
    *   **File Path Normalization:** Backslashes converted to forward slashes, leading slashes stripped, `..` directory traversal patterns removed, null bytes stripped.
    *   **Sanitization Pipeline:** `sanitize_llm_response()` → Pydantic `model_validate()` → business logic validators — defense in depth.
*   **Tool Call Response Validation:** Tool call responses from the LLM are validated through Pydantic models:
    *   **Schema Validation:** `LLMToolCallResponse.model_validate()` ensures each tool call has a valid `tool_name` (string) and `arguments` (dict). Non-dict arguments are coerced to `{}`; missing `tool_name` raises validation error.
    *   **Malformed Tool Call Correction:** If the LLM sends a malformed tool call (invalid structure, empty `tool_calls` list, missing required fields), the malformed response is **NOT included in the conversation context**. Instead, a correction message is appended asking the LLM to fix the format and retry.
    *   **Empty Tool Call Guard:** If `tool_calls` list is present but empty, the LLM is instructed to either call valid tools or provide a final answer — preventing silent infinite loops.
    *   **Malformed Retry Limit:** After **3 consecutive malformed tool call responses**, tool calling is forcibly terminated and the LLM is instructed to provide its final analysis without further tool calls.
    *   **Tool Result Validation:** Before tool results are sent back to the LLM, they are validated through `LLMToolResult` which sanitizes error messages, warning strings, and data payloads (non-printable chars stripped, length limits enforced).
*   **LM Studio Connection Handling:**
    *   **Startup Failure:** If the LLM backend (LM Studio or Ollama) is not running or unreachable at startup, **fail immediately** with a clear error message.
    *   **Mid-Session Failure:** If the LLM backend becomes unavailable during scanning, **pause and retry every 10 seconds** until connection is restored. The scanner handles various connection-related errors including:
        *   Lost connection
        *   Connection refused
        *   Connection reset
        *   Network errors
        *   Timeout errors
    *   **Non-Connection Errors:** Other LLM errors (e.g., malformed JSON after retries) are logged and the scanner continues to the next check.

### 2.3 Output and Reporting

*   **Log Generation:** The system must produce a **Markdown log file** named `code_scanner_results.md` as its primary and only User Interface.
*   **Output Location:** The output file is written to the **target directory** root.
*   **Initial Output:** The output file must be **created at startup** (before scanning begins) to provide immediate feedback that the scanner is running.
*   **Scanner Files Exclusion:** The scanner must automatically exclude its own output files (`code_scanner_results.md`, `code_scanner_results.md.bak`, and `code_scanner.log`) from scanning to prevent self-referential analysis.
*   **Change Detection Exclusion:** The Git watcher must exclude `code_scanner_results.md` and `code_scanner_results.md.bak` from triggering rescans. Without this exclusion, every write to the output file would trigger a false "file changed" detection, causing endless rescan loops.
*   **Unified File Filtering:** All file exclusion rules are consolidated into a single `FileFilter` component for efficiency:
    *   **Scanner files:** Direct set lookup (O(1)) for output files.
    *   **Config ignore patterns:** fnmatch matching for patterns like `*.md, *.txt`.
    *   **Gitignore patterns:** In-memory pathspec matching (eliminates subprocess calls).
    *   **Single-pass filtering:** Files are filtered once early in the pipeline, before content is read.
    *   **Graceful degradation:** If pathspec library is unavailable, falls back to git subprocess.
*   **Detailed Findings:** For every issue found, the log must include:
    *   **File path** (exact location)
    *   **Line number** (specific line)
    *   **Issue description** (nature of the issue)
    *   **Suggested fix** (using markdown code blocks)
    *   **Timestamp** (when the issue was detected)
    *   **Check query prompt** (which check/query caused this issue)
*   **Output Organization:** Issues are grouped **by file**. Within each file section, each issue specifies which query/check caused it.
*   **State Management & Persistence:** The system must maintain an internal model of detected issues **in memory only**.
    *   **No Persistence Across Restarts:** State is **not persisted** to disk. Each scanner session starts fresh with no issues.
    *   **Automatic Results Backup:** On startup, if `code_scanner_results.md` exists, the scanner **automatically appends its content** to `code_scanner_results.md.bak` with a timestamp header, then starts with a fresh empty results file. No user prompt is required.
    *   **In-Session Tracking:** Smart matching, deduplication, and resolution tracking apply **within a single session** only.
    *   **Global Lock File:** The scanner creates a lock file at **`~/.code-scanner/code_scanner.lock`** (centralized location) to prevent multiple instances across all projects.
        *   **PID Tracking:** The lock file stores the PID of the running process.
        *   **Stale Lock Detection:** On startup, if a lock file exists, the scanner checks if the stored PID is still running. If the process is no longer active, the stale lock is automatically removed.
        *   **Active Lock:** If the PID is still running, fail with a clear error showing the active PID.
    *   **Smart Matching & Deduplication:** Issues are tracked primarily by **file** and **issue nature/description/code pattern**, not strictly by line number.
        *   **Matching Algorithm:** Issue matching uses **fuzzy string comparison** with a configurable similarity threshold (default: 0.8). This ensures that minor variations in issue descriptions or code snippets (e.g., whitespace changes, slight wording differences) are correctly identified as the same issue.
        *   **Whitespace Normalization:** Code snippets are compared with whitespace-normalized comparison (truncating/collapsing spaces).
        *   If an issue is detected at a different line number (e.g., due to code added above it) but matches an existing open issue's pattern, the scanner must **update the line number** in the existing record rather than creating a duplicate or resolving/re-opening.
    *   **Resolution Tracking:** If the scanner determines that a previously reported issue is no longer present (fixed), it must update the status of that issue in the output to **"RESOLVED"**. The original entry should remain for historical context, but its status changes.
        *   **Scoped Resolution:** Issues are only resolved based on scan results for files that were **actually scanned**. If a file was not included in the current scan (e.g., not in the changed files set), its issues remain unchanged. This prevents false resolution caused by LLM non-determinism.
    *   **Resolved Issues Lifecycle:** Resolved issues remain in the log **indefinitely** for historical tracking. Users may manually remove them if desired.
    *   **Source of Truth:** The scanner is the **authoritative source** for the log file. Any manual edits by the user (e.g., deleting an "OPEN" issue) will be **overwritten** if the scanner detects that the issue still exists in the code during the next scan.
    *   **File Rewriting:** To reflect these status updates, the scanner **rewrites the entire output file** each time the internal model changes.
    *   **Real-Time Updates:** The output file is updated **immediately** when new issues are found during scanning, not just at the end of a scan cycle. This provides instant feedback to the user.
    *   **System Verbosity:** Verbose logging is **always enabled** (no quiet mode). The output includes system information and detailed runtime data for debugging purposes.
    *   **System Log Destination:** Internal system logs (retry attempts, skipped files, warnings, debug info) are written to **both**:
        *   **Console** (stdout/stderr) for real-time monitoring.
        *   **Separate log file** at **`~/.code-scanner/code_scanner.log`** (centralized location shared across all projects). The scanner automatically ensures the parent directory exists before initializing the file handler.
    *   **Robust Error Handling:** The scanner implements defensive programming for common I/O issues:
        *   **Safe File Reading:** `read_file_content` uses fallback encodings (e.g., latin-1) and logs explicit warnings on decoding failures.
        *   **Recursive Directory creation:** Ensures log directories are created before writing.
        *   **Colored Console Output:** Console log messages use **ANSI color codes** for improved readability:
            *   **DEBUG:** Gray/dim text for low-priority diagnostic information.
            *   **INFO:** Cyan message with green level label for normal operation messages.
            *   **WARNING:** Yellow highlighting for potential issues that don't stop execution.
            *   **ERROR:** Red highlighting for errors that may affect functionality.
            *   **CRITICAL:** Bold red for severe errors requiring immediate attention.
        *   **Automatic Detection:** Colors are automatically disabled when output is not a TTY (e.g., piped to file).
        *   **Environment Variables:** Respects `NO_COLOR` (disables colors) and `FORCE_COLOR` (enables colors) standards.
        *   **File Logs:** The separate log file (`code_scanner.log`) does **not** contain color codes for clean text storage.
    *   **Graceful Shutdown:** On `Ctrl+C` (SIGINT), SIGTERM, or any termination (killing the app):
        *   **Immediate exit** without waiting for the current query to complete.
        *   **Lock file cleanup** is guaranteed via `atexit` handler and signal handlers.
        *   The lock file is removed even on `sys.exit()`, exceptions, or crashes.

### 2.4 Execution Workflow

*   **Check for lock file.** If exists and PID is running, fail with error. If stale (PID not running), remove and continue. Create lock file with current PID.
*   **Backup existing output file.** If `code_scanner_results.md` exists, append to `.bak` with timestamp, then start with a fresh empty results file. Print lock/log file paths.
*   **Initialize by reading the TOML config file.**
*   **Initialize the Git watcher component with status caching.**
*   **Start the scanner loop with AI tool executor initialized.**
*   **Wait Loop:** If no uncommitted changes (relative to HEAD or specified commit) exist, the scanner **must idle/wait**.
*   **Scanning:** When changes are found, identify the **entire content** of the modified files.
    *   **Context Check:** If combined files exceed context limit, apply **context overflow strategy** (group by directory, then file-by-file).
    *   **Skip oversized:** If a single file exceeds context limit, skip and warn.
*   **Trigger the LLM query loop with tool support**, processing check prompts sequentially:
    *   **Tool Execution:** If the LLM requests tools, execute them and send results back in a conversation loop.
    *   **Retry on failure:** If the LLM returns malformed JSON, retry immediately (max 3 retries).
    *   **Graceful Interrupts:** If a Git change is detected during a query, the scanner must **finish the current query** before restarting the loop.
*   **Update Output (Incremental):** After *each* completed query:
        *   **Update the internal model** with new findings.
        *   **immediately rewrite the output Markdown file** to provide real-time feedback.
    *   **Upon completing all checks, loop back** to the first check and continue.
*   **If the Git watcher detects new changes during scanning**, the scanner uses the **watermark algorithm**: complete the current cycle, then rescan only the checks that ran before the change point (checks 0..N where N is the index where the change was detected). This repeats until no changes occur during a cycle.
*   **On SIGINT**, immediately exit and remove lock file.

### 2.4 Multi-Project Support

The scanner supports monitoring **multiple projects simultaneously** and automatically switches between them based on recent changes.

*   **CLI Format:** Supports multiple project-config pairs specified at startup:
    *   Single project (backward compatible): `code-scanner /path/to/project -c /path/to/config.toml`
    *   Multiple projects: `code-scanner /path/to/project1 -c /path/to/config1 /path/to/project2 -c /path/to/config2`
    *   With default configs: `code-scanner /path/to/project1 /path/to/project2`
    *   With commit hashes: `code-scanner /path/to/project1 --commit abc123 -c /path/to/config1 /path/to/project2 --commit def456`
*   **Active Project Selection:** The project with **most recent changes** becomes active for checks
    *   Uses existing git change detection algorithm (max mtime_ns among changed files)
    *   Project switching is **non-blocking** - waits for current check to complete
    *   **Switch Timing:** Project switching is evaluated only when the current project completes a scan cycle
    *   **Verification:** Before switching, verify the target project still has the most recent changes
    *   **Priority:** Always select project with the single most recently changed file (no priority configuration)
*   **Project Switching Behavior:**
    *   **Scan Completion:** When active project completes its scan cycle with no mid-scan changes, scanner immediately switches to another project with uncommitted changes
    *   **Scan All Eligible Projects:** Scanner continues switching until all projects with uncommitted changes have been scanned
    *   **Avoid Redundant Scans:** Scanner compares file modification times against "Last updated" timestamp in results file to identify projects that have changes since last scan, avoiding rescanning of already-scanned worktrees
    *   **All Projects Clean:** When no projects have uncommitted changes, all projects are set to `WAITING_NO_CHANGES` and no project is marked as active
    *   **User Commits Changes:** When user commits all changes in the active project, scanner immediately switches to another project with changes
    *   **Switch Takes Precedence:** Project switching takes precedence over completing the current scan cycle - scanner interrupts after the current check completes
    *   **Cooldown Period:** Minimum 5-minute interval between project switches to prevent bouncing behavior
    *   **Merge/Rebase Escape:** When the active project enters merge/rebase state (detected via unmerged entries in git status), the scanner temporarily switches to another eligible project. The merging project is excluded from active selection until the conflict is resolved and merge/rebase markers are cleared.
*   **State Preservation:** Each project maintains its own state:
    *   **Full State Preservation:** All state is preserved in memory for each project including:
        *   `scan_info` (checks_run, total_checks, files_scanned, skipped_files)
        *   `last_scanned_files` (set of files scanned in last scan)
        *   `last_file_contents_hash` (content hashes for change detection)
        *   `issue_tracker` (detected issues with status)
    *   Switching between projects is seamless - previous project state is retained
    *   State is **not persisted** across restarts (starts fresh each time)
*   **LLM Client Management:**
    *   **Smart Switching:** LLM client is **only disconnected and reconnected** when configs differ
    *   **Client Reuse:** If configs are same, existing client is reused (avoids unnecessary reconnections)
    *   **Config Comparison:** Compares backend, host, port, model, and context_limit
*   **Output Files:** Each project has its own output file:
    *   `code_scanner_results.md` in each project directory
    *   Separate results per project for clear separation
    *   **Timestamps in Status Messages:** Output files include human-readable timestamps:
        *   `WAITING_OTHER_PROJECT`: "Waiting - Another project is currently being scanned (inactive since: January 25, 2026 at 9:05 PM UTC)"
        *   `RUNNING`: "Running - Check 3/10: Check for memory leaks (active since: January 25, 2026 at 9:10 PM UTC)"
        *   `WAITING_NO_CHANGES`: "Waiting - No uncommitted changes detected (since: January 25, 2026 at 9:15 PM UTC)"
    *   **Last Updated Header:** Results files include "Last updated: January 25, 2026 at 9:05 PM UTC" to track when last scan occurred
*   **Logging:** Single global log file with project prefixes:
    *   Log file location (platform-specific):
        *   Windows: `%APPDATA%\code-scanner\code_scanner.log`
        *   macOS: `~/Library/Application Support/code-scanner/code_scanner.log`
        *   Linux/Unix: `~/.code-scanner/code_scanner.log`
    *   Project prefixes: `[dirname]`, `[parent/dirname]`, etc. (using target directory name, with parent path for disambiguation when needed)
    *   System messages use `[SYSTEM]` prefix
    *   INFO-level logging for project switching (not DEBUG)
*   **Ctags Indexing:** Ctags index management for efficient symbol lookups:
    *   **Active Project Only:** Only the active project's ctags index is maintained in memory
    *   **Caching:** Index is cached in memory and reused when switching back to a project
    *   **Cache Invalidation:** Cached index is invalidated based on combination of time and file changes:
        *   Time-based: Index considered stale after extended period
        *   File-based: Index regenerated if new/deleted files detected
    *   **Generation:** Index is generated asynchronously when project becomes active (or regenerated if stale)
    *   Inactive projects do not maintain ctags index in memory (saves memory)
*   **Single Instance:** One scanner instance monitors all projects:
    *   Single global lock file (platform-specific):
        *   Windows: `%APPDATA%\code-scanner\code_scanner.lock`
        *   macOS: `~/Library/Application Support/code-scanner/code_scanner.lock`
        *   Linux/Unix: `~/.code-scanner/code_scanner.lock`
    *   Prevents multiple scanner instances running simultaneously
*   **Use Cases:**
    *   **Seamless Project Switching:** User works on one project, then switches to another project without restarting code scanner:
        *   All projects are specified at startup
        *   User doesn't need to restart with different project/config
        *   Seamless switching preserves work context
    *   **Automatic Multi-Project Scanning:** Scanner automatically scans all projects with uncommitted changes:
        *   Scans projects in order of most recent changes
        *   Continues until all projects have been scanned
        *   Avoids rescanning projects that were already scanned after their last file changes
    *   **Intelligent Activity Detection:** Scanner identifies which project user is actively working on:
        *   Tracks file modification times across all projects
        *   Switches to project with most recent changes
        *   Respects 5-minute minimum cooldown to prevent bouncing

### 2.5 Service Installation

The scanner can be installed as a system service to start automatically on boot. Autostart scripts are provided in the `scripts/` directory:

*   **Linux:** `scripts/autostart-linux.sh` - Creates a systemd user service. See [docs/linux-setup.md](docs/linux-setup.md).
*   **macOS:** `scripts/autostart-macos.sh` - Creates a LaunchAgent plist. See [docs/macos-setup.md](docs/macos-setup.md).
*   **Windows:** `scripts/autostart-windows.bat` - Creates a Task Scheduler task. See [docs/windows-setup.md](docs/windows-setup.md).

All scripts include:

*   **60-second startup delay** to allow LLM servers to initialize.
*   **Reinstall from source:** The `install` command automatically rebuilds and reinstalls code-scanner from the project source directory (using `uv pip install`) before setting up the service. This ensures the service always runs the latest version from the current sources.
*   **Current configuration display:** Scripts display the currently installed code-scanner version/path and the current `<cli_command>` from the existing service (if installed) at startup.
*   **Test launch** before registering the service.
*   **Legacy service detection** and removal.
*   **Full CLI command support:** Scripts accept a full CLI command string as a single argument for multi-project support:
    *   Example: `./scripts/autostart-linux.sh install "/path/to/project1 -c /path/to/config1 /path/to/project2 -c /path/to/config2"`
    *   Script automatically detects code-scanner executable

### 2.6 Version Management

The version is defined in a **single source of truth** in `pyproject.toml` (`version` field). All other locations read it dynamically:

*   `src/code_scanner/__init__.py` reads version from package metadata via `importlib.metadata.version("code-scanner")`.
*   `src/code_scanner/cli.py` imports `__version__` from the package and uses it in `--version` output.
*   The version should **never** be hardcoded in multiple files.

### 2.6 Sample Configuration Checks

The following checks are provided as **examples only** and can be completely customized or replaced by the user in the TOML configuration file. Checks are organized into **groups by file pattern**:

**C++/Qt-specific checks (pattern: `"*.cpp, *.h"`):**
*   Check that iteration continues automatically until the final result, without requiring user prompts to proceed.
*   Check that `constexpr` and compile-time programming techniques are applied where appropriate.
*   Check that stack allocation is preferred over heap allocation whenever possible.
*   Check that string literals are handled through `QStringView` variables.
*   Check that string literals used multiple times are stored in named `QStringView` constants instead of being repeated.
*   Check that comments provide meaningful context or rationale and avoid restating obvious code behavior.
*   Check that functions are implemented in `.cpp` files rather than `.h` files.

**Architectural checks leveraging AI tools (pattern: `"*"`):**
*   Check for architectural violations (e.g., MVC pattern breakage) using `search_text` to verify layer separation.
*   Check for inconsistent naming patterns across the codebase using `list_directory` and `read_file`.
*   Check for duplicate or similar function implementations using `search_text`.
*   Check for any detectable errors and suggest code simplifications where possible.

**General checks for all files (pattern: `"*"`):**
*   Check for any detectable errors and suggest code simplifications where possible.
*   Check for unused files or dead code.

**Example TOML configuration (LM Studio):**
```toml
[llm]
backend = "lm-studio"
host = "localhost"
port = 1234
# model = "specific-model-name"  # Optional for LM Studio
context_limit = 32768

[[checks]]
pattern = "*.cpp, *.h"
checks = [
    "Check for memory leaks",
    "Check that RAII is used properly"
]
```

**Example TOML configuration (Ollama):**
```toml
[llm]
backend = "ollama"
host = "localhost"
port = 11434
model = "qwen3:4b"  # Required for Ollama
context_limit = 16384  # Minimum 16384 recommended

[[checks]]
pattern = "*.py"
checks = [
    "Check for type hints",
    "Check for docstrings",
    "Check for duplicate function names across modules"
]
```

***

## 3. AI Tools for Context Expansion

The scanner provides AI tools that allow the LLM to interactively request additional codebase information for sophisticated architectural checks and deeper analysis.

### 3.1 Tool Overview

The LLM can invoke the following tools during analysis:

*   **search_text** - Fast text search using ripgrep
    *   Search for patterns across the codebase
    *   Supports regex, case sensitivity, whole word matching
    *   File pattern filtering (e.g., `*.py`, `*.cpp`)
    *   Returns matches with file paths, line numbers, and code snippets
    *   Automatically detects definition lines
*   **read_file** - Read file contents
    *   Read entire file or specific line ranges
    *   Supports line ranges for targeted reading
    *   Returns file content as plain text
*   **list_directory** - List directory contents
    *   List files and directories in a path
    *   Supports recursive directory listing
    *   Returns file names and types
*   **get_file_diff** - Get git diff for a file
    *   Show changes between commits or working tree
    *   Configurable context lines (default: 3)
    *   Returns unified diff format
*   **get_file_summary** - Get file statistics
    *   Returns file size, line count, language detection
    *   Useful for understanding file context before reading
*   **symbol_exists** - Check if symbol exists (Ctags)
    *   Query symbol index for existence
    *   Filter by symbol type (function, class, variable, etc.)
    *   Returns boolean and matching symbols
*   **find_definition** - Find symbol definition (Ctags)
    *   Locate where a symbol is defined
    *   Returns file path, line number, and symbol details
    *   Supports kind filtering
*   **find_symbols** - Find symbols by pattern (Ctags)
    *   Search for symbols matching a pattern
    *   Filter by symbol kind
    *   Returns all matching symbols with locations
*   **get_enclosing_scope** - Get parent scope (Ctags)
    *   Find the class/function containing a line
    *   Returns scope name and type
    *   Useful for understanding code context
*   **find_usages** - Find all usages of a symbol (Ctags)
    *   Locate all references to a symbol
    *   Filter by file or include definitions
    *   Returns list of usage locations

### 3.2 Ctags Integration

The scanner uses **Universal Ctags** for efficient symbol indexing and navigation.

*   **Symbol Indexing:** Generates an in-memory index of all symbols (functions, classes, variables, etc.) in the codebase
*   **Fast Lookups:** Provides O(1) symbol lookups instead of O(n) file scanning
*   **Language Support:** Supports 50+ programming languages via Universal Ctags
*   **Async Generation:** Index is generated asynchronously in background to avoid blocking startup
*   **Index Persistence:** Index is regenerated when switching projects to save memory
*   **Tool Integration:** AI tools query the index for symbol lookups, definitions, and usages

### 3.3 Ripgrep Integration

The scanner uses **ripgrep** for fast code search operations.

*   **Performance:** Ripgrep is significantly faster than grep for large codebases
*   **Gitignore Respect:** Automatically respects `.gitignore` patterns
*   **Regex Support:** Full regex pattern matching for advanced searches
*   **JSON Output:** Structured output for easy parsing
*   **File Filtering:** Built-in file type filtering (e.g., `*.py`, `*.cpp`)

## 4. Analogy for Understanding

Think of this code scanner as a **diligent proofreader** sitting over a writer's shoulder. Instead of waiting for the writer to finish the whole book, the proofreader only looks at the sentences the writer just typed (the uncommitted changes). The proofreader uses a specialized guidebook (the config file) to check for specific mistakes.

**With AI Tooling**, the proofreader now has a **library card**. Instead of just looking at the new sentences, the proofreader can get up, go to the bookshelf (the codebase), and pull out an old chapter (another file) to make sure a character's name is still spelled correctly or that a plot point remains consistent. The proofreader can even browse the table of contents (directory listings) to understand the book's structure before making recommendations.

**With Multi-Project Support**, the proofreader can monitor multiple books simultaneously, automatically switching between them based on which one the writer is currently editing. Each book maintains its own set of notes and corrections, and switching between them is seamless without losing context.
