#!/bin/bash
# Code Scanner Autostart Management - Linux (systemd)
# Usage: ./autostart-linux.sh [install|remove|status] "<cli_command>"
# Example: ./autostart-linux.sh install "/path/to/project1 -c /path/to/config1 /path/to/project2 -c /path/to/config2 --mode branch"

set -e

SERVICE_NAME="code-scanner"
USER_SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$USER_SERVICE_DIR/$SERVICE_NAME.service"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

show_installed_info() {
    if command -v code-scanner &> /dev/null; then
        local installed_path
        installed_path=$(command -v code-scanner)
        local installed_version
        installed_version=$(code-scanner --version 2>/dev/null || echo "unknown")
        print_info "Installed code-scanner: $installed_path ($installed_version)"
    else
        print_info "code-scanner is not currently installed on PATH."
    fi

    if [[ -f "$SERVICE_FILE" ]]; then
        local exec_line
        exec_line=$(grep "^ExecStart=" "$SERVICE_FILE" | cut -d= -f2-)
        if [[ "$exec_line" == /bin/bash* ]]; then
            local inner
            inner=$(echo "$exec_line" | sed "s|/bin/bash -c '||" | sed "s|'$||")
            inner=$(echo "$inner" | sed "s|^sleep [0-9]* && ||")
            local cli_cmd
            cli_cmd=$(echo "$inner" | sed 's|^code-scanner ||' | sed 's|^uv run code-scanner ||')
            echo ""
            print_info "Current <cli_command> is \"$cli_cmd\""
        fi
    fi
}

reinstall_from_source() {
    if [[ ! -f "$PROJECT_ROOT/pyproject.toml" ]]; then
        print_warning "No pyproject.toml found at $PROJECT_ROOT. Skipping reinstall."
        return 1
    fi

    print_info "Reinstalling code-scanner from source: $PROJECT_ROOT"

    # The on-PATH binary is typically pipx-managed. Prefer pipx so the
    # installed entry point is actually updated. Fall back to uv/pip into the
    # ambient environment if pipx is not available.
    if command -v pipx &> /dev/null; then
        if pipx list 2>/dev/null | grep -q "package code-scanner"; then
            # Reinstall from local source (not from PyPI)
            pipx install --force "$PROJECT_ROOT" 2>&1
        else
            pipx install --force "$PROJECT_ROOT" 2>&1
        fi
        local rc=$?
    elif command -v uv &> /dev/null; then
        print_warning "pipx not found; falling back to 'uv pip install' into the active environment."
        uv pip install "$PROJECT_ROOT" 2>&1
        local rc=$?
    elif command -v pip3 &> /dev/null; then
        print_warning "Neither pipx nor uv found; falling back to 'pip3 install'."
        pip3 install --user "$PROJECT_ROOT" 2>&1
        local rc=$?
    else
        print_error "Could not find pipx, uv, or pip3 to reinstall code-scanner."
        return 1
    fi

    if [[ $rc -ne 0 ]]; then
        print_error "Failed to reinstall code-scanner from source (exit code $rc)."
        return 1
    fi

    local new_version
    new_version=$(code-scanner --version 2>/dev/null || echo "unknown")
    print_success "Reinstalled code-scanner: $new_version"
    return 0
}

# Detect whether the installed binary supports a required flag by scanning
# its --help output. Returns 0 (supported) or 1 (not supported / no binary).
supports_flag() {
    local flag="$1"
    command -v code-scanner &> /dev/null || return 1
    code-scanner --help 2>&1 | grep -q -- "$flag"
}

# Verify the installed binary actually parses the requested CLI args. Runs the
# exact command with --help-style probing of every long flag found in cli_args.
# Emits a precise diagnostic when a flag is unsupported (stale install).
verify_cli_compatibility() {
    local cli_args="$1"

    # Extract long flags (--mode) and short clusterable flags (-c, -d) from the
    # user command so we can probe the installed binary's --help for each.
    local flags_found=0
    local missing=()

    # Long flags: --mode, --commit, --debug, --version
    while read -r flag; do
        [[ -z "$flag" ]] && continue
        flags_found=1
        if ! supports_flag "$flag"; then
            missing+=("$flag")
        fi
    done < <(echo "$cli_args" | grep -oE -- '--[a-z][a-z0-9-]*' | sort -u)

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo ""
        print_error "Installed code-scanner does not support: ${missing[*]}"
        print_error "This usually means the installed binary is older than the source."
        print_error "Source: $PROJECT_ROOT  ($(stat -c '%y' "$PROJECT_ROOT/src/code_scanner/cli.py" 2>/dev/null | cut -d. -f1))"
        print_error "Re-install from source, for example:"
        print_error "  pipx install --force \"$PROJECT_ROOT\""
        return 1
    fi

    return 0
}

usage() {
    echo "Code Scanner Autostart Management - Linux"
    echo ""
    echo "Usage: $0 <command> \"<cli_command>\""
    echo ""
    echo "Commands:"
    echo "  install \"<cli_command>\"  Install autostart service with full CLI command"
    echo "  remove                      Remove autostart service"
    echo "  status                      Check service status"
    echo ""
    echo "Examples:"
    echo "  $0 install \"/path/to/project1 -c /path/to/config1 /path/to/project2 -c /path/to/config2 --mode branch\""
    echo "  $0 remove"
    echo "  $0 status"
    exit 1
}

