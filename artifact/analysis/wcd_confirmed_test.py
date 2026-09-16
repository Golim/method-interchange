#!/usr/bin/env python3

__license__ = "MIT"

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
MIDE_ROOT = REPO_ROOT / "mide"
for path in (REPO_ROOT, MIDE_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    import requests
    from tabulate import tabulate
except ImportError as e:
    print(
        f"Missing dependency: {e.name}. Install project dependencies with `uv sync`.",
        file=sys.stderr,
    )
    sys.exit(1)

from csrf_analysis import domain_from_replay_file
from csrf_analysis import find_replay_files
from csrf_analysis import high_confidence_candidate
from csrf_analysis import read_jsonl
from lib.comparator import ResponseComparator
from lib.wcde import WCDE


OUTPUT_CLASSES = [
    "not_tested_safety",
    "not_dynamic",
    "normal_url_already_cacheable",
    "wcd_payload_not_routed",
    "wcd_payload_static_error",
    "wcd_miss_no_hit",
    "wcd_hit_but_body_mismatch",
    "wcd_confirmed",
]

OUTPUT_CLASS_PRIORITY = {
    "not_tested_safety": 0,
    "not_dynamic": 1,
    "wcd_payload_static_error": 2,
    "wcd_payload_not_routed": 3,
    "wcd_miss_no_hit": 4,
    "wcd_hit_but_body_mismatch": 5,
    "normal_url_already_cacheable": 6,
    "wcd_confirmed": 7,
}

PAPER_OUTCOME_LABELS = {
    "wcd_confirmed": "Confirmed WCD",
    "normal_url_already_cacheable": "Normal URL already cacheable",
    "wcd_hit_but_body_mismatch": "Cache hit, body mismatch",
    "wcd_miss_no_hit": "Routed, no cache hit",
    "wcd_payload_not_routed": "Payload not routed/static error",
    "wcd_payload_static_error": "Payload not routed/static error",
    "not_dynamic": "Not dynamic",
    "not_tested_safety": "Not tested for safety",
}

PAPER_OUTCOME_ORDER = [
    "Confirmed WCD",
    "Normal URL already cacheable",
    "Cache hit, body mismatch",
    "Routed, no cache hit",
    "Payload not routed/static error",
    "Not dynamic",
    "Not tested for safety",
]

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

def clean_headers(headers, strip_credentials=False):
    '''
    Remove headers that requests should compute and optionally strip credentials.

    :param headers: Captured request headers.
    :param strip_credentials: Whether to remove Cookie/Authorization-like headers.
    :return: Sanitized headers.
    '''
    cleaned = {}
    for name, value in (headers or {}).items():
        lower = str(name).lower()
        if lower in DROP_HEADERS:
            continue
        if strip_credentials and lower in {"cookie", "authorization", "x-csrf-token", "x-xsrf-token"}:
            continue
        cleaned[str(name)] = str(value)
    return cleaned


def response_dict(response):
    '''
    Convert a requests response to a serializable dictionary.

    :param response: requests.Response object.
    :return: Response dictionary.
    '''
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": response.text,
        "url": response.url,
        "redirect_chain": [r.url for r in response.history],
    }


def failure_response(error):
    '''
    Convert a request exception to a response-like dictionary.

    :param error: Exception from requests.
    :return: Failure dictionary.
    '''
    return {
        "failure": type(error).__name__,
        "error": str(error),
        "status_code": None,
        "headers": {},
        "body": "",
        "url": "",
        "redirect_chain": [],
    }


def fetch(session, url, headers, timeout):
    '''
    Fetch one URL and return a response dictionary.

    :param session: requests.Session.
    :param url: URL to request.
    :param headers: Request headers.
    :param timeout: Timeout in seconds.
    :return: Response dictionary.
    '''
    try:
        response = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return response_dict(response)
    except requests.RequestException as e:
        return failure_response(e)


def request_dict(label, method, url, headers, body=None):
    '''
    Represent a request sent by the WCD experiment.

    :param label: Request role label.
    :param method: HTTP method.
    :param url: Request URL.
    :param headers: Request headers.
    :param body: Request body.
    :return: Request dictionary.
    '''
    return {
        "label": label,
        "method": method,
        "url": url,
        "headers": dict(headers or {}),
        "body": body,
    }


