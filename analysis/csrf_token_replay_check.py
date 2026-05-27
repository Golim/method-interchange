#!/usr/bin/env python3

__license__ = """
This code is provided solely for the purpose of anonymous peer review of the associated academic paper.
All other uses, including but not limited to copying, distribution, modification, or commercial use, are strictly prohibited.
© 2026 Anonymous. All rights reserved.
"""

import argparse
import csv
import hashlib
import json
import logging
import sys
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import requests
    from tabulate import tabulate
except ImportError as e:
    print(
        f"Missing dependency: {e.name}. Install project dependencies with `uv sync`.",
        file=sys.stderr,
    )
    sys.exit(1)

from csrf_analysis import CSRF_RE
from csrf_analysis import csrf_token_names
from csrf_analysis import domain_from_replay_file
from csrf_analysis import find_replay_files
from csrf_analysis import high_confidence_candidate
from csrf_analysis import read_jsonl
from lib.comparator import ResponseComparator


INVALID_TOKEN = "INVALID_CSRF_TOKEN_REPLAY_CHECK"
DROP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "proxy-connection",
    "accept-encoding",
    ":authority",
    ":method",
    ":path",
    ":scheme",
}


def token_name_matches(name, token_names):
    '''
    Decide whether a parameter/header name is CSRF-like or matches a known token name.

    :param name: Parameter/header name.
    :param token_names: Token names detected in the original POST.
    :return: True if the name should be treated as a CSRF token carrier.
    '''
    if CSRF_RE.search(str(name)):
        return True
    leaves = {token.split(".")[-1].lower() for token in token_names}
    return str(name).lower() in leaves


def clean_headers(headers):
    '''
    Remove headers that requests should compute itself.

    :param headers: Captured request headers.
    :return: Sanitized copy of the headers.
    '''
    cleaned = {}
    for name, value in (headers or {}).items():
        if str(name).lower() in DROP_HEADERS:
            continue
        cleaned[str(name)] = str(value)
    return cleaned


def mutate_headers(headers, token_names, mode):
    '''
    Remove or change CSRF-like header tokens.

    :param headers: Request headers.
    :param token_names: Token names detected in the original POST.
    :param mode: Either "original", "remove", or "change".
    :return: Tuple of (mutated headers, number of changed token carriers).
    '''
    if mode == "original":
        return clean_headers(headers), 0
    mutated = {}
    changed = 0
    for name, value in clean_headers(headers).items():
        if token_name_matches(name, token_names):
            changed += 1
            if mode == "remove":
                continue
            if mode == "change":
                mutated[name] = INVALID_TOKEN
                continue
        mutated[name] = value
    return mutated, changed


def mutate_url(url, token_names, mode):
    '''
    Remove or change CSRF-like query parameters.

    :param url: Request URL.
    :param token_names: Token names detected in the original POST.
    :param mode: Either "original", "remove", or "change".
    :return: Tuple of (mutated URL, number of changed token carriers).
    '''
    if mode == "original":
        return url, 0
    parts = urlsplit(url)
    params = []
    changed = 0
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if token_name_matches(name, token_names):
            changed += 1
            if mode == "remove":
                continue
            params.append((name, INVALID_TOKEN))
        else:
            params.append((name, value))
    query = urlencode(params, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment)), changed


def mutate_json_value(value, token_names, mode):
    '''
    Remove or change CSRF-like keys in a JSON value.

    :param value: Parsed JSON value.
    :param token_names: Token names detected in the original POST.
    :param mode: Either "remove" or "change".
    :return: Tuple of (mutated value, number of changed token carriers).
    '''
    changed = 0
    if isinstance(value, dict):
        mutated = {}
        for key, child in value.items():
            if token_name_matches(key, token_names):
                changed += 1
                if mode == "change":
                    mutated[key] = INVALID_TOKEN
                continue
            new_child, child_changed = mutate_json_value(child, token_names, mode)
            changed += child_changed
            mutated[key] = new_child
        return mutated, changed
    if isinstance(value, list):
        mutated = []
        for child in value:
            new_child, child_changed = mutate_json_value(child, token_names, mode)
            changed += child_changed
            mutated.append(new_child)
        return mutated, changed
    return value, 0


