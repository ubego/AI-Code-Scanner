"""Abstract base class for LLM clients."""

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Shared JSON-fix system prompt (used by both client's _try_fix_json_response)
JSON_FIX_SYSTEM_PROMPT = (
    "You are a JSON extractor. Extract and return ONLY valid JSON. "
    "Do NOT include markdown code fences (```), explanations, or any other text. "
    "Output ONLY the raw JSON object, nothing else. "
    "Expected format: {\"issues\": [{\"file\": \"...\", \"line_number\": N, "
    "\"description\": \"...\", \"suggested_fix\": \"...\", \"code_snippet\": \"...\"}]} "
    "If the input has no valid issues, return: {\"issues\": []}"
)

_FENCE_PATTERN = re.compile(
    r'^```(?:json)?\s*\n?(.*?)\n?```\s*$',
    re.DOTALL | re.IGNORECASE,
)


class LLMClientError(Exception):
    """Error communicating with LLM backend."""

    pass


class ContextOverflowError(LLMClientError):
    """Fatal error when model context length is exceeded.
    
    This error should not be caught by retry logic - it requires
    user intervention to fix (change model settings or config).
    """

    pass


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients.

    Both LMStudioClient and OllamaClient must implement this interface
    to ensure interchangeable usage by the Scanner.

    Concrete implementations shared across backends:
      - ``strip_markdown_fences()``  strip ```json wrappers from LLM output
      - ``wait_for_connection()``    blocking retry loop
      - ``set_context_limit()``      manual context limit override
      - ``context_limit`` / ``model_id`` properties with connected checks
    """

    def __init__(self) -> None:
        self._context_limit: Optional[int] = None
        self._model_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Abstract interface — must be implemented by every backend
    # ------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the LLM backend and get model info.

        Raises:
            LLMClientError: If connection fails.
        """
        pass

    @abstractmethod
    def query(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Send a query to the LLM and get JSON response.

        Args:
            system_prompt: System instructions for the LLM.
            user_prompt: User message with code context.
            max_retries: Maximum number of retries for malformed responses.
            tools: Optional list of tool definitions for function calling.

        Returns:
            Parsed JSON response from the LLM. If tools are provided and LLM
            requests tool calls, response includes 'tool_calls' key with list
            of {tool_name, arguments} dicts.

        Raises:
            LLMClientError: If query fails after all retries.
            ContextOverflowError: If context limit is exceeded.
        """
        pass

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Get the human-readable backend name for logging.

        Returns:
            Backend name (e.g., "LM Studio", "Ollama").
        """
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if client is connected.

        Returns:
            True if connected, False otherwise.
        """
        pass

    # ------------------------------------------------------------------
    # Concrete properties — shared across all backends
    # ------------------------------------------------------------------

    @property
    def context_limit(self) -> int:
        """Get the context limit in tokens.

        Raises:
            LLMClientError: If not connected or limit unavailable.
        """
        if self._context_limit is None:
            raise LLMClientError("Not connected or context limit unavailable")
        return self._context_limit

    @property
    def model_id(self) -> str:
        """Get the model ID being used.

        Raises:
            LLMClientError: If not connected.
        """
        if self._model_id is None:
            raise LLMClientError("Not connected")
        return self._model_id

    # ------------------------------------------------------------------
    # Concrete methods — shared across all backends
    # ------------------------------------------------------------------

    def wait_for_connection(self, retry_interval: int = 10, max_attempts: int = 0) -> None:
        """Wait for LLM backend to become available.

        Retries connection every ``retry_interval`` seconds until successful.
        If ``max_attempts`` is > 0, stops after that many attempts.

        Args:
            retry_interval: Seconds between retry attempts.
            max_attempts: Maximum connection attempts (0 = unlimited).
        """
        logger.info("Waiting for %s connection...", self.backend_name)
        attempt = 0
        while True:
            attempt += 1
            try:
                self.connect()
                logger.info("%s connection restored", self.backend_name)
                return
            except LLMClientError as e:
                logger.warning("Connection failed (attempt %d): %s", attempt, e)
                if 0 < max_attempts <= attempt:
                    raise
                logger.info("Retrying in %d seconds...", retry_interval)
                time.sleep(retry_interval)

    def set_context_limit(self, limit: int) -> None:
        """Manually set the context limit.

        Args:
            limit: Context limit in tokens.

        Raises:
            ValueError: If limit is not positive.
        """
        if limit <= 0:
            raise ValueError("Context limit must be a positive integer")
        self._context_limit = limit
        logger.info("Context limit manually set to: %d tokens", limit)

    @staticmethod
    def strip_markdown_fences(content: str) -> str:
        """Strip markdown code fences from LLM response content.

        LLMs often wrap JSON in ```json ... ``` blocks despite
        instructions not to.  This extracts the content inside.

        Args:
            content: Raw response content.

        Returns:
            Content with markdown fences stripped.
        """
        content = content.strip()
        match = _FENCE_PATTERN.match(content)
        if match:
            return match.group(1).strip()
        return content

    def _try_fix_json_response(self, malformed_content: str) -> Optional[dict]:
        """Try to get LLM to fix its own malformed JSON response.

        Must be overridden by subclasses since the API call mechanism
        differs between backends.

        Args:
            malformed_content: The malformed response from LLM.

        Returns:
            Parsed JSON dict if successful, None if fix attempt failed.
        """
        return None

    def _parse_and_fix_json_response(
        self, content: str, attempt: int, max_retries: int
    ) -> Optional[dict]:
        """Parse LLM content as JSON, auto-fixing via LLM on failure.

        Args:
            content: Raw LLM response content (fences already stripped).
            attempt: Current attempt number (0-indexed).
            max_retries: Maximum number of retries.

        Returns:
            Parsed JSON dict on success, None if parsing failed and fix
            also failed.
        """
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raw_preview = content[:500] if content else "(empty)"
            logger.info(
                "LLM returned non-JSON response (attempt %d/%d). "
                "This is normal and will be auto-corrected.\nParse error: %s",
                attempt + 1, max_retries, e,
            )
            logger.debug("--- Raw response ---\n%s\n--- End raw response ---", raw_preview)
            fix_result = self._try_fix_json_response(content)
            if fix_result is not None:
                logger.info("%s successfully reformatted response to valid JSON.", self.backend_name)
                return fix_result
            return None


class StreamAccumulator:
    """Accumulates streaming content chunks with validation."""

    def __init__(self) -> None:
        from .stream_validator import StreamValidator
        self.validator = StreamValidator()
        self._parts: list[str] = []

    def feed(self, content: str) -> None:
        """Feed a content chunk. Validates and accumulates."""
        if content:
            self._parts.append(content)
            self.validator.feed(content)
        else:
            self.validator.feed("")

    @property
    def content(self) -> str:
        """The accumulated full content."""
        return "".join(self._parts)


# System prompt template for code analysis (shared across all backends)
SYSTEM_PROMPT_TEMPLATE = """You are an expert code analysis assistant. Your task is to find real, actionable issues in the provided code.

## RULES

1. **STAY ON TOPIC** - Only report issues matching the check query. Ignore unrelated problems.
2. **USE TOOLS** - Verify findings with available tools before reporting.
3. **USE EXACT FILE INFO** - Only reference files and lines from "Files to analyze" section.

## OUTPUT FORMAT (strict JSON, no markdown)

{"issues": [{"file": "path", "line_number": 42, "description": "...", "suggested_fix": "...", "code_snippet": "..."}]}

No issues found: {"issues": []}"""


def build_user_prompt(check_query: str, files_content: dict[str, str]) -> str:
    """Build the user prompt with file contents.

    Files are formatted with line numbers and boundary markers to prevent
    hallucination and ensure precise line number references.

    Args:
        check_query: The check/query to run against the code.
        files_content: Dictionary mapping file paths to their content.

    Returns:
        Formatted user prompt.
    """
    prompt_parts = [
        f"## Check to perform:\n{check_query}\n",
        "## Files to analyze:\n"
    ]

    for file_path, content in files_content.items():
        lines = content.split('\n')
        total_lines = len(lines)
        
        # Add line numbers to each line
        numbered_lines = []
        for i, line in enumerate(lines, start=1):
            numbered_lines.append(f"L{i}: {line}")
        numbered_content = '\n'.join(numbered_lines)
        
        # Format with boundary markers and metadata
        prompt_parts.append(
            f"### File: {file_path} (lines 1-{total_lines}, total: {total_lines})\n"
            f"<<<FILE_START>>>\n{numbered_content}\n<<<FILE_END>>>\n"
        )

    return "\n".join(prompt_parts)


class RequestBuilder:
    """Utility class for building LLM API requests.
    
    Centralizes common request structure while allowing backend-specific
    customizations through optional parameters.
    """
    
    @staticmethod
    def build_chat_request(
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools: Optional[list[dict[str, Any]]] = None,
        context_limit: Optional[int] = None,
        temperature: float = 0.1,
        stream: bool = False,
        **backend_options: Any
    ) -> dict[str, Any]:
        """Build a chat completion request dictionary.
        
        Args:
            model: Model identifier to use.
            system_prompt: System instructions for the LLM.
            user_prompt: User message with code context.
            tools: Optional list of tool definitions for function calling.
            context_limit: Optional context limit in tokens.
            temperature: Temperature parameter (default 0.1 for consistent output).
            stream: Whether to stream responses (default False).
            **backend_options: Backend-specific options (e.g., reasoning_effort, response_format).
        
        Returns:
            Request dictionary with common structure.
        """
        request = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        
        # Add stream parameter
        if stream:
            request["stream"] = stream
        
        # Add tools if provided
        if tools:
            request["tools"] = tools
        
        # Add backend-specific options
        request.update(backend_options)
        
        return request
    
    @staticmethod
    def build_ollama_request(
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools: Optional[list[dict[str, Any]]] = None,
        context_limit: Optional[int] = None,
        temperature: float = 0.1,
        top_p: float = 0.85,
        top_k: int = 35,
        repeat_penalty: float = 1.05,
    ) -> dict[str, Any]:
        """Build an Ollama-specific chat request.
        
        Args:
            model: Model identifier to use.
            system_prompt: System instructions for the LLM.
            user_prompt: User message with code context.
            tools: Optional list of tool definitions for function calling.
            context_limit: Optional context limit in tokens.
            temperature: Temperature parameter (default 0.1 for consistent output).
            top_p: Top-p (nucleus) sampling parameter (default 0.85).
            top_k: Top-k sampling parameter (default 35).
            repeat_penalty: Repeat penalty parameter (default 1.05).
        
        Returns:
            Request dictionary formatted for Ollama API.
        """
        request = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "top_p": top_p,
                "top_k": top_k,
                "repeat_penalty": repeat_penalty,
            }
        }
        
        # Add context limit if provided
        if context_limit:
            request["options"]["num_ctx"] = context_limit
        
        # Add tools if provided
        if tools:
            request["tools"] = tools
        
        return request
    
    @staticmethod
    def build_openai_request(
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools: Optional[list[dict[str, Any]]] = None,
        context_limit: Optional[int] = None,
        temperature: float = 0.1,
        top_p: float = 0.85,
        reasoning_effort: str = "high",
        response_format: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Build an OpenAI-compatible chat request (for LM Studio, etc.).
        
        Args:
            model: Model identifier to use.
            system_prompt: System instructions for the LLM.
            user_prompt: User message with code context.
            tools: Optional list of tool definitions for function calling.
            context_limit: Optional context limit in tokens (passed as max_tokens).
            temperature: Temperature parameter (default 0.1 for consistent output).
            top_p: Top-p (nucleus) sampling parameter (default 0.85).
            reasoning_effort: Reasoning effort level (default "high").
            response_format: Optional response format specification (e.g., {"type": "json_object"}).
        
        Returns:
            Request dictionary formatted for OpenAI-compatible API.
        """
        request = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "top_p": top_p,
            "reasoning_effort": reasoning_effort,
        }
        
        # Add tools if provided
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        
        # Add response format if provided
        if response_format:
            request["response_format"] = response_format
        
        # Add max_tokens if context limit provided
        if context_limit:
            request["max_tokens"] = context_limit
        
        return request
