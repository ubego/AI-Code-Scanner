"""LM Studio API client using OpenAI-compatible interface."""

import json
import logging
import re
import time
from typing import Any, Optional

from openai import OpenAI, APIConnectionError, APIError

from .base_client import BaseLLMClient, LLMClientError, ContextOverflowError, RequestBuilder
from .models import LLMConfig, LLMToolCallResponse
from .stream_validator import (
    StreamValidator,
    RunawayGenerationError,
    ResponseSizeExceededError,
)

logger = logging.getLogger(__name__)

# Re-export exceptions for backward compatibility
__all__ = ["LMStudioClient", "LLMClient", "LLMClientError", "ContextOverflowError"]


class LMStudioClient(BaseLLMClient):
    """Client for communicating with LM Studio via OpenAI-compatible API."""

    def __init__(self, config: LLMConfig):
        """Initialize the LM Studio client.

        Args:
            config: LLM configuration with host, port, etc.
        """
        self.config = config
        self._client: Optional[OpenAI] = None
        self._context_limit: Optional[int] = None
        self._model_id: Optional[str] = None
        self._supports_json_format: bool = True  # Assume supported, fallback if not

    @property
    def backend_name(self) -> str:
        """Get the human-readable backend name for logging."""
        return "LM Studio"

    def is_connected(self) -> bool:
        """Check if client is connected.

        Returns:
            True if connected, False otherwise.
        """
        return self._client is not None

    def connect(self) -> None:
        """Establish connection to LM Studio and get model info.

        Raises:
            LLMClientError: If connection fails or context limit unavailable.
        """
        logger.info(f"Connecting to LM Studio at {self.config.base_url}")

        try:
            self._client = OpenAI(
                base_url=self.config.base_url,
                api_key="lm-studio",  # LM Studio doesn't require a real key
                timeout=self.config.timeout,
            )

            # Get available models
            models = self._client.models.list()
            if not models.data:
                raise LLMClientError("No models available in LM Studio")

            # Use first model or configured model
            if self.config.model:
                model_ids = [m.id for m in models.data]
                if self.config.model not in model_ids:
                    raise LLMClientError(
                        f"Model '{self.config.model}' not found. "
                        f"Available: {model_ids}"
                    )
                self._model_id = self.config.model
            else:
                self._model_id = models.data[0].id

            logger.info(f"Using model: {self._model_id}")

            # Get context limit from config or model metadata
            if self.config.context_limit:
                self._context_limit = self.config.context_limit
                logger.info(f"Using configured context limit: {self._context_limit} tokens")
            else:
                self._context_limit = self._get_context_limit()
                if self._context_limit is not None:
                    logger.info(f"Context window size: {self._context_limit} tokens")
                else:
                    logger.warning(
                        "Could not determine context limit from LM Studio API. "
                        "Context limit must be set manually."
                    )

        except APIConnectionError as e:
            raise LLMClientError(
                f"\n{'='*70}\n"
                f"CONNECTION ERROR: LM Studio\n"
                f"{'='*70}\n\n"
                f"Could not connect to LM Studio.\n\n"
                f"Connection parameters:\n"
                f"  Backend:  lm-studio\n"
                f"  Host:     {self.config.host}\n"
                f"  Port:     {self.config.port}\n"
                f"  URL:      {self.config.base_url}\n"
                f"  Model:    {self.config.model or '(default)'}\n"
                f"  Timeout:  {self.config.timeout}s\n\n"
                f"Please ensure:\n"
                f"1. LM Studio is running\n"
                f"2. A model is loaded in LM Studio\n"
                f"3. The local server is started (Developer tab → Start Server)\n"
                f"4. Host and port match your LM Studio settings\n\n"
                f"Error: {e}\n"
                f"{'='*70}"
            )
        except APIError as e:
            raise LLMClientError(f"LM Studio API error: {e}")

    def _get_context_limit(self) -> Optional[int]:
        """Get context limit from model metadata.

        Returns:
            Context limit in tokens, or None if unavailable.
        """
        if self._client is None:
            return None

        try:
            # Try to get model info
            models = self._client.models.list()
            for model in models.data:
                if model.id == self._model_id:
                    # LM Studio may provide context length in different fields
                    if hasattr(model, "context_length"):
                        return model.context_length
                    if hasattr(model, "max_tokens"):
                        return model.max_tokens
                    # Check in model metadata if available
                    if hasattr(model, "metadata"):
                        metadata = model.metadata or {}
                        if "context_length" in metadata:
                            return metadata["context_length"]

            # Fallback: try a test request to see what the model reports
            # Some LM Studio versions include context info in completions
            return self._probe_context_limit()

        except Exception as e:
            logger.warning(f"Error getting context limit: {e}")
            return None

    def _probe_context_limit(self) -> Optional[int]:
        """Probe the model to determine context limit.

        Returns:
            Estimated context limit, or None.
        """
        # LM Studio typically exposes this via the /v1/models endpoint
        # If not available, we cannot proceed (per PRD requirements)
        try:
            import urllib.request
            import urllib.error

            url = f"{self.config.base_url}/models"
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode())
                for model in data.get("data", []):
                    if model.get("id") == self._model_id:
                        # Check various possible fields
                        for field in ["context_length", "max_context_length", "n_ctx"]:
                            if field in model:
                                return model[field]
        except Exception:
            pass

        return None

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
            requests tool calls, response includes 'tool_calls' key.

        Raises:
            LLMClientError: If query fails after all retries.
            ContextOverflowError: If context limit is exceeded.
        """
        if self._client is None:
            raise LLMClientError("Not connected")

        last_raw_response = "(no response received)"

        for attempt in range(max_retries):
            try:
                logger.debug(
                    f"Sending query (attempt {attempt + 1}/{max_retries})\n"
                    f"--- SYSTEM PROMPT ---\n{system_prompt}\n--- END SYSTEM PROMPT ---\n"
                    f"--- USER PROMPT ---\n{user_prompt}\n--- END USER PROMPT ---"
                )

                # Build request parameters using RequestBuilder
                response_format = {"type": "json_object"} if self._supports_json_format and not tools else None
                request_params = RequestBuilder.build_openai_request(
                    model=self._model_id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    context_limit=self._context_limit,
                    reasoning_effort="high",
                    response_format=response_format,
                )
                request_params["stream"] = True

                stream = self._client.chat.completions.create(**request_params)

                validator = StreamValidator()
                accumulated_content_parts: list[str] = []
                tool_call_buffers: dict[int, dict] = {}

                for chunk in stream:
                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    if delta.content:
                        accumulated_content_parts.append(delta.content)
                        validator.feed(delta.content)
                    else:
                        validator.feed("")

                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_call_buffers:
                                tool_call_buffers[idx] = {"tool_name": "", "arguments": ""}
                            buf = tool_call_buffers[idx]
                            if tc_delta.id:
                                buf["id"] = tc_delta.id
                            if tc_delta.function and tc_delta.function.name:
                                buf["tool_name"] = tc_delta.function.name
                            if tc_delta.function and tc_delta.function.arguments:
                                buf["arguments"] += tc_delta.function.arguments

                    if chunk.choices[0].finish_reason is not None:
                        break

                if tool_call_buffers:
                    tool_calls = []
                    for idx in sorted(tool_call_buffers.keys()):
                        buf = tool_call_buffers[idx]
                        try:
                            args = json.loads(buf["arguments"]) if buf["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        tool_calls.append({
                            "tool_name": buf["tool_name"],
                            "arguments": args,
                        })
                    logger.info(f"LLM requested {len(tool_calls)} tool call(s)")
                    validated = LLMToolCallResponse.model_validate({"tool_calls": tool_calls})
                    return validated.model_dump()

                content = "".join(accumulated_content_parts)
                if not content:
                    logger.warning(f"Empty response from LLM (attempt {attempt + 1}/{max_retries}). Will retry automatically.")
                    continue

                # Strip markdown code fences if present (common LLM behavior)
                content = self._strip_markdown_fences(content)

                # Parse JSON response
                try:
                    result = json.loads(content)
                    logger.debug("Successfully parsed JSON response")
                    return result
                except json.JSONDecodeError as e:
                    # This is normal - LLMs sometimes return non-JSON. Auto-fix will handle it.
                    last_raw_response = content if content else "(empty)"
                    raw_preview = content[:500] if content else "(empty)"
                    logger.info(
                        f"LLM returned non-JSON response (attempt {attempt + 1}/{max_retries}). "
                        f"This is normal and will be auto-corrected.\n"
                        f"Parse error: {e}"
                    )
                    logger.debug(f"--- Raw LLM response ---\n{raw_preview}\n--- End raw response ---")
                    
                    # Try to get LLM to fix its own response
                    fix_result = self._try_fix_json_response(content)
                    if fix_result is not None:
                        logger.info("LLM successfully reformatted response to valid JSON.")
                        return fix_result
                    
                    # If fix failed, continue to next retry attempt
                    continue

            except (RunawayGenerationError, ResponseSizeExceededError) as e:
                logger.warning(
                    f"LM Studio response issue (attempt {attempt + 1}/{max_retries}): {e}"
                )
                last_raw_response = str(e)[:1000]
                continue

            except APIConnectionError as e:
                # Connection lost mid-session - this needs special handling
                raise LLMClientError(f"Lost connection to LM Studio: {e}")
            except APIError as e:
                error_msg = str(e)
                # Check if this is a context length overflow error
                if "context" in error_msg.lower() and ("overflow" in error_msg.lower() or "context length" in error_msg.lower()):
                    # Extract the model's actual context length from the error message
                    # Example: "model is loaded with context length of only 4096 tokens"
                    import re
                    actual_ctx_match = re.search(r'context length of (?:only )?(\d+)', error_msg)
                    actual_ctx = actual_ctx_match.group(1) if actual_ctx_match else "unknown"
                    
                    # Raise ContextOverflowError which is FATAL - should not be caught
                    raise ContextOverflowError(
                        f"\n{'='*70}\n"
                        f"CONTEXT LENGTH MISMATCH ERROR\n"
                        f"{'='*70}\n\n"
                        f"The model in LM Studio is loaded with a context length of {actual_ctx} tokens,\n"
                        f"but Code Scanner is configured to use {self._context_limit} tokens.\n\n"
                        f"To fix this, do ONE of the following:\n\n"
                        f"1. INCREASE MODEL CONTEXT IN LM STUDIO (Recommended):\n"
                        f"   - Open LM Studio\n"
                        f"   - Go to the model settings\n"
                        f"   - Increase 'Context Length' to at least {self._context_limit} tokens\n"
                        f"   - Reload the model\n\n"
                        f"2. LOAD A DIFFERENT MODEL:\n"
                        f"   - Choose a model that supports larger context windows\n"
                        f"   - Models like Llama 3, Mistral, or Qwen often support 8K-128K context\n\n"
                        f"3. REDUCE CONTEXT LIMIT IN CONFIG:\n"
                        f"   - Edit config.toml and set: context_limit = {actual_ctx}\n"
                        f"   - Note: This will process fewer files per batch\n\n"
                        f"{'='*70}"
                    )
                # Check if this is a response_format not supported error
                if "response_format" in error_msg.lower() or "json_object" in error_msg.lower():
                    logger.info(
                        "[OK] Model doesn't support response_format='json_object' parameter (this is normal for many models). "
                        "Using prompt-based JSON formatting instead. This does not affect functionality."
                    )
                    self._supports_json_format = False
                    # Don't count this as a failed attempt, retry immediately
                    continue
                logger.warning(f"API error (attempt {attempt + 1}): {e}")
                continue

        # Show the last raw response to help debug
        raw_preview = last_raw_response[:1000] if len(last_raw_response) > 1000 else last_raw_response
        raise LLMClientError(
            f"Failed to get valid JSON response after {max_retries} attempts.\n"
            f"--- Last raw LLM response ---\n{raw_preview}\n--- End raw response ---"
        )

    def _try_fix_json_response(self, malformed_content: str) -> Optional[dict]:
        """Try to get LLM to fix its own malformed JSON response.

        Args:
            malformed_content: The malformed response from LLM.

        Returns:
            Parsed JSON dict if successful, None if fix attempt failed.
        """
        if self._client is None:
            return None

        try:
            # Create a fix request asking LLM to reformat its response
            fix_params = {
                "model": self._model_id,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a JSON extractor. Extract and return ONLY valid JSON. "
                            "Do NOT include markdown code fences (```), explanations, or any other text. "
                            "Output ONLY the raw JSON object, nothing else. "
                            "Expected format: {\"issues\": [{\"file\": \"...\", \"line_number\": N, "
                            "\"description\": \"...\", \"suggested_fix\": \"...\", \"code_snippet\": \"...\"}]} "
                            "If the input has no valid issues, return: {\"issues\": []}"
                        )
                    },
                    {
                        "role": "user",
                        "content": f"Extract the JSON from this response:\n\n{malformed_content[:4000]}"
                    },
                ],
                "temperature": 0.1,
                "top_p": 0.85,
            }

            # Add response_format if supported
            if self._supports_json_format:
                fix_params["response_format"] = {"type": "json_object"}

            response = self._client.chat.completions.create(**fix_params)
            content = response.choices[0].message.content

            if content:
                # Strip markdown fences from fix response too
                content = self._strip_markdown_fences(content)
                result = json.loads(content)
                return result

        except Exception as e:
            logger.debug(f"JSON fix attempt failed: {e}")

        return None

    def _strip_markdown_fences(self, content: str) -> str:
        """Strip markdown code fences from content.

        LLMs often wrap JSON in ```json ... ``` blocks despite instructions not to.
        This extracts the content inside the fences.

        Args:
            content: Raw response content.

        Returns:
            Content with markdown fences stripped.
        """
        content = content.strip()

        # Pattern to match ```json or ``` at start and ``` at end
        # Handles: ```json\n{...}\n``` or ```\n{...}\n```
        fence_pattern = re.compile(
            r'^```(?:json)?\s*\n?(.*?)\n?```\s*$',
            re.DOTALL | re.IGNORECASE
        )

        match = fence_pattern.match(content)
        if match:
            return match.group(1).strip()

        return content

    def wait_for_connection(self, retry_interval: int = 10, max_attempts: int = 0) -> None:
        """Wait for LM Studio to become available.

        Retries connection every `retry_interval` seconds until successful.
        If `max_attempts` is > 0, stops after that many attempts.

        Args:
            retry_interval: Seconds between retry attempts.
            max_attempts: Maximum connection attempts (0 = unlimited).
        """
        logger.info("Waiting for LM Studio connection...")
        attempt = 0

        while True:
            attempt += 1
            try:
                self.connect()
                logger.info("LM Studio connection restored")
                return
            except LLMClientError as e:
                logger.warning(f"Connection failed (attempt {attempt}): {e}")
                if 0 < max_attempts <= attempt:
                    raise
                logger.info(f"Retrying in {retry_interval} seconds...")
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
        logger.info(f"Context limit manually set to: {limit} tokens")


# Backward compatibility alias - LLMClient maps to LMStudioClient
LLMClient = LMStudioClient

# Re-export from base_client for backward compatibility
from .base_client import SYSTEM_PROMPT_TEMPLATE, build_user_prompt