def exchange_dict(label, request, response):
    '''
    Pair a request with its response.

    :param label: Exchange label.
    :param request: Request dictionary.
    :param response: Response dictionary or None for dry runs.
    :return: Exchange dictionary.
    '''
    return {
        "label": label,
        "request": request,
        "response": response,
    }


def body_hash(response):
    '''
    Hash a response body.

    :param response: Response dictionary.
    :return: SHA-256 hex digest.
    '''
    body = response.get("body", "") or ""
    return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()


def cache_status(wcde, response):
    '''
    Compute a cache-status label from response headers.

    :param wcde: WCDE helper.
    :param response: Response dictionary.
    :return: Cache-status heuristic result.
    '''
    if response.get("failure"):
        return "network_error"
    return wcde.cache_headers_heuristics(response.get("headers", {}))


def has_cache_hit(wcde, response):
    '''
    Decide whether response headers indicate a cache HIT.

    :param wcde: WCDE helper.
    :param response: Response dictionary.
    :return: True if cache headers indicate HIT.
    '''
    status = cache_status(wcde, response)
    if status == "HIT":
        return True
    age = response.get("headers", {}).get("Age") or response.get("headers", {}).get("age")
    try:
        return age is not None and int(age) > 0
    except ValueError:
        return False


def route_matches_normal(comparator, normal_response, attack_response):
    '''
    Decide whether a WCD payload URL reached the same dynamic response surface.

    :param comparator: ResponseComparator instance.
    :param normal_response: Normal converted-GET response.
    :param attack_response: WCD payload response.
    :return: Tuple of (matched, similarity).
    '''
    if normal_response.get("failure") or attack_response.get("failure"):
        return (False, "")
    result = comparator.compare(normal_response, attack_response)
    score = getattr(result, "overall_similarity", result.structural)
    return (result.is_interchangeable, f"{score:.1f}")


def looks_static_error(response):
    '''
    Identify static-looking error responses from WCD payload URLs.

    :param response: Response dictionary.
    :return: True if the response looks like a static error.
    '''
    status = response.get("status_code")
    if status in {400, 401, 403, 404, 405, 415, 422}:
        return True
    body = (response.get("body") or "").lower()
    return status is not None and status >= 500 and any(word in body for word in ["error", "not found", "forbidden"])


def classify_mode(wcde, comparator, normal_response, induce_response, probe_response):
    '''
    Assign one of the requested output classes to a WCD mode result.

    :param wcde: WCDE helper.
    :param comparator: ResponseComparator instance.
    :param normal_response: Normal converted-GET response.
    :param induce_response: First WCD payload response.
    :param probe_response: Second WCD payload response.
    :return: Tuple of (class label, route matched, similarity, body matched, cache hit).
    '''
    if normal_response.get("failure"):
        return ("not_dynamic", False, "", False, False)
    if looks_static_error(induce_response):
        return ("wcd_payload_static_error", False, "", False, has_cache_hit(wcde, probe_response))

    routed, similarity = route_matches_normal(comparator, normal_response, induce_response)
    if not routed:
        return ("wcd_payload_not_routed", False, similarity, False, has_cache_hit(wcde, probe_response))

    hit = has_cache_hit(wcde, probe_response)
    body_match = wcde.identicality_checks(induce_response.get("body", ""), probe_response.get("body", ""))
    if not hit:
        return ("wcd_miss_no_hit", True, similarity, body_match, False)
    if not body_match:
        return ("wcd_hit_but_body_mismatch", True, similarity, False, True)
    return ("wcd_confirmed", True, similarity, True, True)


def load_confirmed_interchangeable(data_dir, threshold, limit):
    '''
    Load high-confidence interchangeable converted GET endpoints.

    :param data_dir: Batch/session directory or replay JSONL file.
    :param threshold: Similarity threshold.
    :param limit: Optional maximum endpoint count.
    :return: List of endpoint dictionaries.
    '''
    comparator = ResponseComparator(threshold=threshold)
    rows = []
    for path in find_replay_files(data_dir):
        domain = domain_from_replay_file(path)
        for line_index, record in enumerate(read_jsonl(path), 1):
            candidate, _, _ = high_confidence_candidate(record, comparator)
            if candidate is None:
                continue
            rows.append({
                "domain": domain,
                "source_file": str(path),
                "line_index": line_index,
                "url": record.get("url"),
                "converted_url": candidate.get("converted_url"),
                "headers": candidate.get("headers", {}),
                "content_type": record.get("source_content_type_bucket"),
            })
            if limit and len(rows) >= limit:
                return rows
    return rows


