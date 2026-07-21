import json
import pytest
from unittest.mock import MagicMock, patch
from code_scanner.base_client import (
    BaseLLMClient, build_user_prompt, StreamAccumulator,
    LLMClientError,
)
from code_scanner.models import Issue


class ConcreteLLMClient(BaseLLMClient):
    """Concrete implementation for testing abstract base class."""
    def __init__(self, config):
        super().__init__()
        self.config = config
        self._context_limit = 4096

    def connect(self) -> None:
        pass

    def query(self, system_prompt, user_prompt, max_retries=3, tools=None):
        return {"issues": []}

    @property
    def backend_name(self) -> str:
        return "TestBackend"

    def is_connected(self) -> bool:
        return True

class TestBaseClientCoverage:
    """Test suite for base_client.py coverage."""

    def test_abstract_class_instantiation(self):
        """Test that BaseLLMClient cannot be instantiated directly."""
        with pytest.raises(TypeError):
            BaseLLMClient()

    def test_concrete_implementation(self):
        """Test that concrete implementation works."""
        client = ConcreteLLMClient(config=MagicMock())
        assert client is not None

    def test_build_user_prompt_structure(self):
        """Test build_user_prompt formats content correctly."""
        check = "Check for bugs"
        batch = {
            "file1.py": "def foo(): pass",
            "file2.py": "class Bar: pass"
        }
        
        prompt = build_user_prompt(check, batch)
        
        assert "## Check to perform:\nCheck for bugs" in prompt
        assert "file1.py" in prompt
        assert "L1: def foo(): pass" in prompt
        assert "file2.py" in prompt
        assert "L1: class Bar: pass" in prompt

    def test_build_user_prompt_treats_all_files_equally(self):
        """Test that what were 'core files' are now treated as regular files."""
        check = "Check logic"
        batch = {
            "src/main.py": "print('hello')",
            "models.py": "class Issue: pass",
            "base_client.py": "class Base: pass"
        }
        
        prompt = build_user_prompt(check, batch)
        
        # Core files section should NOT be present
        assert "## Core definition files" not in prompt
        
        # All files should be under "Files to analyze"
        assert "## Files to analyze:" in prompt
        assert "src/main.py" in prompt
        assert "models.py" in prompt
        assert "base_client.py" in prompt

    def test_build_user_prompt_empty_batch(self):
        """Test build_user_prompt with empty batch."""
        prompt = build_user_prompt("Check", {})
        assert "Check" in prompt
        assert "## Files to analyze:" in prompt

    def test_context_limit_property(self):
        """Test context_limit property access."""
        mock_config = MagicMock()
        mock_config.context_limit = 4096
        client = ConcreteLLMClient(config=mock_config)
        assert client.context_limit == 4096


class TestParseAndFixJsonResponse:
    """Tests for _parse_and_fix_json_response in BaseLLMClient."""

    def test_valid_json_parsed_directly(self):
        client = ConcreteLLMClient(config=MagicMock())
        result = client._parse_and_fix_json_response('{"issues": []}', 0, 3)
        assert result == {"issues": []}

    def test_invalid_json_fix_fails_returns_none(self):
        client = ConcreteLLMClient(config=MagicMock())
        result = client._parse_and_fix_json_response('not json', 0, 3)
        assert result is None

    def test_invalid_json_fix_succeeds(self):
        client = ConcreteLLMClient(config=MagicMock())
        client._try_fix_json_response = MagicMock(return_value={"issues": [{"file": "a.py"}]})
        result = client._parse_and_fix_json_response('not json', 0, 3)
        assert result == {"issues": [{"file": "a.py"}]}
        client._try_fix_json_response.assert_called_once_with('not json')


class TestStripMarkdownFences:
    """Tests for strip_markdown_fences static method."""

    def test_strip_json_fence(self):
        content = '```json\n{"issues": []}\n```'
        result = BaseLLMClient.strip_markdown_fences(content)
        assert result == '{"issues": []}'

    def test_strip_plain_fence(self):
        content = '```\n{"issues": []}\n```'
        result = BaseLLMClient.strip_markdown_fences(content)
        assert result == '{"issues": []}'

    def test_no_fence_unchanged(self):
        content = '{"issues": []}'
        result = BaseLLMClient.strip_markdown_fences(content)
        assert result == content.strip()

    def test_case_insensitive(self):
        content = '```JSON\n{"issues": []}\n```'
        result = BaseLLMClient.strip_markdown_fences(content)
        assert result == '{"issues": []}'

    def test_truncated_opening_fence_only(self):
        """Truncated stream: opening ```json fence with no closer.

        Regression test: a stream cut off mid-generation leaves an unclosed
        fence. Previously the regex required both fences, so the leading
        backticks survived and broke JSON parsing. The opener must now be
        stripped so the remainder can be parsed/repaired.
        """
        content = '```json\n{"issues": [{"file": "x.py", "line_number": 1,'
        result = BaseLLMClient.strip_markdown_fences(content)
        assert not result.startswith('`')
        assert result.startswith('{')

    def test_truncated_plain_fence_only(self):
        """Same regression for an unclosed ``` fence (no language tag)."""
        content = '```\n{"issues": []'
        result = BaseLLMClient.strip_markdown_fences(content)
        assert not result.startswith('`')


