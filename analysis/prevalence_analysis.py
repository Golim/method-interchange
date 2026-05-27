#!/usr/bin/env python3

__license__ = """
This code is provided solely for the purpose of anonymous peer review of the associated academic paper.
All other uses, including but not limited to copying, distribution, modification, or commercial use, are strictly prohibited.
© 2026 Anonymous. All rights reserved.
"""

import argparse
import csv
import json
import logging
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from tabulate import tabulate
except ImportError as e:
    print(
        f"Missing dependency: {e.name}. Install project dependencies with `uv sync`.",
        file=sys.stderr,
    )
    sys.exit(1)

from lib.comparator import ResponseComparator


BASELINE_CLASSES = {
    "A": "Strong interchangeability",
    "B": "Ambiguous",
    "C": "Baseline collision",
    "D": "Not interchangeable",
}


def read_jsonl(path):
    '''
    Read a JSON Lines file and return a list of records. Skips invalid JSON lines with a warning.

    :param path: Path to the JSONL file.
    :return: List of parsed JSON records.
    '''

    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"Skipping invalid JSON at {path}:{line_num}", file=sys.stderr)
    return records


def read_json_file(path):
    '''
    Read a JSON file and return the parsed object.

    :param path: Path to the JSON file.
    :return: Parsed JSON object.
    '''
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_replay_files(data_dir):
    '''
    Find all replay record JSONL files in the given directory or return the file if a specific path is provided.

    :param data_dir: Directory to search for replay record files or a specific file path.
    :return: List of paths to replay record JSONL files.
    '''
    data_dir = Path(data_dir)
    if data_dir.is_file() and data_dir.name.endswith("-replay-records.jsonl"):
        return [data_dir]
    return sorted(data_dir.rglob("*-replay-records.jsonl"))


def find_batch_summary(data_dir):
    '''
    Find the batch summary file for a batch directory if it exists.

    :param data_dir: Batch directory, session directory, or replay file path.
    :return: Path to batch-summary.json if found, otherwise None.
    '''
    data_dir = Path(data_dir)
    if data_dir.is_dir():
        candidate = data_dir / "batch-summary.json"
        if candidate.is_file():
            return candidate
    return None


def domain_from_replay_file(path):
    '''
    Extract the domain name from a replay record file path. Assumes the file is named like "<domain>-replay-records.jsonl" or is located in a directory named after the domain.

    :param path: Path to the replay record JSONL file.
    :return: Extracted domain name.
    '''
    suffix = "-replay-records.jsonl"
    if path.name.endswith(suffix):
        return path.name[:-len(suffix)]
    return path.parent.parent.name


def domain_from_site_url(site):
    '''
    Normalize a site URL or domain-like string to a bare host/domain for set comparisons.

    :param site: Site URL or domain string from the batch summary.
    :return: Normalized domain/host string.
    '''
    parsed = urlsplit(site if "://" in str(site) else f"https://{site}")
    return (parsed.netloc or parsed.path).lower().strip("/")


def header_value(headers, name):
    '''
    Retrieve a header value from a headers dictionary in a case-insensitive way.

    :param headers: Dictionary of HTTP headers.
    :param name: Name of the header to retrieve.
    :return: Value of the header if found, otherwise None.
    '''
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return value
    return None


def normalized_content_type(record):
    '''
    Extract and normalize the content type from the record's headers or source content type bucket.
    Strips parameters and lowercases the main content type.

    :param record: Replay record containing headers and source content type bucket.
    :return: Normalized content type string.
    '''
    content_type = header_value(record.get("headers", {}), "content-type")
    if content_type:
        return str(content_type).split(";", 1)[0].strip().lower()
    return record.get("source_content_type_bucket") or "unknown"


def route_key(record):
    '''
    Create a key representing the route of the request, based on the URL, method, and content type.
    Normalizes the URL components and method for consistent grouping.

    :param record: Replay record containing the URL, method, and headers.
    :return: Tuple representing the route key (scheme, netloc, path, method, content_type).
    '''
    parsed = urlsplit(record.get("url") or "")
    return (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "/",
        (record.get("method") or "POST").upper(),
        normalized_content_type(record),
    )


def replayed(record):
    '''
    Determine if a record was replayed based on the presence of get candidates and whether it was skipped.

    :param record: Replay record containing replay information.
    :return: True if the record was replayed, False otherwise.
    '''
    return record.get("replay_skipped") is None and bool(record.get("get_candidates"))


def route_reached_status(status):
    '''
    Determine if a route reached status is within the expected range.

    :param status: HTTP status code.
    :return: True if the status indicates a successful route reach, False otherwise.
    '''
    return status is not None and (200 <= status < 400 or status == 400)


