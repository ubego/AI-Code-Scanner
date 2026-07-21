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

# Opening fence without a closing one — happens when the LLM stream is
# truncated (e.g. by max_tokens) mid-way through a fenced JSON block.
# Captures everything after the opening fence so it can be parsed directly.
_UNCLOSED_FENCE_PATTERN = re.compile(
    r'^```(?:json)?\s*\n?(.*)$',
    re.DOTALL | re.IGNORECASE,
)


def _repair_truncated_json(content: str) -> Optional[dict]:
    """Locally repair JSON that was truncated mid-generation.

    Generates a series of candidate cut points (positions right after a
    comma or right after a complete value at the outer container level),
    and for each one (most-data-preserving first), tries to close the
    still-open containers and parse the result. Returns the first that
    parses successfully.

    Args:
        content: JSON text that begins with '{' or '[' but may be cut off.

    Returns:
        Parsed JSON object on success.

    Raises:
        ValueError: If content does not look like repairable JSON.
        json.JSONDecodeError: If no candidate produced valid JSON.
    """
    if not content or content[0] not in "{[":
        raise ValueError("content does not start with a JSON container")

    in_string = False
    escape = False
    stack: list[str] = []
    # Candidate cut points: positions in the content that mark the end of
    # a complete value at some level. We try them in reverse (latest first)
    # so we recover as much data as possible.
    candidates: list[int] = []

    for i, ch in enumerate(content):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch in "{[":
            stack.append(ch)
            continue

        if ch in "}]":
            if not stack:
                raise ValueError("unbalanced closing brace")
            opener = stack.pop()
            if (opener == "{" and ch != "}") or (opener == "[" and ch != "]"):
                raise ValueError("mismatched closing brace")
            # Right after closing a container is a candidate — the value
            # before this position is complete at the new (popped) depth.
            candidates.append(i + 1)
            continue

        # A comma marks the end of a complete value at this nesting level.
        if ch == ",":
            candidates.append(i)

    # Well-formed: nothing to repair.
    if not stack:
        return json.loads(content)

    final_stack = tuple(stack)

    # Try each candidate cut point, latest first. For each, drop anything
    # after the cut, strip a trailing comma, and close all containers that
    # were still open at this cut. Parse — return on first success.
    #
    # First try the full content as-is: many truncations just drop the
    # trailing close brace(s), and closing from the final stack preserves
    # the most recent complete value (e.g. the last object member).
    all_candidates = list(candidates) + [len(content)]
    for pos in reversed(all_candidates):
        # Reconstruct the stack as it was at this candidate position by
        # re-scanning. (Cheap relative to network I/O this avoids.)
        prefix = content[:pos]
        scan_stack = _scan_container_stack(prefix)
        if scan_stack is None:
            continue  # malformed; skip
        # The candidate is only valid if the stack at that point is a
        # prefix of the final (still-open) stack — otherwise closing
        # would not produce the same outer structure.
        if tuple(final_stack[:len(scan_stack)]) != tuple(scan_stack):
            continue

        truncated = prefix.rstrip()
        # Drop a trailing partial key:value fragment if it looks incomplete
        # (heuristic: a key with no value ends with `: ` or just `"key":`).
        # Specifically, if the truncated text ends with `:` or `: `, the
        # last key had no value and json.loads will fail.
        if truncated.endswith(":") or truncated.endswith(": "):
            # Cut back to before the incomplete key.
            cut = truncated.rfind(",")
            if cut > 0:
                truncated = truncated[:cut]
            else:
                continue  # nothing left to recover here

        if truncated.endswith(","):
            truncated = truncated[:-1]
        for opener in reversed(scan_stack):
            truncated += "}" if opener == "{" else "]"
        try:
            return json.loads(truncated)
        except json.JSONDecodeError:
            continue

    # No usable candidate — recover as an empty outer container.
    opener = content[0]
    repaired = opener + ("}" if opener == "{" else "]")
    return json.loads(repaired)


