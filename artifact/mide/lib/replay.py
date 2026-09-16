import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
import requests

from lib.config import Config
from lib.converter import ParameterConverter, ConversionResult, has_top_level_json_array
from lib.logging_setup import get_logger
from lib.output import OutputWriter


def _bucket_for_request(headers: Dict[str, Any]) -> str:
    """Return one of {"json", "form-urlencoded", "multipart", "unknown"}.

    Case-insensitive Content-Type lookup, charset stripped. Pure helper.
    """
    if not headers:
        return "unknown"
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() == "content-type":
            ct = (v or "").lower().split(";", 1)[0].strip()
            if "application/json" in ct:
                return "json"
            if "application/x-www-form-urlencoded" in ct:
                return "form-urlencoded"
            if "multipart/form-data" in ct:
                return "multipart"
            return "unknown"
    return "unknown"


def _classify_replay_skipped(error: Optional[str]) -> str:
    """Map ConversionResult.error -> replay_skipped enum value.

    Enum values: depth_truncation | unsupported_content_type | malformed_body.
    """
    if not error:
        return "malformed_body"
    err = error.lower()
    if "depth_truncation" in err or "depth limit" in err:
        return "depth_truncation"
    if "unsupported content type" in err or "no content-type header" in err:
        return "unsupported_content_type"
    return "malformed_body"