def query_length(url):
    '''
    Return query string length.

    :param url: URL.
    :return: Query length.
    '''
    return len(urlsplit(url or "").query)


def request_parameter_count(url):
    '''
    Count query parameters in a URL.

    :param url: URL.
    :return: Number of query parameters.
    '''
    return len(parse_qsl(urlsplit(url or "").query, keep_blank_values=True))


def endpoint_key(row):
    '''
    Build a readable endpoint key.

    :param row: Endpoint row.
    :return: Key string.
    '''
    return f"{row['domain']}:{row['line_index']}"


def run_endpoint(wcde, comparator, endpoint, args):
    '''
    Run all WCDE modes for one endpoint.

    :param wcde: WCDE helper.
    :param comparator: ResponseComparator.
    :param endpoint: Endpoint dictionary.
    :param args: Parsed CLI arguments.
    :return: Tuple of (per-mode rows, endpoint JSON document).
    '''
    victim = requests.Session()
    attacker = requests.Session()
    victim_headers = clean_headers(endpoint["headers"], strip_credentials=False)
    attacker_headers = clean_headers(endpoint["headers"], strip_credentials=args.strip_probe_credentials)

    endpoint_doc = {
        "endpoint": endpoint_key(endpoint),
        "domain": endpoint["domain"],
        "source_file": endpoint["source_file"],
        "line_index": endpoint["line_index"],
        "original_url": endpoint["url"],
        "converted_url": endpoint["converted_url"],
        "content_type": endpoint["content_type"],
        "extension": args.extension,
        "dry_run": args.dry_run,
        "normal_exchanges": [],
        "modes": [],
    }

    normal_first_request = request_dict("normal_victim", "GET", endpoint["converted_url"], victim_headers)
    normal_second_request = request_dict("normal_probe", "GET", endpoint["converted_url"], attacker_headers)

    if args.dry_run:
        normal_first = {}
        normal_second = {}
        normal_already_cacheable = False
        endpoint_doc["normal_exchanges"].append(exchange_dict("normal_victim", normal_first_request, None))
        endpoint_doc["normal_exchanges"].append(exchange_dict("normal_probe", normal_second_request, None))
    else:
        normal_first = fetch(victim, endpoint["converted_url"], victim_headers, args.timeout)
        if args.sleep:
            time.sleep(args.sleep)
        normal_second = fetch(attacker, endpoint["converted_url"], attacker_headers, args.timeout)
        normal_already_cacheable = has_cache_hit(wcde, normal_second)
        endpoint_doc["normal_exchanges"].append(exchange_dict("normal_victim", normal_first_request, normal_first))
        endpoint_doc["normal_exchanges"].append(exchange_dict("normal_probe", normal_second_request, normal_second))

    rows = []
    for mode in wcde.MODES:
        attack_url = wcde.generate_attack_url(endpoint["converted_url"], mode, extension=args.extension)
        induce_request = request_dict("wcd_induce", "GET", attack_url, victim_headers)
        probe_request = request_dict("wcd_probe", "GET", attack_url, attacker_headers)
        if args.dry_run:
            endpoint_doc["modes"].append({
                "mode": mode,
                "attack_url": attack_url,
                "output_class": "not_tested_safety",
                "exchanges": [
                    exchange_dict("wcd_induce", induce_request, None),
                    exchange_dict("wcd_probe", probe_request, None),
                ],
            })
            rows.append({
                "endpoint": endpoint_key(endpoint),
                "domain": endpoint["domain"],
                "source_file": endpoint["source_file"],
                "line_index": endpoint["line_index"],
                "original_url": endpoint["url"],
                "converted_url": endpoint["converted_url"],
                "attack_url": attack_url,
                "mode": mode,
                "extension": args.extension,
                "output_class": "not_tested_safety",
                "normal_status": "",
                "induce_status": "",
                "probe_status": "",
                "normal_cache_status": "",
                "induce_cache_status": "",
                "probe_cache_status": "",
                "route_matched": "",
                "route_similarity": "",
                "body_match": "",
                "cache_hit": "",
                "query_length": query_length(endpoint["converted_url"]),
                "parameter_count": request_parameter_count(endpoint["converted_url"]),
            })
            continue

        induce_response = fetch(victim, attack_url, victim_headers, args.timeout)
        if args.sleep:
            time.sleep(args.sleep)
        probe_response = fetch(attacker, attack_url, attacker_headers, args.timeout)
        if normal_already_cacheable:
            output_class = "normal_url_already_cacheable"
            routed = ""
            similarity = ""
            body_match = ""
            hit = has_cache_hit(wcde, probe_response)
        else:
            output_class, routed, similarity, body_match, hit = classify_mode(
                wcde, comparator, normal_first, induce_response, probe_response
            )

        endpoint_doc["modes"].append({
            "mode": mode,
            "attack_url": attack_url,
            "extension": args.extension,
            "output_class": output_class,
            "route_matched": routed,
            "route_similarity": similarity,
            "body_match": body_match,
            "cache_hit": hit,
            "normal_url_already_cacheable": normal_already_cacheable,
            "normal_cache_status": cache_status(wcde, normal_second),
            "induce_cache_status": cache_status(wcde, induce_response),
            "probe_cache_status": cache_status(wcde, probe_response),
            "exchanges": [
                exchange_dict("wcd_induce", induce_request, induce_response),
                exchange_dict("wcd_probe", probe_request, probe_response),
            ],
        })

        rows.append({
            "endpoint": endpoint_key(endpoint),
            "domain": endpoint["domain"],
            "source_file": endpoint["source_file"],
            "line_index": endpoint["line_index"],
            "original_url": endpoint["url"],
            "converted_url": endpoint["converted_url"],
            "attack_url": attack_url,
            "mode": mode,
            "extension": args.extension,
            "output_class": output_class,
            "normal_status": normal_first.get("status_code"),
            "induce_status": induce_response.get("status_code"),
            "probe_status": probe_response.get("status_code"),
            "normal_cache_status": cache_status(wcde, normal_second),
            "induce_cache_status": cache_status(wcde, induce_response),
            "probe_cache_status": cache_status(wcde, probe_response),
            "route_matched": routed,
            "route_similarity": similarity,
            "body_match": body_match,
            "cache_hit": hit,
            "normal_body_sha256": body_hash(normal_first),
            "induce_body_sha256": body_hash(induce_response),
            "probe_body_sha256": body_hash(probe_response),
            "query_length": query_length(endpoint["converted_url"]),
            "parameter_count": request_parameter_count(endpoint["converted_url"]),
        })
        if args.sleep:
            time.sleep(args.sleep)
    return rows, endpoint_doc


