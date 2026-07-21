"""Tests for Ollama client module."""

import pytest
from unittest.mock import patch, MagicMock
import json
import urllib.error

from code_scanner.ollama_client import OllamaClient
from code_scanner.base_client import LLMClientError, ContextOverflowError
from code_scanner.models import LLMConfig


def _make_ollama_stream_response(content: str, done: bool = True) -> list[bytes]:
    """Build a streaming Ollama response from a final content string.

    Splits the content into individual chunks (one per character group)
    and appends a final ``done`` chunk — emulating Ollama's streaming API.
    """
    lines: list[bytes] = []
    chunk_size = max(1, len(content) // 4) if content else 10
    for i in range(0, len(content), chunk_size):
        piece = content[i:i + chunk_size]
        lines.append(
            json.dumps({"message": {"content": piece}, "done": False}).encode() + b"\n"
        )
    if done:
        lines.append(
            json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n"
        )
    else:
        lines.append(
            json.dumps({"message": {"content": content[-chunk_size:]}, "done": True}).encode() + b"\n"
        )
    return lines


def _make_ollama_stream_with_tools(tool_calls: list[dict]) -> list[bytes]:
    """Build a streaming Ollama response that returns tool calls."""
    lines: list[bytes] = []
    lines.append(
        json.dumps({"message": {"content": ""}, "done": False}).encode() + b"\n"
    )
    lines.append(
        json.dumps({
            "message": {"content": "", "tool_calls": tool_calls},
            "done": True,
        }).encode() + b"\n"
    )
    return lines


class TestOllamaClientInit:
    """Tests for OllamaClient initialization."""

    @pytest.fixture
    def ollama_config(self) -> LLMConfig:
        """Create Ollama config for testing."""
        return LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
        )

    def test_create_client(self, ollama_config: LLMConfig):
        """Test creating Ollama client."""
        client = OllamaClient(ollama_config)
        
        assert client.config == ollama_config
        assert client.backend_name == "Ollama"

    def test_backend_name(self, ollama_config: LLMConfig):
        """Test backend name property."""
        client = OllamaClient(ollama_config)
        assert client.backend_name == "Ollama"

    def test_initial_state(self, ollama_config: LLMConfig):
        """Test client initial state."""
        client = OllamaClient(ollama_config)
        
        # model_id should raise when not connected
        with pytest.raises(LLMClientError):
            _ = client.model_id