def mutate_body(record, token_names, mode):
    '''
    Remove or change CSRF-like POST body parameters.

    :param record: Original replay record.
    :param token_names: Token names detected in the original POST.
    :param mode: Either "original", "remove", or "change".
    :return: Tuple of (mutated body, number of changed token carriers).
    '''
    body = record.get("postData", "") or ""
    if mode == "original":
        return body, 0
    content_type = record.get("source_content_type_bucket") or ""
    if content_type == "form-urlencoded":
        params = []
        changed = 0
        for name, value in parse_qsl(body, keep_blank_values=True):
            if token_name_matches(name, token_names):
                changed += 1
                if mode == "remove":
                    continue
                params.append((name, INVALID_TOKEN))
            else:
                params.append((name, value))
        return urlencode(params, doseq=True), changed
    if content_type == "json":
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, TypeError, ValueError):
            return body, 0
        mutated, changed = mutate_json_value(value, token_names, mode)
        return json.dumps(mutated, separators=(",", ":")), changed
    return body, 0


def response_to_comparator_dict(response):
    '''
    Convert a requests response to the shape expected by ResponseComparator.

    :param response: requests.Response object.
    :return: Response dictionary.
    '''
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": response.text,
        "redirect_chain": [r.url for r in response.history],
    }


def failure_response(error):
    '''
    Represent a failed request in the same loose shape as successful responses.

    :param error: Exception raised by requests.
    :return: Failure dictionary.
    '''
    return {
        "failure": type(error).__name__,
        "error": str(error),
        "status_code": None,
        "headers": {},
        "body": "",
        "redirect_chain": [],
    }


def send_request(session, variant, timeout):
    '''
    Send one planned request variant.

    :param session: requests session.
    :param variant: Request variant dictionary.
    :param timeout: Request timeout in seconds.
    :return: Response dictionary with timing metadata.
    '''
    started = time.time()
    try:
        response = session.request(
            variant["method"],
            variant["url"],
            headers=variant["headers"],
            data=variant.get("body"),
            timeout=timeout,
            allow_redirects=True,
        )
        result = response_to_comparator_dict(response)
    except requests.RequestException as e:
        result = failure_response(e)
    result["elapsed_seconds"] = round(time.time() - started, 3)
    return result


def body_digest(body):
    '''
    Hash a response body for output without storing private contents by default.

    :param body: Response body.
    :return: SHA-256 hex digest.
    '''
    return hashlib.sha256((body or "").encode("utf-8", errors="replace")).hexdigest()


def output_response(response):
    '''
    Prepare a response for JSONL output.

    :param response: Response dictionary.
    :return: Redacted response dictionary.
    '''
    copied = dict(response)
    body = copied.get("body", "") or ""
    copied["body_sha256"] = body_digest(body)
    copied["body_length"] = len(body)
    return copied


def discover_candidates(data_dir, threshold, include_without_tokens):
    '''
    Find high-confidence interchangeable records to test dynamically.

    :param data_dir: Batch/session directory or replay JSONL path.
    :param threshold: Similarity threshold.
    :param include_without_tokens: Include high-confidence records without CSRF-like tokens.
    :return: List of candidate dictionaries.
    '''
    comparator = ResponseComparator(threshold=threshold)
    candidates = []
    for path in find_replay_files(data_dir):
        domain = domain_from_replay_file(path)
        for index, record in enumerate(read_jsonl(path), 1):
            candidate, _, _ = high_confidence_candidate(record, comparator)
            if candidate is None:
                continue
            token_names = csrf_token_names(record)
            if not token_names and not include_without_tokens:
                continue
            candidates.append({
                "domain": domain,
                "source_file": str(path),
                "line_index": index,
                "record": record,
                "candidate": candidate,
                "token_names": token_names,
            })
    return candidates


def scoped_candidates(candidates, args):
    '''
    Apply user-supplied domain and URL filters.

    :param candidates: Candidate dictionaries.
    :param args: Parsed command-line arguments.
    :return: Filtered candidates.
    '''
    domains = set(args.domain or [])
    url_needles = args.url_contains or []
    filtered = []
    for item in candidates:
        record = item["record"]
        candidate = item["candidate"]
        if domains and item["domain"] not in domains:
            continue
        haystack = "\n".join([record.get("url") or "", candidate.get("converted_url") or ""])
        if url_needles and not any(needle in haystack for needle in url_needles):
            continue
        filtered.append(item)
    return filtered[:args.limit] if args.limit else filtered


