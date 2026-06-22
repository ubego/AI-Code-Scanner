
import pytest
from unittest.mock import patch, MagicMock
import json
import urllib.error
from code_scanner.ollama_client import OllamaClient, LLMClientError, ContextOverflowError
from code_scanner.models import LLMConfig


def _make_ollama_stream_response(content: str) -> list[bytes]:
    """Build a streaming Ollama response from final content."""
    lines: list[bytes] = []
    chunk_size = max(1, len(content) // 4) if content else 10
    for i in range(0, len(content), chunk_size):
        piece = content[i:i + chunk_size]
        lines.append(
            json.dumps({"message": {"content": piece}, "done": False}).encode() + b"\n"
        )
    lines.append(
        json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n"
    )
    return lines

class TestOllamaClientCoverage:
    """Additional tests for OllamaClient execution coverage."""

    @pytest.fixture
    def ollama_config(self) -> LLMConfig:
        return LLMConfig(
            backend="ollama",
            host="localhost",
            port=11434,
            model="llama3",
            timeout=120,
        )

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_get_model_context_limit_alternatives(self, mock_urlopen, ollama_config):
        """Test getting context limit from different fields in the response."""
        # Setup common mocks
        tags_response = MagicMock()
        tags_response.read.return_value = json.dumps({"models": [{"name": "llama3:latest"}]}).encode()
        tags_response.__enter__ = MagicMock(return_value=tags_response)
        tags_response.__exit__ = MagicMock(return_value=False)
        
        # Scenario 1: context_length in modelinfo
        resp1 = MagicMock()
        resp1.read.return_value = json.dumps({"modelinfo": {"context_length": 4096}}).encode()
        resp1.__enter__ = MagicMock(return_value=resp1)
        resp1.__exit__ = MagicMock(return_value=False)
        
        # Scenario 2: n_ctx in details
        resp2 = MagicMock()
        resp2.read.return_value = json.dumps({"details": {"n_ctx": 2048}}).encode()
        resp2.__enter__ = MagicMock(return_value=resp2)
        resp2.__exit__ = MagicMock(return_value=False)
        
        # Scenario 3: parameters string
        resp3 = MagicMock()
        resp3.read.return_value = json.dumps({
            "parameters": "stop \"<|end|>\"\nnum_ctx 1024\ntemperature 0.7"
        }).encode()
        resp3.__enter__ = MagicMock(return_value=resp3)
        resp3.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [tags_response, resp1, tags_response, resp2, tags_response, resp3]

        # Test Case 1
        client1 = OllamaClient(ollama_config)
        client1.connect()
        assert client1.context_limit == 4096

        # Test Case 2
        client2 = OllamaClient(ollama_config)
        client2.connect()
        assert client2.context_limit == 2048

        # Test Case 3
        client3 = OllamaClient(ollama_config)
        client3.connect()
        assert client3.context_limit == 1024

    @patch("code_scanner.ollama_client.time.sleep")
    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_wait_for_connection(self, mock_urlopen, mock_sleep, ollama_config):
        """Test wait_for_connection re-tries."""
        # First call raises URLError, second call succeeds
        error_side_effect = urllib.error.URLError("Connection refused")
        
        # Success mocks
        tags_response = MagicMock()
        tags_response.read.return_value = json.dumps({"models": [{"name": "llama3:latest"}]}).encode()
        tags_response.__enter__ = MagicMock(return_value=tags_response)
        tags_response.__exit__ = MagicMock(return_value=False)
        
        show_response = MagicMock()
        show_response.read.return_value = json.dumps({"modelinfo": {"num_ctx": 4096}}).encode()
        show_response.__enter__ = MagicMock(return_value=show_response)
        show_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [error_side_effect, tags_response, show_response]
        
        client = OllamaClient(ollama_config)
        client.wait_for_connection(retry_interval=1)
        
        assert mock_sleep.call_count == 1

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_json_fix_mechanism(self, mock_urlopen, ollama_config):
        """Test that malformed JSON is auto-fixed."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3"
        client._context_limit = 4096

        # 1. Malformed response (streaming — first call)
        malformed_lines = _make_ollama_stream_response("Here is the code: {issues: []")
        malformed_resp = MagicMock()
        malformed_resp.__iter__ = MagicMock(return_value=iter(malformed_lines))
        malformed_resp.__enter__ = MagicMock(return_value=malformed_resp)
        malformed_resp.__exit__ = MagicMock(return_value=False)

        # 2. Fix response from LLM (non-streaming — uses .read())
        fixed_resp = MagicMock()
        fixed_resp.read.return_value = json.dumps({
            "message": {"content": "{\"issues\": []}"}
        }).encode()
        fixed_resp.__enter__ = MagicMock(return_value=fixed_resp)
        fixed_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [malformed_resp, fixed_resp]

        result = client.query("sys", "user")
        assert result == {"issues": []}
        
    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_context_overflow_error(self, mock_urlopen, ollama_config):
        """Test handling of context overflow HTTP error."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3"
        client._context_limit = 4096

        # HTTP Error with context overflow message
        err_fp = MagicMock()
        err_fp.read.return_value = b'{"error": "model requires more context, context length exceeds limit"}'
        
        http_error = urllib.error.HTTPError(
            url="http://localhost",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=err_fp
        )
        
        mock_urlopen.side_effect = http_error

        with pytest.raises(ContextOverflowError) as exc_info:
            client.query("sys", "user")
        
        assert "context limit" in str(exc_info.value).lower()

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_connect_model_not_found(self, mock_urlopen, ollama_config):
        """Test connect raises error if model not found."""
        # Valid tags response, but model not in it
        tags_response = MagicMock()
        tags_response.read.return_value = json.dumps({"models": [{"name": "other-model"}]}).encode()
        tags_response.__enter__ = MagicMock(return_value=tags_response)
        tags_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.return_value = tags_response
        
        client = OllamaClient(ollama_config)
        with pytest.raises(LLMClientError) as exc_info:
            client.connect()
        
        assert "not found" in str(exc_info.value)

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_connect_connection_error(self, mock_urlopen, ollama_config):
        """Test connect raises error on connection failure."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        
        client = OllamaClient(ollama_config)
        with pytest.raises(LLMClientError) as exc_info:
            client.connect()
            
        assert "Could not connect" in str(exc_info.value)

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_timeout(self, mock_urlopen, ollama_config):
        """Test query handles timeouts and retries."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3"
        
        valid_lines = _make_ollama_stream_response("{}")
        valid_resp = MagicMock()
        valid_resp.__iter__ = MagicMock(return_value=iter(valid_lines))
        valid_resp.__enter__ = MagicMock(return_value=valid_resp)
        valid_resp.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.side_effect = [TimeoutError("timed out"), valid_resp]
        
        result = client.query("sys", "user")
        assert result == {}

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_fix_failure(self, mock_urlopen, ollama_config):
        """Test query raises error if JSON fix also fails."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3"
        
        malformed_lines = _make_ollama_stream_response("INVALID")
        malformed_resp = MagicMock()
        malformed_resp.__iter__ = MagicMock(return_value=iter(malformed_lines))
        malformed_resp.__enter__ = MagicMock(return_value=malformed_resp)
        malformed_resp.__exit__ = MagicMock(return_value=False)
        
        # Fix response still uses non-streaming (.read())
        malformed_fix_resp = MagicMock()
        malformed_fix_resp.read.return_value = json.dumps({
            "message": {"content": "STILL INVALID"}
        }).encode()
        malformed_fix_resp.__enter__ = MagicMock(return_value=malformed_fix_resp)
        malformed_fix_resp.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.side_effect = [
            malformed_resp, malformed_fix_resp,
            malformed_resp, malformed_fix_resp,
            malformed_resp, malformed_fix_resp
        ]
        
        with pytest.raises(LLMClientError) as exc_info:
            client.query("sys", "user", max_retries=3)
            
        assert "Failed to get valid JSON" in str(exc_info.value)

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_tool_calls(self, mock_urlopen, ollama_config):
        """Test query handles tool_calls in response."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3"
        
        tool_lines = [
            json.dumps({"message": {"content": ""}, "done": False}).encode() + b"\n",
            json.dumps({
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "search_text",
                                "arguments": {"patterns": ["test"]},
                            }
                        }
                    ],
                },
                "done": True,
            }).encode() + b"\n",
        ]
        
        tool_response = MagicMock()
        tool_response.__iter__ = MagicMock(return_value=iter(tool_lines))
        tool_response.__enter__ = MagicMock(return_value=tool_response)
        tool_response.__exit__ = MagicMock(return_value=False)
        
        mock_urlopen.return_value = tool_response
        
        tools = [{"type": "function", "function": {"name": "search_text"}}]
        result = client.query("sys", "user", tools=tools)
        
        assert "tool_calls" in result
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["tool_name"] == "search_text"

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_http_error_with_context_overflow(self, mock_urlopen, ollama_config):
        """Test query handles HTTP errors with context overflow in response."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3"
        
        # Create HTTPError with context overflow message
        error_response = MagicMock()
        error_response.read.return_value = b'{"error": "context length exceeds maximum"}'
        
        http_error = urllib.error.HTTPError(
            url="http://localhost/api/chat",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=error_response
        )
        
        mock_urlopen.side_effect = http_error
        
        # Should raise ContextOverflowError for context length errors
        with pytest.raises(ContextOverflowError):
            client.query("sys", "user")

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_general_exception_with_timeout_in_message(self, mock_urlopen, ollama_config):
        """Test query handles general exceptions with timeout in message."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3"
        
        # General exception with "timed out" in message
        mock_urlopen.side_effect = Exception("Connection timed out")
        
        # Should retry and eventually raise
        with pytest.raises(LLMClientError):
            client.query("sys", "user", max_retries=1)

    @patch("code_scanner.ollama_client.urllib.request.urlopen")
    def test_query_url_error_lost_connection(self, mock_urlopen, ollama_config):
        """Test query raises LLMClientError on URLError."""
        client = OllamaClient(ollama_config)
        client._connected = True
        client._model_id = "llama3"
        
        mock_urlopen.side_effect = urllib.error.URLError("Connection reset")
        
        with pytest.raises(LLMClientError) as exc_info:
            client.query("sys", "user")
        
        assert "Lost connection" in str(exc_info.value)
