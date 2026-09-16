#!/usr/bin/env python3

__license__ = "MIT"

import argparse
import csv
import json
import logging
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
MIDE_ROOT = REPO_ROOT / "mide"
for path in (REPO_ROOT, MIDE_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

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


PRIMARY_REASON_ORDER = [
    "unsupported_content_type",
    "body_parse_error",
    "top_level_JSON_array_failure",
    "file_upload_removed",
    "empty_body",
    "oversized_query_after_conversion",
    "non-UTF8_or_binary_body",
    "network_or_replay_error",
    "replayed_not_equivalent",
    "valid_replay_candidate",
]


def read_jsonl(path):
    '''
    Read a JSON Lines file and return parsed JSON objects.

    :param path: Path to a JSONL file.
    :return: List of JSON dictionaries.
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


def find_replay_files(data_dir):
    '''
    Find replay-record JSONL files under a batch/session directory.

    :param data_dir: Batch directory, session directory, or replay JSONL path.
    :return: Sorted list of replay JSONL paths.
    '''
    data_dir = Path(data_dir)
    if data_dir.is_file() and data_dir.name.endswith("-replay-records.jsonl"):
        return [data_dir]
    return sorted(data_dir.rglob("*-replay-records.jsonl"))


def domain_from_replay_file(path):
    '''
    Extract the domain name from a replay JSONL filename.

    :param path: Path named like <domain>-replay-records.jsonl.
    :return: Domain string.
    '''
    suffix = "-replay-records.jsonl"
    if path.name.endswith(suffix):
        return path.name[:-len(suffix)]
    return path.parent.parent.name


def header_value(headers, name):
    '''
    Retrieve a header value case-insensitively.

    :param headers: HTTP headers dictionary.
    :param name: Header name.
    :return: Header value or None.
    '''
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return value
    return None


def normalized_content_type(record):
    '''
    Return the normalized source content-type bucket used for grouping.

    :param record: Replay record.
    :return: One of the known buckets or a normalized raw media type.
    '''
    bucket = record.get("source_content_type_bucket")
    if bucket:
        return bucket
    raw = header_value(record.get("headers", {}), "content-type")
    if not raw:
        return "unknown"
    media_type = str(raw).split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        return "json"
    if media_type == "application/x-www-form-urlencoded":
        return "form-urlencoded"
    if media_type == "multipart/form-data":
        return "multipart"
    return media_type or "unknown"


def replayed(record):
    '''
    Decide whether a POST record produced at least one converted GET candidate.

    :param record: Replay record.
    :return: True if conversion produced replay candidates.
    '''
    return record.get("replay_skipped") is None and bool(record.get("get_candidates"))


def replay_success(record):
    '''
    Decide whether any converted GET candidate received an HTTP response.

    :param record: Replay record.
    :return: True if at least one candidate has a status code and no failure object.
    '''
    for candidate in record.get("get_candidates", []) or []:
        response = candidate.get("get_response", {}) or {}
        if response.get("failure") is None and response.get("status_code") is not None:
            return True
    return False


def replay_failure_label(record):
    '''
    Categorize replay failures using recorded failure objects and status codes.

    :param record: Replay record.
    :return: Replay failure label or empty string if replay succeeded.
    '''
    if not replayed(record):
        return ""
    if replay_success(record):
        return ""
    failures = []
    statuses = []
    for candidate in record.get("get_candidates", []) or []:
        response = candidate.get("get_response", {}) or {}
        if response.get("status_code") is not None:
            statuses.append(response.get("status_code"))
        failure = response.get("failure")
        if isinstance(failure, dict):
            failures.append(failure)

    if statuses and all(status in {401, 403, 429} for status in statuses):
        return "replay_blocked"
    if not failures:
        return "network_or_replay_error"

    text = " ".join(
        f"{f.get('failure_type', '')} {f.get('failure_message', '')}"
        for f in failures
    ).lower()
    if "timeout" in text:
        return "replay_timeout"
    if "ssl" in text or "tls" in text or "certificate" in text:
        return "replay_tls_error"
    if "redirect" in text:
        return "replay_redirect_loop"
    return "network_or_replay_error"


def compare_response(comparator, post_response, get_response):
    '''
    Compare POST and GET responses while treating failed responses as not comparable.

    :param comparator: ResponseComparator instance.
    :param post_response: Original POST response dictionary.
    :param get_response: Converted GET response dictionary.
    :return: ComparisonResult or None.
    '''
    if not post_response or not get_response or get_response.get("failure"):
        return None
    try:
        return comparator.compare(post_response, get_response)
    except (KeyError, TypeError):
        return None


def equivalent(record, comparator):
    '''
    Decide whether any converted GET candidate is equivalent to the original POST response.

    :param record: Replay record.
    :param comparator: ResponseComparator instance.
    :return: True if any candidate is equivalent.
    '''
    post_response = record.get("response")
    for candidate in record.get("get_candidates", []) or []:
        result = compare_response(comparator, post_response, candidate.get("get_response", {}))
        if result is not None and result.is_interchangeable:
            return True
    return False


def query_lengths(record):
    '''
    Compute converted query-string lengths for all GET candidates.

    :param record: Replay record.
    :return: List of query-string lengths in characters.
    '''
    lengths = []
    for candidate in record.get("get_candidates", []) or []:
        converted_url = candidate.get("converted_url") or ""
        lengths.append(len(urlsplit(converted_url).query))
    return lengths


def max_query_length(record):
    '''
    Return the maximum converted query-string length for a record.

    :param record: Replay record.
    :return: Maximum query length, or 0 if there are no candidates.
    '''
    lengths = query_lengths(record)
    return max(lengths) if lengths else 0


def parse_json_body(record):
    '''
    Parse a JSON POST body.

    :param record: Replay record.
    :return: Parsed JSON value, or None on parse failure.
    '''
    try:
        return json.loads(record.get("postData", "") or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def walk_json(value):
    '''
    Yield every JSON value in a parsed JSON tree.

    :param value: Parsed JSON value.
    :return: Iterator of JSON subtree values.
    '''
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def json_shape(record):
    '''
    Classify JSON body shape for separate JSON-methodology reporting.

    :param record: Replay record with JSON content type.
    :return: Shape label.
    '''
    value = parse_json_body(record)
    if value is None:
        return "body_parse_error"
    labels = json_shape_labels(record)
    if "JSON object" in labels and len(labels) == 1:
        return "JSON object"
    if "JSON array" in labels and len(labels) == 1:
        return "JSON array"
    if "scalar top-level" in labels:
        return "scalar top-level"
    for label in ("mixed arrays", "nested array", "nested object", "JSON array", "JSON object"):
        if label in labels:
            return label
    return labels[0] if labels else "body_parse_error"


def json_shape_labels(record):
    '''
    Return non-exclusive JSON body shape labels for methodology reporting.

    :param record: Replay record with JSON content type.
    :return: List of shape labels.
    '''
    value = parse_json_body(record)
    if value is None:
        return ["body_parse_error"]

    labels = []
    if isinstance(value, dict):
        labels.append("JSON object")
    elif isinstance(value, list):
        labels.append("JSON array")
    else:
        labels.append("scalar top-level")
        return labels

    all_values = list(walk_json(value))
    if any(isinstance(v, dict) for v in all_values if v is not value):
        labels.append("nested object")
    if any(isinstance(v, list) for v in all_values if v is not value):
        labels.append("nested array")
    if any(
        isinstance(v, list) and len({json_value_kind(item) for item in v}) > 1
        for v in all_values
    ):
        labels.append("mixed arrays")
    return labels


def json_value_kind(value):
    '''
    Return a coarse JSON value kind for array-mixedness checks.

    :param value: Parsed JSON value.
    :return: Kind label.
    '''
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return type(value).__name__


def multipart_parts(record):
    '''
    Parse multipart parts well enough to count field and file parts.

    :param record: Replay record with multipart content type.
    :return: Tuple of (field_count, file_count).
    '''
    body = record.get("postData", "") or ""
    content_type = header_value(record.get("headers", {}), "content-type") or ""
    boundary_match = re.search(r'boundary="?([^"\s;]+)"?', content_type)
    if not body or not boundary_match:
        return (0, 0)

    boundary = boundary_match.group(1)
    field_count = 0
    file_count = 0
    for part in body.split(f"--{boundary}"):
        if not part.strip() or part.strip() == "--":
            continue
        if "Content-Disposition" not in part or "name=" not in part:
            continue
        if "filename=" in part:
            file_count += 1
        else:
            field_count += 1
    return (field_count, file_count)


def multipart_shape(record):
    '''
    Classify multipart bodies by whether they contain fields and/or files.

    :param record: Replay record with multipart content type.
    :return: Shape label.
    '''
    field_count, file_count = multipart_parts(record)
    if field_count and file_count:
        return "fields_plus_files"
    if file_count:
        return "files_only"
    return "fields_only"


def looks_binary_or_non_utf8(record):
    '''
    Heuristically detect bodies that look binary or replacement-character damaged.

    :param record: Replay record.
    :return: True if the body has replacement characters or many control characters.
    '''
    body = record.get("postData", "")
    if not body:
        return False
    if "\ufffd" in body:
        return True
    control = sum(1 for ch in body if ord(ch) < 32 and ch not in "\r\n\t")
    return control > 0 and control / max(len(body), 1) > 0.02


def primary_miss_reason(record, is_equivalent, oversized_query_threshold):
    '''
    Assign a single primary reason explaining why the POST was not a clean candidate.

    :param record: Replay record.
    :param is_equivalent: Whether converted GET matched the POST response.
    :param oversized_query_threshold: Query-string length threshold for oversized URL risk.
    :return: Reason label.
    '''
    content_type = normalized_content_type(record)
    body = record.get("postData", "")
    skipped = record.get("replay_skipped")

    if skipped == "unsupported_content_type":
        return "unsupported_content_type"
    if not body:
        return "empty_body"
    if looks_binary_or_non_utf8(record):
        return "non-UTF8_or_binary_body"
    if skipped in {"malformed_body", "depth_truncation"}:
        if content_type == "json" and "JSON array" in json_shape_labels(record):
            return "top_level_JSON_array_failure"
        return "body_parse_error"
    if content_type == "unknown":
        return "unsupported_content_type"
    if content_type == "multipart" and multipart_shape(record) != "fields_only":
        return "file_upload_removed"
    if max_query_length(record) > oversized_query_threshold:
        return "oversized_query_after_conversion"
    if replayed(record) and not replay_success(record):
        return "network_or_replay_error"
    if replayed(record) and not is_equivalent:
        return "replayed_not_equivalent"
    if replayed(record):
        return "valid_replay_candidate"
    return "body_parse_error"


def load_records(data_dir, threshold, oversized_query_threshold):
    '''
    Load replay records and attach content, replay, equivalence, and failure labels.

    :param data_dir: Input directory or replay JSONL file.
    :param threshold: Similarity threshold for equivalence.
    :param oversized_query_threshold: Query length threshold for oversized URL labels.
    :return: List of labeled rows.
    '''
    comparator = ResponseComparator(threshold=threshold)
    rows = []
    for path in find_replay_files(data_dir):
        domain = domain_from_replay_file(path)
        for record in read_jsonl(path):
            content_type = normalized_content_type(record)
            is_equivalent = equivalent(record, comparator)
            query_len = max_query_length(record)
            row = {
                "domain": domain,
                "url": record.get("url"),
                "content_type": content_type,
                "conversion_success": replayed(record),
                "replay_success": replay_success(record),
                "equivalent": is_equivalent,
                "lossy_conversion": bool(record.get("lossy_conversion")) or (
                    content_type == "multipart" and multipart_shape(record) != "fields_only"
                ),
                "primary_miss_reason": primary_miss_reason(record, is_equivalent, oversized_query_threshold),
                "replay_failure": replay_failure_label(record),
                "query_length": query_len,
                "json_shape": "; ".join(json_shape_labels(record)) if content_type == "json" else "",
                "multipart_shape": multipart_shape(record) if content_type == "multipart" else "",
                "raw": record,
            }
            rows.append(row)
    return rows


def pct(count, denominator):
    '''
    Format count divided by denominator as a percentage.

    :param count: Numerator count.
    :param denominator: Denominator count.
    :return: Percentage string.
    '''
    if not denominator:
        return "n/a"
    return f"{(count / denominator) * 100:.1f}%"


def write_table(path, headers, rows, latex=False):
    '''
    Write a table using tabulate.

    :param path: Output path.
    :param headers: Table headers.
    :param rows: Table rows.
    :param latex: Whether to write LaTeX format.
    :return: Formatted table text.
    '''
    tablefmt = "latex" if latex else "github"
    text = tabulate(rows, headers=headers, tablefmt=tablefmt)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.write("\n")
    return text


def write_table_outputs(out_dir, name, headers, rows, latex=False):
    '''
    Write a text table and optionally a LaTeX table.

    :param out_dir: Output directory.
    :param name: Base filename without extension.
    :param headers: Table headers.
    :param rows: Table rows.
    :param latex: Whether to write a LaTeX copy.
    '''
    write_table(out_dir / f"{name}.txt", headers, rows)
    if latex:
        write_table(out_dir / f"{name}.tex", headers, rows, latex=True)


def conversion_success_rows(rows):
    '''
    Build conversion success table by content type.

    :param rows: Labeled request rows.
    :return: Table rows.
    '''
    output = []
    for content_type in sorted({r["content_type"] for r in rows}):
        subset = [r for r in rows if r["content_type"] == content_type]
        success = sum(r["conversion_success"] for r in subset)
        lossy = sum(r["lossy_conversion"] for r in subset)
        output.append([content_type, success, len(subset), pct(success, len(subset)), lossy, pct(lossy, len(subset))])
    return output


def replay_success_rows(rows):
    '''
    Build replay success table by content type among converted candidates.

    :param rows: Labeled request rows.
    :return: Table rows.
    '''
    output = []
    for content_type in sorted({r["content_type"] for r in rows}):
        subset = [r for r in rows if r["content_type"] == content_type and r["conversion_success"]]
        success = sum(r["replay_success"] for r in subset)
        output.append([content_type, success, len(subset), pct(success, len(subset))])
    return output


def equivalence_rows(rows):
    '''
    Build equivalence-rate table by content type among replay-success records.

    :param rows: Labeled request rows.
    :return: Table rows.
    '''
    output = []
    for content_type in sorted({r["content_type"] for r in rows}):
        subset = [r for r in rows if r["content_type"] == content_type and r["replay_success"]]
        equivalent_count = sum(r["equivalent"] for r in subset)
        lossy_equivalent = sum(r["equivalent"] and r["lossy_conversion"] for r in subset)
        output.append([
            content_type,
            equivalent_count,
            len(subset),
            pct(equivalent_count, len(subset)),
            lossy_equivalent,
            "weaker evidence" if lossy_equivalent else "",
        ])
    return output


def lossy_multipart_rows(rows):
    '''
    Build confirmation table for lossy multipart candidates.

    :param rows: Labeled request rows.
    :return: Table rows.
    '''
    multipart = [r for r in rows if r["content_type"] == "multipart"]
    lossy = [r for r in multipart if r["lossy_conversion"]]
    confirmed = [r for r in lossy if r["equivalent"]]
    return [[
        "multipart with files removed",
        len(confirmed),
        len(lossy),
        pct(len(confirmed), len(lossy)),
        "weaker evidence: file upload omitted from GET replay",
    ]]


def count_rows(counter, denominator):
    '''
    Convert a Counter to table rows with shares.

    :param counter: Counter of labels.
    :param denominator: Share denominator.
    :return: Table rows.
    '''
    return [[label, count, pct(count, denominator)] for label, count in sorted(counter.items())]


def primary_reason_rows(rows):
    '''
    Build miss-reason rows with all suggested categories shown, including zero counts.

    :param rows: Labeled request rows.
    :return: Table rows.
    '''
    counter = Counter(r["primary_miss_reason"] for r in rows)
    labels = [label for label in PRIMARY_REASON_ORDER if label != "valid_replay_candidate"]
    labels.append("valid_replay_candidate")
    labels.extend(sorted(label for label in counter if label not in labels))
    return [[label, counter.get(label, 0), pct(counter.get(label, 0), len(rows))] for label in labels]


def json_shape_rows(rows):
    '''
    Build a non-exclusive JSON shape table using the requested paper categories.

    :param rows: Labeled request rows.
    :return: Table rows.
    '''
    json_rows = [r for r in rows if r["content_type"] == "json"]
    counter = Counter()
    for row in json_rows:
        for label in row["json_shape"].split("; "):
            if label:
                counter[label] += 1
    order = [
        "JSON object",
        "JSON array",
        "nested object",
        "nested array",
        "mixed arrays",
        "scalar top-level",
        "body_parse_error",
    ]
    labels = [label for label in order if label in counter]
    labels.extend(sorted(label for label in counter if label not in order))
    return [[label, counter[label], pct(counter[label], len(json_rows))] for label in labels]


def percentile(values, p):
    '''
    Compute a simple percentile without adding a numpy dependency.

    :param values: Sorted numeric values.
    :param p: Percentile in [0, 100].
    :return: Percentile value.
    '''
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * (p / 100)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)


def query_length_rows(rows, oversized_query_threshold):
    '''
    Build query length distribution rows by content type.

    :param rows: Labeled request rows.
    :param oversized_query_threshold: Threshold used for oversized query counts.
    :return: Table rows.
    '''
    output = []
    for content_type in sorted({r["content_type"] for r in rows}):
        values = sorted(r["query_length"] for r in rows if r["content_type"] == content_type and r["conversion_success"])
        if not values:
            output.append([content_type, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "n/a"])
            continue
        over = sum(v > oversized_query_threshold for v in values)
        output.append([
            content_type,
            len(values),
            int(min(values)),
            int(percentile(values, 25)),
            int(percentile(values, 50)),
            int(percentile(values, 75)),
            int(percentile(values, 90)),
            int(percentile(values, 95)),
            int(percentile(values, 99)),
            int(max(values)),
            over,
            pct(over, len(values)),
        ])
    return output


def write_request_csv(path, rows):
    '''
    Write per-request failure-analysis labels.

    :param path: Output CSV path.
    :param rows: Labeled request rows.
    '''
    fieldnames = [
        "domain",
        "url",
        "content_type",
        "conversion_success",
        "replay_success",
        "equivalent",
        "lossy_conversion",
        "primary_miss_reason",
        "replay_failure",
        "query_length",
        "json_shape",
        "multipart_shape",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row[name] for name in fieldnames})


def plot_query_lengths(path, rows, oversized_query_threshold):
    '''
    Plot histogram of converted query-string lengths.

    :param path: Output PNG path.
    :param rows: Labeled request rows.
    :param oversized_query_threshold: Threshold drawn as a vertical line.
    '''
    values = [r["query_length"] for r in rows if r["conversion_success"]]
    if not values:
        return
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(values, bins=40, color="#4C78A8", alpha=0.85)
    ax.axvline(oversized_query_threshold, color="#E45756", linestyle="--", linewidth=1)
    ax.set_xlabel("Converted query length (characters)")
    ax.set_ylabel("Converted POST records")
    ax.set_title("Converted GET query length distribution")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def plot_failure_reasons(path, rows):
    '''
    Plot the most common miss reasons.

    :param path: Output PNG path.
    :param rows: Labeled request rows.
    '''
    counter = Counter(r["primary_miss_reason"] for r in rows)
    labels_counts = counter.most_common(12)
    if not labels_counts:
        return
    labels = [label for label, _ in labels_counts]
    counts = [count for _, count in labels_counts]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh(labels[::-1], counts[::-1], color="#F58518")
    ax.set_xlabel("POST records")
    ax.set_title("Primary conversion/replay miss reasons")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze conversion and replay failures for captured POST records."
    )
    parser.add_argument("data_dir", help="Batch/session directory or replay-records JSONL file.")
    parser.add_argument("--out-dir", default="results/conversion-replay-failure-analysis", help="Directory for tables and charts.")
    parser.add_argument("--threshold", type=float, default=95.0, help="Similarity threshold percentage.")
    parser.add_argument("--oversized-query-threshold", type=int, default=8192, help="Query length threshold for oversized URL labels.")
    parser.add_argument("--latex", action="store_true", help="Also write tables in LaTeX format.")
    parser.add_argument("--no-charts", action="store_true", help="Skip chart generation.")
    parser.add_argument("--verbose", action="store_true", help="Show comparator warnings while processing.")
    args = parser.parse_args()

    if not args.verbose:
        logging.getLogger("comparator").setLevel(logging.ERROR)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_records(args.data_dir, args.threshold, args.oversized_query_threshold)
    if not rows:
        print(f"No replay records found in {args.data_dir}", file=sys.stderr)
        sys.exit(1)

    conversion_headers = ["Content type", "Converted", "Captured POSTs", "Conversion success", "Lossy conversions", "Lossy share"]
    replay_headers = ["Content type", "Replay successes", "Converted POSTs", "Replay success"]
    equivalence_headers = ["Content type", "Equivalent", "Replay successes", "Equivalence rate", "Lossy equivalent", "Caveat"]
    query_headers = ["Content type", "N", "min", "p25", "median", "p75", "p90", "p95", "p99", "max", "over threshold", "over threshold share"]

    tables = [
        ("conversion-success-by-content-type", conversion_headers, conversion_success_rows(rows)),
        ("replay-success-by-content-type", replay_headers, replay_success_rows(rows)),
        ("equivalence-by-content-type", equivalence_headers, equivalence_rows(rows)),
        ("lossy-multipart-confirmation", ["Population", "Equivalent", "Lossy multipart", "Confirmation rate", "Caveat"], lossy_multipart_rows(rows)),
        ("query-length-distribution", query_headers, query_length_rows(rows, args.oversized_query_threshold)),
        ("primary-miss-reasons", ["Reason", "POST records", "Share"], primary_reason_rows(rows)),
        ("json-shapes", ["JSON shape", "POST records", "Share"], json_shape_rows(rows)),
        ("multipart-shapes", ["Multipart shape", "POST records", "Share"], count_rows(Counter(r["multipart_shape"] for r in rows if r["content_type"] == "multipart"), sum(1 for r in rows if r["content_type"] == "multipart"))),
        ("replay-failures", ["Replay failure", "POST records", "Share"], count_rows(Counter(r["replay_failure"] for r in rows if r["replay_failure"]), sum(1 for r in rows if r["replay_failure"]))),
    ]

    for name, headers, table_rows in tables:
        print(f"\n{name.replace('-', ' ').title()}")
        print(tabulate(table_rows, headers=headers, tablefmt="github"))
        write_table_outputs(out_dir, name, headers, table_rows, args.latex)

    write_request_csv(out_dir / "request-failure-labels.csv", rows)
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "records": len(rows),
                "converted_records": sum(r["conversion_success"] for r in rows),
                "replay_success_records": sum(r["replay_success"] for r in rows),
                "equivalent_records": sum(r["equivalent"] for r in rows),
                "lossy_records": sum(r["lossy_conversion"] for r in rows),
                "threshold": args.threshold,
                "oversized_query_threshold": args.oversized_query_threshold,
                "primary_miss_reasons": dict(Counter(r["primary_miss_reason"] for r in rows)),
            },
            f,
            indent=2,
        )

    if not args.no_charts:
        plot_query_lengths(out_dir / "query-length-distribution.png", rows, args.oversized_query_threshold)
        plot_failure_reasons(out_dir / "primary-miss-reasons.png", rows)

    print(f"\nWrote failure analysis outputs to {out_dir}")


if __name__ == "__main__":
    main()
