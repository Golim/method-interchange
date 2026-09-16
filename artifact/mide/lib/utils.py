import hashlib
import json
import logging
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, urljoin, parse_qsl

_log = logging.getLogger(__name__)


# Excluded file extensions (ported from legacy crawler.py with Bug #1 fixed)
# Bug #1 fix: Added missing comma between '.mpd' and '.ps' (line 109-110 in legacy)
EXCLUDED_EXTENSIONS = frozenset([
    '.webm', '.m3u', '.m3u8', '.pls', '.cue', '.wpl', '.asx', '.xspf', '.mpd',
    '.ps', '.tif', '.tiff', '.ppt', '.pptx', '.xls', '.xlsx', '.dll', '.msi',
    '.iso', '.sql', '.apk', '.jar', '.bmp', '.gif', '.jpg', '.jpeg', '.png',
    '.zip', '.exe', '.dmg', '.doc', '.docx', '.odt', '.pdf', '.rtf', '.tex',
    '.mpg', '.mpeg', '.avi', '.mov', '.wmv', '.flv', '.swf', '.mp4', '.m4v',
    '.mp3', '.ogg', '.wav', '.wma', '.7z', '.rpm', '.gz', '.tar', '.deb',
])


def extract_domain(url: str) -> str:
    """
    Extract domain from URL.

    Args:
        url: URL to extract domain from

    Returns:
        Domain name (netloc without port)

    Example:
        >>> extract_domain('https://www.example.com:8080/path')
        'www.example.com'
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        return domain

    except Exception:
        return ''


def extract_filename_from_domain(url: str) -> str:
    """
    Extract domain from URL and format for filesystem safety.

    Replaces port with dash to ensure cross-platform compatibility
    (Windows doesn't allow colons in filenames).

    Args:
        url: URL to extract domain from
    """
    try:
        parsed = urlparse(url)
        # Use hostname to get domain without port
        domain = parsed.hostname.lower() if parsed.hostname else parsed.netloc.lower()

        # If port exists, append with dash (safe for directory names)
        if parsed.port:
            domain = f"{domain}-{parsed.port}"

        return domain
    except Exception:
        return "unknown"


def normalize_url(url: str, base_url: Optional[str] = None) -> str:
    """
    Normalize URL for consistent deduplication.

    - Resolves relative URLs to absolute using base_url
    - Strips fragments (#section)
    - Strips trailing slashes for consistency

    Args:
        url: URL to normalize
        base_url: Base URL for resolving relative URLs (optional)
    """
    # Resolve relative URLs
    if base_url:
        url = urljoin(base_url, url)

    # Parse and strip fragment
    parsed = urlparse(url)
    normalized = parsed._replace(fragment='').geturl()

    # Strip trailing slash (except for root)
    if normalized.endswith('/') and normalized.count('/') > 3:
        normalized = normalized.rstrip('/')

    return normalized


def is_excluded_extension(url: str) -> bool:
    """
    Check if URL has an excluded file extension.

    Excluded extensions are media files, documents, archives, etc.
    that should not be crawled.

    Args:
        url: URL to check

    Returns:
        True if URL has excluded extension, False otherwise

    Example:
        >>> is_excluded_extension('https://site.com/file.pdf')
        True
        >>> is_excluded_extension('https://site.com/file.ps')
        True
        >>> is_excluded_extension('https://site.com/page.html')
        False
    """
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()

        # Check if path ends with any excluded extension
        for ext in EXCLUDED_EXTENSIONS:
            if path.endswith(ext):
                return True

        return False

    except Exception:
        return False


def _normalize_content_type(content_type: str) -> str:
    """Lowercase and strip charset/params (everything after first ';').

    Args:
        content_type: Raw Content-Type header value (may be empty).

    Returns:
        Normalized lowercase media type, no params. Empty string if input falsy.

    Example:
        >>> _normalize_content_type('Application/JSON; charset=utf-8')
        'application/json'
    """
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _extract_multipart_names(content_type: str, body: str) -> Tuple[str, ...]:
    """Walk multipart parts; return sorted unique Content-Disposition `name=` values.

    Uses stdlib `email.parser.BytesParser` (Python 3.11+ stable).
    Returns () on parse failure or non-multipart body (debug log; does NOT raise).
    """
    if not body:
        return ()
    body_bytes = body.encode("utf-8", errors="replace") if isinstance(body, str) else body
    raw = b"Content-Type: " + content_type.encode("ascii", errors="replace") + b"\r\n\r\n" + body_bytes
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as e:
        _log.debug("extract_param_names: multipart parse failed: %s", e)
        return ()
    if not msg.is_multipart():
        return ()
    names = set()
    for part in msg.iter_parts():
        name = part.get_param("name", header="Content-Disposition")
        if name:
            names.add(name)
    return tuple(sorted(names))


def extract_param_names(content_type: str, body: str) -> Tuple[str, ...]:
    """Sorted, deduplicated tuple of top-level parameter names per content-type.

    Args:
        content_type: Raw Content-Type header value (may carry charset suffix).
        body: Request body as a string (already decoded by caller).

    Returns:
        Sorted unique top-level keys for known content-types; empty tuple otherwise.
        Malformed body → () + debug log (does NOT raise — capture path must not crash).

    Example:
        >>> extract_param_names('application/json', '{"a":1, "b":[1,2]}')
        ('a', 'b')
        >>> extract_param_names('application/json', '[1,2,3]')
        ()
        >>> extract_param_names('application/x-www-form-urlencoded', 'a=1&b=2&a=3')
        ('a', 'b')
    """
    norm = _normalize_content_type(content_type)
    if not body:
        return ()

    if norm == "application/x-www-form-urlencoded":
        try:
            pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=False)
            return tuple(sorted({k for k, _ in pairs}))
        except Exception as e:
            _log.debug("extract_param_names: urlencoded parse failed: %s", e)
            return ()

    if norm == "multipart/form-data":
        return _extract_multipart_names(content_type, body)

    if norm == "application/json":
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            _log.debug("extract_param_names: json parse failed: %s", e)
            return ()
        if isinstance(obj, dict):
            return tuple(sorted(obj.keys()))
        return ()

    return ()


def compute_request_hash(url: str, content_type: str, post_data: str) -> str:
    """Composite dedup key: MD5 of (url_without_fragment, normalized_ctype, sorted_param_names).

    Args:
        url: Full request URL (fragment will be stripped before hashing).
        content_type: Raw Content-Type header value (charset stripped internally).
        post_data: Request body as a string.

    Returns:
        32-char lowercase MD5 hex digest.

    Example:
        >>> h1 = compute_request_hash('https://x.com/a', 'application/json', '{"a":1}')
        >>> len(h1)
        32
    """
    parsed = urlparse(url)
    url_without_fragment = parsed._replace(fragment='').geturl()
    norm_ctype = _normalize_content_type(content_type)
    params = extract_param_names(norm_ctype, post_data)
    canonical = "\x00".join([url_without_fragment, norm_ctype, "\x00".join(params)])
    return hashlib.md5(canonical.encode('utf-8')).hexdigest()


def validate_url(url: str) -> bool:
    """
    Validate that URL has proper scheme and netloc.

    Only http and https schemes are considered valid.

    Args:
        url: URL to validate

    Returns:
        True if URL is valid, False otherwise

    Example:
        >>> validate_url('https://example.com/path')
        True
        >>> validate_url('ftp://example.com')
        False
        >>> validate_url('not-a-url')
        False
    """
    try:
        parsed = urlparse(url)

        # Must have http or https scheme
        if parsed.scheme not in ('http', 'https'):
            return False

        # Must have netloc (domain)
        if not parsed.netloc:
            return False

        return True

    except Exception:
        return False


def validate_session_file(path: str) -> None:
    """Validate session JSON has required keys. Exits on failure.

    Called by agent-crawl command (Phase 10) before launching browser.
    """
    p = Path(path)
    if not p.exists():
        print(f"Error: Session file not found: '{path}'", file=sys.stderr)
        sys.exit(1)
    try:
        with open(p) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error: Cannot read session file '{path}': {e}", file=sys.stderr)
        sys.exit(1)
    if "cookies" not in data or "origins" not in data:
        print(
            f"Error: Session file '{path}' is missing required keys "
            f"('cookies', 'origins'). Re-run 'save-session' to regenerate.",
            file=sys.stderr
        )
        sys.exit(1)