class TestRepairTruncatedJson:
    """Tests for _repair_truncated_json helper."""

    def test_well_formed_passes_through(self):
        from code_scanner.base_client import _repair_truncated_json
        assert _repair_truncated_json('{"issues": []}') == {"issues": []}

    def test_missing_closing_brace(self):
        """Trailing close brace dropped by truncation — recover last value."""
        from code_scanner.base_client import _repair_truncated_json
        result = _repair_truncated_json('{"file": "a.py", "line": 5')
        assert result == {"file": "a.py", "line": 5}

    def test_truncated_inside_first_element(self):
        """First array element incomplete — recover whatever completed."""
        from code_scanner.base_client import _repair_truncated_json
        result = _repair_truncated_json('{"issues": [{"file": "a.py",')
        assert result == {"issues": [{"file": "a.py"}]}

    def test_truncated_after_complete_element(self):
        """Both array elements complete — both recovered."""
        from code_scanner.base_client import _repair_truncated_json
        result = _repair_truncated_json(
            '{"issues": [{"file": "a.py", "n": 1}, {"file": "b.py", "n": 2,'
        )
        assert len(result["issues"]) == 2

    def test_truncated_mid_key_drops_incomplete_member(self):
        """A key with no value (just `"k":`) must be dropped."""
        from code_scanner.base_client import _repair_truncated_json
        result = _repair_truncated_json('{"issues": [], "incomplete":')
        assert "issues" in result
        assert "incomplete" not in result

    def test_strings_with_braces_and_commas(self):
        """Braces/commas inside strings must not confuse the scanner."""
        from code_scanner.base_client import _repair_truncated_json
        result = _repair_truncated_json(
            '{"issues": [{"d": "has } and , inside"}, {"file": "b'
        )
        assert result["issues"][0]["d"] == "has } and , inside"
        assert len(result["issues"]) == 1

    def test_top_level_array_truncated(self):
        from code_scanner.base_client import _repair_truncated_json
        result = _repair_truncated_json('[{"file": "a.py"}, {"file": "b')
        assert len(result) == 1
        assert result[0]["file"] == "a.py"

    def test_non_json_content_raises(self):
        """Non-JSON-like input must raise ValueError, not silently return."""
        from code_scanner.base_client import _repair_truncated_json
        with pytest.raises(ValueError):
            _repair_truncated_json("not json at all")

    def test_just_opening_brace(self):
        from code_scanner.base_client import _repair_truncated_json
        assert _repair_truncated_json("{") == {}

    def test_unbalanced_closing_raises(self):
        from code_scanner.base_client import _repair_truncated_json
        with pytest.raises(ValueError):
            _repair_truncated_json('{"x": 1}}')


class TestParseAndFixTruncatedJson:
    """Integration: _parse_and_fix_json_response recovers truncations locally
    before falling back to the LLM repair round-trip."""

    def test_truncated_fenced_json_recovered_without_llm_call(self):
        """End-to-end: a truncated fenced JSON (the exact pattern observed
        in production logs) is recovered locally — _try_fix_json_response
        must NOT be invoked."""
        client = ConcreteLLMClient(config=MagicMock())
        # If recovery fell through, this would be called; assert it isn't.
        client._try_fix_json_response = MagicMock(return_value=None)
        # Simulate what the client does: strip fences, then parse+fix.
        raw = '```json\n{"issues": [{"file": "base_client.py", "line_number": 81,'
        stripped = BaseLLMClient.strip_markdown_fences(raw)
        result = client._parse_and_fix_json_response(stripped, 0, 3)
        assert result is not None
        assert result["issues"][0]["file"] == "base_client.py"
        client._try_fix_json_response.assert_not_called()


class TestStreamAccumulator:
    """Tests for StreamAccumulator helper class."""

    def test_accumulates_content(self):
        acc = StreamAccumulator()
        acc.feed("hello ")
        acc.feed("world")
        assert acc.content == "hello world"

    def test_empty_chunks_ignored_in_content(self):
        acc = StreamAccumulator()
        acc.feed("")
        acc.feed("real")
        assert acc.content == "real"

    def test_validator_rejects_oversized(self):
        from code_scanner.stream_validator import ResponseSizeExceededError
        acc = StreamAccumulator()
        with pytest.raises(ResponseSizeExceededError):
            acc.feed("x" * 600000)

    def test_validator_detects_repeated_content(self):
        from code_scanner.stream_validator import RunawayGenerationError
        acc = StreamAccumulator()
        repeat = "ABCDEFGHIJ" * 20  # 200 chars — exact repeat_window size
        with pytest.raises(RunawayGenerationError):
            for _ in range(15):
                acc.feed(repeat)


class TestWaitForConnection:
    """Tests for wait_for_connection with max_attempts."""

    def test_max_attempts_exceeded_raises(self):
        from code_scanner.base_client import BaseLLMClient
        import time

        class FailingClient(BaseLLMClient):
            @property
            def backend_name(self):
                return "Test"
            def is_connected(self):
                return True
            def connect(self):
                raise LLMClientError("fail")
            def query(self, *args, **kwargs):
                pass

        client = FailingClient()
        with patch("code_scanner.base_client.time.sleep"):
            with pytest.raises(LLMClientError, match="fail"):
                client.wait_for_connection(retry_interval=0, max_attempts=2)
