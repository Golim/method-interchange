import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)


@dataclass
class ConversionResult:
    """Result of POST→GET parameter conversion.

    Fields:
        success: whether conversion succeeded
        url_with_params: original URL with query string appended
        headers: cleaned headers (no Content-Type/Length; keeps Referer)
        query_string: the converted parameters as query string
        warnings: any warnings (omitted fields, depth truncation)
        omitted_fields: fields that couldn't be converted
        error: error message if success=False
        original_request: reference to the original POST request
        lossy: True iff conversion dropped information the GET candidate cannot
            represent (e.g. multipart-with-files; explicit-null JSON values;
            JSON depth truncation). Analysis layer flags these records as
            `lossy_conversion` and excludes them from content-type-equivalence
            aggregates (still counted for route acceptance).
    """
    success: bool
    url_with_params: str
    headers: Dict[str, str]
    query_string: str
    warnings: List[str]
    omitted_fields: List[str]
    error: Optional[str]
    original_request: Dict[str, Any]
    lossy: bool = False  # must remain at end of field list (dataclass default-value ordering).


class _DepthTruncated(Exception):
    """raised by _flatten_json when depth limit is hit on
    a nested structure. Caught by ParameterConverter.convert() and converted
    to ConversionResult(success=False, error='depth_truncation', ...).
    """
    pass


