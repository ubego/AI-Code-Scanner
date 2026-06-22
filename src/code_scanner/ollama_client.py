"""Ollama API client using native /api/chat endpoint."""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .base_client import BaseLLMClient, LLMClientError, ContextOverflowError, RequestBuilder
from .models import LLMConfig, LLMToolCallResponse
from .error_messages import OllamaErrors, GeneralErrors

logger = logging.getLogger(__name__)

__all__ = ["OllamaClient", "LLMClientError", "ContextOverflowError"]


class OllamaClient(BaseLLMClient):
    """Client for communicating with Ollama via native /api/chat endpoint."""

    def __init__(self, config: LLMConfig):
        """Initialize the Ollama client.

        Args:
            config: LLM configuration with host, port, model, etc.
        """
        self.config = config
        self._context_limit: Optional[int] = None
        self._model_id: Optional[str] = None
        self._connected: bool = False
        self._model_context_limit: Optional[int] = None  # Actual limit from model

    @property
    def backend_name(self) -> str:
        """Get the human-readable backend name for logging."""
        return "Ollama"

    def is_connected(self) -> bool:
        """Check if client is connected.

        Returns:
            True if connected, False otherwise.
        """
        return self._connected

    def connect(self) -> None:
        """Establish connection to Ollama and validate model.

        Raises:
            LLMClientError: If connection fails or model not found.
        """
        logger.info(f"Connecting to Ollama at {self.config.base_url}")

        # Validate model is specified (required for Ollama)
        if not self.config.model:
            raise LLMClientError(OllamaErrors.MODEL_REQUIRED)

        self._model_id = self.config.model

        # Check if Ollama is running by querying /api/tags
        try:
            url = f"{self.config.base_url}/api/tags"
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode())
                available_models = [m.get("name", "") for m in data.get("models", [])]
                
                if not available_models:
                    raise LLMClientError(OllamaErrors.NO_MODELS_AVAILABLE)

                # Check if requested model is available
                # Ollama model names can be "qwen3" or "qwen3:4b" etc
                model_found = False
                for available in available_models:
                    if (available == self._model_id or 
                        available.startswith(f"{self._model_id}:") or
                        self._model_id.startswith(f"{available}:")):
                        model_found = True
                        break

                if not model_found:
                    raise LLMClientError(
                        OllamaErrors.MODEL_NOT_FOUND.format(
                            model=self._model_id,
                            available=", ".join(available_models[:5])
                        )
                    )

                logger.info(f"Using model: {self._model_id}")

        except urllib.error.URLError as e:
            raise LLMClientError(
                OllamaErrors.CONNECTION_ERROR_TEMPLATE.format(
                    host=self.config.host,
                    port=self.config.port,
                    url=self.config.base_url,
                    model=self._model_id,
                    timeout=self.config.timeout,
                    error=e
                )
            )
        except json.JSONDecodeError as e:
            raise LLMClientError(OllamaErrors.INVALID_RESPONSE.format(e=e))

        # Get context limit from model info
        self._model_context_limit = self._get_model_context_limit()
        
        # Handle context limit configuration
        if self.config.context_limit:
            if self._model_context_limit and self.config.context_limit > self._model_context_limit:
                raise LLMClientError(
                    f"\n{'='*70}\n"
                    f"CONTEXT LIMIT ERROR\n"
                    f"{'='*70}\n\n"
                    f"Configuration specifies context_limit = {self.config.context_limit} tokens,\n"
                    f"but model '{self._model_id}' only supports {self._model_context_limit} tokens.\n\n"
                    f"To fix this, either:\n"
                    f"1. Reduce context_limit in config.toml to {self._model_context_limit} or less\n"
                    f"2. Use a model with larger context window\n\n"
                    f"{'='*70}"
                )
            elif self._model_context_limit and self.config.context_limit < self._model_context_limit:
                logger.warning(
                    f"Configuration context_limit ({self.config.context_limit}) is less than "
                    f"model's available context ({self._model_context_limit}). "
                    f"Using configured value."
                )
            self._context_limit = self.config.context_limit
            logger.info(f"Using configured context limit: {self._context_limit} tokens")
        elif self._model_context_limit:
            self._context_limit = self._model_context_limit
            logger.info(f"Context window size: {self._context_limit} tokens")
        else:
            logger.warning(
                "Could not determine context limit from Ollama API. "
                "Context limit must be set manually."
            )

        self._connected = True

    def _get_model_context_limit(self) -> Optional[int]:
        """Get context limit from model info via /api/show.

        Returns:
            Context limit in tokens, or None if unavailable.
        """
        try:
            url = f"{self.config.base_url}/api/show"
            request_data = json.dumps({"name": self._model_id}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=request_data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode())
                
                # Ollama returns model info in 'modelinfo' or 'details' field
                model_info = data.get("modelinfo", {})
                details = data.get("details", {})
                parameters = data.get("parameters", "")
                
                # Check various possible fields for context length
                for field in ["num_ctx", "context_length", "n_ctx"]:
                    if field in model_info:
                        return int(model_info[field])
                    if field in details:
                        return int(details[field])
                
                # Try to extract from parameters string
                # Format: "num_ctx 4096\nnum_gpu ..."
                if "num_ctx" in parameters:
                    for line in parameters.split("\n"):
                        if line.strip().startswith("num_ctx"):
                            parts = line.split()
                            if len(parts) >= 2:
                                return int(parts[1])

        except Exception as e:
            logger.warning(f"Could not get context limit from Ollama: {e}")
        
        return None

    @property
    def context_limit(self) -> int:
        """Get the context limit in tokens.

        Raises:
            LLMClientError: If not connected or limit unavailable.
        """
        if self._context_limit is None:
            raise LLMClientError(OllamaErrors.NOT_CONNECTED_OR_NO_CONTEXT)
        return self._context_limit

    @property
    def model_id(self) -> str:
        """Get the model ID being used.

        Raises:
            LLMClientError: If not connected.
        """
        if self._model_id is None:
            raise LLMClientError(OllamaErrors.NOT_CONNECTED)
        return self._model_id

    def query(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Send a query to Ollama and get JSON response.

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
        if not self._connected:
            raise LLMClientError(OllamaErrors.NOT_CONNECTED)

        last_raw_response = "(no response received)"

        for attempt in range(max_retries):
            try:
                logger.debug(
                    f"Sending query to Ollama (attempt {attempt + 1}/{max_retries})\n"
                    f"--- SYSTEM PROMPT ---\n{system_prompt}\n--- END SYSTEM PROMPT ---\n"
                    f"--- USER PROMPT ---\n{user_prompt}\n--- END USER PROMPT ---"
                )

                # Build request for /api/chat using RequestBuilder
                request_data = RequestBuilder.build_ollama_request(
                    model=self._model_id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                    context_limit=self._context_limit,
                )

                url = f"{self.config.base_url}/api/chat"
                req = urllib.request.Request(
                    url,
                    data=json.dumps(request_data).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )

                with urllib.request.urlopen(req, timeout=self.config.timeout) as response:
                    data = json.loads(response.read().decode())

                # Check if Ollama wants to call tools
                message = data.get("message", {})
                if tools and message.get("tool_calls"):
                    tool_calls = []
                    for tool_call in message["tool_calls"]:
                        tool_calls.append({
                            "tool_name": tool_call["function"]["name"],
                            "arguments": tool_call["function"]["arguments"],
                        })
                    logger.info(f"Ollama requested {len(tool_calls)} tool call(s)")
                    # Validate through Pydantic model
                    validated = LLMToolCallResponse.model_validate({"tool_calls": tool_calls})
                    return validated.model_dump()

                content = message.get("content", "")
                if not content:
                    logger.warning(
                        f"Empty response from Ollama (attempt {attempt + 1}/{max_retries}). "
                        "Will retry automatically."
                    )
                    continue

                # Strip markdown code fences if present
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
                    logger.debug(f"--- Raw response ---\n{raw_preview}\n--- End raw response ---")
                    
                    # Try to get LLM to fix its own response
                    fix_result = self._try_fix_json_response(content)
                    if fix_result is not None:
                        logger.info("Ollama successfully reformatted response to valid JSON.")
                        return fix_result
                    
                    continue

            except urllib.error.HTTPError as e:
                error_body = e.read().decode() if e.fp else str(e)
                
                # Check for context overflow error
                if "context" in error_body.lower() and ("overflow" in error_body.lower() or 
                    "too long" in error_body.lower() or "exceeds" in error_body.lower()):
                    raise ContextOverflowError(
                        f"\n{'='*70}\n"
                        f"CONTEXT LENGTH EXCEEDED\n"
                        f"{'='*70}\n\n"
                        f"The request exceeded Ollama's context limit.\n"
                        f"Configured limit: {self._context_limit} tokens\n\n"
                        f"To fix this:\n"
                        f"1. Reduce the number of files per batch\n"
                        f"2. Lower context_limit in config.toml\n"
                        f"3. Use a model with larger context window\n\n"
                        f"Error: {error_body}\n"
                        f"{'='*70}"
                    )
                
                logger.warning(f"Ollama HTTP error (attempt {attempt + 1}): {e}")
                continue

            except urllib.error.URLError as e:
                raise LLMClientError(OllamaErrors.LOST_CONNECTION.format(e=e))

            except TimeoutError as e:
                logger.warning(
                    f"Ollama request timed out (attempt {attempt + 1}/{max_retries}). "
                    f"The model is taking longer than {self.config.timeout}s to respond.\n"
                    f"Tips: 1) Increase 'timeout' in config.toml, "
                    f"2) Lower 'context_limit' to reduce processing time, "
                    f"3) Use a smaller/faster model."
                )
                continue

            except Exception as e:
                error_msg = str(e).lower()
                if "timed out" in error_msg or "timeout" in error_msg:
                    logger.warning(
                        f"Ollama request timed out (attempt {attempt + 1}/{max_retries}). "
                        f"The model is taking longer than {self.config.timeout}s to respond.\n"
                        f"Tips: 1) Increase 'timeout' in config.toml, "
                        f"2) Lower 'context_limit' to reduce processing time, "
                        f"3) Use a smaller/faster model."
                    )
                else:
                    logger.warning(f"Ollama error (attempt {attempt + 1}/{max_retries}): {e}")
                continue

        # Show the last raw response to help debug
        raw_preview = last_raw_response[:1000] if len(last_raw_response) > 1000 else last_raw_response
        raise LLMClientError(
            OllamaErrors.FAILED_JSON_RESPONSE.format(
                max_retries=max_retries,
                preview=raw_preview
            )
        )

    def _try_fix_json_response(self, malformed_content: str) -> Optional[dict]:
        """Try to get Ollama to fix its own malformed JSON response.

        Args:
            malformed_content: The malformed response from LLM.

        Returns:
            Parsed JSON dict if successful, None if fix attempt failed.
        """
        try:
            fix_request = {
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
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "top_p": 0.85,
                    "top_k": 35,
                    "repeat_penalty": 1.05,
                }
            }

            url = f"{self.config.base_url}/api/chat"
            req = urllib.request.Request(
                url,
                data=json.dumps(fix_request).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.loads(response.read().decode())
                content = data.get("message", {}).get("content", "")

            if content:
                content = self._strip_markdown_fences(content)
                result = json.loads(content)
                return result

        except Exception as e:
            logger.debug(f"JSON fix attempt failed: {e}")

        return None

    def _strip_markdown_fences(self, content: str) -> str:
        """Strip markdown code fences from content.

        Args:
            content: Raw response content.

        Returns:
            Content with markdown fences stripped.
        """
        content = content.strip()

        # Pattern to match ```json or ``` at start and ``` at end
        fence_pattern = re.compile(
            r'^```(?:json)?\s*\n?(.*?)\n?```\s*$',
            re.DOTALL | re.IGNORECASE
        )

        match = fence_pattern.match(content)
        if match:
            return match.group(1).strip()

        return content

    def wait_for_connection(self, retry_interval: int = 10) -> None:
        """Wait for Ollama to become available.

        Retries connection every `retry_interval` seconds until successful.

        Args:
            retry_interval: Seconds between retry attempts.
        """
        logger.info("Waiting for Ollama connection...")

        while True:
            try:
                self.connect()
                logger.info("Ollama connection restored")
                return
            except LLMClientError as e:
                logger.warning(f"Connection failed: {e}")
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
            raise ValueError(GeneralErrors.CONTEXT_LIMIT_MUST_BE_POSITIVE)
        self._context_limit = limit
        logger.info(f"Context limit manually set to: {limit} tokens")
