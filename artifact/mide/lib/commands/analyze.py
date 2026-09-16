import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from lib.config import load_config
from lib.logging_setup import setup_logging, get_logger
from lib.comparator import ResponseComparator
from lib.analysis import (
    route_acceptance,
    source_equivalence,
    content_type_equivalence,
    final_verdict,
)
from lib.output import OutputWriter


def read_replay_records(filepath: str, logger) -> List[Dict]:
    """Read replay records with partial-trailing-record tolerance.

    Analyze-side reader (not the writer-side _read_jsonl). A partial trailing
    line is silently skipped via logger.debug — distinguishes the "live
    mid-write reader" idiom from the writer-side reader which uses
    logger.warning.

    Modern replay files are newline-delimited JSON. Some historical artifacts
    contain pretty-printed JSON objects concatenated back-to-back; tolerate
    those too so re-analysis does not silently produce an empty report.
    """
    records: List[Dict] = []
    decode_errors = 0
    with open(filepath, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                decode_errors += 1
                logger.debug(
                    "skipping unparseable line %d in %s: %s",
                    line_num, filepath, e,
                )
    if records or decode_errors == 0:
        return records

    return _read_concatenated_json_records(filepath, logger)


def _read_concatenated_json_records(filepath: str, logger) -> List[Dict]:
    """Read concatenated JSON objects that are not line-delimited."""
    records: List[Dict] = []
    decoder = json.JSONDecoder()
    with open(filepath, "r", encoding="utf-8") as f:
        payload = f.read()

    index = 0
    length = len(payload)
    while index < length:
        while index < length and payload[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            obj, next_index = decoder.raw_decode(payload, index)
        except json.JSONDecodeError as e:
            logger.debug(
                "skipping trailing/unparseable JSON record at byte %d in %s: %s",
                index, filepath, e,
            )
            break
        if isinstance(obj, dict):
            records.append(obj)
        else:
            logger.debug(
                "skipping non-object JSON record at byte %d in %s",
                index, filepath,
            )
        index = next_index
    return records


def _build_aggregate(
    records: List[Dict],
    route_results: List[Dict],
    source_results: List[Dict],
    no_data_results: List[Dict],
    cte_agg: Dict,
) -> Dict:
    """Assemble the analysis.aggregate block."""
    route_agg = route_acceptance.aggregate(route_results)
    source_agg = source_equivalence.aggregate(source_results)
    no_data_agg = source_equivalence.aggregate_no_data(no_data_results)

    # Lossy-conversion exclusion count (records that would otherwise be
    # included in CT-equivalence aggregate).
    lossy_excluded = sum(
        1 for r in records
        if r.get("lossy_conversion") and not r.get("replay_skipped")
    )

    skipped_counts = Counter(
        r.get("replay_skipped") for r in records if r.get("replay_skipped")
    )
    replay_skipped = {
        "unsupported_content_type": int(skipped_counts.get("unsupported_content_type", 0)),
        "malformed_body": int(skipped_counts.get("malformed_body", 0)),
        "depth_truncation": int(skipped_counts.get("depth_truncation", 0)),
    }

    return {
        "total_records": len(records),
        "route_accepted": route_agg["accepted"],
        "route_rejected": route_agg["rejected"],
        "source_equivalent": source_agg["equivalent"],
        "source_not_equivalent": source_agg["not_equivalent"],
        "without_converted_data_equivalent": no_data_agg["equivalent"],
        "without_converted_data_not_equivalent": no_data_agg["not_equivalent"],
        "without_converted_data_not_available": no_data_agg["not_available"],
        "by_source_content_type": cte_agg,
        "lossy_conversion_excluded_from_ct_equivalence": lossy_excluded,
        "replay_skipped": replay_skipped,
    }


def analyze_command(args) -> None:
    """Run offline analysis on a session's <domain>-replay-records.jsonl."""
    # CLI overrides — live values feed applied_thresholds.
    cli_overrides: Dict = {}
    if getattr(args, "threshold", None) is not None:
        cli_overrides.setdefault("detection", {})["similarity_threshold"] = args.threshold
    if getattr(args, "array_style", None) is not None:
        cli_overrides.setdefault("detection", {})["array_style"] = args.array_style

    config = load_config(
        config_path=getattr(args, "config", None),
        cli_overrides=cli_overrides,
    )

    # Validate session_dir
    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        print(f"Error: Session directory not found: {session_dir}", file=sys.stderr)
        sys.exit(1)

    # Discover the replay-records.jsonl by domain prefix
    candidates = list(session_dir.glob("*-replay-records.jsonl"))
    if not candidates:
        print(
            f"Error: No <domain>-replay-records.jsonl found in {session_dir}",
            file=sys.stderr,
        )
        sys.exit(1)
    replay_file = candidates[0]
    domain = replay_file.name[: -len("-replay-records.jsonl")]

    # Setup logging
    verbosity = "quiet" if getattr(args, "quiet", False) else (
        "verbose" if getattr(args, "verbose", False) else "normal"
    )
    setup_logging(domain, verbosity, log_dir=str(session_dir))
    logger = get_logger("analyze")

    # Read records (partial-trailing-line tolerant)
    records = read_replay_records(str(replay_file), logger)

    # Per-record dispatch to all four analysis modules
    comparator = ResponseComparator(threshold=config.similarity_threshold)
    per_record: List[Dict] = []
    route_results: List[Dict] = []
    source_results: List[Dict] = []
    no_data_results: List[Dict] = []

    for r in records:
        ra = route_acceptance.evaluate_record(r)
        se = source_equivalence.evaluate_record(r, comparator)
        nde = source_equivalence.evaluate_no_data_record(r, comparator)
        verdict = final_verdict.verdict(r, ra, se)
        no_data_verdict = nde["verdict"] == "equivalent"
        per_record.append({
            "url": r.get("url"),
            "source_content_type_bucket": r.get("source_content_type_bucket"),
            "lossy_conversion": r.get("lossy_conversion", False),
            "route_acceptance": ra,
            "source_equivalence": se,
            "no_data_source_equivalence": nde,
            "is_interchangeable": verdict,
            "is_interchangeable_without_converted_data": no_data_verdict,
        })
        route_results.append(ra)
        source_results.append(se)
        no_data_results.append(nde)

    # Per-bucket source-equivalence rate
    cte_agg = content_type_equivalence.aggregate(source_results, records)

    analysis = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session": session_dir.name,
            "replay_records_consumed": len(records),
            "applied_thresholds": {
                "similarity_threshold": config.similarity_threshold,
                "array_style_default": config.array_style,
            },
        },
        "per_record": per_record,
        "aggregate": _build_aggregate(
            records, route_results, source_results, no_data_results, cte_agg,
        ),
    }

    # Atomic write via OutputWriter (tmp-then-os.replace). We construct the
    # writer and override session_dir to the caller's path so writes target
    # the existing session — not a fresh timestamped subdir.
    output_base = str(session_dir.parent.parent) if session_dir.parent.parent else str(session_dir)
    writer = OutputWriter(output_base_dir=output_base, domain=domain)
    writer.session_dir = session_dir
    writer.write_analysis(domain, analysis)

    # Console summary (skip in --quiet)
    if not getattr(args, "quiet", False):
        print("\n" + "=" * 60)
        print("ANALYSIS SUMMARY")
        print("=" * 60)
        print(f"Session:              {session_dir.name}")
        print(f"Records analyzed:     {len(records)}")
        print(f"Route accepted:       {analysis['aggregate']['route_accepted']}")
        print(f"Source equivalent:    {analysis['aggregate']['source_equivalent']}")
        print(f"Output: {session_dir}/{domain}-analysis.json")
        print("=" * 60)


def register(subparsers):
    parser = subparsers.add_parser(
        "analyze",
        help="Run offline analysis on a session's replay-records.jsonl",
    )
    parser.add_argument(
        "session_dir",
        help="Session directory (contains <domain>-replay-records.jsonl)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        help="Override similarity threshold %% (default: 95.0)",
    )
    parser.add_argument(
        "--array-style",
        choices=["php", "numbered", "repeated"],
        help="Override default array style for non-array-bearing bodies",
    )
    parser.add_argument("--config", help="Path to custom config YAML file")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    parser.add_argument("--verbose", action="store_true", help="Debug output")
    parser.set_defaults(func=analyze_command)