def build_variants(item, args):
    '''
    Build POST and GET variants for one candidate.

    :param item: Candidate dictionary.
    :param args: Parsed command-line arguments.
    :return: List of request variant dictionaries.
    '''
    record = item["record"]
    candidate = item["candidate"]
    token_names = item["token_names"]
    variants = []

    post_headers, post_header_changes = mutate_headers(record.get("headers", {}), token_names, "original")
    post_url, post_url_changes = mutate_url(record.get("url") or "", token_names, "original")
    post_body, post_body_changes = mutate_body(record, token_names, "original")
    variants.append({
        "label": "post_original_1",
        "method": "POST",
        "url": post_url,
        "headers": post_headers,
        "body": post_body,
        "token_carriers_changed": post_header_changes + post_url_changes + post_body_changes,
    })

    if args.repeat_post:
        repeated = deepcopy(variants[0])
        repeated["label"] = "post_original_2"
        variants.append(repeated)

    if args.test_invalid_post_token:
        for mode, label in [("remove", "post_without_token"), ("change", "post_changed_token")]:
            headers, header_changes = mutate_headers(record.get("headers", {}), token_names, mode)
            url, url_changes = mutate_url(record.get("url") or "", token_names, mode)
            body, body_changes = mutate_body(record, token_names, mode)
            variants.append({
                "label": label,
                "method": "POST",
                "url": url,
                "headers": headers,
                "body": body,
                "token_carriers_changed": header_changes + url_changes + body_changes,
            })

    for mode, label in [
        ("original", "get_converted_original"),
        ("remove", "get_without_token"),
        ("change", "get_changed_token"),
    ]:
        headers, header_changes = mutate_headers(candidate.get("headers", {}), token_names, mode)
        url, url_changes = mutate_url(candidate.get("converted_url") or "", token_names, mode)
        variants.append({
            "label": label,
            "method": "GET",
            "url": url,
            "headers": headers,
            "body": None,
            "token_carriers_changed": header_changes + url_changes,
        })
    return variants


def similarity(comparator, left, right):
    '''
    Compare two live responses.

    :param comparator: ResponseComparator instance.
    :param left: First response dictionary.
    :param right: Second response dictionary.
    :return: Tuple of (is_equivalent, score).
    '''
    if not left or not right or left.get("failure") or right.get("failure"):
        return False, ""
    result = comparator.compare(left, right)
    score = getattr(result, "overall_similarity", result.structural)
    return result.is_interchangeable, f"{score:.1f}"


def variant_row(results, label):
    '''
    Look up one variant row by label.

    :param results: Per-variant result rows for one candidate.
    :param label: Variant label.
    :return: Row dictionary or empty dictionary.
    '''
    return {row["label"]: row for row in results}.get(label, {})


def is_equivalent(row):
    '''
    Convert the string equivalence field to a boolean-ish value.

    :param row: Result row.
    :return: True, False, or None if the row is absent.
    '''
    value = row.get("equivalent_to_post_original_1")
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def token_variant_was_mutated(row):
    '''
    Check whether a variant actually removed or changed at least one token carrier.

    :param row: Result row.
    :return: True if a token carrier changed.
    '''
    try:
        return int(row.get("token_carriers_changed") or 0) > 0
    except ValueError:
        return False


def post_token_evidence(results):
    '''
    Summarize whether invalid-token POST variants show CSRF-token enforcement.

    :param results: Per-variant result rows for one candidate.
    :return: Evidence label.
    '''
    base = variant_row(results, "post_original_1")
    if not base:
        return "POST baseline missing"
    status = base.get("status_code")
    if status is not None and int(status) >= 400:
        return "POST baseline non-success; no POST-token evidence"

    tested = []
    for label, description in [
        ("post_without_token", "missing token"),
        ("post_changed_token", "changed token"),
    ]:
        row = variant_row(results, label)
        if row and token_variant_was_mutated(row):
            tested.append((description, is_equivalent(row)))

    if not tested:
        return "invalid POST variants not tested"

    rejected = [description for description, equivalent in tested if equivalent is False]
    accepted = [description for description, equivalent in tested if equivalent is True]
    if rejected and not accepted:
        return "POST rejects " + " and ".join(rejected)
    if accepted and not rejected:
        return "no POST token enforcement observed"
    if rejected and accepted:
        return "POST rejects " + " and ".join(rejected) + "; accepts " + " and ".join(accepted)
    return "POST token evidence inconclusive"


