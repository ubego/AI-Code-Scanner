"""Text utilities for string processing and fuzzy matching."""

import difflib
from pathlib import Path
from typing import Optional


# Truncation constants (inspired by opencode)
MAX_OUTPUT_LINES = 2000
MAX_OUTPUT_BYTES = 50 * 1024  # 50KB


def levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate the Levenshtein distance between two strings.
    
    The Levenshtein distance is the minimum number of single-character edits
    (insertions, deletions, substitutions) required to transform one string into another.
    
    Uses rapidfuzz library for optimized performance.
    
    Args:
        s1: First string.
        s2: Second string.
        
    Returns:
        Edit distance between the strings.
    """
    from rapidfuzz.distance import Levenshtein
    return Levenshtein.distance(s1, s2)


def similarity_ratio(s1: str, s2: str) -> float:
    """Calculate similarity ratio between two strings using SequenceMatcher.
    
    Args:
        s1: First string.
        s2: Second string.
        
    Returns:
        Similarity ratio between 0.0 (completely different) and 1.0 (identical).
    """
    return difflib.SequenceMatcher(None, s1, s2).ratio()


def fuzzy_match(target: str, candidate: str, threshold: float = 0.7) -> bool:
    """Check if candidate is a fuzzy match for target.
    
    Args:
        target: The string to match against.
        candidate: The candidate string to check.
        threshold: Minimum similarity ratio to consider a match (0.0 to 1.0).
        
    Returns:
        True if candidate is similar enough to target.
    """
    return similarity_ratio(target, candidate) >= threshold


def find_similar_strings(
    target: str,
    candidates: list[str],
    max_results: int = 5,
    threshold: float = 0.5,
) -> list[tuple[str, float]]:
    """Find strings similar to target from a list of candidates.
    
    Args:
        target: The string to match against.
        candidates: List of candidate strings.
        max_results: Maximum number of results to return.
        threshold: Minimum similarity ratio to include in results.
        
    Returns:
        List of (candidate, similarity) tuples, sorted by similarity descending.
    """
    results = []
    for candidate in candidates:
        ratio = similarity_ratio(target, candidate)
        if ratio >= threshold:
            results.append((candidate, ratio))
    
    # Sort by similarity descending
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:max_results]


def normalize_whitespace(text: str) -> str:
    """Normalize whitespace in text for comparison.
    
    Collapses multiple whitespace characters into single spaces,
    strips leading/trailing whitespace.
    
    Args:
        text: Text to normalize.
        
    Returns:
        Whitespace-normalized text.
    """
    return " ".join(text.split())


def code_snippet_still_present(
    snippet: str,
    file_content: Optional[str],
    threshold: float = 85.0,
) -> Optional[bool]:
    """Check whether an issue's code snippet is still in the file content.

    Used to make issue resolution "steady": an issue is only auto-resolved
    by LLM non-detection when its own problematic code is actually gone from
    the file. This prevents LLM non-determinism from marking still-present
    issues as resolved.

    Comparison is whitespace-insensitive (both sides are normalized) and uses
    ``rapidfuzz.fuzz.partial_ratio`` so a snippet still matches when embedded
    anywhere in the file (e.g. a 2-line snippet inside a longer file).

    Args:
        snippet: The issue's ``code_snippet`` (problematic code).
        file_content: Current file content, or None if unavailable.
        threshold: Minimum ``partial_ratio`` score (0-100) to consider the
                   snippet present. Default 85.

    Returns:
        True if the snippet is still present in the file (keep issue OPEN),
        False if it is not found (legitimate fix, OK to resolve),
        None if it cannot be determined (empty snippet or missing file
        content) — callers should fall back to legacy behavior.
    """
    # Cannot determine presence without a snippet to look for.
    if not snippet or not snippet.strip():
        return None
    # Cannot determine presence without file content to search in.
    if not file_content:
        return None

    norm_snippet = normalize_whitespace(snippet)
    norm_content = normalize_whitespace(file_content)

    # Re-check after normalization: pure-whitespace snippets resolve to "".
    if not norm_snippet:
        return None

    from rapidfuzz import fuzz
    score = fuzz.partial_ratio(norm_snippet, norm_content)
    return score >= threshold


def truncate_output(
    content: str,
    max_lines: int = MAX_OUTPUT_LINES,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> tuple[str, bool, str]:
    """Truncate content if it exceeds limits.
    
    Args:
        content: Content to potentially truncate.
        max_lines: Maximum number of lines.
        max_bytes: Maximum size in bytes.
        
    Returns:
        Tuple of (truncated_content, was_truncated, hint_message).
    """
    was_truncated = False
    hint = ""
    
    # Check byte limit first
    content_bytes = content.encode('utf-8')
    if len(content_bytes) > max_bytes:
        # Truncate to approximately max_bytes
        content = content_bytes[:max_bytes].decode('utf-8', errors='ignore')
        was_truncated = True
        hint = (
            f"⚠️ OUTPUT TRUNCATED: Content exceeded {max_bytes // 1024}KB limit. "
            "Use search_text to find specific patterns or read_file with line range."
        )
    
    # Then check line limit
    lines = content.split('\n')
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        content = '\n'.join(lines)
        was_truncated = True
        hint = (
            f"⚠️ OUTPUT TRUNCATED: Content exceeded {max_lines} lines. "
            "Use search_text to find specific patterns or read_file with start_line parameter."
        )
    
    return content, was_truncated, hint


def suggest_similar_files(
    target_path: str,
    directory: Path,
    max_suggestions: int = 5,
) -> list[str]:
    """Find files with similar names in the directory.
    
    Args:
        target_path: The path that wasn't found.
        directory: Base directory to search in.
        max_suggestions: Maximum number of suggestions.
        
    Returns:
        List of similar file paths.
    """
    target_name = Path(target_path).name
    target_parts = Path(target_path).parts
    
    # Collect candidate files
    candidates = []
    try:
        for file_path in directory.rglob("*"):
            if not file_path.is_file():
                continue
            
            # Skip hidden files and common build directories
            relative_path = file_path.relative_to(directory)
            if any(part.startswith(".") for part in relative_path.parts):
                continue
            if any(
                part in {"node_modules", "__pycache__", "build", "dist", "target", ".git"}
                for part in relative_path.parts
            ):
                continue
            
            candidates.append(str(relative_path))
            
            # Limit search to avoid slowness on large repos
            if len(candidates) > 10000:
                break
    except Exception:
        return []
    
    # Score each candidate
    scored = []
    for candidate in candidates:
        candidate_name = Path(candidate).name
        candidate_parts = Path(candidate).parts
        
        # Name similarity is most important
        name_sim = similarity_ratio(target_name, candidate_name)
        
        # Path component similarity is secondary
        path_sim = 0.0
        if len(target_parts) > 1 and len(candidate_parts) > 1:
            # Compare parent directories
            target_parent = "/".join(target_parts[:-1])
            candidate_parent = "/".join(candidate_parts[:-1])
            path_sim = similarity_ratio(target_parent, candidate_parent)
        
        # Combined score (name weighted more heavily)
        score = name_sim * 0.7 + path_sim * 0.3
        
        if score > 0.3:  # Only include reasonably similar files
            scored.append((candidate, score))
    
    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)
    
    return [path for path, _ in scored[:max_suggestions]]


def format_validation_error(
    field_name: str,
    received_value: str,
    expected_type: str,
    hint: str = "",
) -> str:
    """Format a helpful validation error message.
    
    Inspired by opencode's formatValidationError pattern.
    
    Args:
        field_name: Name of the field that failed validation.
        received_value: The invalid value received.
        expected_type: Description of expected type/format.
        hint: Additional helpful hint for fixing the error.
        
    Returns:
        Formatted error message.
    """
    msg = f"Invalid '{field_name}': received '{received_value}', expected {expected_type}."
    if hint:
        msg += f" {hint}"
    return msg


def validate_file_path(file_path: str, base_dir: Path) -> tuple[bool, str, Optional[list[str]]]:
    """Validate a file path with helpful error messages and suggestions.
    
    Args:
        file_path: The path to validate.
        base_dir: Base directory for resolution and security check.
        
    Returns:
        Tuple of (is_valid, error_message, suggestions).
        If valid, error_message is empty and suggestions is None.
    """
    if not file_path:
        return False, format_validation_error(
            "file_path", "", "non-empty string",
            "Provide the path relative to the repository root."
        ), None
    
    # Resolve and check security
    try:
        full_path = (base_dir / file_path).resolve()
        full_path.relative_to(base_dir)
    except ValueError:
        return False, f"Access denied: path '{file_path}' is outside repository.", None
    except Exception as e:
        return False, f"Invalid path '{file_path}': {e}", None
    
    if not full_path.exists():
        suggestions = suggest_similar_files(file_path, base_dir)
        error_msg = f"File not found: {file_path}"
        if suggestions:
            error_msg += f". Did you mean: {', '.join(suggestions[:3])}?"
        return False, error_msg, suggestions
    
    if not full_path.is_file():
        return False, f"Not a file: {file_path}. This appears to be a directory.", None
    
    return True, "", None


def validate_line_number(
    line_number: int,
    total_lines: int,
    field_name: str = "line_number",
) -> tuple[bool, str]:
    """Validate a line number.
    
    Args:
        line_number: The line number to validate.
        total_lines: Total lines in the file.
        field_name: Name of the field for error message.
        
    Returns:
        Tuple of (is_valid, error_message).
    """
    if line_number < 1:
        return False, format_validation_error(
            field_name, str(line_number), "positive integer >= 1",
            "Line numbers are 1-based."
        )
    
    if line_number > total_lines:
        return False, format_validation_error(
            field_name, str(line_number),
            f"integer between 1 and {total_lines}",
            f"The file only has {total_lines} lines."
        )
    
    return True, ""