def write_csv(path, rows):
    '''
    Write dictionaries to CSV.

    :param path: Output path.
    :param rows: Row dictionaries.
    '''
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def safe_filename(value):
    '''
    Convert a domain into a filesystem-safe filename.

    :param value: Domain string.
    :return: Safe filename stem.
    '''
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "unknown")


def write_domain_jsons(out_dir, endpoint_docs, args):
    '''
    Write one detailed JSON artifact per domain.

    :param out_dir: Output directory.
    :param endpoint_docs: Endpoint-level JSON documents.
    :param args: Parsed CLI arguments.
    '''
    domain_dir = out_dir / "domains"
    domain_dir.mkdir(parents=True, exist_ok=True)

    grouped = {}
    for doc in endpoint_docs:
        grouped.setdefault(doc["domain"], []).append(doc)

    for domain, docs in grouped.items():
        path = domain_dir / f"{safe_filename(domain)}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "domain": domain,
                    "generated_by": "analysis/wcd_confirmed_test.py",
                    "configuration": {
                        "extension": args.extension,
                        "threshold": args.threshold,
                        "timeout": args.timeout,
                        "sleep": args.sleep,
                        "strip_probe_credentials": args.strip_probe_credentials,
                        "dry_run": args.dry_run,
                    },
                    "endpoints": docs,
                },
                f,
                indent=2,
            )


def write_table(path, headers, rows, latex=False):
    '''
    Write a table with tabulate.

    :param path: Output path.
    :param headers: Table headers.
    :param rows: Table rows.
    :param latex: Whether to write LaTeX.
    '''
    tablefmt = "latex" if latex else "github"
    with open(path, "w", encoding="utf-8") as f:
        f.write(tabulate(rows, headers=headers, tablefmt=tablefmt))
        f.write("\n")