class ParameterConverter:
    """
    Converts POST request bodies to GET query strings.

    Supports three content types:
    - application/x-www-form-urlencoded
    - multipart/form-data
    - application/json

    Configurable array serialization and JSON depth limits.
    """

    def __init__(self, array_style: str = "php", max_depth: int = 2):
        """
        Initialize converter with configuration.

        Args:
            array_style: Array serialization style - "php" (key[]=val),
                        "numbered" (key[0]=val), or "repeated" (key=val&key=val)
            max_depth: Maximum nesting depth for JSON objects (default 2)
        """
        if array_style not in ["php", "numbered", "repeated"]:
            raise ValueError(f"Invalid array_style: {array_style}. Must be 'php', 'numbered', or 'repeated'")

        self.array_style = array_style
        self.max_depth = max_depth

    def convert(self, request_data: Dict[str, Any]) -> ConversionResult:
        """
        Convert POST request to GET request.

        Args:
            request_data: Request dict with url, headers, postData

        Returns:
            ConversionResult with converted request or error
        """
        url = request_data.get("url", "")
        headers = request_data.get("headers", {})
        post_data = request_data.get("postData", "")

        # Get content type header value (preserve case for boundary extraction)
        content_type_raw = self._get_content_type_raw(headers)

        if not content_type_raw:
            return ConversionResult(
                success=False,
                url_with_params=url,
                headers=self._clean_headers(headers),
                query_string="",
                warnings=[],
                omitted_fields=[],
                error="No Content-Type header found",
                original_request=request_data
            )

        # Lowercase for type detection only
        content_type_lower = content_type_raw.lower()

        # Dispatch to appropriate handler
        try:
            lossy = False  # only multipart-with-files (and explicit-null JSON) flips this on.
            if "application/x-www-form-urlencoded" in content_type_lower:
                query_string, warnings, omitted = self._convert_url_encoded(post_data)
            elif "multipart/form-data" in content_type_lower:
                # Extract boundary from raw (case-sensitive) content type
                boundary = self._extract_boundary(content_type_raw)
                query_string, warnings, omitted, lossy = self._convert_form_data(post_data, boundary)
            elif "application/json" in content_type_lower:
                query_string, warnings, omitted, json_lossy = self._convert_json(post_data)
                lossy = lossy or json_lossy
            else:
                # Unsupported content type
                return ConversionResult(
                    success=False,
                    url_with_params=url,
                    headers=self._clean_headers(headers),
                    query_string="",
                    warnings=[],
                    omitted_fields=[],
                    error=f"Unsupported content type: {content_type_raw}",
                    original_request=request_data
                )

            # Build URL with query string
            url_with_params = self._append_query_string(url, query_string)

            return ConversionResult(
                success=True,
                url_with_params=url_with_params,
                headers=self._clean_headers(headers),
                query_string=query_string,
                warnings=warnings,
                omitted_fields=omitted,
                error=None,
                original_request=request_data,
                lossy=lossy,
            )

        except _DepthTruncated:
            # JSON depth limit exceeded with nested structure present.
            return ConversionResult(
                success=False,
                url_with_params=url,
                headers=self._clean_headers(headers),
                query_string="",
                warnings=[f"JSON depth limit ({self.max_depth}) exceeded; conversion failed."],
                omitted_fields=[],
                error="depth_truncation",
                original_request=request_data,
                lossy=True,
            )

        except Exception as e:
            logger.error(f"Conversion error: {e}")
            return ConversionResult(
                success=False,
                url_with_params=url,
                headers=self._clean_headers(headers),
                query_string="",
                warnings=[],
                omitted_fields=[],
                error=f"Conversion failed: {str(e)}",
                original_request=request_data
            )

    def _get_content_type_raw(self, headers: Dict[str, str]) -> Optional[str]:
        """
        Extract Content-Type header value preserving case.

        Used for boundary extraction where case matters.
        """
        for key, value in headers.items():
            if key.lower() == "content-type":
                return value
        return None

    def _extract_boundary(self, content_type: str) -> str:
        """Extract boundary from multipart content type."""
        match = re.search(r'boundary="?([^"\s;]+)"?', content_type)
        if match:
            return match.group(1)
        return ""

    def _convert_url_encoded(self, body: str) -> Tuple[str, List[str], List[str]]:
        """
        Convert URL-encoded body to query string.

        Args:
            body: URL-encoded POST body

        Returns:
            Tuple of (query_string, warnings, omitted_fields)
        """
        warnings = []
        omitted_fields = []

        try:
            # Parse URL-encoded data
            params = parse_qsl(body, keep_blank_values=True)

            # Re-encode as query string
            query_string = urlencode(params)

            return query_string, warnings, omitted_fields

        except Exception as e:
            logger.warning(f"Error parsing URL-encoded data: {e}")
            return "", [f"Parse error: {str(e)}"], []

    def _convert_form_data(self, body: str, boundary: str) -> Tuple[str, List[str], List[str], bool]:
        """
        Convert multipart/form-data body to query string.

        Omits file upload fields with warnings.

        Args:
            body: Multipart form-data body
            boundary: Boundary string

        Returns:
            Tuple of (query_string, warnings, omitted_fields, lossy_seen).
            `lossy_seen` is True when at least one multipart part carried a
            `filename=` attribute (i.e. a file upload was stripped). This
            surfaces as ConversionResult.lossy=True so the analysis layer can
            exclude these records from content-type-equivalence aggregates.
        """
        warnings = []
        omitted_fields = []
        params = []
        lossy_seen = False

        if not boundary:
            warnings.append("No boundary found in multipart data")
            return "", warnings, omitted_fields, lossy_seen

        # Split by boundary - this removes the boundary itself, leaving content after each boundary
        parts = body.split(f"--{boundary}")

        for part in parts:
            # Skip empty parts or closing boundary marker (--)
            if not part.strip() or part.strip() == "--":
                continue

            # Extract field name from Content-Disposition header
            name_match = re.search(r'name="([^"]+)"', part)
            if not name_match:
                continue

            field_name = name_match.group(1)

            # Check if this is a file upload (has filename attribute)
            if 'filename=' in part:
                warnings.append(f"Omitted file upload field: {field_name}")
                omitted_fields.append(field_name)
                lossy_seen = True
                continue

            # Extract value: content after the header section (after blank line)
            # Parts start with \n (from boundary split), so we need to handle this
            # Look for the double-newline pattern that separates headers from content

            # Split on \r\n\r\n or \n\n to separate headers from content
            if '\r\n\r\n' in part:
                _, value = part.split('\r\n\r\n', 1)
            elif '\n\n' in part:
                _, value = part.split('\n\n', 1)
            else:
                # No clear header/content separation, skip
                continue

            # Clean up value (strip leading/trailing whitespace)
            value = value.strip()

            if value is not None:  # Allow empty strings, reject None
                params.append((field_name, value))

        query_string = urlencode(params)
        return query_string, warnings, omitted_fields, lossy_seen

    def _convert_json(self, body: str) -> Tuple[str, List[str], List[str], bool]:
        """
        Convert JSON body to query string with bracket notation.

        Respects max_depth limit for nested objects.

        Args:
            body: JSON string

        Returns:
            Tuple of (query_string, warnings, omitted_fields, lossy_seen).
            `lossy_seen` is True when at least one null value was dropped
            during flattening — the GET candidate cannot represent "key was
            present with explicit null".
        """
        warnings = []
        omitted_fields = []
        lossy_seen = [False]

        try:
            # Parse JSON
            data = json.loads(body)

            # Flatten JSON structure
            params = self._flatten_json(
                data, prefix="", depth=0,
                warnings=warnings, omitted_fields=omitted_fields,
                lossy_seen=lossy_seen,
            )

            # Encode as query string
            query_string = urlencode(params, doseq=True)

            return query_string, warnings, omitted_fields, lossy_seen[0]

        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON: {e}")
            return "", [f"JSON parse error: {str(e)}"], [], False

    def _flatten_json(
        self,
        obj: Any,
        prefix: str = "",
        depth: int = 0,
        warnings: Optional[List[str]] = None,
        omitted_fields: Optional[List[str]] = None,
        lossy_seen: Optional[List[bool]] = None,
    ) -> List[Tuple[str, str]]:
        """
        Recursively flatten JSON object with bracket notation.

        Args:
            obj: Object to flatten (dict, list, or scalar)
            prefix: Current key prefix
            depth: Current nesting depth
            warnings: List to append warnings to
            omitted_fields: List to append omitted field names to
            lossy_seen: Single-element mutable flag list flipped to [True]
                when a null value is dropped.

        Returns:
            List of (key, value) tuples
        """
        if warnings is None:
            warnings = []
        if omitted_fields is None:
            omitted_fields = []
        if lossy_seen is None:
            lossy_seen = [False]

        params = []

        if isinstance(obj, dict):
            # Depth limit + nested structure FAILS the conversion via the
            # _DepthTruncated sentinel. Previously this emitted a warning
            # and returned partial scalars, which silently produced a
            # degraded GET candidate.
            if depth >= self.max_depth:
                has_nested = any(isinstance(v, (dict, list)) for v in obj.values())
                if has_nested:
                    raise _DepthTruncated(prefix)

            for key, value in obj.items():
                new_key = f"{prefix}[{key}]" if prefix else key

                if value is None:
                    warnings.append(f"Omitted null value for key: {new_key}")
                    omitted_fields.append(new_key)
                    lossy_seen[0] = True
                elif isinstance(value, (dict, list)):
                    params.extend(self._flatten_json(value, new_key, depth + 1, warnings, omitted_fields, lossy_seen))
                elif isinstance(value, bool):
                    # Convert boolean to lowercase string
                    params.append((new_key, str(value).lower()))
                else:
                    params.append((new_key, str(value)))

        elif isinstance(obj, list):
            # Serialize array based on configured style
            params.extend(self._serialize_array(prefix, obj, depth, warnings, omitted_fields, lossy_seen))

        else:
            # Scalar value
            if obj is None:
                warnings.append(f"Omitted null value for key: {prefix}")
                omitted_fields.append(prefix)
                lossy_seen[0] = True
            else:
                params.append((prefix, str(obj)))

        return params

    def _serialize_array(
        self,
        key: str,
        values: List[Any],
        depth: int,
        warnings: List[str],
        omitted_fields: List[str],
        lossy_seen: Optional[List[bool]] = None,
    ) -> List[Tuple[str, str]]:
        """
        Serialize array based on configured array_style.

        Args:
            key: Parameter key name
            values: List of values
            depth: Current nesting depth
            warnings: List to append warnings to
            omitted_fields: List to append omitted field names to
            lossy_seen: Single-element mutable flag flipped on null array items.

        Returns:
            List of (key, value) tuples
        """
        if lossy_seen is None:
            lossy_seen = [False]

        params = []

        for idx, value in enumerate(values):
            if isinstance(value, (dict, list)):
                # Nested structure in array
                if self.array_style == "php":
                    array_key = f"{key}[]"
                elif self.array_style == "numbered":
                    array_key = f"{key}[{idx}]"
                else:  # repeated
                    array_key = key

                params.extend(self._flatten_json(value, array_key, depth + 1, warnings, omitted_fields, lossy_seen))
            else:
                # Scalar value
                if value is None:
                    warnings.append(f"Omitted null value in array: {key}[{idx}]")
                    omitted_fields.append(f"{key}[{idx}]")
                    lossy_seen[0] = True
                    continue

                if self.array_style == "php":
                    # PHP style: key[]=value1&key[]=value2
                    params.append((f"{key}[]", str(value)))
                elif self.array_style == "numbered":
                    # Numbered style: key[0]=value1&key[1]=value2
                    params.append((f"{key}[{idx}]", str(value)))
                else:  # repeated
                    # Repeated style: key=value1&key=value2
                    params.append((key, str(value)))

        return params

    def _clean_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Clean headers for GET request.

        Removes Content-Type and Content-Length (no body in GET).
        Preserves Referer and other headers.

        Args:
            headers: Original request headers

        Returns:
            Cleaned headers dict
        """
        cleaned = {}

        for key, value in headers.items():
            key_lower = key.lower()
            # Remove body-related headers
            if key_lower in ["content-type", "content-length"]:
                continue
            cleaned[key] = value

        return cleaned

    def _append_query_string(self, url: str, query_string: str) -> str:
        """
        Append query string to URL.

        Handles existing query parameters properly.

        Args:
            url: Original URL
            query_string: Query string to append

        Returns:
            URL with query string appended
        """
        if not query_string:
            return url

        parsed = urlparse(url)

        # Merge with existing query string if present
        if parsed.query:
            combined = f"{parsed.query}&{query_string}"
        else:
            combined = query_string

        # Reconstruct URL
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            combined,
            ''  # Empty fragment instead of parsed.fragment
        ))


def has_top_level_json_array(request_data: Dict[str, Any]) -> bool:
    """Return True iff this captured POST body should trigger dual array-style emission.

    Pure function. Used by ReplayEngine to decide whether to issue one or
    two GET candidates per captured POST.
    """
    headers = request_data.get("headers", {}) or {}
    content_type = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            content_type = (v or "").lower()
            break
    if "application/json" not in content_type:
        return False
    try:
        data = json.loads(request_data.get("postData", "") or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if isinstance(data, list):
        return True
    if isinstance(data, dict):
        return any(isinstance(v, list) for v in data.values())
    return False
