# Windows Setup Guide

This guide covers setting up Code Scanner on Windows.

## Python Installation

### Option 1: Microsoft Store (Recommended for beginners)

1. Open Microsoft Store
2. Search for "Python 3.12"
3. Click "Install"
4. Open PowerShell and verify: `python --version`

### Option 2: python.org Installer

1. Download from [python.org](https://www.python.org/downloads/windows/)
2. Run the installer
3. **Important**: Check "Add Python to PATH" during installation
4. Complete installation

### Verify Installation

Open PowerShell or Command Prompt:

```powershell
python --version   # Should be 3.10 or higher
pip --version
```

## UV Installation

Open PowerShell as Administrator:

```powershell
# Install UV
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Restart PowerShell, then verify
uv --version
```

## Git Installation

If Git isn't already installed:

1. Download from [git-scm.com](https://git-scm.com/download/win)
2. Run the installer
3. Use default options (Git Bash is recommended)

Verify installation:

```powershell
git --version
```

## Universal Ctags Installation

Universal Ctags is required for symbol indexing, which enables AI tools to efficiently navigate your codebase.

### Option 1: Using Chocolatey (Recommended)

If you have [Chocolatey](https://chocolatey.org/) installed:

```powershell
# Open PowerShell as Administrator
choco install universal-ctags

# Verify installation
ctags --version
```

### Option 2: Using Scoop

If you have [Scoop](https://scoop.sh/) installed:

```powershell
scoop install universal-ctags
ctags --version
```

### Option 3: Manual Installation

1. Download from [Universal Ctags GitHub releases](https://github.com/universal-ctags/ctags-win32/releases)
2. Extract to a folder (e.g., `C:\Program Files\ctags`)
3. Add the folder to your PATH:
   - Open System Properties → Advanced → Environment Variables
   - Edit PATH and add the ctags folder
4. Restart PowerShell and verify: `ctags --version`

> **Note**: Make sure it shows "Universal Ctags" (not "Exuberant Ctags").

## Ripgrep Installation

Ripgrep is required for fast code search across the repository.

### Option 1: Using Chocolatey (Recommended)

```powershell
# Open PowerShell as Administrator
choco install ripgrep

# Verify installation
rg --version
```

### Option 2: Using Scoop

```powershell
scoop install ripgrep
rg --version
```

### Option 3: Using Winget

```powershell
winget install BurntSushi.ripgrep
rg --version
```

### Option 4: Manual Installation

1. Download from [ripgrep GitHub releases](https://github.com/BurntSushi/ripgrep/releases)
2. Extract to a folder (e.g., `C:\Program Files\ripgrep`)
3. Add the folder to your PATH
4. Restart PowerShell and verify: `rg --version`

## Code Scanner Installation

Open PowerShell or Git Bash:

```powershell
# Clone the repository
git clone https://github.com/ubego/Code-Scanner.git
cd code-scanner

# Install dependencies with UV
uv sync

# Verify installation
uv run code-scanner --help
```

## LLM Backend Setup

Choose one of the following backends:

### Option 1: Ollama

Ollama now supports Windows natively.

1. Download from [ollama.ai/download](https://ollama.ai/download)
2. Run the installer
3. Ollama starts automatically

Open PowerShell:

```powershell
# Pull a model
ollama pull qwen3:4b

# Verify it's working
ollama list

# Test the API
curl http://localhost:11434/api/tags
# Or with PowerShell:
Invoke-WebRequest http://localhost:11434/api/tags
```

**Configuration for Ollama:**

Copy a language-specific example (e.g., `sample_configs/python-config.toml`) and update the `[llm]` section:

```toml
[llm]
backend = "ollama"
host = "localhost"
port = 11434
model = "qwen3:4b"
timeout = 120
context_limit = 16384  # Required
```

### Option 2: LM Studio

LM Studio provides a user-friendly Windows application.

1. Download from [lmstudio.ai](https://lmstudio.ai)
2. Run the installer
3. Launch LM Studio

**In LM Studio:**
1. Search for "qwen2.5-coder-7b-instruct"
2. Download the model
3. Load the model
4. Go to "Local Server" tab (click the "<->" icon)
5. **Set "Context Length" to at least 16384** in the right sidebar
6. Click "Start Server" (default port: 1234)

**Configuration for LM Studio:**

Copy a language-specific example (e.g., `sample_configs/python-config.toml`) and update the `[llm]` section:

```toml
[llm]
backend = "lm-studio"
host = "localhost"
port = 1234
timeout = 120
context_limit = 16384  # Required
```

## Running Code Scanner

### Using PowerShell

```powershell
# Navigate to your project
cd C:\path\to\your\project

# Create code_scanner_config.toml (copy from examples)
Copy-Item C:\path\to\code-scanner\examples\python-config.toml code_scanner_config.toml

# Edit code_scanner_config.toml with your preferred editor
notepad code_scanner_config.toml

# Run the scanner (runs continuously until Ctrl+C)
uv run code-scanner
```

### Using Git Bash

```bash
# Navigate to your project
cd /c/path/to/your/project

# Create code_scanner_config.toml
cp /c/path/to/code-scanner/sample_configs/python-config.toml code_scanner_config.toml

# Run the scanner
uv run code-scanner
```

### Using Command Prompt (cmd)

```cmd
:: Navigate to your project
cd C:\path\to\your\project

:: Run the scanner
uv run code-scanner
```

## GPU Acceleration

### NVIDIA GPUs

LM Studio and Ollama can use NVIDIA GPUs for faster inference:

1. Install [NVIDIA CUDA Toolkit](https://developer.nvidia.com/cuda-toolkit)
2. Install latest [NVIDIA drivers](https://www.nvidia.com/Download/index.aspx)
3. In LM Studio, enable GPU acceleration in settings
4. Ollama automatically uses CUDA if available

### AMD GPUs

- LM Studio supports AMD GPUs via ROCm
- Ollama support for AMD is limited on Windows

## Running as a Background Service

You can configure Code Scanner to start automatically on login using Windows Task Scheduler.

### Prerequisites

- Code Scanner installed and working via command line
- LLM backend (Ollama or LM Studio) installed
- PowerShell or Command Prompt with administrator access (for some operations)

### Quick Setup

Run the autostart script with `install` and pass the **full CLI command** (the same arguments you would pass to `code-scanner`) as a single quoted string, from a Command Prompt in the code-scanner directory:

```batch
scripts\autostart-windows.bat install "C:\path\to\project -c C:\path\to\config.toml"
```

Multiple projects and modes are supported in one command:

```batch
scripts\autostart-windows.bat install "C:\path\to\project1 -c C:\path\to\config1.toml C:\path\to\project2 -c C:\path\to\config2.toml --mode branch"
```

The script will:

1. **Reinstall from source** — rebuilds and reinstalls code-scanner from the project source (preferring pipx, falling back to `uv`/`pip`), so the service runs the latest version.
2. **Verify CLI compatibility** — checks the installed binary supports every flag in your command and prints a clear diagnostic if the install is stale.
3. **Test launch** the scanner to verify the command works.
4. **Register the scheduled task** named "CodeScanner" via Task Scheduler.

Other commands: `scripts\autostart-windows.bat remove` (uninstall) and `scripts\autostart-windows.bat status`.

### What the Script Does

1. **Reinstalls** code-scanner from source (pipx/uv/pip)
2. **Verifies** the installed binary supports all CLI flags used
3. **Detects legacy tasks** and offers to remove them
4. **Test launches** the scanner to verify configuration
5. **Creates wrapper script** at `%USERPROFILE%\.code-scanner\launch-wrapper.bat`
6. **Creates scheduled task** named "CodeScanner" via Task Scheduler
7. **Configures login trigger** to start on user logon
8. **Includes 60-second delay** to allow LLM backend startup

### Managing the Service

```batch
REM Check status
schtasks /query /tn "CodeScanner"

REM Run manually
schtasks /run /tn "CodeScanner"

REM Stop
schtasks /end /tn "CodeScanner"

REM Remove completely
schtasks /delete /tn "CodeScanner" /f
del "%USERPROFILE%\.code-scanner\launch-wrapper.bat"
```

### Using Task Scheduler GUI

You can also manage the task via the graphical interface:

1. Press `Win + R`, type `taskschd.msc`, press Enter
2. Find "CodeScanner" in the task list
3. Right-click to Run, End, Disable, or Delete

### Autostart Log Files

- **Scanner log:** `%USERPROFILE%\.code-scanner\code_scanner.log`
- **Results:** `<project>\code_scanner_results.md`

### Autostart Troubleshooting

**Task won't start:**
1. Check scanner log: `type %USERPROFILE%\.code-scanner\code_scanner.log`
2. Verify config file path is correct
3. Ensure LLM backend is running
4. Open Task Scheduler and check the task's "History" tab

**Lock file errors:**
Another instance may be running. Check with:

```batch
type %USERPROFILE%\.code-scanner\code_scanner.lock
tasklist | findstr code-scanner
```

Delete stale lock if needed:

```batch
del %USERPROFILE%\.code-scanner\code_scanner.lock
```

**Permission issues:**
If the task fails to create, try running Command Prompt as Administrator:

1. Right-click Command Prompt
2. Select "Run as administrator"
3. Navigate to the code-scanner directory
4. Run the autostart script again

## Troubleshooting

### "python is not recognized"

Python isn't in your PATH. Either:
- Reinstall Python with "Add to PATH" checked
- Add Python manually to PATH:
  1. Search "Environment Variables" in Start
  2. Edit PATH
  3. Add Python installation directory (e.g., `C:\Users\YourName\AppData\Local\Programs\Python\Python312`)

### "uv is not recognized"

Restart PowerShell after UV installation. If still not working:

```powershell
# Check UV location
$env:USERPROFILE\.local\bin\uv.exe --version

# Add to PATH manually
$env:PATH += ";$env:USERPROFILE\.local\bin"
```

### Firewall blocking connections

Windows Firewall may block LLM backends:

1. Open Windows Security
2. Firewall & network protection
3. Allow an app through firewall
4. Add LM Studio or Ollama

### Antivirus interference

Some antivirus software may slow down or block the scanner:
- Add code-scanner directory to exclusions
- Add LM Studio/Ollama to exclusions

### Long path issues

If you see path-related errors:

1. Open Group Policy Editor (gpedit.msc)
2. Navigate to: Computer Configuration > Administrative Templates > System > Filesystem
3. Enable "Enable Win32 long paths"
4. Restart your computer

### PowerShell script execution policy

If you see "execution of scripts is disabled":

```powershell
# Run as Administrator
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Connection refused errors

```powershell
# Check if Ollama is running
Get-Process ollama

# Check if LM Studio server is running
Test-NetConnection -ComputerName localhost -Port 1234

# For Ollama
Test-NetConnection -ComputerName localhost -Port 11434
```