def write_table_outputs(out_dir, name, headers, rows, latex=False):
    '''
    Write text and optional LaTeX table outputs.

    :param out_dir: Output directory.
    :param name: Base table name.
    :param headers: Table headers.
    :param rows: Table rows.
    :param latex: Whether to also write LaTeX.
    '''
    write_table(out_dir / f"{name}.txt", headers, rows)
    if latex:
        write_table(out_dir / f"{name}.tex", headers, rows, latex=True)


def confirmed_class(label):
    '''
    Decide whether an output class is confirmed WCD.

    :param label: Output class.
    :return: True if confirmed.
    '''
    return label == "wcd_confirmed"


def strongest_output_class(labels):
    '''
    Select the strongest WCD outcome from a collection of mode labels.

    :param labels: Output class labels.
    :return: Highest-priority output class.
    '''
    return max(labels, key=lambda label: OUTPUT_CLASS_PRIORITY.get(label, -1))


def endpoint_summaries(mode_rows):
    '''
    Aggregate per-mode rows into per-endpoint summaries.

    :param mode_rows: Per-mode result rows.
    :return: Per-endpoint summary rows.
    '''
    grouped = {}
    for row in mode_rows:
        grouped.setdefault(row["endpoint"], []).append(row)

    summaries = []
    for endpoint, rows in grouped.items():
        confirmed = [row for row in rows if confirmed_class(row["output_class"])]
        best_label = strongest_output_class(row["output_class"] for row in rows)
        best = next(row for row in rows if row["output_class"] == best_label)
        summaries.append({
            "endpoint": endpoint,
            "domain": best["domain"],
            "line_index": best["line_index"],
            "converted_url": best["converted_url"],
            "confirmed": bool(confirmed),
            "confirmed_modes": ";".join(row["mode"] for row in confirmed),
            "best_output_class": best["output_class"],
            "tested_modes": len(rows),
        })
    return summaries


def endpoint_site_outcome_rows(summaries):
    '''
    Build paper-facing WCD outcomes from per-endpoint summaries.

    Each endpoint counts once, using the strongest outcome observed across its
    tested payloads. The site column is non-exclusive: it counts sites with at
    least one endpoint in that endpoint-outcome class.

    :param summaries: Per-endpoint summary rows.
    :return: Table rows plus endpoint and site-with-outcome counters.
    '''
    endpoint_labels = [PAPER_OUTCOME_LABELS[row["best_output_class"]] for row in summaries]
    endpoint_counter = Counter(endpoint_labels)

    domains = set()
    sites_by_outcome = {}
    for row in summaries:
        domains.add(row["domain"])
        label = PAPER_OUTCOME_LABELS[row["best_output_class"]]
        sites_by_outcome.setdefault(label, set()).add(row["domain"])

    site_counter = Counter({label: len(domains) for label, domains in sites_by_outcome.items()})
    total_endpoints = len(summaries)
    total_sites = len(domains)

    table_rows = []
    for label in PAPER_OUTCOME_ORDER:
        endpoint_count = endpoint_counter.get(label, 0)
        site_count = site_counter.get(label, 0)
        if endpoint_count == 0 and site_count == 0:
            continue
        table_rows.append([
            label,
            endpoint_count,
            pct(endpoint_count, total_endpoints),
            site_count,
            pct(site_count, total_sites),
        ])
    return table_rows, endpoint_counter, site_counter


def pct(count, denominator):
    '''
    Format a percentage.

    :param count: Numerator.
    :param denominator: Denominator.
    :return: Percentage string.
    '''
    if not denominator:
        return "n/a"
    return f"{(count / denominator) * 100:.1f}%"


