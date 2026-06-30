"""Pytest configuration and fixtures."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import shutil
import tempfile
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

from code_scanner.config import Config
from code_scanner.models import LLMConfig, Issue, IssueStatus, CheckGroup
from code_scanner.issue_tracker import IssueTracker
from code_scanner.ctags_index import CtagsIndex, Symbol
from datetime import datetime


def pytest_addoption(parser):
    """Add custom pytest options."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that require LM Studio",
    )


def pytest_configure(config):
    """Add custom markers."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test requiring LM Studio"
    )


@pytest.fixture
def integration_enabled(request):
    """Check if integration tests are enabled."""
    return request.config.getoption("--run-integration")


@pytest.fixture
def skip_without_integration(integration_enabled):
    """Skip test if integration tests are not enabled."""
    if not integration_enabled:
        pytest.skip("Integration tests disabled. Use --run-integration to enable.")


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def git_repo(temp_dir: Path) -> Generator[Path, None, None]:
    """Create a temporary Git repository."""
    import subprocess
    
    # Initialize git repo
    subprocess.run(["git", "init"], cwd=temp_dir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=temp_dir,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=temp_dir,
        capture_output=True,
    )
    
    # Create initial commit
    readme = temp_dir / "README.md"
    readme.write_text("# Test Project\n")
    subprocess.run(["git", "add", "README.md"], cwd=temp_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=temp_dir,
        capture_output=True,
    )
    
    yield temp_dir


@pytest.fixture
def sample_config(temp_dir: Path) -> Config:
    """Create a sample configuration."""
    config_file = temp_dir / "config.toml"
    config_file.write_text("""
checks = [
    "Check for errors",
    "Check for style issues"
]

[llm]
backend = "lm-studio"
host = "localhost"
port = 1234
context_limit = 16384
""")
    
    from code_scanner.models import CheckGroup
    return Config(
        target_directory=temp_dir,
        config_file=config_file,
        check_groups=[CheckGroup(pattern="*", checks=["Check for errors", "Check for style issues"])],
        llm=LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384),
    )


@pytest.fixture
def lmstudio_config() -> LLMConfig:
    """Create an LM Studio LLMConfig for testing."""
    return LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)


@pytest.fixture
def ollama_config() -> LLMConfig:
    """Create an Ollama LLMConfig for testing."""
    return LLMConfig(backend="ollama", host="localhost", port=11434, model="llama3", context_limit=16384)


@pytest.fixture
def sample_issue() -> Issue:
    """Create a sample issue."""
    return Issue(
        file_path="src/main.cpp",
        line_number=10,
        description="Heap allocation used where stack would suffice",
        suggested_fix="Use stack allocation: MyClass obj;",
        check_query="Check for heap allocation issues",
        timestamp=datetime.now(),
        status=IssueStatus.OPEN,
        code_snippet="MyClass* obj = new MyClass();",
    )


@pytest.fixture
def issue_tracker() -> IssueTracker:
    """Create an empty issue tracker."""
    return IssueTracker()


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client."""
    client = MagicMock()
    client.is_connected.return_value = True
    client.context_limit = 8000
    client.model_id = "test-model"
    client.query.return_value = {"issues": []}
    return client


@pytest.fixture
def sample_qt_project_path() -> Path:
    """Get path to the sample Qt project."""
    return Path(__file__).parent / "sample_qt_project"


@pytest.fixture
def mock_config(temp_dir: Path) -> MagicMock:
    """Create a mock Config object for testing."""
    from code_scanner.utils import get_config_dir
    config_dir = get_config_dir()
    
    mock = MagicMock(spec=Config)
    mock.target_directory = temp_dir
    mock.config_file = temp_dir / "config.toml"
    mock.commit_hash = None
    mock.output_path = temp_dir / "output.md"
    mock.output_file = "output.md"
    mock.log_file = config_dir / "code_scanner.log"
    mock.lock_path = config_dir / "code_scanner.lock"
    mock.llm = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
    mock.check_groups = [CheckGroup(pattern="*", checks=["Check for errors", "Check for style issues"])]
    
    return mock


@pytest.fixture
def mock_ctags_index():
    """Create a mock CtagsIndex for testing without requiring ctags installed."""
    mock_index = MagicMock(spec=CtagsIndex)
    mock_index.target_directory = Path("/tmp/mock_repo")
    
    # Default behaviors - use correct method names from CtagsIndex
    mock_index.find_symbol.return_value = []
    mock_index.find_definitions.return_value = []
    mock_index.find_symbols_by_pattern.return_value = []
    mock_index.get_symbols_in_file.return_value = []
    mock_index.get_class_members.return_value = []
    mock_index.get_file_structure.return_value = {
        "file": "/tmp/mock_repo/test.py",
        "language": "Python",
        "symbols": [],
        "structure_summary": "",
    }
    mock_index.get_stats.return_value = {
        "total_symbols": 0,
        "files_indexed": 0,
        "symbols_by_kind": {},
        "languages": [],
    }
    
    return mock_index


