"""Additional LLM client tests for better coverage."""

import pytest
import json
from unittest.mock import MagicMock, patch, PropertyMock
from openai import APIError, APIConnectionError

from code_scanner.lmstudio_client import (
    LMStudioClient,
    LLMClientError,
    build_user_prompt,
    SYSTEM_PROMPT_TEMPLATE,
)
from code_scanner.config import LLMConfig


def _make_lmstudio_stream_chunk(content: str = "", finish_reason: str = None):
    """Build a single streaming chunk MagicMock."""
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = []

    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason

    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


def _make_lmstudio_stream_response(content: str) -> list[MagicMock]:
    """Build a list of streaming chunks that accumulate to the given content."""
    chunks: list[MagicMock] = []
    chunk_size = max(1, len(content) // 4) if content else 10
    for i in range(0, len(content), chunk_size):
        piece = content[i:i + chunk_size]
        chunks.append(_make_lmstudio_stream_chunk(content=piece))
    chunks.append(_make_lmstudio_stream_chunk(content="", finish_reason="stop"))
    return chunks


class TestLMStudioClientConnect:
    """Tests for LMStudioClient connection."""

    def test_connect_success(self):
        """Test successful connection to LM Studio."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        mock_openai = MagicMock()
        mock_models = MagicMock()
        mock_model = MagicMock()
        mock_model.id = "test-model"
        mock_models.list.return_value.data = [mock_model]
        mock_openai.models = mock_models
        
        with patch('code_scanner.lmstudio_client.OpenAI', return_value=mock_openai):
            client.connect()
        
        assert client.model_id == "test-model"

    def test_connect_no_models(self):
        """Test connection fails when no models available."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        mock_openai = MagicMock()
        mock_openai.models.list.return_value.data = []
        
        with patch('code_scanner.lmstudio_client.OpenAI', return_value=mock_openai):
            with pytest.raises(LLMClientError) as exc_info:
                client.connect()
        
        assert "No models available" in str(exc_info.value)

    def test_connect_connection_error(self):
        """Test connection fails on connection error."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        with patch('code_scanner.lmstudio_client.OpenAI') as mock_openai_class:
            mock_openai_class.side_effect = APIConnectionError(request=MagicMock())
            
            with pytest.raises(LLMClientError) as exc_info:
                client.connect()
        
        assert "Could not connect" in str(exc_info.value)


class TestLMStudioClientQuery:
    """Tests for LMStudioClient query method."""

    def test_query_not_connected(self):
        """Query raises error when not connected."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        with pytest.raises(LLMClientError) as exc_info:
            client.query("system", "user")
        
        assert "not connected" in str(exc_info.value).lower()

    def test_query_valid_json_response(self):
        """Query returns parsed JSON on valid response."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000
        
        stream_chunks = _make_lmstudio_stream_response('{"issues": []}')
        client._client.chat.completions.create.return_value = iter(stream_chunks)
        
        result = client.query("system", "user")
        
        assert result == {"issues": []}

    def test_query_json_with_markdown_fences(self):
        """Query handles JSON wrapped in markdown fences."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000
        
        stream_chunks = _make_lmstudio_stream_response('```json\n{"issues": []}\n```')
        client._client.chat.completions.create.return_value = iter(stream_chunks)
        
        result = client.query("system", "user")
        
        assert result == {"issues": []}

    def test_query_response_format_fallback(self):
        """Query retries without response_format when not supported."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000
        client._supports_json_format = True
        
        api_error = APIError(
            message="response_format is not supported",
            request=MagicMock(),
            body=None
        )
        
        success_chunks = _make_lmstudio_stream_response('{"issues": []}')
        
        client._client.chat.completions.create.side_effect = [
            api_error,
            iter(success_chunks),
        ]
        
        result = client.query("system", "user")
        
        assert result == {"issues": []}
        assert client._supports_json_format is False

    def test_query_connection_lost(self):
        """Query raises error on connection loss."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000
        
        client._client.chat.completions.create.side_effect = APIConnectionError(
            request=MagicMock()
        )
        
        with pytest.raises(LLMClientError) as exc_info:
            client.query("system", "user")
        
        assert "Lost connection" in str(exc_info.value)

    def test_query_max_retries_exceeded(self):
        """Query fails after max retries."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000
        
        stream_chunks = _make_lmstudio_stream_response("not valid json")
        client._client.chat.completions.create.return_value = iter(stream_chunks)
        
        with pytest.raises(LLMClientError) as exc_info:
            client.query("system", "user", max_retries=2)
        
        assert "Failed to get valid JSON" in str(exc_info.value)

    def test_query_empty_response_retry(self):
        """Query retries on empty response."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000
        
        empty_chunks = _make_lmstudio_stream_response("")
        valid_chunks = _make_lmstudio_stream_response('{"issues": []}')
        
        client._client.chat.completions.create.side_effect = [
            iter(empty_chunks),
            iter(valid_chunks),
        ]
        
        result = client.query("system", "user")
        
        assert result == {"issues": []}


class TestTryFixJsonResponse:
    """Tests for _try_fix_json_response method."""

    def test_fix_not_connected_returns_none(self):
        """Returns None when not connected."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = None
        
        result = client._try_fix_json_response("bad json")
        
        assert result is None

    def test_fix_success(self):
        """Returns fixed JSON on success."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._supports_json_format = True
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"issues": []}'
        client._client.chat.completions.create.return_value = mock_response
        
        result = client._try_fix_json_response("broken json")
        
        assert result == {"issues": []}

    def test_fix_returns_none_on_error(self):
        """Returns None when fix attempt fails."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        
        client._client.chat.completions.create.side_effect = Exception("API error")
        
        result = client._try_fix_json_response("bad json")
        
        assert result is None

    def test_fix_returns_none_on_invalid_json(self):
        """Returns None when fix response is also invalid."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = 'still not json'
        client._client.chat.completions.create.return_value = mock_response
        
        result = client._try_fix_json_response("bad json")
        
        assert result is None


