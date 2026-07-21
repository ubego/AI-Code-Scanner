# Code Scanner

![Code Scanner Banner](images/banner.png)

AI-powered code scanner for immediate, background review of uncommitted changes as you work. Code Scanner continuously monitors your working directory and provides instant feedback on code issues before you commit—helping you catch bugs, style problems, and architectural issues early in your local development workflow. Uses local LLMs (LM Studio or Ollama) to identify issues based on configurable checks. **Your code never leaves your machine.**
---

⭐ **Star this project on GitHub to support its development!** [Code-Scanner on GitHub](https://github.com/ubego/Code-Scanner)

---

## Why Code Scanner?

Code Scanner is like having a senior code reviewer watching over your shoulder 24/7—without sending your code to the cloud.

- **Privacy First**: All analysis happens on your machine. Your proprietary code stays yours.
- **Zero Cost**: No API subscriptions, no token limits. Use your own hardware.
- **Language Agnostic**: Works with any programming language—Python, JavaScript, C++, Java, Rust, and more.
- **Continuous Monitoring**: Runs in the background, scanning every change you make in real-time.
- **Smart Context**: AI tools let the LLM explore your codebase to find architectural issues that simple linters miss.

---

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-1064%20tests-green.svg)](#development)
[![Coverage](https://img.shields.io/badge/Coverage-89%25-brightgreen.svg)](#development)

---

## Quick Start

Get Code Scanner running in 5 minutes:

### Step 1: Install Prerequisites

**Ubuntu/Debian:**
```bash
sudo apt install git universal-ctags ripgrep
```

**macOS:**
```bash
brew install git universal-ctags ripgrep
```

**Windows:**

For detailed Windows setup including multiple installation options, see [Windows Setup Guide](docs/windows-setup.md).

Quick option using Chocolatey (if installed):
```batch
choco install git universal-ctags ripgrep
```

Or using Scoop (if installed):
```batch
scoop install git universal-ctags ripgrep
```

### Step 2: Install Code Scanner

Using uv (recommended):
```bash
uv pip install code-scanner
```

Verify installation:
```bash
code-scanner --version
```

### Step 3: Start a Local LLM


**LM Studio** (GUI-based, easier)
1. Download from [lmstudio.ai](https://lmstudio.ai)
2. Load the model "qwen2.5-coder-7b-instruct"
3. Start the server (default: `localhost:1234`)


### Step 4: Configuration
Copy a sample config to your project (choose one based on your programming language):

- Python: `sample_configs/python-config.toml`
- JavaScript: `sample_configs/javascript-config.toml`
- C++: `sample_configs/cpp-config.toml` or `sample_configs/cpp-qt-config.toml`
- Java: `sample_configs/java-config.toml`
- Android: `sample_configs/android-config.toml`
- iOS/macOS: `sample_configs/ios-macos-config.toml`

### Step 5: Run the scanner

```bash
code-scanner /path/to/your/project -c code_scanner_config.toml
```

The scanner will:
1. Monitor your Git repository for changes
2. Scan modified files every 30 seconds
3. Report issues to `code_scanner_results.md`

### Step 6: View Results

Open `code_scanner_results.md` in your project directory to see:
- Files with issues
- Line numbers
- Issue descriptions
- Suggested fixes
- Resolution status (OPEN/RESOLVED)

**Pro tip**: Keep the results file open in your IDE. It updates in real-time as issues are found!

---

## Features

### Core Capabilities

- 🏠 **100% Local (Privacy first)**: Uses LM Studio or Ollama with local APIs. All processing happens on your machine, no cloud required.
- 🖥️ **Hardware Efficient**: Designed for small local models. Runs comfortably on consumer GPUs like **NVIDIA RTX 3060**.
- 💰 **Cost Effective**: Zero token costs. Use your local resources instead of expensive API subscriptions.
- 🔍 **Language-agnostic**: Works with any programming language.

### AI-Powered Analysis

- 🧰 **AI Tools for Context Expansion**: LLM can interactively request additional codebase information (find usages, read files, list directories) for sophisticated architectural checks.
- 🛡️ **Hallucination Prevention**: Validates file paths from LLM responses with helpful suggestions for similar files when paths don't exist.

### Continuous Monitoring

- ⚡ **Continuous Monitoring**: Runs in background mode, monitoring Git changes every 30 seconds and scanning indefinitely until stopped (`Ctrl+C`).
- 🔄 **Smart Change Detection**: Efficient git status caching with configurable TTL prevents redundant git operations. When changes are detected mid-scan, continues from current check with refreshed file contents (preserves progress).
- 📊 **Issue Tracking**: Tracks issue lifecycle (new, existing, resolved) with scoped resolution—issues are only resolved for files that were actually scanned.
- 📝 **Real-time Updates**: Output file updates immediately when issues are found (not just at end of scan).

### Configuration & Deployment

- 🔧 **Configurable Checks**: Define checks in plain English via TOML configuration with file pattern support.
- 📖 **Daemon-Ready**: Fully uninteractive mode—no prompts, configurable via file only. Supports autostart on all platforms.
- ✅ **Well-Tested**: 89% code coverage with 1064 unit tests ensuring reliability and maintainability.

---

## Use Cases

Code Scanner helps developers and teams in various scenarios:

### Before Commit: Catch Bugs Early

Find issues before pushing to remote:
- Memory leaks and null pointer dereferences
- Race conditions and thread safety issues
- Missing error handling
- Logic errors and edge cases

**Example:**
```python
# Before Code Scanner
def process_data(data):
    result = []
    for item in data:
        result.append(item * 2)
    return result

# Code Scanner reports:
# ❌ process_data.py:3 - Missing type hints
#    Suggested: def process_data(data: list[int]) -> list[int]:
#
# ❌ process_data.py:4 - Inefficient list append in loop
#    Suggested: Use list comprehension: return [item * 2 for item in data]
```

### Code Standards Enforcement

Ensure team-wide consistency:
- **C++**: RAII usage, proper const correctness, smart pointers
- **Python**: Type hints, docstrings, PEP 8 compliance
- **JavaScript**: ESLint rules, async/await patterns
- **Java**: Proper exception handling, resource management

### Architectural Reviews

Detect high-level design issues:
- MVC pattern violations
- Circular dependencies
- Layering violations (UI accessing database directly)
- Duplicate code across modules

### Security Scanning

Identify potential vulnerabilities:
- SQL injection risks
- XSS vulnerabilities in web code
- Hardcoded credentials
- Insecure random number generation

### Code Quality

Find improvement opportunities:
- Dead code and unused functions
- Performance bottlenecks
- Code complexity issues
- Missing documentation

---

## Code Scanner vs Other Tools

| Feature | Code Scanner | Traditional Linters | Cloud AI Tools |
|---------|--------------|-------------------|----------------|
| **Privacy** | ✅ 100% local | ✅ 100% local | ❌ Code sent to cloud |
| **Cost** | ✅ Free (your hardware) | ✅ Free | ❌ API subscriptions |
| **Context Awareness** | ✅ AI tools explore codebase | ❌ File-by-file only | ✅ Full codebase |
| **Custom Checks** | ✅ Plain English prompts | ❌ Complex rules | ✅ Plain English |
| **Real-time** | ✅ Continuous monitoring | ❌ Manual runs | ❌ Manual runs |
| **Language Support** | ✅ Any language | ❌ Language-specific | ✅ Any language |
| **Architectural Analysis** | ✅ Cross-file analysis | ❌ Single file | ✅ Cross-file |

---

## Installation

### System Requirements

- **Python 3.11 or higher**
- **Git** (for tracking file changes)
- **Universal Ctags** (for symbol indexing)
- **ripgrep** (for fast code search)

### Install Code Scanner

#### Using uv (recommended)

uv is faster and more reliable for Python package management:

```bash
pip install uv
uv pip install code-scanner
```

#### From Source

```bash
git clone https://github.com/ubego/Code-Scanner.git
cd Code-Scanner
uv pip install -e .
```
Or using uv sync (recommended for development):
```bash
git clone https://github.com/ubego/Code-Scanner.git
cd Code-Scanner
uv sync
uv run code-scanner --help
```

### Verify Installation

```bash
code-scanner --version
```

Expected output:
```
code-scanner X.Y.Z
```

### Platform-Specific Setup

For detailed setup instructions including autostart configuration (background service via systemd / LaunchAgent / Task Scheduler):

- **[Linux Setup](docs/linux-setup.md)** - systemd user service
- **[macOS Setup](docs/macos-setup.md)** - LaunchAgent, Homebrew setup
- **[Windows Setup](docs/windows-setup.md)** - Task Scheduler, Chocolatey/Scoop installation

---

## Configuration

Code Scanner uses TOML configuration files to define:
- LLM backend settings
- File patterns to scan
- Custom checks in plain English

### Basic Configuration

#### For Ollama

```toml
[llm]
backend = "lm-studio"
host = "localhost"
port = 1234
timeout = 120                # Request timeout in seconds
context_limit = 32768        # Model's context window (tokens)

[[checks]]
pattern = "*.py"
checks = [
    "Check for bugs and issues",
    "Check for security vulnerabilities",
    "Check for type hints and docstrings"
]
```

#### For LM Studio

```toml
[llm]
backend = "lm-studio"
host = "localhost"
port = 1234
# model = "specific-model-name"  # Optional - uses first loaded model
context_limit = 32768        # Model's context window (tokens)

[[checks]]
pattern = "*.cpp, *.h"
checks = [
    "Check for memory leaks",
    "Check that RAII is used properly",
    "Check for null pointer dereferences"
]
```

### Configuration Parameters

#### [llm] Section

| Parameter | Required | Description | Example |
|-----------|----------|-------------|---------|
| `backend` | ✅ Yes | LLM backend: `"ollama"` or `"lm-studio"` | `"ollama"` |
| `host` | ✅ Yes | Backend server host | `"localhost"` |
| `port` | ✅ Yes | Backend server port | `11434` |
| `model` | Ollama only | Model name (Ollama) | `"qwen3:4b"` |
| `timeout` | No | Request timeout in seconds | `120` |
| `context_limit` | ✅ Yes | Model's context window size in tokens | `16384` |

**Recommended context_limit values:**
- `4096` - Small models (e.g., CodeLlama 7B)
- `8192` - Medium models
- `16384` - Recommended minimum for most use cases
- `32768` - Large models (e.g., DeepSeek-Coder V2)
- `131072` - Very large models

#### [[checks]] Sections

Each `[[checks]]` section defines a group of checks for files matching a pattern.

| Parameter | Required | Description | Example |
|-----------|----------|-------------|---------|
| `pattern` | ✅ Yes | Glob pattern for files | `"*.py"` or `"*.cpp, *.h"` |
| `checks` | ✅ Yes | List of check prompts | `["Check for bugs"]` |

**Pattern Examples:**
- `"*"` - All files
- `"*.py"` - Python files only
- `"*.cpp, *.h"` - C++ files (headers and sources)
- `"*.js, *.ts, *.jsx, *.tsx"` - JavaScript/TypeScript files
- `"src/**/*.py"` - Python files in src directory

### Ignore Patterns

Exclude files from scanning using empty checks lists:

```toml
[[checks]]
pattern = "*.md, *.txt, *.json"
checks = []  # Empty list = ignore these files

[[checks]]
pattern = "/*tests*/, /*build*/, /*vendor*/"
checks = []  # Ignore entire directories
```

**Ignore pattern syntax:**
- File patterns: `"*.md, *.txt"` - matches by extension
- Directory patterns: `"/*tests*/"` - matches files in any directory named "tests"
- Wildcards: `"/*cmake-build-*/"` - matches cmake-build-debug, cmake-build-release, etc.

### Sample Configurations

Ready-to-use configs for common languages:

- [`sample_configs/python-config.toml`](sample_configs/python-config.toml)
- [`sample_configs/javascript-config.toml`](sample_configs/javascript-config.toml)
- [`sample_configs/cpp-config.toml`](sample_configs/cpp-config.toml)
- [`sample_configs/cpp-qt-config.toml`](sample_configs/cpp-qt-config.toml)
- [`sample_configs/java-config.toml`](sample_configs/java-config.toml)
- [`sample_configs/android-config.toml`](sample_configs/android-config.toml)
- [`sample_configs/ios-macos-config.toml`](sample_configs/ios-macos-config.toml)

### Configuration Validation

Code Scanner validates configs strictly:

- Only `[llm]` and `[[checks]]` sections are allowed
- Unknown parameters cause immediate errors
- Missing required parameters cause immediate errors

Error messages show:
- Unsupported parameters
- Supported alternatives
- Line numbers for easy fixing

---

## Usage

### Basic Usage

Scan a single project:

```bash
code-scanner /path/to/project
```

Uses default config: `code_scanner_config.toml` in the project directory.

### Specify Config File

```bash
code-scanner /path/to/project -c /path/to/config.toml
```

### Multiple Projects

Monitor multiple projects simultaneously:

```bash
code-scanner /path/to/project1 -c /path/to/config1 /path/to/project2 -c /path/to/config2
```

**How it works:**
- Each project has its own config and output file
- Scanner automatically switches to the project with most recent changes
- State is preserved for all projects in memory
- Seamless switching without restarting

### Scan Specific Commit

Scan changes relative to a specific commit:

```bash
code-scanner /path/to/project --commit abc123 -c /path/to/config.toml
```

Useful for:
- Scanning cumulative changes against a parent branch
- Reviewing pull requests before merging
- Analyzing feature branches

### Operation Mode

Control what set of changes the scanner analyzes with the `--mode` option:

```bash
code-scanner /path/to/project -m branch -c /path/to/config.toml
```

**Available modes:**

| Mode | Description |
|------|-------------|
| `uncommitted` (default) | Scans working tree changes since the last commit. Best for real-time feedback as you code. |
| `branch` | Scans all changes in the current branch since it diverged from `main` (or `master`). Includes both committed and uncommitted changes. Useful for reviewing an entire feature branch before merging. |

**Examples:**

```bash
# Default: scan uncommitted changes only
code-scanner /path/to/project

# Scan all changes in the current branch
code-scanner /path/to/project -m branch

# Branch mode with custom config
code-scanner /path/to/project -m branch -c /path/to/config.toml
```

### Output Files

Each project generates:
- `code_scanner_results.md` - Issues and findings (in project directory)
- `code_scanner_results.md.bak` - Backup of previous results (in project directory)

System-wide files:
- `~/.code-scanner/code_scanner.log` - Detailed logs (platform-specific path)
- `~/.code-scanner/code_scanner.lock` - Lock file to prevent multiple instances

### Running in Background

#### Linux/macOS

```bash
nohup code-scanner /path/to/project > /dev/null 2>&1 &
```

#### Windows (PowerShell)

```powershell
Start-Process -WindowStyle Hidden code-scanner -ArgumentList "/path/to/project"
```

### Autostart on Boot

Run Code Scanner automatically when your system starts. Pass the **full CLI command** (the same arguments you would pass to `code-scanner`) as a single quoted argument:

The `install` command automatically reinstalls code-scanner from the project source before configuring the service (preferring **pipx**, falling back to `uv`/`pip`), ensuring the latest version is always deployed. It also verifies the installed binary supports every flag in your command and prints a clear diagnostic if the install is stale.

**Linux:**
```bash
./scripts/autostart-linux.sh install "/path/to/project -c /path/to/config.toml"
```

**macOS:**
```bash
./scripts/autostart-macos.sh install "/path/to/project -c /path/to/config.toml"
```

**Windows:**
```batch
scripts\autostart-windows.bat install "/path/to/project -c /path/to/config.toml"
```

Multiple projects and modes are supported — the quoted string is exactly the `code-scanner` CLI:

```bash
./scripts/autostart-linux.sh install "/path/to/project1 -c /path/to/config1.toml /path/to/project2 -c /path/to/config2.toml --mode branch"
```

See platform-specific setup docs for details.

### Stopping the Scanner

Press `Ctrl+C` to stop. The scanner:
- Completes the current check
- Cleans up lock files
- Exits gracefully

---

## AI Tools

Code Scanner provides powerful AI tools that let the LLM explore your codebase during analysis. This enables sophisticated checks that go beyond simple pattern matching.

### Available Tools

| Tool | Description | Use Case |
|------|-------------|----------|
| `search_text` | Fast text search using ripgrep | Find function usages, locate patterns |
| `read_file` | Read file contents | Get context from related files |
| `list_directory` | List directory contents | Understand project structure |
| `get_file_diff` | Get git diff for a file | See what changed |
| `get_file_summary` | Get file statistics | Understand file before reading |
| `symbol_exists` | Check if symbol exists | Verify function/class exists |
| `find_definition` | Find symbol definition | Locate where symbol is defined |
| `find_symbols` | Find symbols by pattern | Search for related functions |
| `get_enclosing_scope` | Get parent scope | Understand code context |
| `find_usages` | Find all usages of a symbol | Track symbol usage across codebase |

### Example: Architectural Check

```toml
[[checks]]
pattern = "*"
checks = [
    "Check for MVC pattern violations: UI code should not directly access database. Use search_text to find database queries in UI files, and read_file to verify context."
]
```

**How the LLM uses tools:**
1. Scans UI files for database-related keywords (`search_text`)
2. Reads suspicious files to verify context (`read_file`)
3. Reports violations with specific file locations and line numbers

### Example: Duplicate Code Detection

```toml
[[checks]]
pattern = "*.py"
checks = [
    "Find duplicate or similar function implementations. Use search_text to find function definitions with similar names, then read_file to compare implementations."
]
```

### Example: Naming Consistency

```toml
[[checks]]
pattern = "*"
checks = [
    "Check for inconsistent naming patterns across the codebase. Use list_directory to explore structure, then read_file to verify naming conventions."
]
```

### Tool Integration Details

- **Ctags**: Used for symbol indexing (functions, classes, variables)
- **Ripgrep**: Used for fast text search
- **Git**: Used for diff operations
- All tools are local and respect `.gitignore`

### Performance Considerations

- Symbol index is generated asynchronously at startup
- Index is regenerated when switching projects
- Tool calls are tracked to prevent context overflow
- Inactive projects don't maintain indexes (saves memory)

---

## Advanced Features

### Multi-Project Support

Monitor multiple projects with a single scanner instance:

```bash
code-scanner /path/to/project1 -c /path/to/config1 /path/to/project2 -c /path/to/config2
```

**Key Features:**
- **Automatic Switching**: Scanner switches to project with most recent changes
- **Non-blocking**: Current check completes before switching
- **State Preservation**: Each project maintains its own issue tracker state
- **Smart LLM Management**: Client reused if configs are identical
- **Separate Outputs**: Each project has its own `code_scanner_results.md`

**Project Switching Behavior:**
1. Scanner detects which project has most recent changes (based on file modification times)
2. Waits for current check to complete
3. Switches to active project
4. If LLM configs differ, disconnects and reconnects
5. Regenerates ctags index for new active project
6. Continues scanning

### Commit-Based Scanning

Scan changes relative to a specific commit:

```bash
code-scanner /path/to/project --commit abc123 -c /path/to/config.toml
```

**Use Cases:**
- Review cumulative changes in a feature branch
- Analyze pull request before merging
- Compare against stable branch

**Behavior:**
- Scans all uncommitted changes
- Compares against specified commit
- Includes untracked files
- Continues monitoring for new changes after initial scan

### Issue Lifecycle Management

Code Scanner tracks issue state within a session:

**States:**
- `NEW` - Issue detected for the first time
- `EXISTING` - Issue detected in previous scan, still present
- `RESOLVED` - Issue was detected before, no longer present

**Smart Matching:**
- Issues are matched by file and issue nature (not just line number)
- Fuzzy string comparison with configurable threshold (default: 0.8)
- Line numbers update if code shifts
- Prevents duplicate issues for the same problem

**Scoped Resolution:**
- Issues only resolved for files that were actually scanned
- Prevents false resolution from LLM non-determinism
- Resolved issues remain in log for historical tracking

### Context Overflow Strategy

When code exceeds model's context window:

1. **Group by directory hierarchy** - Batch files from same directory
2. **Deterministic batching** - Sort alphabetically, deepest-first
3. **File-by-file fallback** - If directory group still too large
4. **Skip oversized files** - Log warning and continue
5. **Merge results** - Combine issues from all batches

**Token Tracking:**
- Uses 55% of context limit for file content
- Tracks accumulated tokens during tool calls
- Stops tool calling at 85% context usage
- Prevents overflow during multi-turn conversations

### Git Integration

**Change Detection:**
- Monitors staged, unstaged, and untracked files
- Respects `.gitignore` patterns
- Efficient caching with configurable TTL (default: 5 seconds) for low CPU usage during idle
- Detects file modifications via content hashes

**Conflict Handling:**
- Waits for merge/rebase conflicts to resolve
- Polls for completion status
- Resumes scanning automatically

**Binary Files:**
- Silently skipped during scanning
- Tracked to prevent infinite rescan loops

---

## Supported LLM Backends

Code Scanner works with local LLM servers that provide OpenAI-compatible APIs.

### LM Studio

**Best for:** GUI users, trying different models, visual feedback

**Installation:**
1. Download from [lmstudio.ai](https://lmstudio.ai)
2. Install and launch
3. Load the model "qwen2.5-coder-7b-instruct"
4. Start server (default: `localhost:1234`)

**Recommended Models:**
- DeepSeek-Coder V2 (excellent for code analysis)
- Qwen2.5-Coder (fast, good balance)
- CodeLlama 7B/13B (lighter option)

**Configuration:**
```toml
[llm]
backend = "lm-studio"
host = "localhost"
port = 1234
context_limit = 32768
```

### Ollama

**Best for:** CLI users, automation, simpler setup, headless servers

**Installation:**
```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

**Pull a model:**
```bash
ollama pull qwen3:4b
ollama run qwen3:4b
```

**Recommended Models:**
- `qwen3:4b` - Fast, good for code (4B parameters)
- `qwen3:7b` - Better accuracy (7B parameters)
- `deepseek-coder:6.7b` - Excellent for code analysis
- `codellama:7b` - Lightweight option

**Configuration:**
```toml
[llm]
backend = "lm-studio"
host = "localhost"
port = 1234
context_limit = 32768
```

### Hardware Requirements

**Minimum:**
- CPU: Any modern multi-core processor
- RAM: 8GB (16GB recommended)
- GPU: Not required (CPU inference works, but slower)

**Recommended:**
- GPU: NVIDIA RTX 3060 (12GB VRAM) or better
- RAM: 16GB+
- Storage: SSD for faster file access

**Performance Tips:**
- Use smaller models (4B-7B) for faster scanning
- Increase `context_limit` for larger codebases
- Use GPU acceleration if available
- Reduce check complexity for faster results

---

## Troubleshooting

### Common Issues

#### "Connection refused" or "Cannot connect to LLM backend"

**Problem:** Scanner can't connect to LM Studio or Ollama

**Solutions:**
1. Ensure LM Studio or Ollama is running:
   - LM Studio: Check that server is started (green indicator)
   - Ollama: Run `ollama list` to verify it's running

2. Check host/port in config:
   ```bash
   # Test connection
   curl http://localhost:1234/v1/models  # LM Studio
   curl http://localhost:11434/api/tags  # Ollama
   ```

3. Verify model is loaded:
   - LM Studio: Model must be loaded in the server
   - Ollama: Run `ollama list` to see available models

#### "Not a git repository" error

**Problem:** Target directory isn't a Git repository

**Solution:** Initialize git in your project:
```bash
cd /path/to/project
git init
git add .
git commit -m "Initial commit"
```

#### "No changes detected" message

**Problem:** Scanner is waiting for changes

**Solutions:**
1. Make a change to a tracked file
2. Add a new file: `touch newfile.py`
3. Stage files: `git add .`
4. Check if files are in `.gitignore`

#### "Context limit exceeded" warning

**Problem:** Code is too large for model's context window

**Solutions:**
1. Increase `context_limit` in config (if model supports it)
2. Use ignore patterns to exclude large files/directories
3. Use a model with larger context window
4. Split checks into smaller, more specific queries

#### Scanning is too slow

**Problem:** Scanner takes too long to complete

**Solutions:**
1. Use ignore patterns to exclude test/build directories:
   ```toml
   [[checks]]
   pattern = "/*tests*/, /*build*/, /*node_modules*/"
   checks = []
   ```

2. Reduce `context_limit` if model is slow
3. Use a smaller/faster model (e.g., 4B instead of 7B)
4. Reduce number of checks
5. Use file patterns to scan only relevant files

#### "Malformed JSON response" errors

**Problem:** LLM returns invalid JSON, or its output is cut off mid-generation (`max_tokens` hit, dropped stream).

**What the scanner does automatically:**

1. **Markdown fence stripping** — removes ```` ```json ```` wrappers (including the truncated case where the closing fence never arrived).
2. **Local truncation recovery** — if the JSON was cut off mid-object, the scanner repairs it locally by dropping the incomplete tail and closing open braces. This avoids a second LLM round-trip and recovers whatever complete issues the model had already produced.
3. **LLM reformat request** — if local recovery doesn't apply (e.g. the model returned prose instead of JSON), the scanner asks the LLM to reformat its own output.
4. **Retries** — up to 3 attempts before skipping the check and logging an error.

**If these errors persist frequently:**

1. Try a different model
2. Reduce `context_limit` to prevent overflow
3. Simplify check prompts
4. Increase `max_tokens` if the model is hitting the output cap (the scanner hard-caps output tokens to avoid runaway generation; for reasoning models this is rarely the bottleneck)

#### Lock file errors

**Problem:** "Another instance is already running"

**Solutions:**
1. Check if another scanner is running: `ps aux | grep code-scanner`
2. If not, remove stale lock file:
   ```bash
   rm ~/.code-scanner/code_scanner.lock
   ```
3. Verify PID in lock file is not running

### Getting Help

- **Documentation:** Check [platform-specific setup docs](docs/)
- **GitHub Issues:** [Report bugs or request features](https://github.com/ubego/Code-Scanner/issues)
- **Discussions:** [Ask questions and share ideas](https://github.com/ubego/Code-Scanner/discussions)

### Debug Mode

Enable verbose logging for troubleshooting:

```bash
code-scanner /path/to/project --verbose
```

Log file location (platform-specific):
- Linux: `~/.code-scanner/code_scanner.log`
- macOS: `~/Library/Application Support/code-scanner/code_scanner.log`
- Windows: `%APPDATA%\code-scanner\code_scanner.log`

---

## Development

### Project Structure

```
src/code_scanner/
├── models.py              # Data models (LLMConfig, Issue, etc.)
├── config.py              # Configuration loading and validation
├── base_client.py         # Abstract base class for LLM clients
├── lmstudio_client.py     # LM Studio client implementation
├── ollama_client.py       # Ollama client implementation
├── ai_tools.py            # AI tool executor for context expansion
├── text_utils.py          # Text processing utilities
├── git_watcher.py         # Git repository monitoring
├── issue_tracker.py       # Issue lifecycle management
├── output.py              # Markdown report generation
├── scanner.py             # AI scanning logic
├── cli.py                 # CLI and application coordinator
├── utils.py               # Utility functions
└── __main__.py            # Entry point
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Verbose output
uv run pytest -v

# Specific test file
uv run pytest tests/test_scanner.py -v

# Coverage report
uv run pytest --cov=code_scanner --cov-report=term-missing

# HTML coverage report
uv run pytest --cov=code_scanner --cov-report=html
# Open htmlcov/index.html in browser
```

### Current Coverage

**92% code coverage** with **905 unit tests**

### Contributing

We welcome contributions! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

### License

GNU Affero General Public License v3.0

See [LICENSE](LICENSE) for details.

---

## Documentation

For detailed platform-specific setup instructions:

- **[Linux Setup](docs/linux-setup.md)** - systemd service, desktop integration
- **[macOS Setup](docs/macos-setup.md)** - LaunchAgent, Homebrew setup
- **[Windows Setup](docs/windows-setup.md)** - Task Scheduler, Chocolatey/Scoop installation

---

**Made with ❤️ by the Ubego team**

[⭐ Star us on GitHub](https://github.com/ubego/Code-Scanner)
