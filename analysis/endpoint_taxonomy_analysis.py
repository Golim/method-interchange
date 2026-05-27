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
import math
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus, urlsplit

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


CATEGORIES = [
    "authentication",
    "account_settings",
    "profile_update",
    "password_or_email_change",
    "newsletter_or_marketing",
    "search_or_filter",
    "contact_or_feedback",
    "cart_or_checkout",
    "content_creation",
    "admin_or_dashboard",
    "API_or_RPC",
    "analytics_or_tracking",
    "unknown",
]

CLASSIFIER_RULES = {
    "authentication": {
        "login", "signin", "sign-in", "signup", "sign-up", "register", "registration",
        "auth", "oauth", "sso", "session", "sessions", "identity", "authenticate",
    },
    "account_settings": {
        "account", "settings", "preferences", "privacy", "billing", "subscription",
        "subscriptions", "manage-account", "member", "membership",
    },
    "profile_update": {
        "profile", "avatar", "bio", "displayname", "display-name", "username",
        "user-name", "firstname", "lastname", "first-name", "last-name",
    },
    "password_or_email_change": {
        "password", "passwd", "pwd", "reset-password", "forgot-password",
        "change-password", "email-change", "change-email", "verify-email",
        "verification", "recover", "recovery",
    },
    "newsletter_or_marketing": {
        "newsletter", "subscribe", "subscription-form", "marketing", "campaign",
        "lead", "leads", "mailchimp", "braze", "hubspot", "optin", "opt-in",
    },
    "search_or_filter": {
        "search", "query", "q", "filter", "filters", "facet", "facets", "sort",
        "autocomplete", "suggest", "suggestion", "lookup", "find",
    },
    "contact_or_feedback": {
        "contact", "feedback", "support", "help", "message", "ticket", "inquiry",
        "enquiry", "survey", "question", "comment-form",
    },
    "cart_or_checkout": {
        "cart", "basket", "checkout", "order", "orders", "payment", "payments",
        "purchase", "shipping", "address", "coupon", "promo", "booking", "reserve",
    },
    "content_creation": {
        "create", "new", "post", "publish", "upload", "comment", "reply", "review",
        "article", "draft", "editor", "story", "submit",
    },
    "admin_or_dashboard": {
        "admin", "dashboard", "console", "manage", "moderator", "moderation",
        "wp-admin", "cms", "controlpanel", "control-panel",
    },
    "API_or_RPC": {
        "api", "ajax", "rpc", "graphql", "jsonrpc", "rest", "v1", "v2", "v3",
        "endpoint", "service", "mutation", "queryid",
    },
    "analytics_or_tracking": {
        "analytics", "tracking", "track", "telemetry", "metric", "metrics", "beacon",
        "collect", "event", "events", "rum", "sentry", "segment", "amplitude",
        "datadog", "newrelic", "pixel", "impression", "conversion", "stats",
        "log", "logs", "monitoring", "zaraz",
    },
}

CLASSIFIER_PRIORITY = [
    "password_or_email_change",
    "authentication",
    "cart_or_checkout",
    "admin_or_dashboard",
    "account_settings",
    "profile_update",
    "content_creation",
    "contact_or_feedback",
    "newsletter_or_marketing",
    "search_or_filter",
    "analytics_or_tracking",
    "API_or_RPC",
]

SENSITIVE_CATEGORIES = {
    "authentication",
    "account_settings",
    "profile_update",
    "password_or_email_change",
    "cart_or_checkout",
    "content_creation",
    "admin_or_dashboard",
}

REDACTION_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\b(?:\d[ -]?){12,19}\b"), "[NUMBER]"),
    (re.compile(r"\b[A-Za-z0-9_-]{24,}\b"), "[TOKEN]"),
    (re.compile(r"\b\d{6,}\b"), "[NUMBER]"),
]