def route_rejected_status(status):
    '''
    Determine if a route rejected status is within the expected range.

    :param status: HTTP status code.
    :return: True if the status indicates a rejected route, False otherwise.
    '''
    return (
        status is None
        or status in {401, 403, 404, 405, 415, 422}
        or 500 <= status < 600
    )


def response_failure(response):
    '''
    Extract the failure information from a response if it exists and is a dictionary.

    :param response: Response object that may contain failure information.
    :return: Failure dictionary if present and valid, otherwise None.
    '''
    failure = (response or {}).get("failure")
    return failure if isinstance(failure, dict) else None


def replay_outcome(record):
    '''
    Determine the outcome of the replay attempt for a given record, categorizing it into specific buckets based on the presence of candidates, response statuses, and failure types.

    :param record: Replay record containing replay attempt information.
    :return: String label representing the replay outcome category.
    '''
    if record.get("replay_skipped"):
        return "not_replayed"

    candidates = record.get("get_candidates") or []
    responses = [c.get("get_response", {}) for c in candidates]
    failures = [response_failure(r) for r in responses if response_failure(r)]
    statuses = [r.get("status_code") for r in responses if r.get("status_code") is not None]

    if statuses and all(s in {401, 403, 429} for s in statuses):
        return "replay_blocked"
    if statuses:
        return "replay_success"
    if not failures:
        return "replay_network_error"

    failure_text = " ".join(
        str(f.get("failure_type", "")) + " " + str(f.get("failure_message", ""))
        for f in failures
    ).lower()
    if "timeout" in failure_text:
        return "replay_timeout"
    if "too many redirects" in failure_text or "redirect" in failure_text:
        return "replay_redirect_loop"
    if "ssl" in failure_text or "tls" in failure_text or "certificate" in failure_text:
        return "replay_tls_error"
    return "replay_network_error"


def route_outcome(record):
    '''
    Determine the outcome of the route reachability for a given record, categorizing it into "route_reached" or "route_rejected" based on the statuses of the GET candidates.

    :param record: Replay record containing GET candidate information.
    :return: String label representing the route outcome category.
    '''
    statuses = [
        c.get("get_response", {}).get("status_code")
        for c in record.get("get_candidates", [])
    ]
    if any(route_reached_status(s) for s in statuses):
        return "route_reached"
    if any(route_rejected_status(s) for s in statuses) or not statuses:
        return "route_rejected"
    return "route_rejected"


def conversion_outcome(record):
    '''
    Determine the outcome of the conversion attempt for a given record, categorizing it into specific buckets based on the content type, candidate styles, and whether the conversion was lossy or skipped.

    :param record: Replay record containing conversion attempt information.
    :return: String label representing the conversion outcome category.
    '''
    skipped = record.get("replay_skipped")
    if skipped == "unsupported_content_type":
        return "unsupported_content_type"
    if skipped:
        return "conversion_failed"

    content_type = record.get("source_content_type_bucket") or normalized_content_type(record)
    candidates = record.get("get_candidates") or []
    styles = {c.get("array_style_used") for c in candidates}

    if content_type == "multipart" and record.get("lossy_conversion"):
        return "conversion_lossy_multipart"
    if content_type == "json" and {"php", "repeated"}.issubset(styles):
        return "conversion_json_dual_candidate"
    if content_type == "json" and "php" in styles:
        return "conversion_json_php"
    if content_type == "json" and "repeated" in styles:
        return "conversion_json_repeated"
    return "conversion_success"


def compare_response(comparator, post_response, get_response):
    '''
    Compare a POST response with a GET response using the provided comparator, handling any exceptions that may arise during comparison.

    :param comparator: An instance of ResponseComparator to perform the comparison.
    :param post_response: The response object from the POST request.
    :param get_response: The response object from the GET request to compare against.
    :return: The result of the comparison if successful, otherwise None if there was a failure in either response or an exception during comparison.
    '''
    if not post_response or not get_response or get_response.get("failure"):
        return None
    try:
        return comparator.compare(post_response, get_response)
    except (KeyError, TypeError):
        return None


def best_similarity(results):
    '''
    Extract the best structural similarity score from a list of comparison results, ignoring any None results.

    :param results: List of comparison results, which may include None values.
    :return: The maximum structural similarity score found in the results, or 0.0 if there are no valid results.
    '''
    scored = [r.structural for r in results if r is not None]
    return max(scored) if scored else 0.0


def any_equivalent(results):
    '''
    Determine if any of the comparison results indicate interchangeability, ignoring any None results.

    :param results: List of comparison results, which may include None values.
    :return: True if any result indicates interchangeability, False otherwise.
    '''
    return any(r is not None and r.is_interchangeable for r in results)


