#!/bin/bash
# Code Scanner Autostart Management - macOS (LaunchAgents)
# Usage: ./autostart-macos.sh [install|remove|status] "<cli_command>"
# Example: ./autostart-macos.sh install "/path/to/project1 -c /path/to/config1 /path/to/project2 -c /path/to/config2 --mode branch"

set -e

SERVICE_NAME="com.code-scanner"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$LAUNCH_AGENTS_DIR/$SERVICE_NAME.plist"
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

# Resolve the GNU coreutils timeout command. Stock macOS does NOT ship
# `timeout`; it is available as `gtimeout` via `brew install coreutils`.
# Echoes the command name (and returns 0), or returns 1 if unavailable.
resolve_timeout() {
    if command -v gtimeout &> /dev/null; then
        echo "gtimeout"
        return 0
    elif command -v timeout &> /dev/null; then
        echo "timeout"
        return 0
    fi
    return 1
}

reinstall_from_source() {
    if [[ ! -f "$PROJECT_ROOT/pyproject.toml" ]]; then
        print_warning "No pyproject.toml found at $PROJECT_ROOT. Skipping reinstall."
        return 1
    fi

    print_info "Reinstalling code-scanner from source: $PROJECT_ROOT"

    # The on-PATH binary is typically pipx-managed. Prefer pipx so the
    # installed entry point is actually updated. Fall back to uv/pip if needed.
    if command -v pipx &> /dev/null; then
        pipx install --force "$PROJECT_ROOT" 2>&1
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

# Verify the installed binary actually supports every long flag in cli_args.
# Emits a precise diagnostic when a flag is unsupported (stale install).
verify_cli_compatibility() {
    local cli_args="$1"
    local missing=()

    while read -r flag; do
        [[ -z "$flag" ]] && continue
        if ! supports_flag "$flag"; then
            missing+=("$flag")
        fi
    done < <(echo "$cli_args" | grep -oE -- '--[a-z][a-z0-9-]*' | sort -u)

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo ""
        print_error "Installed code-scanner does not support: ${missing[*]}"
        print_error "This usually means the installed binary is older than the source."
        print_error "Source: $PROJECT_ROOT"
        print_error "Re-install from source, for example:"
        print_error "  pipx install --force \"$PROJECT_ROOT\""
        return 1
    fi

    return 0
}

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

    local wrapper_script="$HOME/.code-scanner/launch-wrapper.sh"
    if [[ -f "$wrapper_script" ]]; then
        local exec_line
        exec_line=$(grep "^exec " "$wrapper_script" | sed 's/^exec //')
        # Strip "sleep 60" line is separate; exec line has the actual command
        # Strip the scanner binary (code-scanner or "uv run code-scanner")
        local cli_cmd
        cli_cmd=$(echo "$exec_line" | sed 's|^code-scanner ||' | sed 's|^uv run code-scanner ||')
        if [[ -n "$cli_cmd" ]]; then
            echo ""
            print_info "Current <cli_command> is \"$cli_cmd\""
        fi
    fi
}

usage() {
    echo "Code Scanner Autostart Management - macOS"
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
        which code-scanner
    elif command -v uv &> /dev/null; then
        echo "$(which uv) run code-scanner"
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

    # Resolve a timeout command (macOS ships gtimeout via coreutils, not timeout).
    local timeout_cmd
    if ! timeout_cmd=$(resolve_timeout); then
        print_warning "Neither 'gtimeout' nor 'timeout' found ('brew install coreutils' provides gtimeout)."
        print_warning "Skipping automated test launch; please verify the command runs manually."
        return 0
    fi

    # Run for 5 seconds, capture output
    local output
    output=$($timeout_cmd 5s $scanner_cmd $cli_args 2>&1 | head -30) || true

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

    if [[ -f "$PLIST_FILE" ]]; then
        local current_exec=""
        local wrapper_script="$HOME/.code-scanner/launch-wrapper.sh"
        if [[ -f "$wrapper_script" ]]; then
            # Extract the exec line from wrapper script (last line with actual command)
            current_exec=$(grep "^exec " "$wrapper_script" | sed 's/^exec //')
        fi

        if [[ -z "$current_exec" || "$current_exec" != "$new_exec" ]]; then
            print_warning "Found existing autostart configuration:"
            echo ""
            if [[ -n "$current_exec" ]]; then
                echo "  Current: $current_exec"
                echo "  New:     $new_exec"
            else
                echo "  Existing plist: $PLIST_FILE"
            fi
            echo ""
            read -p "Replace existing configuration? (y/N): " response
            if [[ ! "$response" =~ ^[Yy]$ ]]; then
                print_info "Installation cancelled."
                exit 0
            fi

            # Unload old service before replacing
            print_info "Unloading existing service..."
            launchctl unload "$PLIST_FILE" 2>/dev/null || true
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
    # Non-fatal: if reinstall fails we still verify compatibility below.
    reinstall_from_source || print_warning "Reinstall skipped or failed; continuing with the current binary."

    # Verify the installed binary supports every flag in the command.
    if ! verify_cli_compatibility "$cli_args"; then
        exit 1
    fi

    # Test launch first
    test_launch "$scanner_cmd" "$cli_args"

    # Check for legacy configuration
    check_legacy "$scanner_cmd $cli_args"

    # Create LaunchAgents directory
    mkdir -p "$LAUNCH_AGENTS_DIR"

    # Create wrapper script with 60-second delay
    local wrapper_script="$HOME/.code-scanner/launch-wrapper.sh"
    mkdir -p "$(dirname "$wrapper_script")"
    cat > "$wrapper_script" << EOF
#!/bin/bash
# Code Scanner launch wrapper with startup delay
sleep 60
exec $scanner_cmd $cli_args
EOF
    chmod +x "$wrapper_script"

    # Create plist file
    print_info "Creating LaunchAgent plist..."
    cat > "$PLIST_FILE" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$SERVICE_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$wrapper_script</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$HOME/.code-scanner/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.code-scanner/launchd-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PATH</key>
        <string>$PATH</string>
    </dict>
</dict>
</plist>
EOF

    print_success "Created plist file: $PLIST_FILE"

    # Load the service
    print_info "Loading LaunchAgent..."
    launchctl load "$PLIST_FILE"

    print_success "Code Scanner autostart installed successfully!"
    echo ""
    print_info "Useful commands:"
    echo "  launchctl list | grep code-scanner   # Check if running"
    echo "  launchctl unload \"$PLIST_FILE\"     # Stop service"
    echo "  launchctl load \"$PLIST_FILE\"       # Start service"
    echo "  cat ~/.code-scanner/launchd-*.log   # View logs"
}

remove_service() {
    if [[ ! -f "$PLIST_FILE" ]]; then
        print_warning "No autostart service found."
        exit 0
    fi

    print_info "Unloading LaunchAgent..."
    launchctl unload "$PLIST_FILE" 2>/dev/null || true

    print_info "Removing plist file..."
    rm -f "$PLIST_FILE"
    rm -f "$HOME/.code-scanner/launch-wrapper.sh"

    print_success "Code Scanner autostart removed."
}

show_status() {
    if [[ -f "$PLIST_FILE" ]]; then
        print_info "Plist file: $PLIST_FILE"
        echo ""
        echo "LaunchAgent status:"
        launchctl list | grep -E "code-scanner|$SERVICE_NAME" || echo "  Not currently loaded"
    else
        print_warning "No autostart service configured."
    fi
}

# Main
show_installed_info

case "${1:-}" in
    install)
        install_service "$2"
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
