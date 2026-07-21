# Linux Setup Guide

This guide covers setting up Code Scanner on Linux distributions.

## Python Installation

### Ubuntu/Debian

```bash
# Update package list
sudo apt update

# Install Python 3.10+
sudo apt install python3 python3-pip python3-venv

# Verify installation
python3 --version  # Should be 3.10 or higher
```

### Fedora

```bash
# Install Python
sudo dnf install python3 python3-pip

# Verify installation
python3 --version
```

### Arch Linux

```bash
# Install Python
sudo pacman -S python python-pip

# Verify installation
python --version
```

## Git Installation

Git is required for tracking file changes in your repositories.

### Ubuntu/Debian

```bash
sudo apt install git
git --version
```

### Fedora

```bash
sudo dnf install git
git --version
```

### Arch Linux

```bash
sudo pacman -S git
git --version
```

## Universal Ctags Installation

Universal Ctags is required for symbol indexing, which enables AI tools to efficiently navigate your codebase.

### Ubuntu/Debian

```bash
sudo apt install universal-ctags
ctags --version  # Should show "Universal Ctags"
```

### Fedora

```bash
sudo dnf install ctags
ctags --version
```

### Arch Linux

```bash
sudo pacman -S ctags
ctags --version
```

> **Note**: Make sure it's "Universal Ctags" (not "Exuberant Ctags"). Check with `ctags --version`.

## Ripgrep Installation

Ripgrep is required for fast code search across the repository.

### Ubuntu/Debian

```bash
sudo apt install ripgrep
rg --version
```

### Fedora

```bash
sudo dnf install ripgrep
rg --version
```

### Arch Linux

```bash
sudo pacman -S ripgrep
rg --version
```

## UV Installation

UV is the recommended package manager:

```bash
# Install UV
curl -LsSf https://astral.sh/uv/install.sh | sh

# Add to PATH (if not automatic)
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

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

### Option 1: Ollama (Recommended)

Ollama is lightweight and easy to use on Linux.

```bash
# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# Start Ollama (runs as a service on most distros)
ollama serve &

# Pull a model
ollama pull qwen3:4b

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

LM Studio provides a GUI and is great for trying different models.

```bash
# Download the AppImage from https://lmstudio.ai
# Example for version 0.3.x:
wget https://releases.lmstudio.ai/linux/x86_64/LM-Studio-x.x.x.AppImage

# Make it executable
chmod +x LM-Studio-*.AppImage

# Run LM Studio
./LM-Studio-*.AppImage
```

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

# Create code_scanner_config.toml (see sample_configs/)
cp /path/to/code-scanner/sample_configs/python-config.toml code_scanner_config.toml

# Run the scanner (runs continuously until Ctrl+C)
uv run code-scanner
```

## Running as a Background Service

You can configure Code Scanner to start automatically on login using systemd user services.

### Prerequisites

- Code Scanner installed and working via command line
- systemd user services available (most modern Linux distributions)
- LLM backend (Ollama or LM Studio) installed

### Quick Setup

Run the autostart script with `install` and pass the **full CLI command** (the same arguments you would pass to `code-scanner) as a single quoted string:

```bash
./scripts/autostart-linux.sh install "/path/to/project -c /path/to/config.toml"
```

Multiple projects and modes are supported in one command:

```bash
./scripts/autostart-linux.sh install "/path/to/project1 -c /path/to/config1.toml /path/to/project2 -c /path/to/config2.toml --mode branch"
```

The script will:

1. **Reinstall from source** — rebuilds and reinstalls code-scanner from the project source (preferring pipx, falling back to `uv`/`pip`), so the service runs the latest version.
2. **Verify CLI compatibility** — checks the installed binary supports every flag in your command and prints a clear diagnostic if the install is stale (e.g. missing `--mode`).
3. **Test launch** the scanner to verify the command works.
4. **Register the systemd service** at `~/.config/systemd/user/code-scanner.service`.

Other commands: `./scripts/autostart-linux.sh remove` (uninstall) and `./scripts/autostart-linux.sh status`.

### What the Script Does

1. **Reinstalls** code-scanner from source (pipx/uv/pip)
2. **Verifies** the installed binary supports all CLI flags used
3. **Detects legacy services** and offers to replace them
4. **Test launches** the scanner to verify configuration
5. **Creates systemd service** at `~/.config/systemd/user/code-scanner.service`
6. **Enables autostart** on user login
7. **Includes 60-second delay** to allow LLM backend startup

### Managing the Service

```bash
# Check status
systemctl --user status code-scanner

# View logs
journalctl --user -u code-scanner -f

# Stop service
systemctl --user stop code-scanner

# Restart service
systemctl --user restart code-scanner

# Disable autostart
systemctl --user disable code-scanner

# Remove service completely
systemctl --user stop code-scanner
systemctl --user disable code-scanner
rm ~/.config/systemd/user/code-scanner.service
systemctl --user daemon-reload
```

### Autostart Log Files

- **Scanner log:** `~/.code-scanner/code_scanner.log`
- **Results:** `<project>/code_scanner_results.md`
- **systemd logs:** `journalctl --user -u code-scanner`

### Autostart Troubleshooting

**Service won't start:**
1. Check logs: `journalctl --user -u code-scanner -f`
2. Verify config file path is correct
3. Ensure LLM backend is running
4. Try increasing the startup delay in the service file

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

**User services not working:**
Enable lingering for your user:

```bash
loginctl enable-linger $USER
```

## Troubleshooting

### "Permission denied" for Ollama

```bash
# Add yourself to the ollama group
sudo usermod -aG ollama $USER

# Log out and back in, or:
newgrp ollama
```

### LM Studio AppImage won't run

```bash
# Install FUSE (required for AppImages)
# Ubuntu/Debian:
sudo apt install fuse libfuse2

# Fedora:
sudo dnf install fuse fuse-libs
```

### Out of memory errors

Large models require significant RAM. Try:
- Use a smaller model (7B instead of 13B)
- Close other applications
- Use Ollama's memory-efficient quantized models

### Slow model loading

First load is slow due to model loading. Subsequent queries are faster.
Consider keeping Ollama running as a service.