def label_record(domain, record, comparator):
    '''
    Label a replay record with various outcomes and classifications based on the replay attempt, route reachability, conversion success, and response similarity.
    Uses the provided comparator to assess response similarity and determine baseline classes for interchangeability.

    :param domain: The domain associated with the record.
    :param record: The replay record to label.
    :param comparator: An instance of ResponseComparator to perform response comparisons.
    :return: A dictionary containing the original record along with additional labels and classifications.
    '''
    post_response = record.get("response")
    candidates = record.get("get_candidates") or []

    converted_results = [
        compare_response(comparator, post_response, c.get("get_response", {}))
        for c in candidates
    ]
    no_query_results = [
        compare_response(
            comparator,
            post_response,
            (c.get("no_data_get") or {}).get("response", {}),
        )
        for c in candidates
    ]

    converted_equiv = any_equivalent(converted_results)
    no_query_equiv = any_equivalent(no_query_results)
    if converted_equiv and not no_query_equiv:
        baseline_class = "A"
    elif converted_equiv and no_query_equiv:
        baseline_class = "B"
    elif not converted_equiv and no_query_equiv:
        baseline_class = "C"
    else:
        baseline_class = "D"

    route_label = route_outcome(record)
    source_equivalent = converted_equiv

    return {
        "domain": domain,
        "url": record.get("url"),
        "route": route_key(record),
        "conversion_outcome": conversion_outcome(record),
        "replay_outcome": replay_outcome(record),
        "route_outcome": route_label,
        "route_reached": route_label == "route_reached",
        "route_rejected": route_label == "route_rejected",
        "source_equivalent": source_equivalent,
        "accepted_for_equivalence": route_label == "route_reached" and source_equivalent,
        "converted_equivalent": converted_equiv,
        "no_query_equivalent": no_query_equiv,
        "baseline_class": baseline_class,
        "baseline_class_name": BASELINE_CLASSES[baseline_class],
        "high_confidence": baseline_class == "A",
        "upper_bound": baseline_class in {"A", "B"},
        "converted_similarity": best_similarity(converted_results),
        "no_query_similarity": best_similarity(no_query_results),
        "replayed": replayed(record),
        "raw": record,
    }


def load_labeled_records(data_dir, threshold):
    '''
    Load replay records from the specified directory, label each record using the provided comparator and threshold, and return a list of labeled records.

    :param data_dir: Directory containing replay record JSONL files.
    :param threshold: Similarity threshold to use for labeling records.
    :return: List of labeled records with additional classifications and outcomes.
    '''
    comparator = ResponseComparator(threshold=threshold)
    labeled = []
    for path in find_replay_files(data_dir):
        domain = domain_from_replay_file(path)
        for record in read_jsonl(path):
            labeled.append(label_record(domain, record, comparator))
    return labeled


def site_summaries(records):
    '''Aggregate replay records by domain and compute summary statistics for each domain, including counts of replayed requests, high-confidence interchangeable requests, upper bound interchangeable requests, and the set of routes involved.

    :param records: List of labeled replay records.
    :return: List of summary dictionaries for each domain, containing aggregated statistics and route information.
    '''
    by_domain = defaultdict(list)
    for row in records:
        by_domain[row["domain"]].append(row)

    summaries = []
    for domain, rows in sorted(by_domain.items()):
        replayed_rows = [r for r in rows if r["replayed"]]
        summaries.append({
            "domain": domain,
            "captured_requests": len(rows),
            "replayed_requests": len(replayed_rows),
            "conversion_skipped_requests": len(rows) - len(replayed_rows),
            "high_confidence_requests": sum(r["high_confidence"] for r in replayed_rows),
            "upper_bound_requests": sum(r["upper_bound"] for r in replayed_rows),
            "routes": {r["route"] for r in replayed_rows},
            "high_confidence_routes": {r["route"] for r in replayed_rows if r["high_confidence"]},
            "upper_bound_routes": {r["route"] for r in replayed_rows if r["upper_bound"]},
            "has_captured_records": bool(rows),
            "has_high_confidence": any(r["high_confidence"] for r in replayed_rows),
            "has_upper_bound": any(r["upper_bound"] for r in replayed_rows),
        })
    return summaries


def aggregate_metrics(summaries, upper_bound=False):
    '''
    Aggregate metrics across site summaries to compute the numerator and denominator for site-level, route-level, and request-level prevalence of method interchangeability, based on either high-confidence or upper bound criteria.

    :param summaries: List of site summary dictionaries containing replay and interchangeability statistics.
    :param upper_bound: Boolean flag indicating whether to use upper bound criteria (A+B) instead of high-confidence criteria (A only).
    :return: Dictionary containing aggregated metrics for site, route, and request levels, with numerator and denominator pairs for prevalence calculation.
    '''
    request_key = "upper_bound_requests" if upper_bound else "high_confidence_requests"
    route_key_name = "upper_bound_routes" if upper_bound else "high_confidence_routes"
    site_key = "has_upper_bound" if upper_bound else "has_high_confidence"

    eligible = [s for s in summaries if s["replayed_requests"] > 0]
    sites_den = len(eligible)
    sites_num = sum(s[site_key] for s in eligible)
    req_den = sum(s["replayed_requests"] for s in eligible)
    req_num = sum(s[request_key] for s in eligible)
    routes = set()
    matching_routes = set()
    for summary in eligible:
        routes.update(summary["routes"])
        matching_routes.update(summary[route_key_name])
    route_den = len(routes)
    route_num = len(matching_routes)

    return {
        "site": (sites_num, sites_den),
        "route": (route_num, route_den),
        "request": (req_num, req_den),
    }


