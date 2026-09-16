#!/usr/bin/env python3
import json
import re
from typing import Dict, List, Any
from lib.logging_setup import get_logger

logger = get_logger("normalizer")


class ContentNormalizer:
    """
    Normalizes dynamic content in HTTP responses before comparison.

    Handles three categories of dynamic content:
    1. CSRF/Token normalization (csrf, token, authenticity_token patterns)
    2. Timestamp normalization (ISO dates, Unix timestamps, relative times)
    3. Session ID normalization (session identifiers in cookies, headers, body)
    """

    def __init__(self):
        """Initialize normalizer with compiled regex patterns and empty actions list."""
        self.actions: List[Dict[str, Any]] = []

        # CSRF/Token patterns (case-insensitive, exact field name matching)
        # Matches common CSRF field names: csrf_token, csrf-token, authenticity_token, _token, etc.
        # Does NOT match OAuth tokens (access_token, refresh_token) or API tokens (token_type)
        self.csrf_pattern = re.compile(
            r'^csrf[-_]?token$|^authenticity[-_]?token$|^_token$|^csrfmiddlewaretoken$|^__RequestVerificationToken$',
            re.IGNORECASE
        )

        # Session ID patterns (exact field name matching)
        # Matches common session ID field names: session_id, sid, PHPSESSID, etc.
        # Does NOT match fields containing session-like substrings (user_session_id, previous_sid)
        self.session_pattern = re.compile(
            r'^session[-_]?id$|^sid$|^PHPSESSID$|^JSESSIONID$|^ASP\.NET_SessionId$',
            re.IGNORECASE
        )

        # Timestamp patterns
        # ISO 8601: 2026-02-14T15:30:45Z or 2026-02-14T15:30:45+00:00
        self.iso_timestamp_pattern = re.compile(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})'
        )

        # Unix timestamps (10+ digits)
        self.unix_timestamp_pattern = re.compile(r'\b\d{10,13}\b')

        # Relative times: "2 mins ago", "5 hours ago", "just now"
        self.relative_time_pattern = re.compile(
            r'\d+\s+(min|mins|minute|minutes|hour|hours|day|days|second|seconds|sec|secs)\s+ago|just\s+now',
            re.IGNORECASE
        )

        # Common date formats
        self.date_pattern = re.compile(
            r'\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}',
            re.IGNORECASE
        )

    def normalize_body(self, body: str, content_type: str = "") -> str:
        """
        Normalize response body based on content type.

        Args:
            body: Response body text
            content_type: Content-Type header value

        Returns:
            Normalized body with placeholders replacing dynamic content
        """
        if not body:
            return body

        # Dispatch based on content type
        if "html" in content_type.lower() or "<html" in body.lower() or "<body" in body.lower():
            return self._normalize_html(body)
        elif "json" in content_type.lower() or (body.strip().startswith(("{", "["))):
            return self._normalize_json(body)
        else:
            # Non-HTML/JSON content types: return unchanged
            return body

    def _normalize_html(self, body: str) -> str:
        """
        Normalize HTML content: hidden inputs, meta tags, timestamps in text.

        Args:
            body: HTML body text

        Returns:
            Normalized HTML
        """
        result = body

        # Normalize input fields with CSRF/token/session patterns
        def replace_input_value(match):
            name = match.group(1)
            value = match.group(2)

            if self.csrf_pattern.search(name):
                self._log_action("csrf_hidden_input", name, value, "__CSRF_NORMALIZED__")
                return match.group(0).replace(f'value="{value}"', 'value="__CSRF_NORMALIZED__"')
            elif self.session_pattern.search(name):
                self._log_action("session_hidden_input", name, value, "__SESSION_NORMALIZED__")
                return match.group(0).replace(f'value="{value}"', 'value="__SESSION_NORMALIZED__"')

            return match.group(0)

        result = re.sub(
            r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']+)["\'][^>]*>',
            replace_input_value,
            result,
            flags=re.IGNORECASE
        )

        # Normalize CSRF tokens in meta tags
        def replace_meta_csrf(match):
            name = match.group(1)
            value = match.group(2)
            if self.csrf_pattern.search(name):
                self._log_action("csrf_meta_tag", name, value, "__CSRF_NORMALIZED__")
                return match.group(0).replace(f'content="{value}"', 'content="__CSRF_NORMALIZED__"')
            return match.group(0)

        result = re.sub(
            r'<meta[^>]+name=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\'][^>]*>',
            replace_meta_csrf,
            result,
            flags=re.IGNORECASE
        )

        # Normalize ISO 8601 timestamps in body text
        def replace_iso_timestamp(match):
            self._log_action("iso_timestamp", "body_text", match.group(0), "__TIMESTAMP_NORMALIZED__")
            return "__TIMESTAMP_NORMALIZED__"

        result = self.iso_timestamp_pattern.sub(replace_iso_timestamp, result)

        # Normalize Unix timestamps
        def replace_unix_timestamp(match):
            self._log_action("unix_timestamp", "body_text", match.group(0), "__TIMESTAMP_NORMALIZED__")
            return "__TIMESTAMP_NORMALIZED__"

        result = self.unix_timestamp_pattern.sub(replace_unix_timestamp, result)

        # Normalize relative times
        def replace_relative_time(match):
            self._log_action("relative_time", "body_text", match.group(0), "__TIMESTAMP_NORMALIZED__")
            return "__TIMESTAMP_NORMALIZED__"

        result = self.relative_time_pattern.sub(replace_relative_time, result)

        return result

    def _normalize_json(self, body: str) -> str:
        """
        Normalize JSON content: walk object tree, replace values for matching keys.

        Args:
            body: JSON body text

        Returns:
            Normalized JSON string
        """
        try:
            obj = json.loads(body)
            normalized_obj = self._normalize_json_recursive(obj)
            return json.dumps(normalized_obj, separators=(',', ': '), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            # Invalid JSON - return unchanged
            return body

    def _normalize_json_recursive(self, obj: Any, path: str = "") -> Any:
        """
        Recursively walk JSON object tree and normalize matching fields.

        Args:
            obj: JSON object (dict, list, or primitive)
            path: Current key path (for logging)

        Returns:
            Normalized object
        """
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                full_path = f"{path}.{key}" if path else key

                # Check if key matches CSRF/token pattern
                if self.csrf_pattern.search(key) and isinstance(value, str):
                    self._log_action("csrf_json_field", full_path, value, "__CSRF_NORMALIZED__")
                    result[key] = "__CSRF_NORMALIZED__"
                # Check if key matches session pattern
                elif self.session_pattern.search(key) and isinstance(value, str):
                    self._log_action("session_json_field", full_path, value, "__SESSION_NORMALIZED__")
                    result[key] = "__SESSION_NORMALIZED__"
                # Check if key matches timestamp/date patterns
                elif self._is_timestamp_key(key) and isinstance(value, str):
                    self._log_action("timestamp_json_field", full_path, value, "__TIMESTAMP_NORMALIZED__")
                    result[key] = "__TIMESTAMP_NORMALIZED__"
                else:
                    # Recurse into nested objects/arrays
                    result[key] = self._normalize_json_recursive(value, full_path)
            return result

        elif isinstance(obj, list):
            return [self._normalize_json_recursive(item, f"{path}[{i}]") for i, item in enumerate(obj)]

        else:
            # Primitive value - return as-is
            return obj

    def normalize_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Normalize response headers by removing volatile headers.

        Strips: Set-Cookie, Date, X-Request-ID (case-insensitive)

        Args:
            headers: Response headers dict

        Returns:
            Copy of headers with volatile keys removed
        """
        if not headers:
            return {}

        # Volatile headers to strip (case-insensitive)
        volatile_keys = {"set-cookie", "date", "x-request-id"}

        result = {}
        for key, value in headers.items():
            if key.lower() not in volatile_keys:
                result[key] = value
            else:
                self._log_action("volatile_header", key, value, "REMOVED")

        return result

    def get_actions(self) -> List[Dict[str, Any]]:
        """
        Get list of all normalization actions performed.

        Returns:
            List of action dicts with pattern, field, original, replacement
        """
        return self.actions

    def reset(self):
        """Clear actions list for fresh normalization pass."""
        self.actions = []

    def _is_csrf_match(self, field_name: str) -> bool:
        """Check if field name matches CSRF pattern."""
        return bool(self.csrf_pattern.search(field_name))

    def _is_timestamp_key(self, key: str) -> bool:
        """Check if key name suggests timestamp/date field."""
        timestamp_keywords = [
            'timestamp', 'created_at', 'updated_at', 'modified_at',
            'date', 'time', 'datetime', 'posted_at', 'published_at'
        ]
        return any(keyword in key.lower() for keyword in timestamp_keywords)

    def _log_action(self, pattern: str, field: str, original: str, replacement: str):
        """
        Log a normalization action.

        Args:
            pattern: Pattern type (e.g., 'csrf_hidden_input', 'iso_timestamp')
            field: Field name or location
            original: Original value
            replacement: Replacement value
        """
        action = {
            "pattern": pattern,
            "field": field,
            "original": original,
            "replacement": replacement
        }
        self.actions.append(action)
        logger.debug(f"Normalization: {pattern} in '{field}' - replaced '{original}' with '{replacement}'")
