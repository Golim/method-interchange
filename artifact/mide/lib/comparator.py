import json
import math
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Any, Dict, Tuple

from lib.logging_setup import get_logger


@dataclass
class ComparisonResult:
    """Result of comparing two HTTP responses"""
    exact: float  # 0-100: exact string match
    sequence_matcher: float  # 0-100: Ratcliff/Obershelp similarity (difflib.SequenceMatcher)
    structural: float  # 0-100: JSON/HTML structure similarity
    cosine: float  # 0-100: vector-based text similarity
    status_codes_match: bool  # True if status codes identical
    semantic_mismatch: bool  # True if response-level JSON outcome fields differ
    size_ratio: float  # 0.0-1.0: smaller/larger
    headers_similarity: float  # 0-100: header key overlap
    is_interchangeable: bool  # Verdict based on threshold
    post_status: int  # POST response status code
    get_status: int  # GET response status code


class HTMLTagExtractor(HTMLParser):
    """Extract tag sequence from HTML for structural comparison"""

    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_endtag(self, tag):
        self.tags.append(f"/{tag}")


class ResponseComparator:
    """Compare HTTP responses using multiple similarity algorithms"""

    JSON_SEMANTIC_FIELD_NAMES = frozenset({
        "result",
        "success",
        "succeeded",
        "ok",
        "valid",
        "accepted",
        "error",
        "errors",
        "error_code",
        "errorcode",
        "code",
        "status",
        "status_code",
        "statuscode",
        "state",
        "message",
        "reason",
    })

    def __init__(self, threshold: float = 95.0):
        """
        Initialize comparator with similarity threshold.

        Args:
            threshold: Similarity percentage (0-100) above which responses
                      are considered interchangeable. Default: 95.0
                      Uses structural similarity for the verdict.
        """
        self.threshold = threshold
        self.logger = get_logger("comparator")

    def compare(self, post_response: Dict, get_response: Dict) -> ComparisonResult:
        """
        Compare POST and GET responses using all four algorithms.

        Args:
            post_response: Dict with keys: status_code, headers, body
            get_response: Dict with keys: status_code, headers, body

        Returns:
            ComparisonResult with all similarity scores and verdict
        """
        post_status = post_response["status_code"]
        get_status = get_response["status_code"]
        post_body = post_response["body"]
        get_body = get_response["body"]
        post_headers = post_response.get("headers", {})
        get_headers = get_response.get("headers", {})

        # Check status code match (gate for interchangeability)
        status_match = post_status == get_status

        # Extract content type for structural similarity
        content_type = post_headers.get("Content-Type", "")

        # Early exit #1: Status codes don't match - responses are not interchangeable
        if not status_match:
            self.logger.debug("Status code mismatch (%d vs %d), skipping similarity computation",
                            post_status, get_status)
            exact = 0.0
            sequence_matcher = 0.0
            structural = 0.0
            cosine = 0.0
        else:
            # Calculate exact match first (cheapest check)
            exact = self._exact_match(post_body, get_body)

            # Early exit #2: Exact match - all algorithms will return 100.0
            if exact == 100.0:
                self.logger.debug("Exact match detected, skipping other similarity algorithms")
                sequence_matcher = 100.0
                structural = 100.0
                cosine = 100.0
            else:
                # Calculate remaining similarity scores
                sequence_matcher = self._sequence_matcher_similarity(post_body, get_body)
                structural = self._structural_similarity(post_body, get_body, content_type)
                cosine = self._cosine_similarity(post_body, get_body)

        semantic_mismatch, mismatch_paths = self._json_semantic_mismatch(
            post_body, get_body, content_type
        )
        if semantic_mismatch:
            self.logger.debug(
                "JSON semantic mismatch on response fields: %s",
                ", ".join(mismatch_paths),
            )

        # Compare headers
        headers_sim = self._compare_headers(post_headers, get_headers)

        # Calculate size ratio (smaller/larger)
        size_ratio = self._size_ratio(post_body, get_body)

        # Verdict: status codes must match, structures must be close, and JSON
        # application-level outcome fields must not contradict each other.
        is_interchangeable = (
            status_match
            and structural >= self.threshold
            and not semantic_mismatch
        )

        return ComparisonResult(
            exact=exact,
            sequence_matcher=sequence_matcher,
            structural=structural,
            cosine=cosine,
            status_codes_match=status_match,
            semantic_mismatch=semantic_mismatch,
            size_ratio=size_ratio,
            headers_similarity=headers_sim,
            is_interchangeable=is_interchangeable,
            post_status=post_status,
            get_status=get_status
        )

    def _exact_match(self, a: str, b: str) -> float:
        """
        Exact string match: 100.0 if identical, 0.0 otherwise.

        Args:
            a: First string
            b: Second string

        Returns:
            100.0 if identical, 0.0 otherwise
        """
        return 100.0 if a == b else 0.0

    def _sequence_matcher_similarity(self, a: str, b: str) -> float:
        """
        Character-level similarity using difflib.SequenceMatcher (Ratcliff/Obershelp algorithm).

        The Ratcliff/Obershelp algorithm finds the longest contiguous matching subsequence,
        then recursively processes remaining segments. Quadratic time in worst case,
        linear time in best case. Does not yield minimal edit sequences but produces
        matches that "look right" to humans.

        Args:
            a: First string
            b: Second string

        Returns:
            Similarity percentage (0-100)
        """
        # Handle empty strings
        if not a and not b:
            return 100.0  # Both empty = identical
        if not a or not b:
            return 0.0  # One empty = completely different

        # Size guard: reject large inputs
        total_len = len(a) + len(b)
        if total_len > 200_000:  # 100KB per body
            self.logger.warning(
                "SequenceMatcher skipped due to large response size (%d bytes total, limit: 200KB)",
                total_len
            )
            return 0.0

        matcher = SequenceMatcher(None, a, b)
        
        return matcher.ratio() * 100.0

    def _structural_similarity(self, a: str, b: str, content_type: str = "") -> float:
        """
        Structure-aware similarity for JSON/HTML content.

        - JSON: Compare key structure (ignore dynamic values)
        - HTML: Compare tag sequence (ignore text content)
        - Other: Fallback to SequenceMatcher similarity

        Args:
            a: First string
            b: Second string
            content_type: Content-Type header value

        Returns:
            Structural similarity percentage (0-100)
        """
        # Check both bodies for HTML content
        a_is_html = "html" in content_type.lower() or "<html" in a.lower()[:1000] or "<body" in a.lower()[:1000]
        b_is_html = "<html" in b.lower()[:1000] or "<body" in b.lower()[:1000]

        if a_is_html and b_is_html:
            return self._html_structural_similarity(a, b)
        elif a_is_html != b_is_html:
            # Content type mismatch - one HTML, one not
            return 0.0

        # JSON detection (check both bodies)
        a_is_json = "json" in content_type.lower() or (a.strip().startswith(("{", "[")) if a else False)
        b_is_json = b.strip().startswith(("{", "[")) if b else False

        if a_is_json and b_is_json:
            return self._json_structural_similarity(a, b)
        elif a_is_json != b_is_json:
            return 0.0

        # Fallback to sequence matcher for plaintext
        return self._sequence_matcher_similarity(a, b)

    def _json_structural_similarity(self, a: str, b: str) -> float:
        """
        Compare JSON structure (keys) ignoring values.

        Args:
            a: First JSON string
            b: Second JSON string

        Returns:
            Key structure similarity (0-100)
        """
        try:
            obj_a = json.loads(a)
            obj_b = json.loads(b)

            # Extract all keys recursively
            keys_a = self._extract_json_keys(obj_a)
            keys_b = self._extract_json_keys(obj_b)

            # Calculate key overlap (Jaccard similarity)
            if not keys_a and not keys_b:
                return 100.0  # Both empty

            intersection = len(keys_a & keys_b)
            union = len(keys_a | keys_b)

            if union == 0:
                return 100.0  # Both empty

            return (intersection / union) * 100.0

        except (json.JSONDecodeError, TypeError):
            # Invalid JSON - fallback to SequenceMatcher
            return self._sequence_matcher_similarity(a, b)

    def _json_semantic_mismatch(
        self,
        a: str,
        b: str,
        content_type: str = "",
    ) -> Tuple[bool, Tuple[str, ...]]:
        """Detect contradictory JSON outcome/status fields.

        Structural comparison deliberately ignores values so dynamic tokens do
        not cause false negatives. Some APIs, however, return application-level
        success/failure in a JSON body while keeping HTTP 200. Fields such as
        result/success/code/status are response semantics, not noise, so common
        paths with different primitive values veto interchangeability.
        """
        a_is_json = "json" in content_type.lower() or (
            a.strip().startswith(("{", "[")) if a else False
        )
        b_is_json = b.strip().startswith(("{", "[")) if b else False
        if not (a_is_json and b_is_json):
            return False, ()

        try:
            obj_a = json.loads(a)
            obj_b = json.loads(b)
        except (json.JSONDecodeError, TypeError):
            return False, ()

        values_a = self._extract_json_semantic_values(obj_a)
        values_b = self._extract_json_semantic_values(obj_b)
        common_paths = values_a.keys() & values_b.keys()
        mismatched = tuple(
            sorted(
                path for path in common_paths
                if self._normalize_json_semantic_value(values_a[path])
                != self._normalize_json_semantic_value(values_b[path])
            )
        )
        return bool(mismatched), mismatched

    def _extract_json_semantic_values(self, obj: Any, prefix: str = "") -> Dict[str, Any]:
        """Return primitive values for JSON fields that carry response outcome."""
        values: Dict[str, Any] = {}

        if isinstance(obj, dict):
            for key, value in obj.items():
                key_text = str(key)
                full_key = f"{prefix}.{key_text}" if prefix else key_text
                normalized_key = key_text.lower().replace("-", "_")
                if (
                    normalized_key in self.JSON_SEMANTIC_FIELD_NAMES
                    and self._is_json_primitive(value)
                ):
                    values[full_key] = value
                values.update(self._extract_json_semantic_values(value, full_key))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                list_prefix = f"{prefix}[{i}]" if prefix else f"[{i}]"
                values.update(self._extract_json_semantic_values(item, list_prefix))

        return values

    def _is_json_primitive(self, value: Any) -> bool:
        return value is None or isinstance(value, (bool, int, float, str))

    def _normalize_json_semantic_value(self, value: Any) -> Tuple[str, Any]:
        """Normalize common wire-format variants without merging bool and int."""
        if isinstance(value, bool):
            return ("bool", value)
        if value is None:
            return ("null", None)
        if isinstance(value, (int, float)):
            return ("number", value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "false"}:
                return ("bool", text == "true")
            try:
                if text and all(ch not in text for ch in ".eE"):
                    return ("number", int(text))
                return ("number", float(text))
            except ValueError:
                return ("string", text)
        return (type(value).__name__, value)

    def _extract_json_keys(self, obj, prefix="") -> set:
        """
        Recursively extract all keys from JSON object.

        Args:
            obj: JSON object (dict, list, or primitive)
            prefix: Key prefix for nested objects

        Returns:
            Set of all key paths
        """
        keys = set()

        if isinstance(obj, dict):
            for key, value in obj.items():
                full_key = f"{prefix}.{key}" if prefix else key
                keys.add(full_key)
                keys.update(self._extract_json_keys(value, full_key))

        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                keys.update(self._extract_json_keys(item, f"{prefix}[{i}]"))

        return keys

    def _html_structural_similarity(self, a: str, b: str) -> float:
        """
        Compare HTML tag structure ignoring text content.

        Args:
            a: First HTML string
            b: Second HTML string

        Returns:
            Tag structure similarity (0-100)
        """
        try:
            # Extract tag sequences
            parser_a = HTMLTagExtractor()
            parser_a.feed(a)
            tags_a = parser_a.tags

            parser_b = HTMLTagExtractor()
            parser_b.feed(b)
            tags_b = parser_b.tags

            # Compare tag sequences using SequenceMatcher
            if not tags_a and not tags_b:
                return 100.0  # Both empty

            matcher = SequenceMatcher(None, tags_a, tags_b)
            return matcher.ratio() * 100.0

        except Exception:
            # HTML parsing error - fallback to SequenceMatcher
            return self._sequence_matcher_similarity(a, b)

    def _cosine_similarity(self, a: str, b: str) -> float:
        """
        Vector-based similarity using word frequency vectors.

        Uses collections.Counter for word frequency and basic math
        for cosine similarity (no numpy needed).

        Args:
            a: First string
            b: Second string

        Returns:
            Cosine similarity percentage (0-100)
        """
        # Handle empty strings
        if not a and not b:
            return 100.0  # Both empty = identical
        if not a or not b:
            return 0.0  # One empty = no similarity

        # Tokenize into words (lowercase, split on whitespace)
        words_a = a.lower().split()
        words_b = b.lower().split()

        # Handle empty word lists
        if not words_a and not words_b:
            return 100.0
        if not words_a or not words_b:
            return 0.0

        # Build frequency vectors
        freq_a = Counter(words_a)
        freq_b = Counter(words_b)

        # Get all unique words
        all_words = set(freq_a.keys()) | set(freq_b.keys())

        # Calculate dot product and magnitudes
        dot_product = sum(freq_a.get(word, 0) * freq_b.get(word, 0) for word in all_words)
        magnitude_a = math.sqrt(sum(count ** 2 for count in freq_a.values()))
        magnitude_b = math.sqrt(sum(count ** 2 for count in freq_b.values()))

        # Avoid division by zero
        if magnitude_a == 0 or magnitude_b == 0:
            return 0.0

        # Calculate cosine similarity
        cosine_sim = dot_product / (magnitude_a * magnitude_b)

        return cosine_sim * 100.0

    def _compare_headers(self, h1: Dict, h2: Dict) -> float:
        """
        Compare header key overlap (ignore values for dynamic headers).

        Args:
            h1: First headers dict
            h2: Second headers dict

        Returns:
            Header key similarity (0-100)
        """
        if not h1 and not h2:
            return 100.0  # Both empty

        # Normalize header names to lowercase for case-insensitive comparison
        keys_1 = set(k.lower() for k in h1.keys())
        keys_2 = set(k.lower() for k in h2.keys())

        # Calculate Jaccard similarity
        intersection = len(keys_1 & keys_2)
        union = len(keys_1 | keys_2)

        if union == 0:
            return 100.0  # Both empty

        return (intersection / union) * 100.0

    def _size_ratio(self, a: str, b: str) -> float:
        """
        Calculate size ratio (smaller/larger) as quick heuristic.

        Args:
            a: First string
            b: Second string

        Returns:
            Size ratio (0.0-1.0)
        """
        len_a = len(a)
        len_b = len(b)

        if len_a == 0 and len_b == 0:
            return 1.0  # Both empty = same size

        if len_a == 0 or len_b == 0:
            return 0.0  # One empty = no similarity

        return min(len_a, len_b) / max(len_a, len_b)