def rate(pair):
    '''
    Calculate the rate as the numerator divided by the denominator, handling the case where the denominator is zero.

    :param pair: Tuple containing the numerator and denominator for the rate calculation.
    :return: The calculated rate as a float, or 0.0 if the denominator is zero.
    '''
    numerator, denominator = pair
    return numerator / denominator if denominator else 0.0


def bootstrap_cis(summaries, samples, seed, upper_bound=False):
    '''
    Perform bootstrap resampling on the site summaries to compute confidence intervals for the prevalence metrics at site, route, and request levels.

    :param summaries: List of site summary dictionaries containing replay and interchangeability statistics.
    :param samples: Number of bootstrap samples to draw.
    :param seed: Seed for the random number generator.
    :param upper_bound: Boolean flag indicating whether to use upper bound criteria (A+B) instead of high-confidence criteria (A only).
    :return: Dictionary containing confidence intervals for site, route, and request level prevalence metrics.
    '''
    eligible = [s for s in summaries if s["replayed_requests"] > 0]
    if not eligible:
        return {level: (0.0, 0.0) for level in ("site", "route", "request")}

    rng = random.Random(seed)
    draws = {"site": [], "route": [], "request": []}
    for _ in range(samples):
        sampled = [rng.choice(eligible) for _ in eligible]
        metrics = aggregate_metrics(sampled, upper_bound=upper_bound)
        for level, pair in metrics.items():
            draws[level].append(rate(pair))

    return {level: percentile_ci(values) for level, values in draws.items()}


def percentile_ci(values):
    '''
    Compute the 95% confidence interval for a list of values using the percentile method.

    :param values: List of numeric values from bootstrap resampling.
    :return: Tuple containing the lower and upper bounds of the 95% confidence interval.
    '''
    values = sorted(values)
    if not values:
        return (0.0, 0.0)
    low_index = int(0.025 * (len(values) - 1))
    high_index = int(0.975 * (len(values) - 1))
    return (values[low_index], values[high_index])


def percentage(value):
    '''
    Format a decimal value as a percentage string with one decimal place.

    :param value: Decimal value to format as a percentage.
    :return: Formatted percentage string.
    '''
    return f"{value * 100:.1f}%"


def percentage_of(count, denominator):
    '''
    Format count / denominator as a percentage, returning n/a when the denominator is missing.

    :param count: Numerator count.
    :param denominator: Denominator count or None.
    :return: Percentage string.
    '''
    if count is None or denominator is None or denominator == 0:
        return "n/a"
    return percentage(count / denominator)


def count_text(value):
    '''
    Render missing counts as n/a for tables.

    :param value: Count value or None.
    :return: Count value or n/a.
    '''
    return "n/a" if value is None else value


def ci_text(ci):
    '''
    Format a confidence interval tuple as a string with percentage values.

    :param ci: Tuple containing the lower and upper bounds of the confidence interval as decimal values.
    :return: Formatted confidence interval string with percentage values.
    '''
    return f"[{percentage(ci[0])}, {percentage(ci[1])}]"


def batch_summary_counts(batch_summary, summaries, records, replayed_rows, metrics, upper_metrics):
    '''
    Build a dictionary of useful crawl/replay population counts from the batch summary and labeled records.

    :param batch_summary: Parsed batch-summary.json dictionary or empty dictionary.
    :param summaries: Per-site summaries.
    :param records: All labeled request records.
    :param replayed_rows: Labeled records that were actually replayed.
    :param metrics: High-confidence prevalence metrics.
    :param upper_metrics: Upper-bound prevalence metrics.
    :return: Dictionary of counts used by coverage and prevalence-denominator tables.
    '''
    captured_sites = {
        domain_from_site_url(site)
        for site in batch_summary.get("captured_posts_sites", [])
    }
    replay_file_sites = {s["domain"] for s in summaries}
    high_confidence_sites = metrics["site"][0]
    upper_bound_sites = upper_metrics["site"][0]

    return {
        "total_sites": batch_summary.get("total_sites"),
        "completed": batch_summary.get("completed"),
        "failed": batch_summary.get("failed"),
        "no_posts_found": batch_summary.get("no_posts_found"),
        "sites_with_captured_posts": batch_summary.get("sites_with_captured_posts") or len(captured_sites) or None,
        "captured_posts_sites_listed": len(captured_sites) if captured_sites else None,
        "sites_with_replay_records": len(replay_file_sites),
        "captured_sites_missing_replay_records": max(len(captured_sites - replay_file_sites), 0) if captured_sites else None,
        "sites_with_replayed_posts": metrics["site"][1],
        "sites_without_replayed_posts": len(replay_file_sites) - metrics["site"][1],
        "high_confidence_sites": high_confidence_sites,
        "upper_bound_sites": upper_bound_sites,
        "captured_post_records": len(records),
        "replayed_post_records": len(replayed_rows),
        "conversion_skipped_records": len(records) - len(replayed_rows),
        "unique_replayed_routes": metrics["route"][1],
        "high_confidence_routes": metrics["route"][0],
        "upper_bound_routes": upper_metrics["route"][0],
        "high_confidence_requests": metrics["request"][0],
        "upper_bound_requests": upper_metrics["request"][0],
    }