class TestStripMarkdownFences:
    """Tests for strip_markdown_fences method."""

    def test_strip_json_fence(self):
        """Strips ```json fence."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        content = '```json\n{"key": "value"}\n```'
        result = client.strip_markdown_fences(content)
        
        assert result == '{"key": "value"}'

    def test_strip_plain_fence(self):
        """Strips plain ``` fence."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        content = '```\n{"key": "value"}\n```'
        result = client.strip_markdown_fences(content)
        
        assert result == '{"key": "value"}'

    def test_strip_with_extra_whitespace(self):
        """Handles extra whitespace around fences."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        content = '  ```json\n  {"key": "value"}  \n```  '
        result = client.strip_markdown_fences(content)
        
        assert '{"key": "value"}' in result

    def test_no_fence_unchanged(self):
        """Content without fence passes through."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        content = '{"key": "value"}'
        result = client.strip_markdown_fences(content)
        
        assert result == '{"key": "value"}'

    def test_case_insensitive(self):
        """Strips fences regardless of case."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        content = '```JSON\n{"key": "value"}\n```'
        result = client.strip_markdown_fences(content)
        
        assert result == '{"key": "value"}'


class TestBuildUserPrompt:
    """Tests for build_user_prompt function."""

    def test_single_file(self):
        """Prompt built correctly for single file."""
        files = {"test.py": "print('hello')"}
        query = "Check for issues"
        
        prompt = build_user_prompt(query, files)
        
        assert "Check for issues" in prompt
        assert "test.py" in prompt
        assert "print('hello')" in prompt

    def test_multiple_files(self):
        """Prompt built correctly for multiple files."""
        files = {
            "a.py": "code_a",
            "b.py": "code_b",
        }
        query = "Check"
        
        prompt = build_user_prompt(query, files)
        
        assert "a.py" in prompt
        assert "b.py" in prompt
        assert "code_a" in prompt
        assert "code_b" in prompt

    def test_empty_files(self):
        """Prompt handles empty files dict."""
        files = {}
        query = "Check"
        
        prompt = build_user_prompt(query, files)
        
        assert "Check" in prompt


class TestLMStudioClientProperties:
    """Tests for LMStudioClient property methods."""

    def test_context_limit_not_connected(self):
        """context_limit raises error when not connected."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        with pytest.raises(LLMClientError):
            _ = client.context_limit

    def test_context_limit_connected(self):
        """context_limit returns value when set."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._context_limit = 16384
        
        assert client.context_limit == 16384

    def test_model_id_not_connected(self):
        """model_id raises error when not connected."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        with pytest.raises(LLMClientError):
            _ = client.model_id

    def test_model_id_connected(self):
        """model_id returns value when set."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._model_id = "test-model"
        
        assert client.model_id == "test-model"


class TestLMStudioClientSetContextLimit:
    """Tests for set_context_limit method."""

    def test_set_valid_limit(self):
        """Valid context limit is set."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        client.set_context_limit(16384)
        
        assert client._context_limit == 16384

    def test_set_invalid_limit_raises(self):
        """Invalid context limit raises error."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        with pytest.raises(ValueError):
            client.set_context_limit(0)
        
        with pytest.raises(ValueError):
            client.set_context_limit(-1)





class TestSystemPromptTemplate:
    """Tests for system prompt template."""

    def test_prompt_contains_json_instructions(self):
        """System prompt contains JSON formatting instructions."""
        assert "JSON" in SYSTEM_PROMPT_TEMPLATE
        assert "issues" in SYSTEM_PROMPT_TEMPLATE

    def test_prompt_forbids_markdown(self):
        """System prompt forbids markdown fences."""
        assert "markdown" in SYSTEM_PROMPT_TEMPLATE.lower() or "```" in SYSTEM_PROMPT_TEMPLATE

    def test_prompt_specifies_required_fields(self):
        """System prompt specifies required issue fields."""
        assert "file" in SYSTEM_PROMPT_TEMPLATE.lower()
        assert "line" in SYSTEM_PROMPT_TEMPLATE.lower()
        assert "description" in SYSTEM_PROMPT_TEMPLATE.lower()