find_code_scanner() {
    # Try to find code-scanner executable
    if command -v code-scanner &> /dev/null; then
        echo "code-scanner"
    elif command -v uv &> /dev/null; then
        echo "uv run code-scanner"
    else
        print_error "Could not find code-scanner or uv. Please install code-scanner first."
        exit 1
    fi
}

test_launch() {
    local scanner_cmd="$1"
    local cli_args="$2"

    print_info "Testing code-scanner launch..."
    print_info "Command: $scanner_cmd $cli_args"
    echo ""

    # Run for 5 seconds, capture output
    local output
    output=$(timeout 5s $scanner_cmd $cli_args 2>&1 | head -30) || true

    echo "$output"
    echo ""

    # Check for success indicators
    if echo "$output" | grep -q "Scanner running\|Scanner loop started\|Scanner thread started"; then
        print_success "Test launch succeeded - scanner started correctly."
        return 0
    fi

    # Check for common error patterns
    if echo "$output" | grep -qi "error\|failed\|exception\|traceback\|could not\|cannot\|refused"; then
        print_error "Test launch failed. Please fix the issues above and try again."
        exit 1
    fi

    # No clear success or failure - warn but continue
    print_warning "Could not automatically verify launch success."
    print_warning "Please check the output above and ensure code-scanner starts correctly."
    read -p "Continue with installation? (y/N): " response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        print_error "Installation cancelled."
        exit 1
    fi
}

check_legacy() {
    local new_exec="$1"

    if [[ -f "$SERVICE_FILE" ]]; then
        local current_exec
        # Extract command from ExecStart (may be wrapped in /bin/bash -c '...')
        current_exec=$(grep "^ExecStart=" "$SERVICE_FILE" | cut -d= -f2-)
        # Try to extract the inner command from bash -c '...'
        if [[ "$current_exec" == /bin/bash* ]]; then
            current_exec=$(echo "$current_exec" | sed "s|/bin/bash -c '||" | sed "s|'$||")
        fi

        if [[ "$current_exec" != "$new_exec" ]]; then
            print_warning "Found existing autostart with different configuration:"
            echo ""
            echo "  Current: $current_exec"
            echo "  New:     $new_exec"
            echo ""
            read -p "Replace existing configuration? (y/N): " response
            if [[ ! "$response" =~ ^[Yy]$ ]]; then
                print_info "Installation cancelled."
                exit 0
            fi

            # Stop old service before replacing
            print_info "Stopping existing service..."
            systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
        fi
    fi
}

install_service() {
    local cli_args="$1"

    if [[ -z "$cli_args" ]]; then
        print_error "Missing CLI command. Usage: $0 install \"<cli_command>\""
        exit 1
    fi

    # Find code-scanner
    local scanner_cmd
    scanner_cmd=$(find_code_scanner)

    # Reinstall from source first, so the on-PATH binary matches the source
    # (the install otherwise launches whatever stale binary is present).
    # Non-fatal: if reinstall fails we still verify compatibility below and
    # fail there with a precise message.
    reinstall_from_source || print_warning "Reinstall skipped or failed; continuing with the current binary."

    # Verify the installed binary actually supports every flag in the command.
    # This catches the common "stale install missing --mode" case with a clear,
    # actionable error instead of a confusing argparse failure during test_launch.
    if ! verify_cli_compatibility "$cli_args"; then
        exit 1
    fi

    # Build the full command with 60-second delay
    local exec_start="sleep 60 && $scanner_cmd $cli_args"

    # Test launch first
    test_launch "$scanner_cmd" "$cli_args"

    # Check for legacy configuration
    check_legacy "$exec_start"

    # Create service directory
    mkdir -p "$USER_SERVICE_DIR"

    # Create systemd service file
    print_info "Creating systemd service file..."
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Code Scanner - AI-driven code analysis
After=network.target

[Service]
Type=simple
ExecStart=/bin/bash -c '$exec_start'
Restart=no
Environment=HOME=$HOME
Environment=PATH=$PATH

[Install]
WantedBy=default.target
EOF

    print_success "Created service file: $SERVICE_FILE"

    # Reload systemd and enable service
    print_info "Enabling and starting service..."
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"

    print_success "Code Scanner autostart installed successfully!"
    echo ""
    print_info "Useful commands:"
    echo "  systemctl --user status $SERVICE_NAME  # Check status"
    echo "  systemctl --user stop $SERVICE_NAME    # Stop service"
    echo "  systemctl --user start $SERVICE_NAME   # Start service"
    echo "  journalctl --user -u $SERVICE_NAME     # View logs"
}

remove_service() {
    if [[ ! -f "$SERVICE_FILE" ]]; then
        print_warning "No autostart service found."
        exit 0
    fi

    print_info "Stopping and disabling service..."
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true

    print_info "Removing service file..."
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload

    print_success "Code Scanner autostart removed."
}

show_status() {
    if [[ -f "$SERVICE_FILE" ]]; then
        print_info "Service file: $SERVICE_FILE"
        echo ""
        systemctl --user status "$SERVICE_NAME" || true
    else
        print_warning "No autostart service configured."
    fi
}

# Main
show_installed_info

case "${1:-}" in
    install)
        shift
        install_service "$*"
        ;;
    remove)
        remove_service
        ;;
    status)
        show_status
        ;;
    *)
        usage
        ;;
esac