def _scan_container_stack(prefix: str) -> Optional[list[str]]:
    """Scan a JSON prefix and return the open-container stack.

    Tracks string context and nesting. Used to reconstruct the stack at
    a candidate truncation point.

    Returns:
        List of '{'/'[' chars still open at the end of the prefix, or
        None if a structural error (unbalanced/mismatched brace) is hit.

    Args:
        prefix: A prefix of JSON content.
    """
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in prefix:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            opener = stack.pop()
            if (opener == "{" and ch != "}") or (opener == "[" and ch != "]"):
                return None
    return stack


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

        Handles complete fences (opening + closing) and also the truncated
        case where the stream was cut off (opening fence only, no closing
        fence) — which otherwise leaves stray backticks that break JSON
        parsing.

        Args:
            content: Raw response content.

        Returns:
            Content with markdown fences stripped.
        """
        content = content.strip()
        # Complete fenced block: ```json ... ```
        match = _FENCE_PATTERN.match(content)
        if match:
            return match.group(1).strip()
        # Truncated fenced block: opening fence but no closing (stream cut
        # off mid-generation). Strip the opening fence so the remainder can
        # be parsed/repaired instead of failing on the leading backticks.
        if content.startswith("```"):
            match = _UNCLOSED_FENCE_PATTERN.match(content)
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

            # Before asking the LLM to repair the response (slow, another
            # network round trip), try to recover truncated JSON locally.
            # This commonly happens when generation is cut off mid-object
            # by max_tokens — e.g. '{"issues": [{"file": "x.py", ...'.
            recovered = self._try_recover_truncated_json(content)
            if recovered is not None:
                logger.info(
                    "Recovered truncated JSON response locally "
                    "(no LLM round-trip needed)."
                )
                return recovered

            fix_result = self._try_fix_json_response(content)
            if fix_result is not None:
                logger.info("%s successfully reformatted response to valid JSON.", self.backend_name)
                return fix_result
            return None

    @staticmethod
    def _try_recover_truncated_json(content: str) -> Optional[dict]:
        """Attempt to locally repair truncated JSON without an LLM call.

        Handles the common truncation pattern where the stream ended
        mid-object (e.g. when max_tokens was hit). Strategy:

        1. Only attempt on content that starts with '{' or '[' (JSON-like).
        2. Drop trailing partial key/value fragments after the last
           complete object boundary, then close open structures.
        3. Try json.loads on the repaired string.

        Args:
            content: Raw content (fences already stripped).

        Returns:
            Parsed dict/list on success, None if local repair was not
            possible or also failed.
        """
        if not content:
            return None
        stripped = content.strip()
        if not stripped or stripped[0] not in "{[":
            return None

        # Find the position of the last *complete* top-level value. For an
        # object, that's the last '}' at the same nesting depth as the
        # opening '{'; for an array, the analogous ']' or the last ','.
        # Rather than fully parse (the content is malformed by definition),
        # scan with depth tracking respecting strings.
        try:
            return _repair_truncated_json(stripped)
        except (ValueError, json.JSONDecodeError):
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

    def feed_reasoning(self, reasoning: str) -> None:
        """Register reasoning/thinking tokens without accumulating them.

        These tokens must not appear in the final content (they are not part
        of the JSON answer), but they prove the model is actively generating
        and must not be counted as a stalled stream.
        """
        if reasoning:
            self.validator.note_activity()

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
4. **BE TERSE** - Keep your final JSON answer SHORT:
   - Report at most 5 issues per check.
   - Each "description" and "suggested_fix" must be one sentence (max ~20 words).
   - "code_snippet" must be at most 2 lines.
   - Do NOT explain, summarize, or add any text outside the JSON.

## OUTPUT FORMAT (strict JSON only, no markdown, no preamble)

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


# Hardcoded generation budgets (not user-configurable). These cap how much the
# model may output per call. NOTE: for reasoning models in LM Studio/Ollama the
# "thinking" tokens are NOT counted toward max_tokens, so reasoning_effort is
# the primary control over thinking length; these caps bound the final answer.
DEFAULT_MAX_OUTPUT_TOKENS = 4096


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
        
        # Cap GENERATED tokens (num_predict). Ollama defaults to unlimited
        # generation, which lets reasoning models run for tens of minutes.
        # Hardcoded sane budget (thinking tokens are bounded by reasoning_effort).
        request["options"]["num_predict"] = DEFAULT_MAX_OUTPUT_TOKENS
        
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
            user_prompt: User message with file context.
            tools: Optional list of tool definitions for function calling.
            context_limit: Optional context limit in tokens (for budgeting).
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
        
        # Cap GENERATED tokens (max_tokens). This is the completion budget,
        # NOT the context window. Previously this was set to the full
        # context_limit (e.g. 128000), which let reasoning models generate
        # for tens of minutes per call. Hardcoded to a small, sane budget.
        # NOTE: thinking/reasoning tokens are NOT bounded by this on most
        # backends — reasoning_effort controls those.
        request["max_tokens"] = DEFAULT_MAX_OUTPUT_TOKENS
        
        return request
