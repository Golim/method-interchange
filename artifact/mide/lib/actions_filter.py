from __future__ import annotations

import re
from typing import Mapping, Optional
from urllib.parse import urlparse

# Single regex, compiled at import. Matches Content-Disposition lines that
# bear filename= in any of three RFC-7578-permitted (or seen-in-the-wild)
# spellings: filename="x", filename=x, filename*=UTF-8''x.
# Anchoring on Content-Disposition prevents false positives from text fields
# that happen to contain the literal substring "filename=".
_FILENAME_RE = re.compile(
    rb"Content-Disposition:\s*form-data;[^\r\n]*filename\*?=",
    re.IGNORECASE,
)


def match_dangerous_action(url: str, patterns: list) -> Optional[str]:
    """Return the first pattern that case-insensitively appears in the URL path, else None.

    URL-path substring match, case-insensitive — no regex. Operates on
    raw URL path (no percent-decoding).
    """
    if not patterns:
        return None
    path_lower = urlparse(url).path.lower()
    for p in patterns:
        if p.lower() in path_lower:
            return p
    return None


def detect_file_upload(
    headers: Mapping[str, str],
    post_data_buffer: Optional[bytes],
) -> Optional[str]:
    """
    Return 'multipart_file_field' if the request is multipart/form-data with at least one part bearing filename= in its Content-Disposition,
    else None.
    """
    if not post_data_buffer:
        return None
    ctype = headers.get("content-type", "")
    if not ctype.lower().startswith("multipart/form-data"):
        return None
    if _FILENAME_RE.search(post_data_buffer):
        return "multipart_file_field"
    return None
