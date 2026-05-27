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
import shlex
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher, unified_diff
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import NormalDist
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from tabulate import tabulate
except ImportError as e:
    print(
        f"Missing dependency: {e.name}. Install project dependencies with `uv sync`.",
        file=sys.stderr,
    )
    sys.exit(1)

from lib.comparator import ComparisonResult, ResponseComparator


BASELINE_CLASSES = {
    "A": "Strong interchangeability",
    "B": "Ambiguous",
    "C": "Baseline collision",
    "D": "Not interchangeable",
}

ANNOTATION_VALUES = ("equivalent", "different", "uncertain")


@dataclass
class CandidateComparison:
    candidate_index: int
    converted_url: str
    converted: dict[str, Any] | None
    no_query: dict[str, Any] | None


@dataclass
class StudyItem:
    item_id: str
    domain: str
    source_file: str
    line_number: int
    record_index: int
    url: str
    route: str
    content_type_bucket: str
    resource_type: str
    baseline_class: str
    baseline_class_name: str
    threshold_classes: dict[str, str]
    converted_similarity: float
    no_query_similarity: float
    converted_status: int | None
    no_query_status: int | None
    post_status: int | None
    best_candidate_index: int
    converted_url: str
    no_query_url: str
    sample_stratum: str
    sample_bucket: str


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def read_jsonl_line(path: Path, line_number: int) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        for current, line in enumerate(handle, 1):
            if current == line_number:
                return json.loads(line)
    raise IndexError(f"{path}:{line_number} does not exist")


def find_replay_files(data_dir: Path) -> list[Path]:
    if data_dir.is_file() and data_dir.name.endswith("-replay-records.jsonl"):
        return [data_dir]
    return sorted(data_dir.rglob("*-replay-records.jsonl"))


def domain_from_replay_file(path: Path) -> str:
    suffix = "-replay-records.jsonl"
    if path.name.endswith(suffix):
        return path.name[:-len(suffix)]
    return path.parent.parent.name


def header_value(headers: dict[str, Any] | None, name: str) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def normalized_content_type(record: dict[str, Any]) -> str:
    content_type = header_value(record.get("headers", {}), "content-type")
    if content_type:
        return content_type.split(";", 1)[0].strip().lower()
    return record.get("source_content_type_bucket") or "unknown"


def route_key(record: dict[str, Any]) -> str:
    parsed = urlsplit(record.get("url") or "")
    return "|".join(
        [
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            (record.get("method") or "POST").upper(),
            normalized_content_type(record),
        ]
    )


def replayed(record: dict[str, Any]) -> bool:
    return record.get("replay_skipped") is None and bool(record.get("get_candidates"))


def compare_response(
    comparator: ResponseComparator,
    post_response: dict[str, Any] | None,
    get_response: dict[str, Any] | None,
) -> ComparisonResult | None:
    if not post_response or not get_response or get_response.get("failure"):
        return None
    try:
        return comparator.compare(post_response, get_response)
    except (KeyError, TypeError):
        return None


def equivalent_at(result: dict[str, Any] | None, threshold: float) -> bool:
    return bool(
        result
        and result["status_codes_match"]
        and result["structural"] >= threshold
        and not result["semantic_mismatch"]
    )


def class_at(converted_results: list[dict[str, Any] | None], no_query_results: list[dict[str, Any] | None], threshold: float) -> str:
    converted_equiv = any(equivalent_at(result, threshold) for result in converted_results)
    no_query_equiv = any(equivalent_at(result, threshold) for result in no_query_results)
    if converted_equiv and not no_query_equiv:
        return "A"
    if converted_equiv and no_query_equiv:
        return "B"
    if not converted_equiv and no_query_equiv:
        return "C"
    return "D"


