# macOS Setup Guide

This guide covers setting up Code Scanner on macOS.

## Python Installation

### Using Homebrew (Recommended)

```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python
brew install python@3.12

# Verify installation
python3 --version  # Should be 3.10 or higher
```

### Using python.org Installer

1. Download from [python.org](https://www.python.org/downloads/macos/)
2. Run the installer
3. Follow the installation prompts

## Git Installation

Git is usually pre-installed on macOS. If not:

```bash
# Install Git using Homebrew
brew install git

# Verify installation
git --version
```

Or install Xcode Command Line Tools:

```bash
xcode-select --install
```

## Universal Ctags Installation

Universal Ctags is required for symbol indexing, which enables AI tools to efficiently navigate your codebase.

```bash
# Install using Homebrew
brew install universal-ctags

# Verify installation (should show "Universal Ctags")
ctags --version
```

> **Note**: Make sure it's "Universal Ctags" (not "Exuberant Ctags"). The macOS built-in `ctags` is Exuberant, so always use Homebrew's version.

## Ripgrep Installation

Ripgrep is required for fast code search across the repository.

```bash
# Install using Homebrew
brew install ripgrep

# Verify installation
rg --version
```

## UV Installation

```bash
# Install UV
curl -LsSf https://astral.sh/uv/install.sh | sh

# For Apple Silicon Macs, add to PATH if needed
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# Verify installation
uv --version
```

## Code Scanner Installation

```bash
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

### Option 1: Ollama (Recommended for Apple Silicon)

Ollama runs natively on Apple Silicon and provides excellent performance.

```bash
# Install with Homebrew
brew install ollama

# Or download from https://ollama.ai/download

# Start Ollama (runs automatically after install)
ollama serve &

# Pull a model
ollama pull qwen3:4b

# For Apple Silicon, you can use larger models efficiently:
# ollama pull llama3:70b  # If you have 64GB+ RAM

# Verify it's working
ollama list
curl http://localhost:11434/api/tags
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

LM Studio provides a native macOS app with a great GUI.

1. Download from [lmstudio.ai](https://lmstudio.ai)
2. Open the `.dmg` file
3. Drag LM Studio to Applications
4. Launch LM Studio

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

```bash
# Navigate to your project
cd /path/to/your/project

# Create code_scanner_config.toml
cp /path/to/code-scanner/sample_configs/python-config.toml code_scanner_config.toml

# Run the scanner (runs continuously until Ctrl+C)
uv run code-scanner
```

## Apple Silicon Optimization

Apple Silicon Macs (M1/M2/M3) can run LLMs very efficiently:

### Ollama Performance Tips

```bash
# For M1/M2/M3 Macs with 16GB+ RAM, try larger models if needed:
ollama pull qwen3:4b      # Lightweight (Recommended)
ollama pull llama3.1:8b    # Latest version
ollama pull codellama:13b  # Better for code analysis

# Check GPU utilization
ollama ps
```

### LM Studio Performance Tips

- Use models optimized for Metal (Apple GPU)
- Enable GPU acceleration in settings
- Look for "GGUF" format models

## Running as a Background Service

You can configure Code Scanner to start automatically on login using macOS LaunchAgents.

### Prerequisites

- Code Scanner installed and working via command line
- LLM backend (Ollama or LM Studio) installed
- *(Optional)* **`gtimeout`** via `brew install coreutils` — used by the script's automated test launch. Stock macOS does not ship `timeout`; without `gtimeout` the test-launch step is skipped (the service is still installed). 

### Quick Setup

Run the autostart script with `install` and pass the **full CLI command** (the same arguments you would pass to `code-scanner`) as a single quoted string:

```bash
./scripts/autostart-macos.sh install "/path/to/project -c /path/to/config.toml"
```

Multiple projects and modes are supported in one command:

```bash
./scripts/autostart-macos.sh install "/path/to/project1 -c /path/to/config1.toml /path/to/project2 -c /path/to/config2.toml --mode branch"
```

The script will:

1. **Reinstall from source** — rebuilds and reinstalls code-scanner from the project source (preferring pipx, falling back to `uv`/`pip`), so the service runs the latest version.
2. **Verify CLI compatibility** — checks the installed binary supports every flag in your command and prints a clear diagnostic if the install is stale.
3. **Test launch** the scanner to verify the command works (requires `gtimeout`/`timeout`).
4. **Register the LaunchAgent** at `~/Library/LaunchAgents/com.code-scanner.plist`.

Other commands: `./scripts/autostart-macos.sh remove` (uninstall) and `./scripts/autostart-macos.sh status`.

### What the Script Does

1. **Reinstalls** code-scanner from source (pipx/uv/pip)
2. **Verifies** the installed binary supports all CLI flags used
3. **Detects legacy LaunchAgents** and offers to remove them
4. **Test launches** the scanner to verify configuration (requires `gtimeout`)
5. **Creates wrapper script** at `~/.code-scanner/launch-wrapper.sh`
6. **Creates LaunchAgent** at `~/Library/LaunchAgents/com.code-scanner.plist`
7. **Loads the agent** to start on login
8. **Includes 60-second delay** to allow LLM backend startup

### Managing the Service

```bash
# Check if running
launchctl list | grep code-scanner

# View logs
cat ~/.code-scanner/launchd-stdout.log
cat ~/.code-scanner/launchd-stderr.log

# Stop service
launchctl unload ~/Library/LaunchAgents/com.code-scanner.plist

# Start service
launchctl load ~/Library/LaunchAgents/com.code-scanner.plist

# Remove service completely
launchctl unload ~/Library/LaunchAgents/com.code-scanner.plist
rm ~/Library/LaunchAgents/com.code-scanner.plist
rm ~/.code-scanner/launch-wrapper.sh
```

### Autostart Log Files

- **Scanner log:** `~/.code-scanner/code_scanner.log`
- **Results:** `<project>/code_scanner_results.md`
- **LaunchAgent stdout:** `~/.code-scanner/launchd-stdout.log`
- **LaunchAgent stderr:** `~/.code-scanner/launchd-stderr.log`

### Autostart Troubleshooting

**Service won't start:**
1. Check logs: `cat ~/.code-scanner/launchd-stderr.log`
2. Verify config file path is correct
3. Ensure LLM backend is running
4. Check wrapper script is executable: `chmod +x ~/.code-scanner/launch-wrapper.sh`

**Lock file errors:**
Another instance may be running. Check with:

```bash
cat ~/.code-scanner/code_scanner.lock
ps aux | grep code-scanner
```

Delete stale lock if needed:

```bash
rm ~/.code-scanner/code_scanner.lock
```

**Permission issues:**
Ensure the wrapper script is executable:

```bash
chmod +x ~/.code-scanner/launch-wrapper.sh
```

## Troubleshooting

### "python3: command not found"

```bash
# If using Homebrew Python
echo 'export PATH="/opt/homebrew/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Ollama not starting automatically

```bash
# Check if Ollama is running
pgrep ollama

# Start manually
ollama serve

# Or restart the Ollama app
```

### Slow first model load

The first load downloads and initializes the model. Subsequent loads are faster.
Keep Ollama running in the background for best performance.

### "Cannot connect to backend" errors

```bash
# Check if Ollama is running
curl http://localhost:11434/api/tags

# Check if LM Studio server is running
curl http://localhost:1234/v1/models
```

### Memory issues with large models

- Close memory-intensive applications
- Use smaller quantized models (Q4_K_M instead of Q8)
- Monitor memory with Activity Monitor
