import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

from lib.commands.analyze import read_replay_records
from lib.config import load_config
from lib.logging_setup import get_logger, setup_logging
from lib.replay import ReplayEngine


def _iter_replay_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.name.endswith("-replay-records.jsonl"):
            yield path
        return
    yield from sorted(path.rglob("*-replay-records.jsonl"))


def _write_jsonl_atomic(path: Path, records: List[Dict[str, Any]]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _response_has_ip_address(response: Any) -> bool:
    return isinstance(response, dict) and bool(response.get("ip_address"))


def _candidate_needs_backfill(candidate: Dict[str, Any], force: bool) -> bool:
    if force:
        return True
    no_data_get = candidate.get("no_data_get")
    if not isinstance(no_data_get, dict):
        return True
    return not _response_has_ip_address(no_data_get.get("response"))


def _backfill_file(
    replay_file: Path,
    engine: ReplayEngine,
    logger,
    *,
    force: bool,
    dry_run: bool,
) -> Dict[str, int]:
    records = read_replay_records(str(replay_file), logger)
    added = 0
    existing = 0
    skipped = 0
    failed_records = 0

    for record in records:
        candidates = record.get("get_candidates")
        if record.get("replay_skipped") or not isinstance(candidates, list):
            skipped += 1
            continue

        for candidate in candidates:
            if not isinstance(candidate, dict):
                skipped += 1
                continue
            if not _candidate_needs_backfill(candidate, force):
                existing += 1
                continue

            converted_url = candidate.get("converted_url") or record.get("url")
            if not converted_url:
                skipped += 1
                continue

            no_data_url = engine._no_data_url(converted_url)
            if dry_run:
                added += 1
                continue

            headers = candidate.get("headers", {})
            response = engine._send_get_with_retry(
                lambda url=no_data_url, headers=headers: engine._send_get_url(
                    url, headers
                ),
                record.get("url", no_data_url),
            )
            candidate["no_data_get"] = {
                "url": no_data_url,
                "response": response,
            }
            added += 1
            if response.get("failure") is not None:
                failed_records += 1
            if engine.config.rate_limit_delay > 0:
                time.sleep(engine.config.rate_limit_delay)

    if added and not dry_run:
        _write_jsonl_atomic(replay_file, records)

    return {
        "records": len(records),
        "added": added,
        "existing": existing,
        "skipped": skipped,
        "failures": failed_records,
    }


def backfill_no_data_get_command(args) -> None:
    path = Path(args.path)
    if not path.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        sys.exit(1)

    cli_overrides: Dict[str, Any] = {}
    if args.request_timeout is not None:
        cli_overrides.setdefault("detection", {})["request_timeout"] = args.request_timeout
    if args.rate_limit_delay is not None:
        cli_overrides.setdefault("detection", {})["rate_limit_delay"] = args.rate_limit_delay
    if args.config is not None:
        config_path = args.config
    else:
        config_path = None
    config = load_config(config_path=config_path, cli_overrides=cli_overrides)

    verbosity = "quiet" if args.quiet else ("verbose" if args.verbose else "normal")
    log_dir = str(path if path.is_dir() else path.parent)
    setup_logging("backfill-no-data-get", verbosity, log_dir=log_dir)
    logger = get_logger("backfill-no-data-get")

    replay_files = list(_iter_replay_files(path))
    if not replay_files:
        print(f"Error: no *-replay-records.jsonl files found under {path}", file=sys.stderr)
        sys.exit(1)

    engine = ReplayEngine(config)
    totals = {
        "files": 0,
        "records": 0,
        "added": 0,
        "existing": 0,
        "skipped": 0,
        "failures": 0,
    }

    for replay_file in replay_files:
        logger.info("Backfilling no-data GET controls in %s", replay_file)
        result = _backfill_file(
            replay_file,
            engine,
            logger,
            force=args.force,
            dry_run=args.dry_run,
        )
        totals["files"] += 1
        for key in ("records", "added", "existing", "skipped", "failures"):
            totals[key] += result[key]
        if not args.quiet:
            print(
                f"{replay_file}: added={result['added']} "
                f"existing={result['existing']} skipped={result['skipped']} "
                f"failures={result['failures']}"
            )

    print("\n" + "=" * 60)
    print("NO-DATA GET BACKFILL SUMMARY")
    print("=" * 60)
    print(f"Replay files:       {totals['files']}")
    print(f"Records read:        {totals['records']}")
    print(f"Controls added:      {totals['added']}")
    print(f"Already present:     {totals['existing']}")
    print(f"Skipped:             {totals['skipped']}")
    print(f"Request failures:    {totals['failures']}")
    if args.dry_run:
        print("Dry run:             yes")
    print("=" * 60)


def register(subparsers):
    parser = subparsers.add_parser(
        "backfill-no-data-get",
        help="Add no-data GET control responses to existing replay records",
    )
    parser.add_argument(
        "path",
        help=(
            "A *-replay-records.jsonl file, a session directory, or a batch "
            "directory to scan recursively"
        ),
    )
    parser.add_argument("--config", help="Path to custom config YAML file")
    parser.add_argument(
        "--request-timeout",
        type=int,
        help="Override GET request timeout in seconds",
    )
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        help="Override delay between no-data GET requests in seconds",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-request controls even when no_data_get already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count candidates that would be backfilled without sending requests",
    )
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    parser.add_argument("--verbose", action="store_true", help="Debug output")
    parser.set_defaults(func=backfill_no_data_get_command)
