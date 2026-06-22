"""Tests for streaming response validator."""

import pytest

from code_scanner.stream_validator import (
    StreamValidator,
    RunawayGenerationError,
    ResponseSizeExceededError,
    MAX_RESPONSE_BYTES,
)


class TestStreamValidatorBasics:
    """Tests for basic StreamValidator state tracking."""

    def test_initial_state(self):
        v = StreamValidator()
        assert v.total_bytes == 0
        assert v.total_chunks == 0
        assert v.content == ""

    def test_feed_single_chunk(self):
        v = StreamValidator()
        v.feed("hello")
        assert v.total_chunks == 1
        assert v.total_bytes > 0
        assert v.content == "hello"

    def test_feed_multiple_chunks(self):
        v = StreamValidator()
        v.feed("hello ")
        v.feed("world")
        assert v.total_chunks == 2
        assert v.content == "hello world"

    def test_reset(self):
        v = StreamValidator()
        v.feed("hello")
        v.feed(" world")
        assert v.content == "hello world"

        v.reset()
        assert v.total_bytes == 0
        assert v.total_chunks == 0
        assert v.content == ""

    def test_empty_chunks_not_counted_as_content(self):
        v = StreamValidator()
        v.feed("")
        assert v.total_chunks == 1
        assert v.total_bytes == 0
        assert v.content == ""

    def test_elapsed_time_positive(self):
        v = StreamValidator()
        v.feed("test")
        assert v.elapsed_seconds >= 0


class TestStreamValidatorSizeLimit:
    """Tests for response size limit enforcement."""

    def test_response_within_limit(self):
        v = StreamValidator(max_bytes=1000)
        v.feed("x" * 500)
        v.feed("y" * 400)
        assert v.total_bytes < 1000

    def test_response_exceeds_limit(self):
        v = StreamValidator(max_bytes=100)
        with pytest.raises(ResponseSizeExceededError) as exc_info:
            v.feed("x" * 50)
            v.feed("y" * 60)
        assert exc_info.value.size > 100
        assert exc_info.value.limit == 100

    def test_large_single_chunk_exceeds_limit(self):
        v = StreamValidator(max_bytes=10)
        with pytest.raises(ResponseSizeExceededError):
            v.feed("x" * 20)

    def test_default_limit_is_reasonable(self):
        v = StreamValidator()
        assert v._max_bytes == MAX_RESPONSE_BYTES
        assert v._max_bytes > 100000


class TestStreamValidatorRepeatDetection:
    """Tests for content repetition / loop detection."""

    def test_repeated_content_detected(self):
        v = StreamValidator(repeat_window=10, repeat_threshold=5)
        repeat_unit = "ABCDEFGHIJ"
        with pytest.raises(RunawayGenerationError) as exc_info:
            for _ in range(6):
                v.feed(repeat_unit)
        assert "content loop detected" in str(exc_info.value)

    def test_non_repeating_content_passes(self):
        v = StreamValidator(repeat_window=10, repeat_threshold=5)
        v.feed("A" * 10)
        v.feed("B" * 10)
        v.feed("C" * 10)
        v.feed("D" * 10)
        v.feed("E" * 10)
        assert v.content == "A" * 10 + "B" * 10 + "C" * 10 + "D" * 10 + "E" * 10

    def test_content_too_short_for_repeat_check(self):
        v = StreamValidator(repeat_window=100, repeat_threshold=3)
        v.feed("short")
        assert v.content == "short"

    def test_varied_content_no_repeat(self):
        v = StreamValidator(repeat_window=20, repeat_threshold=5)
        for i in range(10):
            v.feed(f"chunk_{i}_" + "x" * 15)
        assert "chunk_0" in v.content

    def test_exact_repeat_threshold_not_exceeded(self):
        v = StreamValidator(repeat_window=10, repeat_threshold=5)
        repeat_unit = "ABCDEFGHIJ"
        for _ in range(4):
            v.feed(repeat_unit)
        assert v.content == repeat_unit * 4