def read_jsonl(path):
    '''
    Read a JSON Lines file and return parsed dictionaries.

    :param path: Path to a JSONL file.
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


def find_replay_files(data_dir):
    '''
    Find replay record files in a batch/session directory or return a direct replay file.

    :param data_dir: Batch directory, session directory, or replay JSONL path.
    :return: Sorted list of replay JSONL paths.
    '''
    data_dir = Path(data_dir)
    if data_dir.is_file() and data_dir.name.endswith("-replay-records.jsonl"):
        return [data_dir]
    return sorted(data_dir.rglob("*-replay-records.jsonl"))


def domain_from_replay_file(path):
    '''
    Extract the domain name from a replay file path.

    :param path: Path named like <domain>-replay-records.jsonl.
    :return: Domain string.
    '''
    suffix = "-replay-records.jsonl"
    if path.name.endswith(suffix):
        return path.name[:-len(suffix)]
    return path.parent.parent.name


def header_value(headers, name):
    '''
    Get a header value case-insensitively.

    :param headers: HTTP header dictionary.
    :param name: Header name.
    :return: Header value or None.
    '''
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return value
    return None


def normalized_content_type(record):
    '''
    Return the source content-type bucket used for grouping.

    :param record: Replay record.
    :return: Content type bucket.
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
    Decide whether the POST produced a converted GET replay candidate.

    :param record: Replay record.
    :return: True if replay candidates exist.
    '''
    return record.get("replay_skipped") is None and bool(record.get("get_candidates"))


def compare_response(comparator, post_response, get_response):
    '''
    Compare POST and GET responses and ignore failed/incomplete responses.

    :param comparator: ResponseComparator instance.
    :param post_response: Original POST response.
    :param get_response: GET response to compare.
    :return: ComparisonResult or None.
    '''
    if not post_response or not get_response or get_response.get("failure"):
        return None
    try:
        return comparator.compare(post_response, get_response)
    except (KeyError, TypeError):
        return None


def high_confidence_interchangeable(record, comparator):
    '''
    Recompute the A-only high-confidence interchangeability label.

    :param record: Replay record.
    :param comparator: ResponseComparator instance.
    :return: True when converted GET matches POST and no-query GET does not.
    '''
    post_response = record.get("response")
    converted_results = []
    no_query_results = []
    for candidate in record.get("get_candidates", []) or []:
        converted_results.append(compare_response(comparator, post_response, candidate.get("get_response", {})))
        no_query_response = (candidate.get("no_data_get") or {}).get("response", {})
        no_query_results.append(compare_response(comparator, post_response, no_query_response))
    converted_match = any(result is not None and result.is_interchangeable for result in converted_results)
    no_query_match = any(result is not None and result.is_interchangeable for result in no_query_results)
    return converted_match and not no_query_match


def redact(text):
    '''
    Redact sensitive-looking values before writing snippets for manual review.

    :param text: Text to redact.
    :return: Redacted text.
    '''
    text = text or ""
    for pattern, replacement in REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def tokenize(text):
    '''
    Tokenize URL paths, parameter names, and snippets into lowercase terms.

    :param text: Source text.
    :return: List of tokens.
    '''
    text = unquote_plus(str(text or "")).lower()
    return [token for token in re.split(r"[^a-z0-9_-]+", text) if token]


def extract_title(body):
    '''
    Extract a response HTML title if present.

    :param body: Response body string.
    :return: Redacted title text or empty string.
    '''
    match = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return redact(title)


def response_snippet(record, limit=500):
    '''
    Extract a short redacted response snippet for classification and review.

    :param record: Replay record.
    :param limit: Maximum snippet length.
    :return: Redacted body snippet.
    '''
    body = ((record.get("response") or {}).get("body") or "")
    body = re.sub(r"<script\b.*?</script>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<style\b.*?</style>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return redact(body[:limit])


def json_param_names(value, prefix=""):
    '''
    Extract JSON key paths for request parameter-name signals.

    :param value: Parsed JSON value.
    :param prefix: Current key path.
    :return: List of key/path strings.
    '''
    names = []
    if isinstance(value, dict):
        for key, child in value.items():
            key = str(key)
            full = f"{prefix}.{key}" if prefix else key
            names.append(full)
            names.extend(json_param_names(child, full))
    elif isinstance(value, list):
        for child in value[:10]:
            names.extend(json_param_names(child, prefix))
    return names


def parameter_names(record):
    '''
    Extract request parameter names from JSON, form-urlencoded, multipart, and URL query.

    :param record: Replay record.
    :return: List of parameter-name strings.
    '''
    names = []
    url = record.get("url") or ""
    names.extend(name for name, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))
    body = record.get("postData", "") or ""
    content_type = normalized_content_type(record)
    if content_type == "json":
        try:
            names.extend(json_param_names(json.loads(body)))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    elif content_type == "form-urlencoded":
        try:
            names.extend(name for name, _ in parse_qsl(body, keep_blank_values=True))
        except ValueError:
            pass
    elif content_type == "multipart":
        names.extend(re.findall(r'name="([^"]+)"', body))
    return names


def path_tokens(record):
    '''
    Extract URL host and path tokens.

    :param record: Replay record.
    :return: List of tokens.
    '''
    parsed = urlsplit(record.get("url") or "")
    return tokenize(parsed.netloc) + tokenize(parsed.path)


def classifier_signal_text(record):
    '''
    Build a combined signal string from all available classifier inputs.

    :param record: Replay record.
    :return: Combined text signal.
    '''
    pieces = []
    pieces.extend(path_tokens(record))
    pieces.extend(parameter_names(record))
    pieces.append(extract_title((record.get("response") or {}).get("body") or ""))
    pieces.append(response_snippet(record, limit=700))
    pieces.append(normalized_content_type(record))
    return " ".join(str(piece) for piece in pieces if piece)


def classify_endpoint(record):
    '''
    Classify an endpoint into one lightweight functionality category.

    :param record: Replay record.
    :return: Tuple of (category, matched_terms, signal_text).
    '''
    signal = classifier_signal_text(record)
    tokens = set(tokenize(signal))
    scores = {}
    matched = {}
    for category, keywords in CLASSIFIER_RULES.items():
        hits = sorted(keyword for keyword in keywords if keyword in tokens or keyword in signal.lower())
        if hits:
            scores[category] = len(hits)
            matched[category] = hits
    if not scores:
        return "unknown", "", signal

    best_score = max(scores.values())
    candidates = {category for category, score in scores.items() if score == best_score}
    for category in CLASSIFIER_PRIORITY:
        if category in candidates:
            return category, ",".join(matched[category]), signal
    category = sorted(candidates)[0]
    return category, ",".join(matched[category]), signal


def load_labeled_rows(data_dir, threshold):
    '''
    Load replay records, classify endpoints, and compute high-confidence labels.

    :param data_dir: Batch/session directory or replay JSONL path.
    :param threshold: Similarity threshold used for interchangeability.
    :return: List of labeled request rows.
    '''
    comparator = ResponseComparator(threshold=threshold)
    rows = []
    for path in find_replay_files(data_dir):
        domain = domain_from_replay_file(path)
        for record in read_jsonl(path):
            category, matched_terms, signal = classify_endpoint(record)
            rows.append({
                "domain": domain,
                "url": record.get("url"),
                "content_type": normalized_content_type(record),
                "category": category,
                "matched_terms": matched_terms,
                "replayed": replayed(record),
                "interchangeable": replayed(record) and high_confidence_interchangeable(record, comparator),
                "response_title": extract_title((record.get("response") or {}).get("body") or ""),
                "response_snippet": response_snippet(record),
                "parameter_names": ";".join(parameter_names(record)[:80]),
                "signal_text": redact(signal[:1800]),
            })
    return rows


def pct(count, denominator):
    '''
    Format a count as a percentage of a denominator.

    :param count: Numerator count.
    :param denominator: Denominator count.
    :return: Percentage string.
    '''
    if not denominator:
        return "n/a"
    return f"{(count / denominator) * 100:.1f}%"


def distribution_rows(rows, label):
    '''
    Build category distribution rows for a given request population.

    :param rows: Labeled request rows in the population.
    :param label: Count-column label.
    :return: Tuple of (headers, rows).
    '''
    counter = Counter(row["category"] for row in rows)
    table_rows = []
    for category in CATEGORIES:
        count = counter.get(category, 0)
        table_rows.append([category, count, pct(count, len(rows))])
    return ["Category", label, "Share"], table_rows


def enrichment_rows(rows):
    '''
    Compute enrichment of high-confidence interchangeable requests by category.

    :param rows: Labeled request rows.
    :return: Table rows.
    '''
    replayed_rows = [row for row in rows if row["replayed"]]
    interchangeable_rows = [row for row in replayed_rows if row["interchangeable"]]
    replayed_counter = Counter(row["category"] for row in replayed_rows)
    interchangeable_counter = Counter(row["category"] for row in interchangeable_rows)
    table_rows = []
    for category in CATEGORIES:
        all_count = replayed_counter.get(category, 0)
        inter_count = interchangeable_counter.get(category, 0)
        p_all = all_count / len(replayed_rows) if replayed_rows else 0.0
        p_inter = inter_count / len(interchangeable_rows) if interchangeable_rows else 0.0
        enrichment = p_inter / p_all if p_all else None
        table_rows.append([
            category,
            all_count,
            pct(all_count, len(replayed_rows)),
            inter_count,
            pct(inter_count, len(interchangeable_rows)),
            "n/a" if enrichment is None else f"{enrichment:.2f}",
        ])
    return table_rows


def sensitive_surface_rows(rows):
    '''
    Summarize whether interchangeable endpoints fall in security-sensitive categories.

    :param rows: Labeled request rows.
    :return: Table rows.
    '''
    replayed_rows = [row for row in rows if row["replayed"]]
    interchangeable_rows = [row for row in replayed_rows if row["interchangeable"]]
    groups = {
        "security_sensitive": lambda row: row["category"] in SENSITIVE_CATEGORIES,
        "likely_low_sensitivity": lambda row: row["category"] in {"newsletter_or_marketing", "search_or_filter", "contact_or_feedback", "analytics_or_tracking"},
        "API_or_RPC": lambda row: row["category"] == "API_or_RPC",
        "unknown": lambda row: row["category"] == "unknown",
    }
    table_rows = []
    for label, predicate in groups.items():
        all_count = sum(1 for row in replayed_rows if predicate(row))
        inter_count = sum(1 for row in interchangeable_rows if predicate(row))
        table_rows.append([
            label,
            all_count,
            pct(all_count, len(replayed_rows)),
            inter_count,
            pct(inter_count, len(interchangeable_rows)),
        ])
    return table_rows


def write_table(path, headers, rows, latex=False):
    '''
    Write a table in markdown-like or LaTeX format.

    :param path: Output file path.
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
    Write a text table and optionally a LaTeX copy.

    :param out_dir: Output directory.
    :param name: Base filename.
    :param headers: Table headers.
    :param rows: Table rows.
    :param latex: Whether to write LaTeX.
    '''
    write_table(out_dir / f"{name}.txt", headers, rows)
    if latex:
        write_table(out_dir / f"{name}.tex", headers, rows, latex=True)


def write_request_labels(path, rows):
    '''
    Write one classified row per request for audit and manual review sampling.

    :param path: Output CSV path.
    :param rows: Labeled request rows.
    '''
    fieldnames = [
        "domain", "url", "content_type", "category", "matched_terms", "replayed",
        "interchangeable", "response_title", "parameter_names", "response_snippet",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row[name] for name in fieldnames})


def plot_category_bars(path, rows):
    '''
    Plot category counts for all replayed and interchangeable POSTs.

    :param path: Output PNG path.
    :param rows: Labeled request rows.
    '''
    replayed_rows = [row for row in rows if row["replayed"]]
    interchangeable_rows = [row for row in replayed_rows if row["interchangeable"]]
    replayed_counter = Counter(row["category"] for row in replayed_rows)
    inter_counter = Counter(row["category"] for row in interchangeable_rows)
    categories = [category for category in CATEGORIES if replayed_counter.get(category, 0) or inter_counter.get(category, 0)]
    if not categories:
        return
    x = range(len(categories))
    width = 0.42
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar([i - width / 2 for i in x], [replayed_counter.get(c, 0) for c in categories], width=width, label="All replayed", color="#4C78A8")
    ax.bar([i + width / 2 for i in x], [inter_counter.get(c, 0) for c in categories], width=width, label="Interchangeable", color="#F58518")
    ax.set_xticks(list(x))
    ax.set_xticklabels(categories, rotation=35, ha="right")
    ax.set_ylabel("POST requests")
    ax.set_title("Endpoint taxonomy distribution")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def sample_size(population, confidence=0.95, margin=0.05, proportion=0.5):
    '''
    Compute finite-population sample size for a proportion estimate.

    :param population: Population size.
    :param confidence: Confidence level, supports 0.90, 0.95, and 0.99.
    :param margin: Desired margin of error.
    :param proportion: Conservative expected proportion, defaults to 0.5.
    :return: Sample size.
    '''
    z_values = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}
    z = z_values.get(round(confidence, 2), 1.96)
    if population <= 0:
        return 0
    numerator = (z * z) * proportion * (1 - proportion)
    n0 = numerator / (margin * margin)
    return math.ceil(n0 / (1 + ((n0 - 1) / population)))


def write_review_sample(args):
    '''
    Create a random review sample CSV with blank manual-label fields.

    :param args: Parsed argparse namespace.
    '''
    logging.getLogger("comparator").setLevel(logging.ERROR)
    rows = load_labeled_rows(args.data_dir, args.threshold)
    if args.population == "replayed":
        rows = [row for row in rows if row["replayed"]]
    elif args.population == "interchangeable":
        rows = [row for row in rows if row["interchangeable"]]
    population = len(rows)
    n = args.sample_size or sample_size(population, args.confidence, args.margin)
    n = min(n, population)
    rng = random.Random(args.seed)
    sampled = rng.sample(rows, n) if n else []

    fieldnames = [
        "review_id", "manual_category", "manual_correct", "notes",
        "predicted_category", "domain", "url", "content_type", "replayed",
        "interchangeable", "matched_terms", "parameter_names", "response_title",
        "response_snippet",
    ]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, row in enumerate(sampled, 1):
            writer.writerow({
                "review_id": i,
                "manual_category": "",
                "manual_correct": "",
                "notes": "",
                "predicted_category": row["category"],
                "domain": row["domain"],
                "url": row["url"],
                "content_type": row["content_type"],
                "replayed": row["replayed"],
                "interchangeable": row["interchangeable"],
                "matched_terms": row["matched_terms"],
                "parameter_names": row["parameter_names"],
                "response_title": row["response_title"],
                "response_snippet": row["response_snippet"],
            })
    print(f"Wrote {n} review rows from population {population} to {output}")


def review_cli(args):
    '''
    Walk through a review sample CSV and fill manual_category/manual_correct quickly.

    :param args: Parsed argparse namespace.
    '''
    path = Path(args.sample_csv)
    with open(path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys()) if rows else []

    if not rows:
        print("No rows to review.")
        return

    category_numbers = {str(i + 1): category for i, category in enumerate(CATEGORIES)}
    for row in rows:
        if row.get("manual_category") and not args.resume_all:
            continue
        print("\n" + "=" * 80)
        print(f"Review {row.get('review_id')}  predicted={row.get('predicted_category')}")
        print(f"URL: {row.get('url')}")
        print(f"Parameters: {row.get('parameter_names')}")
        print(f"Title: {row.get('response_title')}")
        print(f"Snippet: {row.get('response_snippet')[:600]}")
        print("\nCategories:")
        for i, category in enumerate(CATEGORIES, 1):
            print(f"  {i:2d}. {category}")
        answer = input("Manual category number/name, Enter=accept, s=skip, q=quit: ").strip()
        if answer.lower() == "q":
            break
        if answer.lower() == "s":
            continue
        if not answer:
            manual = row.get("predicted_category")
        else:
            manual = category_numbers.get(answer, answer)
        row["manual_category"] = manual
        row["manual_correct"] = "1" if manual == row.get("predicted_category") else "0"
        note = input("Notes (optional): ").strip()
        if note:
            row["notes"] = note

        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    reviewed = [row for row in rows if row.get("manual_category")]
    correct = [row for row in reviewed if row.get("manual_correct") == "1"]
    print(f"\nReviewed {len(reviewed)}/{len(rows)} rows. Apparent accuracy: {pct(len(correct), len(reviewed))}")


def analyze(args):
    '''
    Run endpoint taxonomy analysis and write all output tables.

    :param args: Parsed argparse namespace.
    '''
    if not args.verbose:
        logging.getLogger("comparator").setLevel(logging.ERROR)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_labeled_rows(args.data_dir, args.threshold)
    if not rows:
        print(f"No replay records found in {args.data_dir}", file=sys.stderr)
        sys.exit(1)

    all_headers, all_rows = distribution_rows(rows, "All POSTs")
    replayed_headers, replayed_rows = distribution_rows([row for row in rows if row["replayed"]], "Replayed POSTs")
    inter_headers, inter_rows = distribution_rows([row for row in rows if row["interchangeable"]], "Interchangeable POSTs")
    enrichment_headers = ["Category", "All replayed", "P category given replayed", "Interchangeable", "P category given interchangeable", "Enrichment"]
    enrichment_table = enrichment_rows(rows)
    sensitive_headers = ["Surface group", "All replayed", "Share of all replayed", "Interchangeable", "Share of interchangeable"]
    sensitive_table = sensitive_surface_rows(rows)

    tables = [
        ("all-post-category-distribution", all_headers, all_rows),
        ("replayed-post-category-distribution", replayed_headers, replayed_rows),
        ("interchangeable-category-distribution", inter_headers, inter_rows),
        ("category-enrichment", enrichment_headers, enrichment_table),
        ("security-sensitive-surface", sensitive_headers, sensitive_table),
    ]
    for name, headers, table_rows in tables:
        print(f"\n{name.replace('-', ' ').title()}")
        print(tabulate(table_rows, headers=headers, tablefmt="github"))
        write_table_outputs(out_dir, name, headers, table_rows, args.latex)

    write_request_labels(out_dir / "request-endpoint-labels.csv", rows)
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "records": len(rows),
                "replayed_records": sum(row["replayed"] for row in rows),
                "interchangeable_records": sum(row["interchangeable"] for row in rows),
                "threshold": args.threshold,
                "categories": CATEGORIES,
                "classifier_caveat": "Lightweight heuristic classifier; use manual review sample to estimate accuracy.",
                "unavailable_signals": ["button text", "form labels", "input labels"],
            },
            f,
            indent=2,
        )
    if not args.no_charts:
        plot_category_bars(out_dir / "endpoint-category-distribution.png", rows)
    print(f"\nWrote endpoint taxonomy outputs to {out_dir}")


def main():
    parser = argparse.ArgumentParser(description="Analyze endpoint taxonomy and security-sensitive surface.")
    subparsers = parser.add_subparsers(dest="command")

    analyze_parser = subparsers.add_parser("analyze", help="Run taxonomy analysis")
    analyze_parser.add_argument("data_dir", help="Batch/session directory or replay JSONL file.")
    analyze_parser.add_argument("--out-dir", default="analysis/endpoint-taxonomy-analysis")
    analyze_parser.add_argument("--threshold", type=float, default=95.0)
    analyze_parser.add_argument("--latex", action="store_true")
    analyze_parser.add_argument("--no-charts", action="store_true")
    analyze_parser.add_argument("--verbose", action="store_true")
    analyze_parser.set_defaults(func=analyze)

    sample_parser = subparsers.add_parser("sample-review", help="Create a statistically sized review CSV")
    sample_parser.add_argument("data_dir", help="Batch/session directory or replay JSONL file.")
    sample_parser.add_argument("--output", default="analysis/endpoint-taxonomy-analysis/manual-review-sample.csv")
    sample_parser.add_argument("--population", choices=["all", "replayed", "interchangeable"], default="replayed")
    sample_parser.add_argument("--sample-size", type=int)
    sample_parser.add_argument("--confidence", type=float, default=0.95)
    sample_parser.add_argument("--margin", type=float, default=0.05)
    sample_parser.add_argument("--seed", type=int, default=42)
    sample_parser.add_argument("--threshold", type=float, default=95.0)
    sample_parser.set_defaults(func=write_review_sample)

    review_parser = subparsers.add_parser("review-cli", help="Manually label a review CSV in the terminal")
    review_parser.add_argument("sample_csv")
    review_parser.add_argument("--resume-all", action="store_true", help="Review rows even if manual_category is already filled")
    review_parser.set_defaults(func=review_cli)

    if len(sys.argv) > 1 and sys.argv[1] not in {"analyze", "sample-review", "review-cli", "-h", "--help"}:
        sys.argv.insert(1, "analyze")

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
