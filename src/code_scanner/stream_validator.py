"""Streaming response validator — detects runaway LLM generation."""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 512 * 1024  # 512KB hard cap
REPEAT_WINDOW_SIZE = 200  # characters to check for repetition
REPEAT_THRESHOLD = 10  # consecutive identical windows trigger abort
MAX_EMPTY_CHUNKS = 20  # consecutive empty chunks trigger abort


class RunawayGenerationError(Exception):
    """Raised when the LLM appears to be generating content endlessly."""

    def __init__(self, reason: str, partial_content: str = ""):
        preview = partial_content[-200:] if len(partial_content) > 200 else partial_content
        super().__init__(
            f"Runaway LLM generation detected: {reason}. "
            f"Last 200 chars of output: {preview!r}"
        )
        self.reason = reason
        self.partial_content = partial_content


class ResponseSizeExceededError(Exception):
    """Raised when LLM response exceeds the maximum allowed size."""

    def __init__(self, size: int, limit: int):
        super().__init__(
            f"LLM response size ({size} bytes) exceeds limit ({limit} bytes)"
        )
        self.size = size
        self.limit = limit


class StreamValidator:
    """Validates streaming LLM responses to detect runaway generation.

    Monitors accumulated content for:
    - Total size exceeding the hard cap
    - Repeated/looping content patterns (same text repeated indefinitely)
    - Stalled output (consecutive empty chunks)
    """

    def __init__(
        self,
        max_bytes: int = MAX_RESPONSE_BYTES,
        repeat_window: int = REPEAT_WINDOW_SIZE,
        repeat_threshold: int = REPEAT_THRESHOLD,
    ):
        self._max_bytes = max_bytes
        self._repeat_window = repeat_window
        self._repeat_threshold = repeat_threshold
        self._accumulated: list[str] = []
        self._total_bytes = 0
        self._total_chunks = 0
        self._empty_chunks = 0
        self._start_time = time.monotonic()
        self._last_repeat_count = 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def total_chunks(self) -> int:
        return self._total_chunks

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start_time

    @property
    def content(self) -> str:
        return "".join(self._accumulated)

    def feed(self, chunk: str) -> None:
        """Feed a text chunk from the streaming response.

        Raises:
            ResponseSizeExceededError: If accumulated size exceeds the cap.
            RunawayGenerationError: If repetitive/looping content is detected.
        """
        self._total_chunks += 1

        if not chunk:
            self._empty_chunks += 1
            if self._empty_chunks >= MAX_EMPTY_CHUNKS:
                raise RunawayGenerationError(
                    f"{MAX_EMPTY_CHUNKS} consecutive empty chunks (stalled stream)",
                    self.content,
                )
            return

        self._empty_chunks = 0
        self._accumulated.append(chunk)
        self._total_bytes += len(chunk.encode("utf-8"))

        if self._total_bytes > self._max_bytes:
            raise ResponseSizeExceededError(self._total_bytes, self._max_bytes)

        self._check_repetition()

    def note_activity(self) -> None:
        """Register non-content activity (e.g. reasoning/thinking tokens).

        Reasoning models stream long stretches of thinking tokens where the
        ``content`` field is empty. Without this, such a thinking phase is
        misclassified as a stalled stream. Calling this resets the
        consecutive-empty-chunk counter so a legitimate reasoning phase is
        not aborted.
        """
        self._total_chunks += 1
        self._empty_chunks = 0

    def _check_repetition(self) -> None:
        """Detect if the LLM is stuck in a content loop.

        Compares the tail window of accumulated content against
        earlier windows. If the same exact window repeats
        ``repeat_threshold`` times, the generation is considered runaway.
        """
        content = self.content
        if len(content) < self._repeat_window * 2:
            return

        tail = content[-self._repeat_window:]
        repeat_count = 0
        pos = len(content) - self._repeat_window

        while pos >= self._repeat_window:
            pos -= self._repeat_window
            window = content[pos:pos + self._repeat_window]
            if window == tail:
                repeat_count += 1
            else:
                break

        if repeat_count != self._last_repeat_count:
            self._last_repeat_count = repeat_count

        if repeat_count >= self._repeat_threshold:
            raise RunawayGenerationError(
                f"content loop detected ({repeat_count} repeats of {self._repeat_window}-char window)",
                content,
            )

    def reset(self) -> None:
        """Reset the validator for a new streaming session."""
        self._accumulated = []
        self._total_bytes = 0
        self._total_chunks = 0
        self._empty_chunks = 0
        self._start_time = time.monotonic()
        self._last_repeat_count = 0