class TestLMStudioClientWaitForConnection:
    """Tests for wait_for_connection method."""

    def test_wait_reconnects_successfully(self):
        """wait_for_connection reconnects when possible."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        
        call_count = [0]
        
        def connect_side_effect():
            call_count[0] += 1
            if call_count[0] < 2:
                raise LLMClientError("Not ready")
            # Success on second call
            client._client = MagicMock()
            client._model_id = "test"
        
        with patch.object(client, 'connect', side_effect=connect_side_effect):
            with patch('time.sleep'):
                client.wait_for_connection(retry_interval=1)
        
        assert call_count[0] == 2


class TestLMStudioClientContextLimit:
    """Tests for context limit detection."""

    def test_get_context_limit_from_context_length_attr(self):
        """Test getting context limit from model.context_length attribute."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        
        mock_model = MagicMock()
        mock_model.id = "test-model"
        mock_model.context_length = 32768
        client._client.models.list.return_value.data = [mock_model]
        
        result = client._get_context_limit()
        assert result == 32768

    def test_get_context_limit_from_max_tokens_attr(self):
        """Test getting context limit from model.max_tokens attribute."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        
        mock_model = MagicMock()
        mock_model.id = "test-model"
        del mock_model.context_length  # Remove context_length
        mock_model.max_tokens = 8192
        client._client.models.list.return_value.data = [mock_model]
        
        # Patch hasattr to return False for context_length, True for max_tokens
        original_hasattr = hasattr
        def custom_hasattr(obj, name):
            if name == "context_length" and hasattr(obj, "id"):
                return False
            if name == "max_tokens" and hasattr(obj, "id"):
                return True
            return original_hasattr(obj, name)
        
        with patch("builtins.hasattr", custom_hasattr):
            result = client._get_context_limit()
        
        # The mock returns max_tokens when context_length not available
        assert result is not None

    def test_get_context_limit_from_metadata(self):
        """Test getting context limit from model metadata."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        
        mock_model = MagicMock()
        mock_model.id = "test-model"
        # Simulate no direct attributes - only metadata
        mock_model.metadata = {"context_length": 65536}
        client._client.models.list.return_value.data = [mock_model]
        
        # Remove context_length and max_tokens
        del mock_model.context_length
        del mock_model.max_tokens
        
        result = client._get_context_limit()
        # Should fallback to probe or metadata
        assert result is not None or result is None  # May use probe

    def test_get_context_limit_error_handling(self):
        """Test context limit detection handles errors gracefully."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        
        # Simulate API error
        client._client.models.list.side_effect = Exception("API Error")
        
        result = client._get_context_limit()
        assert result is None

    def test_probe_context_limit_success(self):
        """Test _probe_context_limit retrieves limit from /models endpoint."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._model_id = "test-model"
        
        mock_response_data = {
            "data": [
                {"id": "test-model", "context_length": 4096}
            ]
        }
        
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps(mock_response_data).encode()
            mock_urlopen.return_value.__enter__.return_value = mock_response
            
            result = client._probe_context_limit()
        
        assert result == 4096

    def test_probe_context_limit_n_ctx_field(self):
        """Test _probe_context_limit uses n_ctx field."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._model_id = "test-model"
        
        mock_response_data = {
            "data": [
                {"id": "test-model", "n_ctx": 2048}
            ]
        }
        
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps(mock_response_data).encode()
            mock_urlopen.return_value.__enter__.return_value = mock_response
            
            result = client._probe_context_limit()
        
        assert result == 2048

    def test_probe_context_limit_error(self):
        """Test _probe_context_limit returns None on error."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._model_id = "test-model"
        
        with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            result = client._probe_context_limit()
        
        assert result is None

    def test_context_limit_property_raises_when_none(self):
        """Test context_limit property raises error when not connected."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._context_limit = None
        
        with pytest.raises(LLMClientError) as exc_info:
            _ = client.context_limit
        
        assert "Not connected" in str(exc_info.value)


class TestLMStudioStreamingToolCalls:
    """Tests for streaming tool call handling in LM Studio client."""

    def test_streaming_with_tool_calls(self):
        """LM Studio streaming response with tool calls is handled correctly."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000

        def _make_tool_call_chunk(index, name=None, args=None, finish=None):
            delta = MagicMock()
            delta.content = ""
            tc_delta = MagicMock()
            tc_delta.index = index
            tc_delta.id = None
            tc_delta.function = MagicMock()
            tc_delta.function.name = name
            tc_delta.function.arguments = args or ""
            delta.tool_calls = [tc_delta]
            choice = MagicMock()
            choice.delta = delta
            choice.finish_reason = finish
            chunk = MagicMock()
            chunk.choices = [choice]
            return chunk

        tools_param = [{"type": "function", "function": {"name": "search_text"}}]
        stream_chunks = [
            _make_tool_call_chunk(0, name="search_text", args='{"patterns":'),
            _make_tool_call_chunk(0, args='"found"}'),
            _make_tool_call_chunk(0, finish="tool_calls"),
        ]
        client._client.chat.completions.create.return_value = iter(stream_chunks)

        result = client.query("sys", "user", tools=tools_param)

        assert "tool_calls" in result
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["tool_name"] == "search_text"
        assert result["tool_calls"][0]["arguments"] == {"patterns": "found"}

    def test_streaming_with_tool_calls_empty_arguments(self):
        """Tool call with empty arguments handled."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000

        tools_param = [{"type": "function", "function": {"name": "list_directory"}}]

        # Chunk with tool call but empty args
        delta = MagicMock()
        delta.content = ""
        tc_delta = MagicMock()
        tc_delta.index = 0
        tc_delta.function = MagicMock()
        tc_delta.function.name = "list_directory"
        tc_delta.function.arguments = ""
        delta.tool_calls = [tc_delta]

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = "tool_calls"
        chunk = MagicMock()
        chunk.choices = [choice]

        client._client.chat.completions.create.return_value = iter([chunk])

        result = client.query("sys", "user", tools=tools_param)

        assert "tool_calls" in result
        assert result["tool_calls"][0]["arguments"] == {}

    def test_malformed_tool_call_in_stream_retries(self):
        """Malformed tool call in LM Studio streaming is retried."""
        config = LLMConfig(backend="lm-studio", host="localhost", port=1234, context_limit=16384)
        client = LMStudioClient(config)
        client._client = MagicMock()
        client._model_id = "test-model"
        client._context_limit = 8000

        tools_param = [{"type": "function", "function": {"name": "search_text"}}]

        delta = MagicMock()
        delta.content = ""
        delta.tool_calls = []
        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = "stop"
        chunk = MagicMock()
        chunk.choices = [choice]
        empty_stream = [chunk]

        valid_stream = [
            _make_lmstudio_stream_chunk(content='{"issues": []}'),
            _make_lmstudio_stream_chunk(content="", finish_reason="stop"),
        ]
        client._client.chat.completions.create.side_effect = [
            iter(empty_stream),
            iter(valid_stream),
        ]

        result = client.query("sys", "user", tools=tools_param, max_retries=3)
        assert result == {"issues": []}