def get_token_evidence(results):
    '''
    Summarize whether invalid-token GET variants remain equivalent to the POST baseline.

    :param results: Per-variant result rows for one candidate.
    :return: Evidence label.
    '''
    original = variant_row(results, "get_converted_original")
    if is_equivalent(original) is False:
        return "live converted GET not equivalent"

    tested = []
    for label, description in [
        ("get_without_token", "missing token"),
        ("get_changed_token", "changed token"),
    ]:
        row = variant_row(results, label)
        if row and token_variant_was_mutated(row):
            tested.append((description, is_equivalent(row)))

    if not tested:
        return "invalid GET variants not tested"

    accepted = [description for description, equivalent in tested if equivalent is True]
    rejected = [description for description, equivalent in tested if equivalent is False]
    if accepted and not rejected:
        return "GET accepts " + " and ".join(accepted)
    if rejected and not accepted:
        return "GET rejects " + " and ".join(rejected)
    if accepted and rejected:
        return "GET accepts " + " and ".join(accepted) + "; rejects " + " and ".join(rejected)
    return "GET token evidence inconclusive"


def method_gap_evidence(results):
    '''
    Check pairwise whether GET accepts the same invalid token variant that POST rejects.

    :param results: Per-variant result rows for one candidate.
    :return: Evidence label.
    '''
    base = variant_row(results, "post_original_1")
    status = base.get("status_code")
    if status is not None and int(status) >= 400:
        return "no gap evidence; POST baseline non-success"

    gaps = []
    for description, post_label, get_label in [
        ("missing token", "post_without_token", "get_without_token"),
        ("changed token", "post_changed_token", "get_changed_token"),
    ]:
        post_row = variant_row(results, post_label)
        get_row = variant_row(results, get_label)
        if not token_variant_was_mutated(post_row) or not token_variant_was_mutated(get_row):
            continue
        if is_equivalent(post_row) is False and is_equivalent(get_row) is True:
            gaps.append(description)

    if gaps:
        return "GET bypasses POST rejection for " + " and ".join(gaps)
    return "no pairwise GET-over-POST token gap observed"


def infer_behavior(results):
    '''
    Assign a short dynamic behavior label from executed variants.

    :param results: Per-variant result rows for one candidate.
    :return: Behavior label.
    '''
    post_evidence = post_token_evidence(results)
    get_evidence = get_token_evidence(results)
    gap_evidence = method_gap_evidence(results)

    if gap_evidence.startswith("GET bypasses"):
        return "GET token validation gap"
    invalid_get_accepted = get_evidence.startswith("GET accepts")
    if invalid_get_accepted:
        return "GET accepts invalid token; POST enforcement not shown"
    if variant_row(results, "get_converted_original").get("equivalent_to_post_original_1") == "True":
        return "GET accepts original token only"
    return "No live GET equivalence observed"


def write_table(path, headers, rows, latex=False):
    '''
    Write a tabulate table.

    :param path: Output path.
    :param headers: Table headers.
    :param rows: Table rows.
    :param latex: Whether to write LaTeX format.
    '''
    tablefmt = "latex" if latex else "github"
    with open(path, "w", encoding="utf-8") as f:
        f.write(tabulate(rows, headers=headers, tablefmt=tablefmt))
        f.write("\n")


def candidate_level_rows(rows):
    '''
    Collapse per-variant live replay rows into one row per tested candidate.

    :param rows: Per-variant result rows.
    :return: List of candidate-level rows.
    '''
    by_candidate = {}
    for row in rows:
        key = (row["domain"], row["source_file"], row["line_index"])
        by_candidate.setdefault(key, row)
    return list(by_candidate.values())