def result_to_dict(result: ComparisonResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    data = asdict(result)
    return {key: round(value, 4) if isinstance(value, float) else value for key, value in data.items()}


def best_result_index(results: list[dict[str, Any] | None]) -> int:
    if not results:
        return 0
    return max(range(len(results)), key=lambda i: results[i]["structural"] if results[i] else -1.0)


def build_population(data_dir: Path, thresholds: list[float]) -> list[dict[str, Any]]:
    comparator = ResponseComparator(threshold=thresholds[0] if thresholds else 95.0)
    rows = []
    for replay_file in find_replay_files(data_dir):
        domain = domain_from_replay_file(replay_file)
        for record_index, record in enumerate(read_jsonl(replay_file)):
            if not replayed(record):
                continue
            candidates = record.get("get_candidates") or []
            post_response = record.get("response") or {}
            converted = []
            no_query = []
            for candidate in candidates:
                converted.append(result_to_dict(compare_response(comparator, post_response, candidate.get("get_response", {}))))
                no_query_response = (candidate.get("no_data_get") or {}).get("response", {})
                no_query.append(result_to_dict(compare_response(comparator, post_response, no_query_response)))

            converted_best = best_result_index(converted)
            no_query_best = best_result_index(no_query)
            best_candidate = candidates[converted_best] if candidates else {}
            best_no_query = candidates[no_query_best] if candidates else {}
            threshold_classes = {threshold_key(t): class_at(converted, no_query, t) for t in thresholds}
            main_class = threshold_classes.get(threshold_key(95.0)) or class_at(converted, no_query, 95.0)
            converted_result = converted[converted_best] if converted else None
            no_query_result = no_query[no_query_best] if no_query else None
            converted_url = best_candidate.get("converted_url") or ""
            no_query_url = ((best_no_query.get("no_data_get") or {}).get("url") if best_no_query else "") or ""
            rows.append(
                {
                    "domain": domain,
                    "source_file": str(replay_file.resolve()),
                    "line_number": record_index + 1,
                    "record_index": record_index,
                    "url": record.get("url") or "",
                    "route": route_key(record),
                    "content_type_bucket": record.get("source_content_type_bucket") or normalized_content_type(record),
                    "resource_type": record.get("resourceType") or "",
                    "baseline_class": main_class,
                    "threshold_classes": threshold_classes,
                    "converted_similarity": converted_result["structural"] if converted_result else 0.0,
                    "no_query_similarity": no_query_result["structural"] if no_query_result else 0.0,
                    "converted_result": converted_result,
                    "no_query_result": no_query_result,
                    "converted_status": converted_result["get_status"] if converted_result else None,
                    "no_query_status": no_query_result["get_status"] if no_query_result else None,
                    "post_status": post_response.get("status_code"),
                    "best_candidate_index": converted_best,
                    "converted_url": converted_url,
                    "no_query_url": no_query_url,
                }
            )
    return rows


def threshold_key(threshold: float) -> str:
    return f"{threshold:g}"


def finite_sample_size(population_size: int, confidence: float, margin: float) -> int:
    if population_size <= 0:
        return 0
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    n0 = (z * z * 0.25) / (margin * margin)
    finite = (population_size * n0) / (population_size + n0 - 1)
    return min(population_size, max(1, math.ceil(finite)))


def allocate_by_bucket(rows: list[dict[str, Any]], sample_size: int) -> dict[str, int]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[row["content_type_bucket"] or "unknown"].append(row)
    if sample_size >= len(rows):
        return {bucket: len(bucket_rows) for bucket, bucket_rows in buckets.items()}

    allocations = {}
    remainders = []
    for bucket, bucket_rows in buckets.items():
        exact = sample_size * (len(bucket_rows) / len(rows))
        base = min(len(bucket_rows), math.floor(exact))
        if base == 0 and bucket_rows:
            base = 1
        allocations[bucket] = base
        remainders.append((exact - math.floor(exact), bucket))

    while sum(allocations.values()) > sample_size:
        removable = sorted(
            (len(buckets[bucket]) - allocations[bucket], bucket)
            for bucket in allocations
            if allocations[bucket] > 1
        )
        if not removable:
            break
        _, bucket = removable[-1]
        allocations[bucket] -= 1

    for _, bucket in sorted(remainders, reverse=True):
        if sum(allocations.values()) >= sample_size:
            break
        if allocations[bucket] < len(buckets[bucket]):
            allocations[bucket] += 1
    return allocations


def select_sample(
    population: list[dict[str, Any]],
    classes: list[str],
    confidence: float,
    margin: float,
    seed: int,
) -> tuple[list[StudyItem], list[dict[str, Any]]]:
    rng = random.Random(seed)
    sample_items: list[StudyItem] = []
    summary_rows = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in population:
        grouped[row["baseline_class"]].append(row)

    for class_name in classes:
        rows = list(grouped.get(class_name, []))
        target = finite_sample_size(len(rows), confidence, margin)
        allocations = allocate_by_bucket(rows, target)
        chosen: list[dict[str, Any]] = []
        for bucket, bucket_count in sorted(allocations.items()):
            bucket_rows = [row for row in rows if (row["content_type_bucket"] or "unknown") == bucket]
            rng.shuffle(bucket_rows)
            for row in bucket_rows[:bucket_count]:
                row = dict(row)
                row["sample_bucket"] = bucket
                chosen.append(row)
        rng.shuffle(chosen)
        summary_rows.append(
            {
                "class": class_name,
                "class_name": BASELINE_CLASSES[class_name],
                "population": len(rows),
                "sample": len(chosen),
                "confidence": confidence,
                "margin": margin,
            }
        )
        for offset, row in enumerate(chosen, 1):
            item_id = f"{class_name}-{offset:04d}-{stable_id(row)}"
            sample_items.append(
                StudyItem(
                    item_id=item_id,
                    domain=row["domain"],
                    source_file=row["source_file"],
                    line_number=row["line_number"],
                    record_index=row["record_index"],
                    url=row["url"],
                    route=row["route"],
                    content_type_bucket=row["content_type_bucket"],
                    resource_type=row["resource_type"],
                    baseline_class=row["baseline_class"],
                    baseline_class_name=BASELINE_CLASSES[row["baseline_class"]],
                    threshold_classes=row["threshold_classes"],
                    converted_similarity=row["converted_similarity"],
                    no_query_similarity=row["no_query_similarity"],
                    converted_status=row["converted_status"],
                    no_query_status=row["no_query_status"],
                    post_status=row["post_status"],
                    best_candidate_index=row["best_candidate_index"],
                    converted_url=row["converted_url"],
                    no_query_url=row["no_query_url"],
                    sample_stratum=class_name,
                    sample_bucket=row.get("sample_bucket", row["content_type_bucket"] or "unknown"),
                )
            )
    return sample_items, summary_rows


def stable_id(row: dict[str, Any]) -> str:
    value = f"{row['source_file']}:{row['line_number']}:{row['url']}"
    total = 0
    for char in value:
        total = (total * 33 + ord(char)) % 16_777_216
    return f"{total:06x}"


def threshold_sensitivity(population: list[dict[str, Any]], thresholds: list[float]) -> list[dict[str, Any]]:
    rows = []
    eligible_sites = {row["domain"] for row in population}
    all_routes = {row["route"] for row in population}
    denominator = len(population)
    for threshold in thresholds:
        key = threshold_key(threshold)
        counts = Counter(row["threshold_classes"][key] for row in population)
        a_sites = {row["domain"] for row in population if row["threshold_classes"][key] == "A"}
        upper_sites = {row["domain"] for row in population if row["threshold_classes"][key] in {"A", "B"}}
        a_routes = {row["route"] for row in population if row["threshold_classes"][key] == "A"}
        upper_routes = {row["route"] for row in population if row["threshold_classes"][key] in {"A", "B"}}
        rows.append(
            {
                "threshold": threshold,
                "requests": denominator,
                "A": counts.get("A", 0),
                "B": counts.get("B", 0),
                "C": counts.get("C", 0),
                "D": counts.get("D", 0),
                "A_request_rate": counts.get("A", 0) / denominator if denominator else 0.0,
                "upper_request_rate": (counts.get("A", 0) + counts.get("B", 0)) / denominator if denominator else 0.0,
                "A_site_rate": len(a_sites) / len(eligible_sites) if eligible_sites else 0.0,
                "upper_site_rate": len(upper_sites) / len(eligible_sites) if eligible_sites else 0.0,
                "A_route_rate": len(a_routes) / len(all_routes) if all_routes else 0.0,
                "upper_route_rate": len(upper_routes) / len(all_routes) if all_routes else 0.0,
            }
        )
    return rows


def prepare_study(args: argparse.Namespace) -> None:
    if not args.verbose:
        logging.getLogger("comparator").setLevel(logging.ERROR)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    thresholds = sorted(args.thresholds)
    population = build_population(Path(args.data_dir), thresholds)
    items, sample_rows = select_sample(population, args.classes, args.confidence, args.margin, args.seed)
    study = {
        "created_at": now_iso(),
        "data_dir": str(Path(args.data_dir).resolve()),
        "confidence": args.confidence,
        "margin": args.margin,
        "seed": args.seed,
        "thresholds": thresholds,
        "classes": args.classes,
        "items": [asdict(item) for item in items],
    }
    write_json(out_dir / "sample.json", study)
    write_json(out_dir / "annotations.json", {})
    sensitivity = threshold_sensitivity(population, thresholds)
    write_json(
        out_dir / "population-summary.json",
        {
            "created_at": now_iso(),
            "replayed_records": len(population),
            "threshold_sensitivity": sensitivity,
            "sample_summary": sample_rows,
        },
    )
    write_table_outputs(
        out_dir,
        "validation-sample-summary",
        ["Class", "Class name", "Population", "Sample", "Confidence", "Margin"],
        [
            [
                row["class"],
                row["class_name"],
                row["population"],
                row["sample"],
                pct(row["confidence"]),
                pct(row["margin"]),
            ]
            for row in sample_rows
        ],
        latex=args.latex,
    )
    write_threshold_table(out_dir, sensitivity, latex=args.latex)
    print(f"Prepared {len(items)} validation items in {out_dir}")


def load_study(study_dir: Path) -> dict[str, Any]:
    return read_json(study_dir / "sample.json")


def load_annotations(study_dir: Path) -> dict[str, Any]:
    path = study_dir / "annotations.json"
    return read_json(path) if path.exists() else {}


def save_annotations(study_dir: Path, annotations: dict[str, Any]) -> None:
    write_json(study_dir / "annotations.json", annotations)


def next_unannotated(items: list[dict[str, Any]], annotations: dict[str, Any], after: str | None = None) -> str | None:
    start = 0
    if after:
        for index, item in enumerate(items):
            if item["item_id"] == after:
                start = index + 1
                break
    ordered = items[start:] + items[:start]
    for item in ordered:
        ann = annotations.get(item["item_id"], {})
        if not ann.get("completed"):
            return item["item_id"]
    return items[0]["item_id"] if items else None


def serve_study(args: argparse.Namespace) -> None:
    study_dir = Path(args.study_dir)
    if not (study_dir / "sample.json").exists():
        raise SystemExit(f"{study_dir}/sample.json does not exist. Run the prepare command first.")

    class Handler(ValidationHandler):
        pass

    Handler.study_dir = study_dir
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving validation UI at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


class ValidationHandler(BaseHTTPRequestHandler):
    study_dir: Path

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        if split.path == "/":
            self.send_html(render_dashboard(self.study_dir))
            return
        if split.path.startswith("/item/"):
            item_id = split.path.rsplit("/", 1)[-1]
            self.send_html(render_item(self.study_dir, item_id))
            return
        if split.path == "/report":
            generate_report(self.study_dir, latex=True)
            self.redirect("/")
            return
        if split.path == "/style.css":
            self.send_bytes(STYLE.encode("utf-8"), content_type="text/css; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        split = urlsplit(self.path)
        if split.path != "/annotate":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        data = {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}
        study = load_study(self.study_dir)
        annotations = load_annotations(self.study_dir)
        item_id = data.get("item_id", "")
        if item_id not in {item["item_id"] for item in study["items"]}:
            self.send_error(HTTPStatus.BAD_REQUEST, "Unknown item")
            return
        annotations[item_id] = normalize_annotation(data)
        save_annotations(self.study_dir, annotations)
        if data.get("action") == "save":
            self.redirect(f"/item/{item_id}")
            return
        target = next_unannotated(study["items"], annotations, after=item_id)
        self.redirect(f"/item/{target}" if target else "/")

    def send_html(self, html: str, status: int = 200) -> None:
        self.send_bytes(html.encode("utf-8"), status=status, content_type="text/html; charset=utf-8")

    def send_bytes(self, content: bytes, status: int = 200, content_type: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("location", location)
        self.end_headers()


def normalize_annotation(data: dict[str, str]) -> dict[str, Any]:
    converted = data.get("converted_equivalence", "uncertain")
    baseline = data.get("baseline_equivalence", "uncertain")
    return {
        "converted_equivalence": converted if converted in ANNOTATION_VALUES else "uncertain",
        "baseline_equivalence": baseline if baseline in ANNOTATION_VALUES else "uncertain",
        "notes": data.get("notes", "").strip(),
        "completed": data.get("completed") == "1",
        "updated_at": now_iso(),
    }


def render_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header class="topbar">
    <a href="/">Response Equivalence Validation</a>
    <nav><a href="/report">Generate reports</a></nav>
  </header>
  <main>{body}</main>
</body>
</html>"""


def render_dashboard(study_dir: Path) -> str:
    study = load_study(study_dir)
    annotations = load_annotations(study_dir)
    items = study["items"]
    completed = sum(1 for item in items if annotations.get(item["item_id"], {}).get("completed"))
    by_class = Counter(item["baseline_class"] for item in items)
    completed_by_class = Counter(
        item["baseline_class"]
        for item in items
        if annotations.get(item["item_id"], {}).get("completed")
    )
    target = next_unannotated(items, annotations)
    rows = "".join(
        f"<tr><td>{h(cls)}</td><td>{h(BASELINE_CLASSES[cls])}</td><td>{completed_by_class.get(cls, 0)} / {count}</td></tr>"
        for cls, count in sorted(by_class.items())
    )
    item_rows = "".join(render_dashboard_item(item, annotations.get(item["item_id"], {})) for item in items)
    body = f"""
<section class="toolbar">
  <div>
    <h1>Manual Validation Study</h1>
    <p>{completed} / {len(items)} completed · confidence {pct(study["confidence"])} · margin {pct(study["margin"])} · seed {study["seed"]}</p>
  </div>
  <div class="actions">
    {f'<a class="button primary" href="/item/{h(target)}">Continue annotation</a>' if target else ''}
    <a class="button" href="/report">Generate reports</a>
  </div>
</section>
<section class="summary-grid">
  <div><strong>Sample</strong><span>{len(items)}</span></div>
  <div><strong>Completed</strong><span>{completed}</span></div>
  <div><strong>Remaining</strong><span>{len(items) - completed}</span></div>
  <div><strong>Thresholds</strong><span>{", ".join(str(t) for t in study["thresholds"])}</span></div>
</section>
<section class="panel">
  <h2>Progress By Stratum</h2>
  <table><thead><tr><th>Class</th><th>Name</th><th>Completed</th></tr></thead><tbody>{rows}</tbody></table>
</section>
<section class="panel">
  <h2>Sample Items</h2>
  <table class="listing">
    <thead><tr><th>Status</th><th>Class</th><th>Domain</th><th>Content</th><th>Similarity</th><th>URL</th></tr></thead>
    <tbody>{item_rows}</tbody>
  </table>
</section>"""
    return render_page("Manual Validation Study", body)


def render_dashboard_item(item: dict[str, Any], annotation: dict[str, Any]) -> str:
    done = annotation.get("completed")
    status = "done" if done else "open"
    return f"""<tr>
  <td><span class="pill {status}">{status}</span></td>
  <td>{h(item["baseline_class"])}</td>
  <td>{h(item["domain"])}</td>
  <td>{h(item["content_type_bucket"])}</td>
  <td>{item["converted_similarity"]:.1f} / {item["no_query_similarity"]:.1f}</td>
  <td><a href="/item/{h(item["item_id"])}">{h(shorten(item["url"], 120))}</a></td>
</tr>"""


def render_item(study_dir: Path, item_id: str) -> str:
    study = load_study(study_dir)
    annotations = load_annotations(study_dir)
    item = next((candidate for candidate in study["items"] if candidate["item_id"] == item_id), None)
    if item is None:
        return render_page("Not Found", "<section class='panel'><h1>Item not found</h1></section>")
    index = study["items"].index(item)
    previous_item = study["items"][index - 1] if index > 0 else study["items"][-1]
    next_item = study["items"][index + 1] if index < len(study["items"]) - 1 else study["items"][0]
    annotation = annotations.get(item_id, {})
    detail = load_item_detail(item)
    converted_diff = render_diff_block(detail["post_body"], detail["converted_body"], "POST", "Converted GET")
    baseline_diff = render_diff_block(detail["post_body"], detail["no_query_body"], "POST", "No-query GET") if detail["no_query_response"] else ""
    threshold_cells = " ".join(
        f"<span class='pill neutral'>{h(key)}: {h(value)}</span>"
        for key, value in item["threshold_classes"].items()
    )
    body = f"""
<section class="toolbar sticky-tools">
  <div>
    <h1>{h(item["domain"])}</h1>
    <p>Item {index + 1} / {len(study["items"])} · Class {h(item["baseline_class"])}: {h(item["baseline_class_name"])}</p>
  </div>
  <div class="actions">
    <a class="button" href="/item/{h(previous_item["item_id"])}">Previous</a>
    <a class="button" href="/item/{h(next_item["item_id"])}">Next</a>
    <a class="button" href="/">Dashboard</a>
  </div>
</section>
<section class="score-row">
  <div><strong>POST status</strong><span>{h(item["post_status"])}</span></div>
  <div><strong>GET status</strong><span>{h(item["converted_status"])}</span></div>
  <div><strong>No-query status</strong><span>{h(item["no_query_status"])}</span></div>
  <div><strong>Structural similarity</strong><span>{item["converted_similarity"]:.1f}</span></div>
  <div><strong>No-query similarity</strong><span>{item["no_query_similarity"]:.1f}</span></div>
</section>
<section class="panel">
  <h2>Threshold Classifications</h2>
  <div class="pill-row">{threshold_cells}</div>
</section>
<section class="panel focus-panel">
    <h2>What To Check</h2>
    <ol>
        <li>Use the status codes and the body diff below to decide whether the converted GET preserves the same application-level outcome as the POST.</li>
        <li>Check whether the no-query GET also looks equivalent. If yes, the item is probably a baseline match rather than strong evidence.</li>
        <li>Ignore headers and parameter mapping unless the body diff is ambiguous. The UI only shows body-level evidence by default.</li>
    </ol>
</section>
<form class="annotation" method="post" action="/annotate">
  <input type="hidden" name="item_id" value="{h(item_id)}">
  <input type="hidden" name="completed" value="1">
  <section class="annotation-grid">
    {radio_group("converted_equivalence", "POST vs converted GET", ANNOTATION_VALUES, annotation.get("converted_equivalence", ""), "Does the converted GET preserve the same application-level outcome as the POST?")}
    {radio_group("baseline_equivalence", "POST vs no-query GET", ANNOTATION_VALUES, annotation.get("baseline_equivalence", ""), "Does the no-query control also look equivalent to the POST?")}
  </section>
  <section class="annotation-extra">
    <label class="notes">Notes
      <textarea name="notes" rows="3">{h(annotation.get("notes", ""))}</textarea>
    </label>
  </section>
  <div class="actions">
    <button class="primary" type="submit" name="action" value="next">Save and next</button>
    <button type="submit" name="action" value="save">Save</button>
  </div>
</form>
<section class="panel">
    <h2>Compared URLs</h2>
  <dl class="kv">
    <dt>Original POST</dt><dd><code>{h(item["url"])}</code></dd>
    <dt>Converted GET</dt><dd><code>{h(item["converted_url"])}</code></dd>
    <dt>No-query GET</dt><dd><code>{h(item["no_query_url"] or "n/a")}</code></dd>
  </dl>
</section>
<details open>
  <summary>POST vs converted GET response diff</summary>
    <div class="diff-block">{converted_diff}</div>
</details>
<details>
  <summary>POST vs no-query GET response diff</summary>
    <div class="diff-block">{baseline_diff or "<p>No no-query response available.</p>"}</div>
</details>
<details>
    <summary>Response body previews</summary>
    <section class="preview-grid">
        {response_body_panel("POST", detail["post_response"], detail["post_body"])}
        {response_body_panel("Converted GET", detail["converted_response"], detail["converted_body"])}
        {response_body_panel("No-query GET", detail["no_query_response"], detail["no_query_body"])}
    </section>
</details>
<script>
document.addEventListener("keydown", (event) => {{
  if (event.target && ["TEXTAREA", "INPUT", "SELECT"].includes(event.target.tagName)) return;
  const mapping = {{
    "1": ["converted_equivalence", "equivalent"],
    "2": ["converted_equivalence", "different"],
    "3": ["converted_equivalence", "uncertain"],
    "q": ["baseline_equivalence", "equivalent"],
    "w": ["baseline_equivalence", "different"],
        "e": ["baseline_equivalence", "uncertain"]
  }};
  const hit = mapping[event.key];
  if (!hit) return;
  const input = document.querySelector(`input[name="${{hit[0]}}"][value="${{hit[1]}}"]`);
  if (input) input.checked = true;
}});
</script>"""
    return render_page(f"{item['domain']} validation", body)


def radio_group(name: str, title: str, values: tuple[str, ...], selected: str, help_text: str) -> str:
    buttons = "".join(
        f"""<label class="radio-card">
  <input type="radio" name="{h(name)}" value="{h(value)}" {'checked' if selected == value else ''}>
  <span>{h(value.replace("_", " "))}</span>
</label>"""
        for value in values
    )
    return f"<fieldset><legend>{h(title)}</legend><p>{h(help_text)}</p><div class='radio-row'>{buttons}</div></fieldset>"


def options(values: tuple[str, ...], selected: str) -> str:
    rendered = []
    for value in values:
        label = value.replace("_", " ") if value else "none"
        rendered.append(f"<option value='{h(value)}' {'selected' if value == selected else ''}>{h(label)}</option>")
    return "".join(rendered)


def load_item_detail(item: dict[str, Any]) -> dict[str, Any]:
    record = read_jsonl_line(Path(item["source_file"]), item["line_number"])
    candidates = record.get("get_candidates") or []
    candidate = candidates[item["best_candidate_index"]] if candidates else {}
    no_query = candidate.get("no_data_get") if isinstance(candidate.get("no_data_get"), dict) else {}
    post_response = record.get("response") or {}
    converted_response = candidate.get("get_response") or {}
    no_query_response = no_query.get("response") if isinstance(no_query.get("response"), dict) else {}
    post_body = pretty_response_body(post_response)
    converted_body = pretty_response_body(converted_response)
    no_query_body = pretty_response_body(no_query_response)
    return {
        "record": record,
        "post_response": post_response,
        "converted_response": converted_response,
        "no_query_response": no_query_response,
        "post_body": post_body,
        "converted_body": converted_body,
        "no_query_body": no_query_body,
        "converted_result": item_result(record, candidate.get("get_response", {})),
        "no_query_result": item_result(record, no_query_response),
    }


def item_result(record: dict[str, Any], response: dict[str, Any]) -> dict[str, Any] | None:
    return result_to_dict(compare_response(ResponseComparator(), record.get("response") or {}, response))


def response_body_panel(title: str, response: dict[str, Any], body: str) -> str:
        if not response:
                return f"<section class='panel'><h3>{h(title)}</h3><p class='muted'>Not available.</p></section>"

        headers = response.get("headers", {})
        status = response.get("status_code", "n/a")
        body_bytes = len(response.get("body", ""))
        return f"""<section class="panel request-panel">
    <h3>{h(title)}</h3>
    <p>Status <strong>{h(status)}</strong> · Content-Type {h(header_value(headers, "content-type") or "n/a")} · Body bytes {body_bytes}</p>
    <pre>{h(body_preview(body))}</pre>
</section>"""


def extract_request_params(record: dict[str, Any]) -> dict[str, list[str]]:
    params: dict[str, list[str]] = {}
    add_pairs(params, parse_qsl(urlsplit(record.get("url", "")).query, keep_blank_values=True), "query")
    body = record.get("postData") or ""
    bucket = record.get("source_content_type_bucket") or normalized_content_type(record)
    if bucket == "json" or "json" in bucket:
        try:
            parsed = json.loads(body)
            for key, value in flatten_json(parsed).items():
                add_pairs(params, [(key, value)], "body")
        except (TypeError, json.JSONDecodeError):
            pass
    elif bucket == "form-urlencoded" or "urlencoded" in bucket:
        add_pairs(params, parse_qsl(body, keep_blank_values=True), "body")
    elif bucket == "multipart":
        for name in re.findall(r'name="([^"]+)"\r?\n\r?\n(.*?)(?=\r?\n--)', body, re.DOTALL):
            add_pairs(params, [(name[0], name[1].strip())], "body")
    return params


def extract_get_params(url: str) -> dict[str, list[str]]:
    params: dict[str, list[str]] = {}
    add_pairs(params, parse_qsl(urlsplit(url).query, keep_blank_values=True), "query")
    return params


def flatten_json(value: Any, prefix: str = "") -> dict[str, str]:
    rows = {}
    if isinstance(value, dict):
        for key, child in value.items():
            full = f"{prefix}.{key}" if prefix else str(key)
            rows.update(flatten_json(child, full))
    elif isinstance(value, list):
        for index, child in enumerate(value[:20]):
            full = f"{prefix}[{index}]" if prefix else f"[{index}]"
            rows.update(flatten_json(child, full))
    else:
        rows[prefix or "$"] = json.dumps(value, ensure_ascii=False)
    return rows


def add_pairs(target: dict[str, list[str]], pairs: list[tuple[str, str]], source: str) -> None:
    for key, value in pairs:
        target.setdefault(key, []).append(f"{source}: {value}")


def compare_params(left: dict[str, list[str]], right: dict[str, list[str]]) -> list[dict[str, str]]:
    rows = []
    for key in sorted(set(left) | set(right)):
        left_value = "\n".join(left.get(key, []))
        right_value = "\n".join(right.get(key, []))
        rows.append({"key": key, "left": left_value, "right": right_value, "status": diff_status(left_value, right_value)})
    return rows


def diff_status(left: str, right: str) -> str:
    if left and right and left == right:
        return "same"
    if left and right:
        return "changed"
    if left:
        return "removed"
    return "added"


def pretty_response_body(response: dict[str, Any]) -> str:
    body = response.get("body")
    text = body if isinstance(body, str) else json.dumps(body, sort_keys=True, ensure_ascii=False)
    content_type = header_value(response.get("headers", {}), "content-type").lower()
    if "json" in content_type or text.strip().startswith(("{", "[")):
        try:
            return json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False)
        except (TypeError, json.JSONDecodeError):
            return text
    return text


def body_preview(body: str, max_lines: int = 80, max_chars: int = 12000) -> str:
    lines = body.splitlines()
    preview = "\n".join(lines[:max_lines]) if lines else body[:max_chars]
    if len(preview) > max_chars:
        preview = preview[:max_chars]
    truncated = len(lines) > max_lines or len(body) > len(preview)
    return f"{preview}\n\n... truncated ..." if truncated else preview


def render_diff_block(left: str, right: str, left_label: str, right_label: str) -> str:
    if left == right:
        return "<p class='muted'>Bodies are identical after pretty-printing.</p>"
    raw_lines = list(
        unified_diff(
            diff_lines(left),
            diff_lines(right),
            fromfile=left_label,
            tofile=right_label,
            lineterm="",
            n=3,
        )
    )
    filtered = [line for line in raw_lines if not line.startswith(("---", "+++", "@@"))]
    if not filtered:
        filtered = raw_lines
    excerpt = trim_diff_lines(filtered, limit=240)
    rendered = []
    for line in excerpt:
        css_class = "diff-context"
        if line.startswith("+") and not line.startswith("+++"):
            css_class = "diff-add"
        elif line.startswith("-") and not line.startswith("---"):
            css_class = "diff-remove"
        rendered.append(f"<div class='{css_class}'>{h(line or ' ')}</div>")
    if len(excerpt) < len(filtered):
        rendered.append("<div class='diff-context'>... diff truncated ...</div>")
    similarity = SequenceMatcher(a=left, b=right).ratio()
    return f"<div class='diff-meta'>Changed-body similarity: {similarity:.3f}</div><div class='diff-lines'>{''.join(rendered)}</div>"


def trim_diff_lines(lines: list[str], limit: int) -> list[str]:
    if len(lines) <= limit:
        return lines
    head = max(20, limit // 2)
    tail = max(20, limit - head - 1)
    return lines[:head] + ["..."] + lines[-tail:]


def diff_lines(text: str, width: int = 220) -> list[str]:
    lines = []
    for line in text.splitlines() or [""]:
        if len(line) <= width:
            lines.append(line)
        else:
            for start in range(0, len(line), width):
                lines.append(line[start:start + width])
    return lines or [""]


def generate_report(study_dir: Path, latex: bool = False) -> None:
    study = load_study(study_dir)
    annotations = load_annotations(study_dir)
    population_summary = read_json(study_dir / "population-summary.json")
    rows = []
    for item in study["items"]:
        ann = annotations.get(item["item_id"], {})
        if not ann.get("completed"):
            continue
        rows.append({**item, **ann})

    write_annotations_csv(study_dir / "annotations.csv", rows)
    manual_rows = manual_validation_rows(rows)
    write_table_outputs(
        study_dir,
        "manual-validation-results",
        ["Class", "Manual endpoint", "Annotated", "Yes", "No", "Uncertain", "Yes rate", "Wilson 95% CI"],
        manual_rows,
        latex=latex,
    )
    write_threshold_table(study_dir, population_summary.get("threshold_sensitivity", []), latex=latex)
    summary = {
        "generated_at": now_iso(),
        "completed_annotations": len(rows),
        "sample_size": len(study["items"]),
        "class_A_precision": class_precision_summary(rows, "A"),
        "class_D_false_negative_rate": class_positive_rate_summary(rows, "D"),
        "manual_validation_rows": manual_rows,
        "threshold_sensitivity": population_summary.get("threshold_sensitivity", []),
    }
    write_json(study_dir / "validation-summary.json", summary)
    write_report_markdown(study_dir / "validation-report.md", summary, study)
    print(f"Wrote validation reports to {study_dir}")


def manual_validation_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    table = []
    for class_name in ("A", "B", "D"):
        class_rows = [row for row in rows if row["baseline_class"] == class_name]
        for endpoint, label in (
            ("converted_equivalence", "POST vs converted GET"),
            ("baseline_equivalence", "POST vs no-query GET"),
        ):
            counts = Counter(row.get(endpoint, "uncertain") for row in class_rows)
            denominator = counts["equivalent"] + counts["different"]
            rate = counts["equivalent"] / denominator if denominator else None
            ci = wilson_ci(counts["equivalent"], denominator) if denominator else None
            table.append(
                [
                    class_name,
                    label,
                    len(class_rows),
                    counts["equivalent"],
                    counts["different"],
                    counts["uncertain"],
                    pct(rate),
                    f"[{pct(ci[0])}, {pct(ci[1])}]" if ci else "n/a",
                ]
            )
    return table


def class_precision_summary(rows: list[dict[str, Any]], class_name: str) -> dict[str, Any]:
    class_rows = [row for row in rows if row["baseline_class"] == class_name]
    yes = sum(1 for row in class_rows if row.get("converted_equivalence") == "equivalent")
    no = sum(1 for row in class_rows if row.get("converted_equivalence") == "different")
    denominator = yes + no
    ci = wilson_ci(yes, denominator) if denominator else (0.0, 0.0)
    return {
        "annotated": len(class_rows),
        "usable": denominator,
        "manual_equivalent": yes,
        "manual_different": no,
        "estimate": yes / denominator if denominator else None,
        "wilson_95_ci": ci,
    }


def class_positive_rate_summary(rows: list[dict[str, Any]], class_name: str) -> dict[str, Any]:
    return class_precision_summary(rows, class_name)


def write_annotations_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "item_id",
        "domain",
        "baseline_class",
        "content_type_bucket",
        "url",
        "converted_url",
        "no_query_url",
        "converted_similarity",
        "no_query_similarity",
        "converted_equivalence",
        "baseline_equivalence",
        "notes",
        "updated_at",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def write_report_markdown(path: Path, summary: dict[str, Any], study: dict[str, Any]) -> None:
    precision = summary["class_A_precision"]
    fn = summary["class_D_false_negative_rate"]
    lines = [
        "# Response Equivalence Manual Validation",
        "",
        f"Generated at `{summary['generated_at']}`.",
        "",
        f"The validation sample contains {study_sample_size(study)} records selected with stratified finite-population sampling at {pct(study['confidence'])} confidence and {pct(study['margin'])} margin of error.",
        "",
        "## Key Estimates",
        "",
        f"- Class-A precision: {format_estimate(precision)}.",
        f"- Class-D manual-equivalence rate, an estimate of potential false negatives in the sampled D stratum: {format_estimate(fn)}.",
        "",
        "## Caveats",
        "",
        "- Uncertain annotations are excluded from the point estimates and reported separately in the tables.",
        "- This study validates the response-equivalence criterion, not real-world exploitability or state mutation.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def study_sample_size(study: dict[str, Any]) -> int:
    return len(study.get("items", []))


def format_estimate(summary: dict[str, Any]) -> str:
    if summary["estimate"] is None:
        return "n/a"
    low, high = summary["wilson_95_ci"]
    return f"{pct(summary['estimate'])} ({summary['manual_equivalent']}/{summary['usable']}, 95% CI [{pct(low)}, {pct(high)}])"


def write_threshold_table(out_dir: Path, rows: list[dict[str, Any]], latex: bool = False) -> None:
    table = [
        [
            row["threshold"],
            row["A"],
            row["B"],
            row["C"],
            row["D"],
            pct(row["A_request_rate"]),
            pct(row["upper_request_rate"]),
            pct(row["A_site_rate"]),
            pct(row["A_route_rate"]),
        ]
        for row in rows
    ]
    write_table_outputs(
        out_dir,
        "threshold-sensitivity",
        ["Threshold", "A", "B", "C", "D", "A request", "A+B request", "A site", "A route"],
        table,
        latex=latex,
    )


def write_table_outputs(out_dir: Path, name: str, headers: list[str], rows: list[list[Any]], latex: bool = False) -> None:
    (out_dir / f"{name}.txt").write_text(tabulate(rows, headers=headers, tablefmt="github") + "\n", encoding="utf-8")
    if latex:
        (out_dir / f"{name}.tex").write_text(tabulate(rows, headers=headers, tablefmt="latex") + "\n", encoding="utf-8")


def wilson_ci(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def h(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def shorten(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


STYLE = """
* { box-sizing: border-box; }
body { margin: 0; color: #1f2933; background: #f5f7fa; font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
a { color: #075985; text-decoration: none; }
a:hover { text-decoration: underline; }
.topbar { position: sticky; top: 0; z-index: 20; display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 12px 20px; background: #16202a; color: white; }
.topbar a { color: white; font-weight: 700; }
main { padding: 18px; }
h1, h2, h3, p { margin: 0 0 8px; }
table { width: 100%; border-collapse: collapse; background: white; }
th, td { padding: 8px 10px; border-bottom: 1px solid #e1e7ef; text-align: left; vertical-align: top; }
th { background: #edf2f7; color: #475569; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }
pre { max-height: 520px; overflow: auto; margin: 0; padding: 10px; border: 1px solid #d8e0ea; border-radius: 4px; background: #f8fafc; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
code { overflow-wrap: anywhere; }
button, .button, select, textarea { border: 1px solid #b8c4d2; border-radius: 4px; background: white; color: #1f2933; font: inherit; }
button, .button { display: inline-flex; min-height: 34px; align-items: center; justify-content: center; padding: 7px 11px; cursor: pointer; background: #e7edf4; }
button.primary, .button.primary { background: #0f766e; color: white; border-color: #0f766e; font-weight: 700; }
select, textarea { width: 100%; padding: 7px 9px; }
fieldset { margin: 0; border: 1px solid #d8e0ea; border-radius: 4px; background: white; }
legend { font-weight: 700; }
.toolbar { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.sticky-tools { position: sticky; top: 48px; z-index: 10; padding: 10px; background: rgba(245, 247, 250, 0.96); border-bottom: 1px solid #d8e0ea; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.summary-grid, .score-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-bottom: 16px; }
.summary-grid div, .score-row div, .panel { background: white; border: 1px solid #d8e0ea; border-radius: 4px; padding: 10px; }
.summary-grid strong, .score-row strong { display: block; color: #64748b; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }
.summary-grid span, .score-row span { display: block; margin-top: 4px; font-size: 18px; font-weight: 700; }
.panel { margin-bottom: 16px; }
.listing td:last-child { overflow-wrap: anywhere; }
.pill { display: inline-flex; align-items: center; justify-content: center; min-width: 34px; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 700; background: #e6edf3; color: #334155; }
.pill.done { background: #bbf7d0; color: #14532d; }
.pill.open { background: #fee2e2; color: #7f1d1d; }
.pill.neutral { margin: 0 5px 5px 0; background: #e0f2fe; color: #075985; }
.annotation { margin-bottom: 16px; }
.annotation-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; margin-bottom: 10px; }
.annotation-extra { display: grid; grid-template-columns: minmax(320px, 1fr); gap: 10px; align-items: end; margin-bottom: 10px; }
.notes textarea { min-height: 70px; }
.radio-row { display: flex; gap: 8px; flex-wrap: wrap; }
.radio-card { display: inline-flex; align-items: center; gap: 5px; padding: 7px 9px; border: 1px solid #d8e0ea; border-radius: 4px; background: #f8fafc; cursor: pointer; }
.preview-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; align-items: start; }
.request-panel { min-width: 0; }
.kv { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 6px 10px; margin: 0; }
.kv dt { color: #64748b; font-weight: 700; }
.kv dd { margin: 0; min-width: 0; }
.focus-panel ol { margin: 0; padding-left: 18px; }
.diff-block { overflow: auto; background: white; border: 1px solid #d8e0ea; border-radius: 4px; padding: 10px; }
.diff-meta { margin-bottom: 8px; color: #64748b; font-size: 12px; text-transform: uppercase; }
.diff-lines { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
.diff-context, .diff-add, .diff-remove { padding: 2px 8px; border-radius: 4px; }
.diff-context { color: #475569; }
.diff-add { background: #dcfce7; color: #14532d; }
.diff-remove { background: #fee2e2; color: #7f1d1d; }
.changed { background: #fff7ed; }
.removed { background: #fef2f2; }
.added { background: #f0fdf4; }
.muted { color: #64748b; }
@media (max-width: 1100px) {
    .preview-grid { grid-template-columns: 1fr; }
  .annotation-extra { grid-template-columns: 1fr; }
}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare, serve, and report a manual response-equivalence validation study.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Select a stratified validation sample.")
    prepare.add_argument("data_dir", help="Batch/session directory or replay JSONL file.")
    prepare.add_argument("--out-dir", default="analysis/response-validation")
    prepare.add_argument("--confidence", type=float, default=0.90)
    prepare.add_argument("--margin", type=float, default=0.15)
    prepare.add_argument("--seed", type=int, default=1337)
    prepare.add_argument("--thresholds", nargs="+", type=float, default=[90.0, 95.0, 98.0])
    prepare.add_argument("--classes", nargs="+", choices=sorted(BASELINE_CLASSES), default=["A", "D"])
    prepare.add_argument("--latex", action="store_true")
    prepare.add_argument("--verbose", action="store_true")
    prepare.set_defaults(func=prepare_study)

    serve = subparsers.add_parser("serve", help="Serve the local annotation web UI.")
    serve.add_argument("--study-dir", default="analysis/response-validation")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=serve_study)

    report = subparsers.add_parser("report", help="Generate tables and paper-ready report files from annotations.")
    report.add_argument("--study-dir", default="analysis/response-validation")
    report.add_argument("--latex", action="store_true")
    report.set_defaults(func=lambda args: generate_report(Path(args.study_dir), latex=args.latex))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