@pytest.fixture
def mock_ctags_index_with_data(temp_repo):
    """Create a mock CtagsIndex with sample data matching temp_repo structure."""
    mock_index = MagicMock(spec=CtagsIndex)
    mock_index.target_directory = temp_repo
    
    # Sample entries matching temp_repo fixture structure
    sample_entries = {
        "main": [Symbol(
            name="main",
            file_path=str(temp_repo / "src" / "main.py"),
            line=1,
            kind="function",
            language="Python",
            scope=None,
            signature="()",
        )],
        "calculate_total": [Symbol(
            name="calculate_total",
            file_path=str(temp_repo / "src" / "utils" / "math.py"),
            line=1,
            kind="function",
            language="Python",
            scope=None,
            signature="(items)",
        )],
        "calculate_average": [Symbol(
            name="calculate_average",
            file_path=str(temp_repo / "src" / "utils" / "math.py"),
            line=4,
            kind="function",
            language="Python",
            scope=None,
            signature="(items)",
        )],
        "Helper": [Symbol(
            name="Helper",
            file_path=str(temp_repo / "src" / "utils" / "helpers.py"),
            line=1,
            kind="class",
            language="Python",
            scope=None,
            signature=None,
        )],
    }
    
    mock_index.tags = sample_entries
    
    def find_symbol(name, kind=None):
        entries = sample_entries.get(name, [])
        if kind:
            entries = [e for e in entries if e.kind == kind]
        return entries
    
    mock_index.find_symbol.side_effect = find_symbol
    mock_index.find_definitions.return_value = []
    mock_index.find_symbols_by_pattern.return_value = []
    
    def get_symbols_in_file(file_path, kind=None):
        result = []
        for entries in sample_entries.values():
            for e in entries:
                if e.file_path == str(file_path):
                    if kind is None or e.kind == kind:
                        result.append(e)
        return result
    
    mock_index.get_symbols_in_file.side_effect = get_symbols_in_file
    mock_index.get_class_members.return_value = []
    mock_index.get_file_structure.return_value = {
        "file": str(temp_repo / "test.py"),
        "language": "Python",
        "symbols": [],
        "structure_summary": "",
    }
    mock_index.get_stats.return_value = {
        "total_symbols": 4,
        "files_indexed": 3,
        "symbols_by_kind": {"function": 3, "class": 1},
        "languages": ["Python"],
    }
    
    return mock_index
    
    return mock_index


@pytest.fixture
def sample_cpp_files(temp_dir: Path) -> dict[str, str]:
    """Create sample C++ files in temp directory."""
    src_dir = temp_dir / "src"
    src_dir.mkdir()
    
    # Create main.cpp
    main_cpp = src_dir / "main.cpp"
    main_cpp.write_text("""
#include <iostream>

int main() {
    // Create object on heap
    std::string* msg = new std::string("Hello");
    std::cout << *msg << std::endl;
    delete msg;
    return 0;
}
""")
    
    # Create utils.h with implementation
    utils_h = src_dir / "utils.h"
    utils_h.write_text("""
#ifndef UTILS_H
#define UTILS_H

inline int add(int a, int b) {
    return a + b;
}

#endif
""")
    
    return {
        "src/main.cpp": main_cpp.read_text(),
        "src/utils.h": utils_h.read_text(),
    }


@pytest.fixture
def mock_dependencies(mock_config, mock_ctags_index):
    """Create mock dependencies for Scanner."""
    from code_scanner.models import Project, ScanStatus
    
    git_watcher = MagicMock()
    llm_client = MagicMock()
    llm_client.context_limit = 8000
    issue_tracker = MagicMock()
    issue_tracker.add_issues.return_value = 0
    issue_tracker.update_from_scan.return_value = (0, 0)
    issue_tracker.get_stats.return_value = {"total": 0}
    output_generator = MagicMock()
    
    # Create mock project with required attributes for Scanner
    mock_project = MagicMock(spec=Project)
    mock_project.scan_status = ScanStatus.RUNNING
    mock_project.current_check_index = 0
    mock_project.total_checks = 0
    mock_project.current_check_query = ""
    mock_project.error_message = ""
    mock_project.inactive_since = None
    mock_project.issue_tracker = issue_tracker
    mock_project.output_generator = output_generator
    # Additional attributes needed by Scanner.__init__ and _run_scan
    mock_project.scan_info = {}
    mock_project.last_scanned_files = set()
    mock_project.last_file_contents_hash = {}
    mock_project.last_scan_time = None
    
    return {
        "config": mock_config,
        "git_watcher": git_watcher,
        "llm_client": llm_client,
        "issue_tracker": issue_tracker,
        "output_generator": output_generator,
        "ctags_index": mock_ctags_index,
        "project": mock_project,
    }