def coverage_rows(counts):
    '''
    Generate rows describing the crawl and replay coverage of the batch.

    :param counts: Count dictionary from batch_summary_counts.
    :return: Table rows.
    '''
    total = counts["total_sites"]
    completed = counts["completed"]
    terminal = (counts["completed"] or 0) + (counts["no_posts_found"] or 0)
    terminal = terminal or None
    captured = counts["sites_with_captured_posts"]
    replay_records = counts["sites_with_replay_records"]
    replayed_sites = counts["sites_with_replayed_posts"]
    records = counts["captured_post_records"]

    rows = [
        ["Sites in input batch", count_text(counts["total_sites"]), "", "", "Sites attempted by the batch runner"],
        ["Completed full POST/replay pipeline", count_text(completed), percentage_of(completed, total), percentage_of(completed, terminal), "Sites where the batch completed the requested pipeline"],
        ["Failed site crawls", count_text(counts["failed"]), percentage_of(counts["failed"], total), "", "Sites marked failed by the batch runner"],
        ["Sites with no captured POSTs", count_text(counts["no_posts_found"]), percentage_of(counts["no_posts_found"], total), percentage_of(counts["no_posts_found"], terminal), "Sites where the crawler saw no POST requests"],
        ["Sites with captured POSTs", count_text(captured), percentage_of(captured, total), percentage_of(captured, terminal), "Sites that produced captured POST records"],
        ["Sites with replay record files", count_text(replay_records), percentage_of(replay_records, total), percentage_of(replay_records, captured), "Sites represented by replay JSONL files analyzed by this script"],
        ["Captured POST sites missing replay files", count_text(counts["captured_sites_missing_replay_records"]), percentage_of(counts["captured_sites_missing_replay_records"], captured), "", "Listed in batch summary but not found in replay JSONL inputs"],
        ["Sites with replayed POSTs", count_text(replayed_sites), percentage_of(replayed_sites, total), percentage_of(replayed_sites, replay_records), "Sites with at least one POST converted and replayed as GET"],
        ["Sites with only conversion-skipped POSTs", count_text(counts["sites_without_replayed_posts"]), percentage_of(counts["sites_without_replayed_posts"], replay_records), "", "Replay record sites with no replayed GET candidate"],
        ["Captured POST request records", count_text(records), "", "", "Individual POST records in replay JSONL files"],
        ["Replayed POST request records", count_text(counts["replayed_post_records"]), percentage_of(counts["replayed_post_records"], records), "", "POST records with at least one converted GET candidate"],
        ["Conversion-skipped POST records", count_text(counts["conversion_skipped_records"]), percentage_of(counts["conversion_skipped_records"], records), "", "POST records skipped before replay"],
        ["Unique replayed POST routes", count_text(counts["unique_replayed_routes"]), "", "", "Unique scheme+host+path+method+content-type among replayed POSTs"],
    ]
    return rows


def site_prevalence_denominator_rows(counts):
    '''
    Generate site-level prevalence rows using several relevant denominators.

    :param counts: Count dictionary from batch_summary_counts.
    :return: Table rows.
    '''
    denominators = [
        ("All sites tested", counts["total_sites"]),
        ("Completed full POST/replay pipeline", counts["completed"]),
        ("Sites with captured POSTs", counts["sites_with_captured_posts"]),
        ("Sites with replay record files", counts["sites_with_replay_records"]),
        ("Sites with replayed POSTs", counts["sites_with_replayed_posts"]),
    ]
    rows = []
    for name, denominator in denominators:
        rows.append([
            name,
            count_text(counts["high_confidence_sites"]),
            count_text(denominator),
            percentage_of(counts["high_confidence_sites"], denominator),
            count_text(counts["upper_bound_sites"]),
            percentage_of(counts["upper_bound_sites"], denominator),
        ])
    return rows


