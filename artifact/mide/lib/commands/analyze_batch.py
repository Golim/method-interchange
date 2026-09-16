import argparse
import sys
from pathlib import Path
from typing import Iterator, Tuple


def _iter_session_dirs(batch_dir: Path) -> Iterator[Tuple[str, Path]]:
    """Yield (domain, session_dir) for every session in a batch directory.

    Matches webapp/data_loader._find_session_dir timestamp-folder convention
    (YYYY-MM-DD_HHMMSS, len==17, sep at index 10). Skips non-directory
    entries and ignores files with .json/.log/.jsonl/.csv suffixes
    (checkpoint.json, batch-summary.json, etc.).
    """
    for domain_entry in batch_dir.iterdir():
        if not domain_entry.is_dir():
            continue
        if domain_entry.suffix in {".json", ".log", ".jsonl", ".csv"}:
            continue
        for sess in domain_entry.iterdir():
            if sess.is_dir() and len(sess.name) == 17 and sess.name[10] == "_":
                yield domain_entry.name, sess


def analyze_batch_command(args) -> None:
    """Walk a batch directory and run `analyze` on every session inside."""
    batch_dir = Path(args.batch_dir)
    if not batch_dir.is_dir():
        print(f"Error: Batch directory not found: {batch_dir}", file=sys.stderr)
        sys.exit(1)

    sessions = list(_iter_session_dirs(batch_dir))
    print(f"Analyzing {len(sessions)} sessions in {batch_dir}")

    # Local import avoids registration-time circular import surprises and
    # mirrors the per-session dispatch pattern in auth_crawl_batch.
    from lib.commands.analyze import analyze_command

    completed = 0
    failed = 0
    for i, (domain, session_dir) in enumerate(sessions, 1):
        print(f"[{i}/{len(sessions)}] {domain}/{session_dir.name} -- ANALYZING")
        single_args = argparse.Namespace(
            session_dir=str(session_dir),
            threshold=getattr(args, "threshold", None),
            array_style=getattr(args, "array_style", None),
            config=getattr(args, "config", None),
            quiet=True,
            verbose=False,
        )
        try:
            analyze_command(single_args)
            completed += 1
        except SystemExit as e:
            # analyze_command sys.exit(1)s on missing inputs — treat as failure
            # so other sessions still get processed.
            failed += 1
            print(f"[{i}/{len(sessions)}] {domain} -- FAILED: sys.exit({e.code})")
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(sessions)}] {domain} -- FAILED: {str(e)[:80]}")

    print(f"\n{'=' * 60}")
    print("ANALYZE-BATCH COMPLETE")
    print(f"{'=' * 60}")
    print(f"Sessions analyzed:    {completed}")
    print(f"Failed:               {failed}")
    print(f"{'=' * 60}")


def register(subparsers):
    parser = subparsers.add_parser(
        "analyze-batch",
        help="Walk a batch directory and run analyze on every session",
    )
    parser.add_argument(
        "batch_dir",
        help="Batch directory (batch-*, auth-batch-*, or auth-crawl-*)",
    )
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--array-style", choices=["php", "numbered", "repeated"])
    parser.add_argument("--config")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.set_defaults(func=analyze_batch_command)