def analyze(args):
    '''
    Run WCDE against confirmed interchangeable endpoints.

    :param args: Parsed CLI arguments.
    '''
    logging.getLogger("comparator").setLevel(logging.ERROR)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    wcde = WCDE()
    comparator = ResponseComparator(threshold=args.threshold)
    endpoints = load_confirmed_interchangeable(args.data_dir, args.threshold, args.limit)
    if not endpoints:
        print("No confirmed interchangeable endpoints found.", file=sys.stderr)
        sys.exit(1)

    mode_rows = []
    endpoint_docs = []
    for endpoint in endpoints:
        print(f"Testing {endpoint_key(endpoint)} {endpoint['converted_url']}")
        rows, endpoint_doc = run_endpoint(wcde, comparator, endpoint, args)
        mode_rows.extend(rows)
        endpoint_docs.append(endpoint_doc)

    summaries = endpoint_summaries(mode_rows)
    write_csv(out_dir / "wcd-mode-results.csv", mode_rows)
    write_csv(out_dir / "wcd-endpoint-summary.csv", summaries)
    write_domain_jsons(out_dir, endpoint_docs, args)

    class_counter = Counter(row["output_class"] for row in mode_rows)
    class_rows = [[label, class_counter.get(label, 0), pct(class_counter.get(label, 0), len(mode_rows))] for label in OUTPUT_CLASSES]
    mode_counter = Counter(row["mode"] for row in mode_rows if confirmed_class(row["output_class"]))
    mode_table = [[mode, mode_counter.get(mode, 0)] for mode in wcde.MODES]
    outcome_rows, endpoint_outcomes, sites_with_endpoint_outcome = endpoint_site_outcome_rows(summaries)
    endpoint_rows = [
        [
            row["domain"], row["line_index"], row["confirmed"],
            row["best_output_class"], row["confirmed_modes"], row["tested_modes"],
        ]
        for row in summaries
    ]

    print("\nWCD Output Classes")
    print(tabulate(class_rows, headers=["Output class", "Mode results", "Share"], tablefmt="github"))
    print("\nConfirmed Modes")
    print(tabulate(mode_table, headers=["Mode", "Confirmed endpoints"], tablefmt="github"))
    print("\nEndpoint/Site Outcomes")
    print(tabulate(outcome_rows, headers=["Outcome", "Endpoints", "Endpoint share", "Sites with >=1 endpoint", "Site share"], tablefmt="github"))
    print("\nEndpoint Summary")
    print(tabulate(endpoint_rows, headers=["Domain", "Line", "Confirmed", "Best class", "Confirmed modes", "Tested modes"], tablefmt="github"))

    write_table_outputs(out_dir, "wcd-output-classes", ["Output class", "Mode results", "Share"], class_rows, args.latex)
    write_table_outputs(out_dir, "wcd-confirmed-modes", ["Mode", "Confirmed endpoints"], mode_table, args.latex)
    write_table_outputs(out_dir, "wcd-endpoint-site-outcomes", ["Outcome", "Endpoints", "Endpoint share", "Sites with >=1 endpoint", "Site share"], outcome_rows, args.latex)
    write_table_outputs(out_dir, "wcd-endpoint-summary", ["Domain", "Line", "Confirmed", "Best class", "Confirmed modes", "Tested modes"], endpoint_rows, args.latex)

    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "endpoints": len(endpoints),
                "sites": len(set(row["domain"] for row in summaries)),
                "mode_results": len(mode_rows),
                "confirmed_endpoints": sum(1 for row in summaries if row["confirmed"]),
                "confirmed_sites": len(set(row["domain"] for row in summaries if row["confirmed"])),
                "extension": args.extension,
                "modes": list(wcde.MODES.keys()),
                "output_classes": dict(class_counter),
                "endpoint_outcomes": dict(endpoint_outcomes),
                "sites_with_endpoint_outcome": dict(sites_with_endpoint_outcome),
                "dry_run": args.dry_run,
                "domain_json_dir": "domains",
            },
            f,
            indent=2,
        )
    print(f"\nWrote WCD confirmation outputs to {out_dir}")


def main():
    parser = argparse.ArgumentParser(description="Run WCDE confirmation tests on interchangeable GET endpoints.")
    parser.add_argument("data_dir", help="Batch/session directory or replay JSONL file.")
    parser.add_argument("--out-dir", default="results/wcd-confirmed-test")
    parser.add_argument("--threshold", type=float, default=95.0)
    parser.add_argument("--extension", default=".css")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=0, help="Maximum endpoints to test. 0 means all.")
    parser.add_argument("--strip-probe-credentials", action="store_true", default=True)
    parser.add_argument("--keep-probe-credentials", dest="strip_probe_credentials", action="store_false")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--latex", action="store_true")
    args = parser.parse_args()
    analyze(args)


if __name__ == "__main__":
    main()