class TestStreamValidatorEmptyChunkDetection:
    """Tests for stalled stream detection via empty chunks."""

    def test_empty_chunks_not_immediately_error(self):
        v = StreamValidator()
        for _ in range(5):
            v.feed("")
        assert v.total_chunks == 5

    def test_too_many_consecutive_empty_chunks(self):
        v = StreamValidator()
        with pytest.raises(RunawayGenerationError) as exc_info:
            for _ in range(25):
                v.feed("")
        assert "stalled stream" in str(exc_info.value) or "empty chunks" in str(exc_info.value)

    def test_empty_chunks_reset_on_content(self):
        v = StreamValidator()
        for _ in range(10):
            v.feed("")
        v.feed("content")
        for _ in range(10):
            v.feed("")
        assert v.content == "content"


class TestExceptionClasses:
    """Tests for exception class behavior."""

    def test_runaway_generation_error_is_exception(self):
        e = RunawayGenerationError("test reason", "partial")
        assert isinstance(e, Exception)
        assert "test reason" in str(e)
        assert e.reason == "test reason"
        assert e.partial_content == "partial"

    def test_response_size_exceeded_error_is_exception(self):
        e = ResponseSizeExceededError(1000, 500)
        assert isinstance(e, Exception)
        assert "1000" in str(e)
        assert "500" in str(e)
        assert e.size == 1000
        assert e.limit == 500


class TestIntegrationOllamaStreaming:
    """Integration tests that verify streaming through the Ollama client.

    These verify the end-to-end streaming flow using the mock helpers.
    """

    def test_valid_content_passes_streaming(self):
        import json
        from unittest.mock import MagicMock, patch
        from code_scanner.ollama_client import OllamaClient
        from code_scanner.models import LLMConfig

        def _make_stream_lines(content: str) -> list[bytes]:
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

        config = LLMConfig(backend="ollama", host="localhost", port=11434, model="test")
        client = OllamaClient(config)
        client._connected = True
        client._model_id = "test"
        client._context_limit = 8192

        stream_lines = _make_stream_lines('{"issues": []}')

        with patch("code_scanner.ollama_client.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__iter__ = MagicMock(return_value=iter(stream_lines))
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = client.query("sys", "user")
            assert result == {"issues": []}

    def test_repeating_content_detected_and_retried(self):
        from unittest.mock import MagicMock, patch
        from code_scanner.ollama_client import OllamaClient
        from code_scanner.models import LLMConfig

        config = LLMConfig(backend="ollama", host="localhost", port=11434, model="test")
        client = OllamaClient(config)
        client._connected = True
        client._model_id = "test"
        client._context_limit = 8192

        repeat_unit = "ABCDEFGHIJ" * 30
        repeat_lines: list[bytes] = []
        for _ in range(30):
            repeat_lines.append(
                f'{{"message":{{"content":"{repeat_unit}"}},"done":false}}\n'.encode()
            )
        repeat_lines.append(b'{"message":{"content":""},"done":true}\n')

        valid_lines: list[bytes] = []
        valid_lines.append(b'{"message":{"content":"{\\"issues\\":[]}"},"done":false}\n')
        valid_lines.append(b'{"message":{"content":""},"done":true}\n')

        with patch("code_scanner.ollama_client.urllib.request.urlopen") as mock_urlopen:
            mock_resp_repeat = MagicMock()
            mock_resp_repeat.__iter__ = MagicMock(return_value=iter(repeat_lines))
            mock_resp_repeat.__enter__ = MagicMock(return_value=mock_resp_repeat)
            mock_resp_repeat.__exit__ = MagicMock(return_value=False)

            mock_resp_valid = MagicMock()
            mock_resp_valid.__iter__ = MagicMock(return_value=iter(valid_lines))
            mock_resp_valid.__enter__ = MagicMock(return_value=mock_resp_valid)
            mock_resp_valid.__exit__ = MagicMock(return_value=False)

            mock_urlopen.side_effect = [mock_resp_repeat, mock_resp_valid]

            result = client.query("sys", "user", max_retries=3)
            assert result == {"issues": []}