class TestOllamaClientConnect:
    """Tests for OllamaClient connection."""

    @pytest.fixture
    def ollama_config(self) -> LLMConfig:
        """Create Ollama config for testing."""
        return LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
        )

    def test_connect_requires_model(self):
        """Test that Ollama requires model to be specified."""
        # LLMConfig validates Ollama model requirement at creation time
        with pytest.raises(ValueError) as exc_info:
            LLMConfig(
                backend="ollama",
                host="localhost",
                port=11434,
                model="",  # Empty model
                timeout=120,
            )
        
        assert "model" in str(exc_info.value).lower()

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_connect_success(self, mock_urlopen, ollama_config: LLMConfig):
        """Test successful connection."""
        # First call for /api/tags
        tags_response = MagicMock()
        tags_response.read.return_value = json.dumps({
            "models": [{"name": "llama3:latest"}]
        }).encode()
        tags_response.__enter__ = MagicMock(return_value=tags_response)
        tags_response.__exit__ = MagicMock(return_value=False)
        
        # Second call for /api/show
        show_response = MagicMock()
        show_response.read.return_value = json.dumps({
            "modelinfo": {"num_ctx": 8192}
        }).encode()
        show_response.__enter__ = MagicMock(return_value=show_response)
        show_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.side_effect = [tags_response, show_response]
        
        client = OllamaClient(ollama_config)
        client.connect()
        
        assert client.model_id == "llama3"
        assert client.context_limit == 8192

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_connect_model_not_found(self, mock_urlopen, ollama_config: LLMConfig):
        """Test connection when model not found."""
        tags_response = MagicMock()
        tags_response.read.return_value = json.dumps({
            "models": [{"name": "other-model"}]  # Different model
        }).encode()
        tags_response.__enter__ = MagicMock(return_value=tags_response)
        tags_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.return_value = tags_response
        
        client = OllamaClient(ollama_config)
        
        with pytest.raises(LLMClientError) as exc_info:
            client.connect()
        
        assert "not found" in str(exc_info.value).lower()

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_connect_no_models_available(self, mock_urlopen, ollama_config: LLMConfig):
        """Test connection when no models available."""
        tags_response = MagicMock()
        tags_response.read.return_value = json.dumps({
            "models": []  # Empty models list
        }).encode()
        tags_response.__enter__ = MagicMock(return_value=tags_response)
        tags_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.return_value = tags_response
        
        client = OllamaClient(ollama_config)
        
        with pytest.raises(LLMClientError) as exc_info:
            client.connect()
        
        assert "no models available" in str(exc_info.value).lower()

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_connect_ollama_not_running(self, mock_urlopen, ollama_config: LLMConfig):
        """Test connection when Ollama is not running."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        
        client = OllamaClient(ollama_config)
        
        with pytest.raises(LLMClientError) as exc_info:
            client.connect()
        
        assert "ollama" in str(exc_info.value).lower()


class TestOllamaClientQuery:
    """Tests for OllamaClient query functionality."""

    @pytest.fixture
    def ollama_config(self) -> LLMConfig:
        """Create Ollama config for testing."""
        return LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
        )

    def test_query_without_connection_raises_error(self, ollama_config: LLMConfig):
        """Test that query without connection raises error."""
        client = OllamaClient(ollama_config)
        
        with pytest.raises(LLMClientError) as exc_info:
            client.query("system prompt", "user prompt")
        
        assert "not connected" in str(exc_info.value).lower()

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_returns_json(self, mock_urlopen, ollama_config: LLMConfig):
        """Test that query returns parsed JSON."""
        # Setup connected client state manually
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3:latest"
        client._context_limit = 8192
        
        response_json = json.dumps({
            "issues": [
                {
                    "file": "main.cpp",
                    "line_number": 10,
                    "description": "Memory leak",
                    "suggested_fix": "Use smart pointer",
                    "code_snippet": "int* p = new int;"
                }
            ]
        })
        stream_lines = _make_ollama_stream_response(response_json)
        
        query_response = MagicMock()
        query_response.__iter__ = MagicMock(return_value=iter(stream_lines))
        query_response.__enter__ = MagicMock(return_value=query_response)
        query_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.return_value = query_response
        
        result = client.query("system prompt", "user prompt")
        
        assert "issues" in result
        assert len(result["issues"]) == 1
        assert result["issues"][0]["file"] == "main.cpp"

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_strips_markdown_fences(self, mock_urlopen, ollama_config: LLMConfig):
        """Test that markdown fences are stripped from response."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3:latest"
        client._context_limit = 8192
        
        # Response wrapped in markdown fences
        response_content = '```json\n{"issues": []}\n```'
        stream_lines = _make_ollama_stream_response(response_content)
        
        query_response = MagicMock()
        query_response.__iter__ = MagicMock(return_value=iter(stream_lines))
        query_response.__enter__ = MagicMock(return_value=query_response)
        query_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.return_value = query_response
        
        result = client.query("system prompt", "user prompt")
        
        assert "issues" in result
        assert result["issues"] == []

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_retries_on_empty_response(self, mock_urlopen, ollama_config: LLMConfig):
        """Test that query retries on empty response."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3:latest"
        client._context_limit = 8192
        
        empty_lines = _make_ollama_stream_response("", done=True)
        empty_response = MagicMock()
        empty_response.__iter__ = MagicMock(return_value=iter(empty_lines))
        empty_response.__enter__ = MagicMock(return_value=empty_response)
        empty_response.__exit__ = MagicMock(return_value=False)
        
        valid_lines = _make_ollama_stream_response('{"issues": []}')
        valid_response = MagicMock()
        valid_response.__iter__ = MagicMock(return_value=iter(valid_lines))
        valid_response.__enter__ = MagicMock(return_value=valid_response)
        valid_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.side_effect = [empty_response, valid_response]
        
        result = client.query("system prompt", "user prompt")
        
        assert "issues" in result
        assert mock_urlopen.call_count == 2


class TestOllamaClientContextLimit:
    """Tests for context limit handling."""

    @pytest.fixture
    def ollama_config(self) -> LLMConfig:
        """Create Ollama config for testing."""
        return LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
        )

    def test_context_limit_from_config(self):
        """Test that context_limit from config is used."""
        config = LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
            context_limit=16384,
        )
        
        client = OllamaClient(config)
        client._connected = True
        client._model_id = "llama3:latest"
        client._context_limit = 16384
        
        assert client.context_limit == 16384




    def test_set_context_limit_manually(self, ollama_config: LLMConfig):
        """Test setting context limit manually."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3:latest"
        client._context_limit = None
        
        client.set_context_limit(32768)
        
        assert client.context_limit == 32768


    def test_set_context_limit_invalid_value(self, ollama_config: LLMConfig):
        """Test that invalid context limit raises error."""
        client = OllamaClient(ollama_config)
        
        with pytest.raises(ValueError):
            client.set_context_limit(0)
        
        with pytest.raises(ValueError):
            client.set_context_limit(-1000)

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_config_limit_exceeds_model_raises_error(self, mock_urlopen):
        """Test that config context_limit > model context_limit raises error."""
        config = LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
            context_limit=32768,  # Config limit higher than model
        )
        
        # First call for /api/tags - model found
        tags_response = MagicMock()
        tags_response.read.return_value = json.dumps({
            "models": [{"name": "llama3:latest"}]
        }).encode()
        tags_response.__enter__ = MagicMock(return_value=tags_response)
        tags_response.__exit__ = MagicMock(return_value=False)
        
        # Second call for /api/show - model has 8192 limit
        show_response = MagicMock()
        show_response.read.return_value = json.dumps({
            "modelinfo": {"num_ctx": 8192}
        }).encode()
        show_response.__enter__ = MagicMock(return_value=show_response)
        show_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.side_effect = [tags_response, show_response]
        
        client = OllamaClient(config)
        
        with pytest.raises(LLMClientError) as exc_info:
            client.connect()
        
        assert "context limit" in str(exc_info.value).lower()