def request_population_rows(records, replayed_rows):
    '''
    Generate request-level population tables by content type and replay/conversion status.

    :param records: All labeled request records.
    :param replayed_rows: Labeled records that were actually replayed.
    :return: Table rows.
    '''
    rows = []
    content_types = sorted({r["raw"].get("source_content_type_bucket") or "unknown" for r in records})
    for content_type in content_types:
        all_rows = [r for r in records if (r["raw"].get("source_content_type_bucket") or "unknown") == content_type]
        replayed_ct = [r for r in replayed_rows if (r["raw"].get("source_content_type_bucket") or "unknown") == content_type]
        high_confidence = [r for r in replayed_ct if r["high_confidence"]]
        rows.append([
            content_type,
            len(all_rows),
            percentage_of(len(all_rows), len(records)),
            len(replayed_ct),
            percentage_of(len(replayed_ct), len(all_rows)),
            len(high_confidence),
            percentage_of(len(high_confidence), len(replayed_ct)),
        ])
    return rows


def metric_rows(metrics, cis):
    '''
    Generate rows of metrics for site, route, and request levels, including the numerator, denominator, rate, and confidence interval text for each level.

    :param metrics: Dictionary containing the numerator and denominator pairs for site, route, and request levels.
    :param cis: Dictionary containing the confidence intervals for site, route, and request levels.
    :return: List of rows with metric information for each level.
    '''
    names = {
        "site": "Site",
        "route": "Route",
        "request": "Request",
    }
    rows = []
    for level in ("site", "route", "request"):
        numerator, denominator = metrics[level]
        rows.append([
            names[level],
            numerator,
            denominator,
            percentage(rate(metrics[level])),
            ci_text(cis[level]),
        ])
    return rows


def write_table(path, headers, rows, latex=False):
    '''
    Write a table to a text file in either GitHub or LaTeX format using the tabulate library.

    :param path: Path to the output file where the table will be written.
    :param headers: List of column headers for the table.
    :param rows: List of rows to include in the table, where each row is a list of values corresponding to the headers.
    :param latex: Boolean flag indicating whether to write the table in LaTeX format (if False, writes in GitHub markdown format).
    :return: The formatted table as a string.
    '''
    tablefmt = "latex" if latex else "github"
    text = tabulate(rows, headers=headers, tablefmt=tablefmt)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.write("\n")
    return text


def write_table_outputs(out_dir, name, headers, rows, latex=False):
    '''
    Write a named table in text format and optionally LaTeX format.

    :param out_dir: Output directory.
    :param name: Base filename without extension.
    :param headers: Table headers.
    :param rows: Table rows.
    :param latex: Whether to also write a LaTeX file.
    '''
    write_table(out_dir / f"{name}.txt", headers, rows)
    if latex:
        write_table(out_dir / f"{name}.tex", headers, rows, latex=True)