def csrf_outcome_class(row):
    '''
    Assign a paper-facing CSRF replay outcome class.

    :param row: Candidate-level result row.
    :return: Outcome class string.
    '''
    method_gap = row.get("method_gap_evidence", "")
    post_evidence = row.get("post_token_evidence", "")
    get_evidence = row.get("get_token_evidence", "")

    if method_gap.startswith("GET bypasses"):
        return "Pairwise GET-over-POST token validation gap"
    if post_evidence.startswith("POST baseline non-success"):
        return "POST baseline failed; no POST-token evidence"
    if post_evidence == "no POST token enforcement observed" and get_evidence.startswith("GET accepts"):
        return "Invalid-token GET accepted; no POST enforcement observed"
    if method_gap.startswith("no pairwise") and get_evidence.startswith("GET accepts"):
        return "Mixed invalid-token behavior; no pairwise gap"
    if get_evidence.startswith("GET rejects"):
        return "Invalid-token GET rejected"
    return "Inconclusive"


def aggregate_replay_rows(rows):
    '''
    Aggregate live CSRF replay results at candidate and site levels.

    :param rows: Per-variant result rows.
    :return: Rows for the aggregate table.
    '''
    candidates = candidate_level_rows(rows)
    candidate_total = len(candidates)
    candidate_counts = {}
    for row in candidates:
        outcome = csrf_outcome_class(row)
        candidate_counts[outcome] = candidate_counts.get(outcome, 0) + 1

    order = [
        "Pairwise GET-over-POST token validation gap",
        "Invalid-token GET accepted; no POST enforcement observed",
        "Mixed invalid-token behavior; no pairwise gap",
        "POST baseline failed; no POST-token evidence",
        "Invalid-token GET rejected",
        "Inconclusive",
    ]
    short_labels = {
        "Pairwise GET-over-POST token validation gap": "POST rejects; GET accepts",
        "Invalid-token GET accepted; no POST enforcement observed": "Both methods accept",
        "Mixed invalid-token behavior; no pairwise gap": "Mixed, no GET-specific gap",
        "POST baseline failed; no POST-token evidence": "POST baseline failed",
        "Invalid-token GET rejected": "GET rejects invalid tokens",
        "Inconclusive": "Inconclusive",
    }

    priority = {outcome: index for index, outcome in enumerate(order)}
    site_outcomes = {}
    for row in candidates:
        outcome = csrf_outcome_class(row)
        domain = row["domain"]
        existing = site_outcomes.get(domain)
        if existing is None or priority[outcome] < priority[existing]:
            site_outcomes[domain] = outcome

    site_total = len(site_outcomes)
    site_counts = {}
    for outcome in site_outcomes.values():
        site_counts[outcome] = site_counts.get(outcome, 0) + 1

    table_rows = []
    for outcome in order:
        candidate_count = candidate_counts.get(outcome, 0)
        site_count = site_counts.get(outcome, 0)
        if candidate_count == 0 and site_count == 0:
            continue
        candidate_share = "n/a" if candidate_total == 0 else f"{(candidate_count / candidate_total) * 100:.1f}%"
        site_share = "n/a" if site_total == 0 else f"{(site_count / site_total) * 100:.1f}%"
        table_rows.append([
            short_labels[outcome],
            candidate_count,
            candidate_total,
            candidate_share,
            site_count,
            site_total,
            site_share,
        ])
    return table_rows


def read_summary_csv(path):
    '''
    Read an existing live replay summary CSV.

    :param path: CSV path.
    :return: List of result rows.
    '''
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_aggregate_outputs(out_dir, rows, latex=False):
    '''
    Write aggregate live CSRF replay result tables.

    :param out_dir: Output directory.
    :param rows: Per-variant result rows.
    :param latex: Whether to write a LaTeX table.
    :return: Aggregate table rows.
    '''
    headers = [
        "Result", "Candidates", "Tested candidates", "Candidate share",
        "Sites", "Tested sites", "Site share",
    ]
    table_rows = aggregate_replay_rows(rows)
    write_table(out_dir / "csrf-token-replay-aggregate.txt", headers, table_rows)
    if latex:
        write_table(out_dir / "csrf-token-replay-aggregate.tex", headers, table_rows, latex=True)
    return table_rows


