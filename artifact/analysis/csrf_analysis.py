"""Shared, offline helpers for selecting token-bearing class-A records."""

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


CSRF_RE = re.compile(
    r"(?:csrf|xsrf|_token|authenticity_?token|nonce|state|"
    r"requestverificationtoken|anti_?forgery)",
    re.IGNORECASE,
)


def read_jsonl(path):
    """Read valid JSON objects from a JSONL file."""
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def find_replay_files(data_dir):
    """Find replay-record JSONL files below a directory or accept one file."""
    path = Path(data_dir)
    if path.is_file() and path.name.endswith("-replay-records.jsonl"):
        return [path]
    return sorted(path.rglob("*-replay-records.jsonl"))


def domain_from_replay_file(path):
    """Derive the domain label from a replay-record filename."""
    suffix = "-replay-records.jsonl"
    return path.name[: -len(suffix)] if path.name.endswith(suffix) else path.parent.name


def _compare(comparator, post_response, get_response):
    if not post_response or not get_response or get_response.get("failure"):
        return None
    try:
        return comparator.compare(post_response, get_response)
    except (KeyError, TypeError):
        return None


def high_confidence_candidate(record, comparator):
    """Return the first class-A GET candidate and its two comparisons.

    A candidate is class A when the converted GET is equivalent to the captured
    POST response and the same endpoint without query parameters is not.
    """
    post_response = record.get("response")
    for candidate in record.get("get_candidates") or []:
        converted = _compare(comparator, post_response, candidate.get("get_response"))
        baseline = _compare(
            comparator,
            post_response,
            (candidate.get("no_data_get") or {}).get("response"),
        )
        if converted and converted.is_interchangeable and not (
            baseline and baseline.is_interchangeable
        ):
            return candidate, converted, baseline
    return None, None, None


def csrf_token_names(record):
    """Return CSRF-like token carrier names found in headers, URL, or body."""
    names = set()
    for name in (record.get("headers") or {}):
        if CSRF_RE.search(str(name)):
            names.add(str(name))
    for name, _ in parse_qsl(urlsplit(record.get("url") or "").query, keep_blank_values=True):
        if CSRF_RE.search(name):
            names.add(name)

    body = record.get("postData") or ""
    for name, _ in parse_qsl(body, keep_blank_values=True):
        if CSRF_RE.search(name):
            names.add(name)
    try:
        parsed = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        parsed = None

    def visit(value, prefix=""):
        if isinstance(value, dict):
            for key, child in value.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                if CSRF_RE.search(str(key)):
                    names.add(name)
                visit(child, name)
        elif isinstance(value, list):
            for child in value:
                visit(child, prefix)

    visit(parsed)
    return sorted(names)