class ReplayEngine:
    """Pure replay orchestrator.

    Reads the captured-posts JSONL produced by the crawler, calls
    ParameterConverter once or twice (twice for top-level-array JSON bodies —
    PHP-style + repeated-keys), issues the resulting GET candidates via the
    existing _send_get plumbing, and writes one <domain>-replay-records.jsonl
    line per captured POST. NO comparator, NO normalizer, NO verdict.
    """

    def __init__(self, config: Config):
        self.config = config
        self.logger = get_logger("replay")
        # php / repeated MUST be hard-coded literals, NOT read from config.array_style.
        self._converter_php = ParameterConverter(
            array_style="php", max_depth=config.max_json_depth
        )
        self._converter_repeated = ParameterConverter(
            array_style="repeated", max_depth=config.max_json_depth
        )
        self._converter_default = ParameterConverter(
            array_style=config.array_style, max_depth=config.max_json_depth
        )
        self.session = requests.Session()
        self._shutdown_requested = False

    def __del__(self):
        if hasattr(self, "session"):
            self.session.close()

    def request_shutdown(self):
        self._shutdown_requested = True
        self.logger.info(
            "Shutdown requested, will finish current request and save records"
        )

    def run(self, input_file: str, writer: OutputWriter) -> Dict[str, Any]:
        """Read captured-posts JSONL → emit one replay record per POST.

        Args:
            input_file: Path to captured-posts JSONL.
            writer: OutputWriter that owns the session directory and the
                <domain>-replay-records.jsonl file. Caller manages lifecycle.

        Returns:
            Summary dict: {total_records, replay_skipped: {depth_truncation, unsupported_content_type, malformed_body}}.
        """
        requests_data = self._read_jsonl(input_file)
        self.logger.info(
            "Loaded %d captured POST requests from %s",
            len(requests_data),
            input_file,
        )

        total_records = 0
        replay_skipped_counts = {
            "depth_truncation": 0,
            "unsupported_content_type": 0,
            "malformed_body": 0,
        }

        for i, request_data in enumerate(requests_data):
            if self._shutdown_requested:
                self.logger.info(
                    "Shutdown requested, stopping after %d/%d requests",
                    i,
                    len(requests_data),
                )
                break

            url = request_data.get("url", "unknown")
            self.logger.info(
                "[%d/%d] Replaying: %s", i + 1, len(requests_data), url
            )

            # Captured POST response is the canonical observation.
            post_response = request_data.get("response")
            if post_response is None:
                raise RuntimeError(
                    f"captured-posts JSONL line for {url} has no 'response' field. "
                    "This captured-posts JSONL was produced by a pre-v2.1 crawler; recapture is required."
                )

            # Detect malformed JSON body up-front so the replay_skipped
            # taxonomy surfaces it as `malformed_body` rather than silently
            # producing an empty-query GET. The converter's own catch arm logs
            # a warning and returns success=True with an empty query string.
            ct_bucket = _bucket_for_request(request_data.get("headers", {}))
            if ct_bucket == "json":
                pd = request_data.get("postData", "") or ""
                try:
                    json.loads(pd)
                except (json.JSONDecodeError, TypeError):
                    record = {
                        "url": request_data.get("url"),
                        "method": "POST",
                        "headers": request_data.get("headers", {}),
                        "postData": pd,
                        "timestamp": request_data.get("timestamp"),
                        "resourceType": request_data.get("resourceType"),
                        "response": post_response,
                        "source_content_type_bucket": "json",
                        "replay_skipped": "malformed_body",
                        "lossy_conversion": False,
                    }
                    writer.write_replay_record(record)
                    replay_skipped_counts["malformed_body"] += 1
                    total_records += 1
                    continue

            # Decide single vs dual emission.
            if has_top_level_json_array(request_data):
                conv_php = self._converter_php.convert(request_data)
                conv_rep = self._converter_repeated.convert(request_data)

                if not conv_php.success:
                    skip_kind = self._record_replay_skipped(
                        request_data, conv_php, writer
                    )
                    replay_skipped_counts[skip_kind] = (
                        replay_skipped_counts.get(skip_kind, 0) + 1
                    )
                    total_records += 1
                    continue
                if not conv_rep.success:
                    skip_kind = self._record_replay_skipped(
                        request_data, conv_rep, writer
                    )
                    replay_skipped_counts[skip_kind] = (
                        replay_skipped_counts.get(skip_kind, 0) + 1
                    )
                    total_records += 1
                    continue

                get_php = self._send_get_with_retry(
                    lambda: self._send_get(conv_php), url
                )
                no_data_php = self._send_get_with_retry(
                    lambda: self._send_no_data_get(conv_php), url
                )
                get_rep = self._send_get_with_retry(
                    lambda: self._send_get(conv_rep), url
                )
                no_data_rep = self._send_get_with_retry(
                    lambda: self._send_no_data_get(conv_rep), url
                )
                candidates = [
                    {
                        "array_style_used": "php",
                        "converted_url": conv_php.url_with_params,
                        "headers": conv_php.headers,
                        "get_response": get_php,
                        "no_data_get": {
                            "url": self._no_data_url(conv_php.url_with_params),
                            "response": no_data_php,
                        },
                    },
                    {
                        "array_style_used": "repeated",
                        "converted_url": conv_rep.url_with_params,
                        "headers": conv_rep.headers,
                        "get_response": get_rep,
                        "no_data_get": {
                            "url": self._no_data_url(conv_rep.url_with_params),
                            "response": no_data_rep,
                        },
                    },
                ]
                lossy = bool(conv_php.lossy or conv_rep.lossy)
            else:
                conv = self._converter_default.convert(request_data)
                if not conv.success:
                    skip_kind = self._record_replay_skipped(
                        request_data, conv, writer
                    )
                    replay_skipped_counts[skip_kind] = (
                        replay_skipped_counts.get(skip_kind, 0) + 1
                    )
                    total_records += 1
                    continue
                get_resp = self._send_get_with_retry(
                    lambda: self._send_get(conv), url
                )
                no_data_resp = self._send_get_with_retry(
                    lambda: self._send_no_data_get(conv), url
                )
                candidates = [
                    {
                        "array_style_used": "na",
                        "converted_url": conv.url_with_params,
                        "headers": conv.headers,
                        "get_response": get_resp,
                        "no_data_get": {
                            "url": self._no_data_url(conv.url_with_params),
                            "response": no_data_resp,
                        },
                    }
                ]
                lossy = bool(conv.lossy)

            record = {
                "url": request_data.get("url"),
                "method": "POST",
                "headers": request_data.get("headers", {}),
                "postData": request_data.get("postData", ""),
                "timestamp": request_data.get("timestamp"),
                "resourceType": request_data.get("resourceType"),
                "response": post_response,
                "source_content_type_bucket": _bucket_for_request(
                    request_data.get("headers", {})
                ),
                "replay_skipped": None,
                "lossy_conversion": lossy,
                "get_candidates": candidates,
            }
            writer.write_replay_record(record)
            total_records += 1

            # Rate-limit between records (dual-emission is serial).
            if self.config.rate_limit_delay > 0:
                time.sleep(self.config.rate_limit_delay)

        return {
            "total_records": total_records,
            "replay_skipped": replay_skipped_counts,
        }

    def _record_replay_skipped(
        self,
        request_data: Dict[str, Any],
        conversion: ConversionResult,
        writer: OutputWriter,
    ) -> str:
        """Emit a replay_skipped record (no get_candidates key) and return the enum value."""
        skip_kind = _classify_replay_skipped(conversion.error)
        record = {
            "url": request_data.get("url"),
            "method": "POST",
            "headers": request_data.get("headers", {}),
            "postData": request_data.get("postData", ""),
            "timestamp": request_data.get("timestamp"),
            "resourceType": request_data.get("resourceType"),
            "response": request_data.get("response"),
            "source_content_type_bucket": _bucket_for_request(
                request_data.get("headers", {})
            ),
            "replay_skipped": skip_kind,
            "lossy_conversion": bool(conversion.lossy),
            # NOTE: `get_candidates` intentionally OMITTED for replay_skipped records.
        }
        writer.write_replay_record(record)
        return skip_kind

    def _send_get_with_retry(self, send_fn, url: str) -> Dict:
        """Send a GET and retry once after Retry-After if the response is 429.

        send_fn always returns a dict (never None).
        """
        response = send_fn()
        if response.get("status_code") == 429:
            retry_after = response.get("headers", {}).get("Retry-After", "60")
            try:
                wait_time = int(retry_after)
            except (ValueError, TypeError):
                wait_time = 60
            self.logger.warning(
                "Rate limit (429) on GET for %s, sleeping %ds and retrying",
                url,
                wait_time,
            )
            time.sleep(wait_time)
            response = send_fn()
        return response

    def _read_jsonl(self, filepath: str) -> List[Dict]:
        """Read JSONL file, one JSON object per line. Drop unparseable lines via warning."""
        requests_data: List[Dict] = []
        with open(filepath, "r") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    requests_data.append(obj)
                except json.JSONDecodeError as e:
                    self.logger.warning(
                        "Invalid JSON at line %d: %s", line_num, str(e)
                    )
        return requests_data

    def _send_get(self, conversion: ConversionResult) -> Dict:
        """Send the converted GET request.

        Pass `allow_redirects=False`; record single-hop `redirect_chain` from
        the verbatim `Location` header. Always return a Dict (never None).
        Three-bucket failure_type taxonomy with arms in order Timeout →
        ConnectionError → RequestException (most-specific first).
        """
        return self._send_get_url(conversion.url_with_params, conversion.headers)

    def _send_no_data_get(self, conversion: ConversionResult) -> Dict:
        """Send the control GET with converted query data removed."""
        return self._send_get_url(
            self._no_data_url(conversion.url_with_params),
            conversion.headers,
        )

    def _send_get_url(self, url: str, headers: Dict[str, Any]) -> Dict:
        """Send a GET to an explicit URL with already-cleaned headers."""
        start = time.monotonic()
        try:
            response = self.session.get(
                url,
                headers=headers,
                timeout=self.config.request_timeout,
                allow_redirects=False,
                verify=True,
                stream=True,
            )
            ip_address = self._response_ip_address(response)
            location = response.headers.get("Location")
            redirect_chain = (
                [{"location": location, "status": response.status_code}]
                if location is not None
                else []
            )
            body = response.text
            response.close()
            return {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": body,
                "redirect_chain": redirect_chain,
                "ip_address": ip_address,
            }
        except requests.exceptions.Timeout as e:
            return self._make_failure(url, "timeout", e, start)
        except requests.exceptions.ConnectionError as e:
            return self._make_failure(url, "connection_error", e, start)
        except requests.exceptions.RequestException as e:
            return self._make_failure(url, "request_exception", e, start)

    @staticmethod
    def _no_data_url(url: str) -> str:
        """Return URL with query string removed, preserving scheme/host/path."""
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))

    @staticmethod
    def _response_ip_address(response: requests.Response) -> Optional[str]:
        """Best-effort remote peer IP extraction from urllib3 internals."""
        raw = getattr(response, "raw", None)
        connection = getattr(raw, "_connection", None)
        sock = getattr(connection, "sock", None)

        if sock is None:
            try:
                sock = raw._fp.fp.raw._sock
            except AttributeError:
                sock = None

        if sock is None:
            return None

        try:
            peer = sock.getpeername()
        except OSError:
            return None

        if isinstance(peer, tuple) and peer:
            return str(peer[0])
        return None

    def _make_failure(
        self, url: str, failure_type: str, exc: Exception, start: float
    ) -> Dict:
        """Assemble the drop-in failure dict for _send_get exception paths."""
        elapsed_ms = (time.monotonic() - start) * 1000.0
        self.logger.error("GET %s for %s: %s", failure_type, url, str(exc))
        return {
            "status_code": None,
            "headers": {},
            "body": "",
            "redirect_chain": [],
            "ip_address": None,
            "failure": {
                "failure_type": failure_type,
                "failure_message": str(exc),
                "attempted_url": url,
                "attempted_method": "GET",
                "elapsed_ms": elapsed_ms,
            },
        }