def write_plan(out_dir, candidates, args):
    '''
    Write a dry-run plan showing what would be replayed.

    :param out_dir: Output directory.
    :param candidates: Candidate dictionaries.
    :param args: Parsed command-line arguments.
    '''
    path = out_dir / "token-replay-plan.csv"
    fieldnames = [
        "domain", "source_file", "line_index", "token_names", "variant",
        "method", "url", "token_carriers_changed",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in candidates:
            for variant in build_variants(item, args):
                writer.writerow({
                    "domain": item["domain"],
                    "source_file": item["source_file"],
                    "line_index": item["line_index"],
                    "token_names": ";".join(item["token_names"]),
                    "variant": variant["label"],
                    "method": variant["method"],
                    "url": variant["url"],
                    "token_carriers_changed": variant["token_carriers_changed"],
                })


def execute_candidates(out_dir, candidates, args):
    '''
    Execute live POST/GET token replay checks.

    :param out_dir: Output directory.
    :param candidates: Candidate dictionaries.
    :param args: Parsed command-line arguments.
    :return: Summary rows.
    '''
    comparator = ResponseComparator(threshold=args.threshold)
    summary_rows = []
    raw_path = out_dir / "token-replay-results.jsonl"
    csv_path = out_dir / "token-replay-summary.csv"
    fieldnames = [
        "domain", "source_file", "line_index", "url", "converted_url",
        "token_names", "label", "method", "status_code", "failure",
        "body_length", "body_sha256", "equivalent_to_post_original_1",
        "similarity_to_post_original_1", "token_carriers_changed",
        "post_token_evidence", "get_token_evidence", "method_gap_evidence",
        "behavior",
    ]

    with open(raw_path, "w", encoding="utf-8") as raw, open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in candidates:
            session = requests.Session()
            variants = build_variants(item, args)
            live = {}
            rows_for_item = []
            for variant in variants:
                response = send_request(session, variant, args.timeout)
                live[variant["label"]] = response
                base = live.get("post_original_1")
                equivalent, score = similarity(comparator, base, response) if base is not None else (False, "")
                row = {
                    "domain": item["domain"],
                    "source_file": item["source_file"],
                    "line_index": item["line_index"],
                    "url": item["record"].get("url"),
                    "converted_url": item["candidate"].get("converted_url"),
                    "token_names": ";".join(item["token_names"]),
                    "label": variant["label"],
                    "method": variant["method"],
                    "status_code": response.get("status_code"),
                    "failure": response.get("failure", ""),
                    "body_length": len(response.get("body", "") or ""),
                    "body_sha256": body_digest(response.get("body", "") or ""),
                    "equivalent_to_post_original_1": str(equivalent),
                    "similarity_to_post_original_1": score,
                    "token_carriers_changed": variant["token_carriers_changed"],
                    "behavior": "",
                }
                rows_for_item.append(row)
                raw.write(json.dumps({
                    "candidate": {
                        "domain": item["domain"],
                        "source_file": item["source_file"],
                        "line_index": item["line_index"],
                        "url": item["record"].get("url"),
                        "converted_url": item["candidate"].get("converted_url"),
                        "token_names": item["token_names"],
                    },
                    "variant": {k: v for k, v in variant.items() if k != "body"},
                    "response": output_response(response),
                }) + "\n")
                if args.sleep:
                    time.sleep(args.sleep)
            post_evidence = post_token_evidence(rows_for_item)
            get_evidence = get_token_evidence(rows_for_item)
            gap_evidence = method_gap_evidence(rows_for_item)
            behavior = infer_behavior(rows_for_item)
            for row in rows_for_item:
                row["post_token_evidence"] = post_evidence
                row["get_token_evidence"] = get_evidence
                row["method_gap_evidence"] = gap_evidence
                row["behavior"] = behavior
                writer.writerow(row)
            summary_rows.extend(rows_for_item)
    return summary_rows


def analyze(args):
    '''
    Build or execute dynamic CSRF token replay checks.

    :param args: Parsed command-line arguments.
    '''
    logging.getLogger("comparator").setLevel(logging.ERROR)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.results_csv:
        summary_rows = read_summary_csv(args.results_csv)
        aggregate_rows = write_aggregate_outputs(out_dir, summary_rows, args.latex)
        print("\nAggregate CSRF Token Replay Results")
        print(tabulate(
            aggregate_rows,
            headers=[
                "Result", "Candidates", "Tested candidates", "Candidate share",
                "Sites", "Tested sites", "Site share",
            ],
            tablefmt="github",
        ))
        print(f"\nWrote aggregate token replay outputs to {out_dir}")
        return

    if not args.data_dir:
        print("data_dir is required unless --results-csv is provided.", file=sys.stderr)
        sys.exit(2)

    candidates = discover_candidates(args.data_dir, args.threshold, args.include_without_tokens)
    candidates = scoped_candidates(candidates, args)
    if not candidates:
        print("No matching high-confidence CSRF-token candidates found.", file=sys.stderr)
        sys.exit(1)

    write_plan(out_dir, candidates, args)
    plan_rows = [
        [item["domain"], item["line_index"], ";".join(item["token_names"]), item["record"].get("url")]
        for item in candidates[:args.review_limit]
    ]
    print("\nSelected Candidates")
    print(tabulate(plan_rows, headers=["Domain", "Line", "Token names", "URL"], tablefmt="github"))
    write_table(out_dir / "selected-candidates.txt", ["Domain", "Line", "Token names", "URL"], plan_rows)
    if args.latex:
        write_table(out_dir / "selected-candidates.tex", ["Domain", "Line", "Token names", "URL"], plan_rows, latex=True)

    if not args.execute:
        print(f"\nDry run only. Wrote replay plan to {out_dir / 'token-replay-plan.csv'}")
        print("Add --execute to send the planned live requests.")
        return

    summary_rows = execute_candidates(out_dir, candidates, args)
    write_aggregate_outputs(out_dir, summary_rows, args.latex)
    compact = [
        [
            row["domain"], row["label"], row["status_code"], row["equivalent_to_post_original_1"],
            row["similarity_to_post_original_1"], row["post_token_evidence"],
            row["get_token_evidence"], row["method_gap_evidence"], row["behavior"],
        ]
        for row in summary_rows
    ]
    print("\nLive Replay Results")
    headers = [
        "Domain", "Variant", "Status", "Equiv to POST1", "Similarity",
        "POST Token Evidence", "GET Token Evidence", "Method Gap Evidence",
        "Behavior",
    ]
    print(tabulate(compact, headers=headers, tablefmt="github"))
    write_table(out_dir / "live-replay-results.txt", headers, compact)
    if args.latex:
        write_table(out_dir / "live-replay-results.tex", headers, compact, latex=True)
    print(f"\nWrote token replay outputs to {out_dir}")


def main():
    parser = argparse.ArgumentParser(description="Opt-in live CSRF token replay checker for interchangeable endpoints.")
    parser.add_argument("data_dir", nargs="?", help="Batch/session directory or replay JSONL file.")
    parser.add_argument("--results-csv", help="Summarize an existing token-replay-summary.csv without sending requests.")
    parser.add_argument("--out-dir", default="analysis/csrf-token-replay-check")
    parser.add_argument("--threshold", type=float, default=95.0)
    parser.add_argument("--domain", action="append", help="Restrict to a domain. Can be repeated.")
    parser.add_argument("--url-contains", action="append", help="Restrict to URLs containing this substring. Can be repeated.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum candidates after filtering. 0 means no limit.")
    parser.add_argument("--review-limit", type=int, default=30, help="Rows shown in the selected-candidate preview.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--sleep", type=float, default=0.5, help="Delay between live requests in seconds.")
    parser.add_argument("--repeat-post", action="store_true", help="Replay the original POST twice with the same token.")
    parser.add_argument("--test-invalid-post-token", action="store_true", help="Also send POST variants with token removed/changed.")
    parser.add_argument("--include-without-tokens", action="store_true", help="Include high-confidence records without CSRF-like tokens.")
    parser.add_argument("--latex", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually send live HTTP requests. Otherwise only writes a plan.")
    args = parser.parse_args()
    analyze(args)


if __name__ == "__main__":
    main()