class TestOllamaClientModelInfo:
    """Tests for model information retrieval."""

    @pytest.fixture
    def ollama_config(self) -> LLMConfig:
        """Create Ollama config for testing."""
        return LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
        )

    def test_model_id_property(self, ollama_config: LLMConfig):
        """Test model_id property."""
        client = OllamaClient(ollama_config)
        client._model_id = "llama3:latest"
        
        assert client.model_id == "llama3:latest"

    def test_model_id_not_connected(self, ollama_config: LLMConfig):
        """Test model_id when not connected raises error."""
        client = OllamaClient(ollama_config)
        
        with pytest.raises(LLMClientError):
            _ = client.model_id


class TestStripMarkdownFences:
    """Tests for strip_markdown_fences method."""

    @pytest.fixture
    def client(self) -> OllamaClient:
        """Create Ollama client for testing."""
        config = LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
        )
        return OllamaClient(config)

    def test_strip_json_fences(self, client: OllamaClient):
        """Test stripping ```json fences."""
        content = '```json\n{"issues": []}\n```'
        result = client.strip_markdown_fences(content)
        assert result == '{"issues": []}'

    def test_strip_plain_fences(self, client: OllamaClient):
        """Test stripping plain ``` fences."""
        content = '```\n{"issues": []}\n```'
        result = client.strip_markdown_fences(content)
        assert result == '{"issues": []}'

    def test_no_fences(self, client: OllamaClient):
        """Test content without fences is unchanged."""
        content = '{"issues": []}'
        result = client.strip_markdown_fences(content)
        assert result == '{"issues": []}'


class TestBuildUserPrompt:
    """Tests for build_user_prompt function."""

    def test_build_user_prompt(self):
        """Test building user prompt."""
        from code_scanner.base_client import build_user_prompt
        
        files_content = {
            "main.cpp": "int* p = new int[10];"
        }
        
        prompt = build_user_prompt(
            check_query="Check for memory leaks",
            files_content=files_content,
        )
        
        assert "Check for memory leaks" in prompt
        assert "int* p = new int[10];" in prompt
        assert "main.cpp" in prompt


class TestSystemPrompt:
    """Tests for system prompt template."""

    def test_system_prompt_exists(self):
        """Test that system prompt template exists."""
        from code_scanner.base_client import SYSTEM_PROMPT_TEMPLATE
        
        assert SYSTEM_PROMPT_TEMPLATE is not None
        assert len(SYSTEM_PROMPT_TEMPLATE) > 0
        assert "issues" in SYSTEM_PROMPT_TEMPLATE.lower()