def write_request_labels(path, records):
    '''
    Write a CSV file containing detailed labels for each replay record, including domain, URL, various outcome classifications, similarity scores, and the original raw record for reference.

    :param path: Path to the output CSV file where the request labels will be written.
    :param records: List of labeled replay records, where each record is a dictionary containing various fields and classifications.
    '''
    fieldnames = [
        "domain",
        "url",
        "conversion_outcome",
        "replay_outcome",
        "route_outcome",
        "source_equivalent",
        "accepted_for_equivalence",
        "baseline_class",
        "baseline_class_name",
        "high_confidence",
        "upper_bound",
        "converted_similarity",
        "no_query_similarity",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in records:
            writer.writerow({key: row[key] for key in fieldnames})


def breakdown_rows(counter, denominator):
    '''
    Generate rows for a breakdown of counts and percentages based on a Counter object and a denominator for percentage calculation.

    :param counter: Counter object containing counts for each label.
    :param denominator: The total count to use as the denominator for percentage calculations.
    :return: List of rows, where each row contains the label, count, and percentage share of the total.
    '''
    rows = []
    for label, count in sorted(counter.items()):
        rows.append([label, count, percentage(count / denominator if denominator else 0.0)])
    return rows


def plot_prevalence(path, metrics, cis):
    '''
    Create a bar chart showing the prevalence of method interchangeability at site, route, and request levels, including error bars representing the confidence intervals.

    :param path: Path to the output image file where the chart will be saved.
    :param metrics: Dictionary containing the numerator and denominator pairs for site, route, and request levels.
    :param cis: Dictionary containing the confidence intervals for site, route, and request levels, used to calculate the error bars for the chart.
    '''
    levels = ["site", "route", "request"]
    labels = ["Site", "Route", "Request"]
    values = [rate(metrics[level]) * 100 for level in levels]
    lower = [(rate(metrics[level]) - cis[level][0]) * 100 for level in levels]
    upper = [(cis[level][1] - rate(metrics[level])) * 100 for level in levels]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.bar(labels, values, yerr=[lower, upper], capsize=6, color=["#4C78A8", "#F58518", "#54A24B"])
    ax.set_ylabel("Prevalence (%)")
    tops = [value + err for value, err in zip(values, upper)]
    ax.set_ylim(0, max(5, min(100, max(tops) * 1.25 if tops else 5)))
    ax.set_title("High-confidence method interchangeability")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def plot_site_funnel(path, counts):
    '''
    Create a simple bar chart showing how many sites remain at each analysis stage.

    :param path: Path to the output image file.
    :param counts: Count dictionary from batch_summary_counts.
    '''
    stages = [
        ("Tested", counts["total_sites"]),
        ("Completed", counts["completed"]),
        ("Captured POSTs", counts["sites_with_captured_posts"]),
        ("Replayed POSTs", counts["sites_with_replayed_posts"]),
        ("High-conf.", counts["high_confidence_sites"]),
        ("Upper bound", counts["upper_bound_sites"]),
    ]
    stages = [(label, value) for label, value in stages if value is not None]
    if not stages:
        return

    labels = [label for label, _ in stages]
    values = [value for _, value in stages]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(labels, values, color=["#4C78A8", "#72B7B2", "#F58518", "#54A24B", "#E45756", "#B279A2"][:len(labels)])
    ax.set_ylabel("Sites")
    ax.set_title("Site coverage through analysis pipeline")
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", rotation=20)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def plot_scatter(path, records, threshold):
    '''
    Create a scatter plot comparing the similarity scores of POST responses to converted GET responses and no-query GET responses for replayed requests, colored by baseline class, with dashed lines indicating the similarity threshold.

    :param path: Path to the output image file where the scatter plot will be saved.
    :param records: List of labeled replay records, where each record contains similarity scores and baseline class information for replayed requests.
    :param threshold: Similarity threshold used for labeling records, which will be indicated on the plot with dashed lines.
    '''
    replayed_rows = [r for r in records if r["replayed"]]
    colors = {
        "A": "#4C78A8",
        "B": "#F58518",
        "C": "#E45756",
        "D": "#72B7B2",
    }

    fig, ax = plt.subplots(figsize=(6, 6))
    for key in ("A", "B", "C", "D"):
        subset = [r for r in replayed_rows if r["baseline_class"] == key]
        ax.scatter(
            [r["converted_similarity"] for r in subset],
            [r["no_query_similarity"] for r in subset],
            label=f"{key}: {BASELINE_CLASSES[key]}",
            color=colors[key],
            alpha=0.65,
            s=24,
            edgecolors="none",
        )
    ax.axvline(threshold, color="#333333", linestyle="--", linewidth=1)
    ax.axhline(threshold, color="#333333", linestyle="--", linewidth=1)
    ax.set_xlim(-1, 101)
    ax.set_ylim(-1, 101)
    ax.set_xlabel("similarity(POST, converted GET)")
    ax.set_ylabel("similarity(POST, no-query GET)")
    ax.set_title("Baseline-control similarity quadrants")
    ax.grid(alpha=0.2)
    ax.legend(fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(
        description="Compute clean prevalence numbers for method interchangeability."
    )
    parser.add_argument("data_dir", help="Batch/session directory containing JSON replay outputs.")
    parser.add_argument("--out-dir", default="prevalence-analysis", help="Directory for tables and charts.")
    parser.add_argument("--threshold", type=float, default=95.0, help="Similarity threshold percentage.")
    parser.add_argument("--bootstrap-samples", type=int, default=5000, help="Site-level bootstrap samples.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for bootstrap resampling.")
    parser.add_argument("--latex", action="store_true", help="Also write tables in LaTeX format.")
    parser.add_argument("--no-charts", action="store_true", help="Skip chart generation.")
    parser.add_argument("--verbose", action="store_true", help="Show comparator warnings while processing.")
    args = parser.parse_args()

    if not args.verbose:
        logging.getLogger("comparator").setLevel(logging.ERROR)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    batch_summary_path = find_batch_summary(args.data_dir)
    batch_summary = read_json_file(batch_summary_path) if batch_summary_path else {}

    records = load_labeled_records(args.data_dir, args.threshold)
    if not records:
        print(f"No replay records found in {args.data_dir}", file=sys.stderr)
        sys.exit(1)

    summaries = site_summaries(records)
    metrics = aggregate_metrics(summaries)
    upper_metrics = aggregate_metrics(summaries, upper_bound=True)
    cis = bootstrap_cis(summaries, args.bootstrap_samples, args.seed)
    upper_cis = bootstrap_cis(summaries, args.bootstrap_samples, args.seed, upper_bound=True)
    replayed_rows = [r for r in records if r["replayed"]]
    counts = batch_summary_counts(
        batch_summary,
        summaries,
        records,
        replayed_rows,
        metrics,
        upper_metrics,
    )

    coverage_headers = ["Population / stage", "Count", "% of total sites", "% of prior/related denominator", "Interpretation"]
    coverage_table_rows = coverage_rows(counts)
    print("\nBatch and analysis coverage")
    print(tabulate(coverage_table_rows, headers=coverage_headers, tablefmt="github"))
    write_table_outputs(out_dir, "batch-coverage", coverage_headers, coverage_table_rows, args.latex)

    denominator_headers = [
        "Site denominator",
        "A sites",
        "Denominator",
        "A rate",
        "A+B sites",
        "A+B rate",
    ]
    denominator_rows = site_prevalence_denominator_rows(counts)
    print("\nSite prevalence across denominators")
    print(tabulate(denominator_rows, headers=denominator_headers, tablefmt="github"))
    write_table_outputs(out_dir, "site-prevalence-denominators", denominator_headers, denominator_rows, args.latex)

    headers = ["Level", "Numerator", "Denominator", "Rate", "95% site-bootstrap CI"]
    main_rows = metric_rows(metrics, cis)
    upper_rows = metric_rows(upper_metrics, upper_cis)

    print("\nHigh-confidence prevalence (A only)")
    print(tabulate(main_rows, headers=headers, tablefmt="github"))
    print("\nBroader upper bound (A+B)")
    print(tabulate(upper_rows, headers=headers, tablefmt="github"))

    write_table_outputs(out_dir, "main-prevalence", headers, main_rows, args.latex)
    write_table_outputs(out_dir, "upper-bound-prevalence", headers, upper_rows, args.latex)

    baseline_counter = Counter(r["baseline_class_name"] for r in replayed_rows)
    conversion_counter = Counter(r["conversion_outcome"] for r in records if r["high_confidence"])
    route_counter = Counter(r["route_outcome"] for r in replayed_rows)
    replay_counter = Counter(r["replay_outcome"] for r in replayed_rows)
    all_conversion_counter = Counter(r["conversion_outcome"] for r in records)

    print("\nBaseline-control classes")
    baseline_rows = breakdown_rows(baseline_counter, len(replayed_rows))
    print(tabulate(baseline_rows, headers=["Class", "Requests", "Share"], tablefmt="github"))

    print("\nConversion outcomes among high-confidence interchangeable requests")
    conversion_rows = breakdown_rows(conversion_counter, sum(conversion_counter.values()))
    print(tabulate(conversion_rows, headers=["Outcome", "Requests", "Share"], tablefmt="github"))

    content_type_rows = request_population_rows(records, replayed_rows)
    content_type_headers = [
        "Content type",
        "Captured POSTs",
        "% of captured POSTs",
        "Replayed POSTs",
        "% replayed within type",
        "A requests",
        "A rate among replayed",
    ]

    write_table_outputs(out_dir, "baseline-classes", ["Class", "Requests", "Share"], baseline_rows, args.latex)
    write_table_outputs(out_dir, "conversion-outcomes-interchangeable", ["Outcome", "Requests", "Share"], conversion_rows, args.latex)
    write_table_outputs(out_dir, "route-outcomes", ["Outcome", "Requests", "Share"], breakdown_rows(route_counter, len(replayed_rows)), args.latex)
    write_table_outputs(out_dir, "replay-outcomes", ["Outcome", "Requests", "Share"], breakdown_rows(replay_counter, len(replayed_rows)), args.latex)
    write_table_outputs(out_dir, "conversion-outcomes-all", ["Outcome", "Requests", "Share"], breakdown_rows(all_conversion_counter, len(records)), args.latex)
    write_table_outputs(out_dir, "content-type-breakdown", content_type_headers, content_type_rows, args.latex)

    write_request_labels(out_dir / "request-labels.csv", records)
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "records": len(records),
                "replayed_records": len(replayed_rows),
                "sites": len(summaries),
                "sites_with_replayed_posts": metrics["site"][1],
                "batch_summary_path": str(batch_summary_path) if batch_summary_path else None,
                "batch_summary": batch_summary,
                "coverage_counts": counts,
                "threshold": args.threshold,
                "main_metrics": metrics,
                "upper_bound_metrics": upper_metrics,
                "main_cis": cis,
                "upper_bound_cis": upper_cis,
            },
            f,
            indent=2,
        )

    if not args.no_charts:
        plot_site_funnel(out_dir / "site-analysis-funnel.png", counts)
        plot_prevalence(out_dir / "prevalence-levels.png", metrics, cis)
        plot_scatter(out_dir / "similarity-scatter.png", records, args.threshold)

    print(f"\nWrote analysis outputs to {out_dir}")


if __name__ == "__main__":
    main()
