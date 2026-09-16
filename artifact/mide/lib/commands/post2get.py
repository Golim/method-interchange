import json
import signal
import sys
from datetime import datetime
from pathlib import Path

from lib.config import load_config
from lib.replay import ReplayEngine
from lib.logging_setup import setup_logging, get_logger
from lib.output import OutputWriter
from lib.utils import validate_url, extract_filename_from_domain

logger = None


def post2get_command(args) -> None:
    """Replay captured POST requests as GETs against the most recent crawl session.

    Writes <domain>-replay-records.jsonl (no inline analysis, no verdict).
    Run `mi-detector analyze <session_dir>` separately to compute the
    analysis output.
    """
    global logger
    from datetime import datetime

    # Build CLI overrides
    cli_overrides = {}
    if args.threshold is not None:
        cli_overrides.setdefault('detection', {})['similarity_threshold'] = args.threshold
    if args.array_style is not None:
        cli_overrides.setdefault('detection', {})['array_style'] = args.array_style
    if args.normalize:
        cli_overrides.setdefault('detection', {})['enable_normalization'] = True
    if args.output_dir is not None:
        cli_overrides.setdefault('output', {})['directory'] = args.output_dir

    # Load config
    config = load_config(config_path=args.config, cli_overrides=cli_overrides)

    # Validate URL and extract domain (filesystem-safe)
    if not validate_url(args.url):
        print(f"Error: Invalid URL '{args.url}'", file=sys.stderr)
        sys.exit(1)

    domain = extract_filename_from_domain(args.url)

    # Find most recent session directory for this domain
    output_base = Path(config.output_directory)
    domain_dir = output_base / domain

    if not domain_dir.exists():
        print(f"Error: No crawl sessions found for {domain}", file=sys.stderr)
        print(f"Run 'python main.py crawl {args.url}' first to capture POST requests", file=sys.stderr)
        sys.exit(1)

    # Get all session directories (timestamp format: YYYY-MM-DD_HHMMSS)
    session_dirs = [d for d in domain_dir.iterdir() if d.is_dir() and '_' in d.name]
    if not session_dirs:
        print(f"Error: No crawl sessions found for {domain}", file=sys.stderr)
        print(f"Run 'python main.py crawl {args.url}' first to capture POST requests", file=sys.stderr)
        sys.exit(1)

    # Sort by timestamp (most recent first)
    session_dirs.sort(key=lambda d: d.name, reverse=True)
    session_dir = session_dirs[0]

    # Find captured-posts.jsonl file
    input_file = session_dir / f"{domain}-captured-posts.jsonl"
    if not input_file.exists():
        print(f"Error: No captured requests found in {session_dir}", file=sys.stderr)
        print(f"Expected file: {input_file}", file=sys.stderr)
        sys.exit(1)

    # Setup logging with session directory
    verbosity = "quiet" if args.quiet else ("verbose" if args.verbose else "normal")
    setup_logging(domain, verbosity, log_dir=str(session_dir))
    logger = get_logger("cli")

    logger.info("Using session: %s", session_dir.name)
    logger.info("Input file: %s", input_file)

    # Replay layer is pure orchestration; analysis is offline.
    engine = ReplayEngine(config)

    # SIGINT handler
    def handle_sigint(signum, frame):
        if logger:
            logger.info("Interrupted, saving partial replay records...")
        engine.request_shutdown()
    signal.signal(signal.SIGINT, handle_sigint)

    # Build an OutputWriter pointing at the existing session directory (not the
    # default timestamped subdir) so writes target the same session.
    writer = OutputWriter(output_base_dir=str(output_base), domain=domain)
    writer.session_dir = session_dir
    replay_records_file = session_dir / f"{domain}-replay-records.jsonl"

    try:
        # Run replay
        logger.info("Starting replay on %s", input_file)
        replay_start = datetime.now()
        replay_summary = engine.run(str(input_file), writer)
        replay_end = datetime.now()
        writer.close()

        # Load existing session summary.json from crawl
        summary_path = session_dir / "summary.json"
        if summary_path.exists():
            with open(summary_path, 'r') as f:
                session_summary = json.load(f)
        else:
            session_summary = {
                "domain": domain,
                "session_start": replay_start.isoformat() + "Z",
            }

        # Extend session summary with replay results
        skipped_counts = replay_summary.get("replay_skipped", {})
        session_summary.update({
            "replay_start": replay_start.isoformat() + "Z",
            "replay_end": replay_end.isoformat() + "Z",
            "replay_duration_seconds": int((replay_end - replay_start).total_seconds()),
            "total_replay_records": replay_summary.get("total_records", 0),
            "replay_skipped": skipped_counts,
        })

        # Add replay output file to output_files list
        if "output_files" not in session_summary:
            session_summary["output_files"] = []
        if f"{domain}-replay-records.jsonl" not in session_summary["output_files"]:
            session_summary["output_files"].append(f"{domain}-replay-records.jsonl")

        # Write extended summary back
        with open(summary_path, 'w') as f:
            json.dump(session_summary, f, indent=2)

        # Print replay summary to console (no interchangeable / success_rate).
        total = replay_summary.get("total_records", 0)
        n_skipped = sum(skipped_counts.values()) if skipped_counts else 0
        print("\n" + "=" * 60)
        print("REPLAY SUMMARY")
        print("=" * 60)
        print(f"Session:                 {session_dir.name}")
        print(f"Total replay records:    {total}")
        print(f"Replay skipped:          {n_skipped}")
        print(f"  depth_truncation:        {skipped_counts.get('depth_truncation', 0)}")
        print(f"  unsupported_content_type:{skipped_counts.get('unsupported_content_type', 0)}")
        print(f"  malformed_body:          {skipped_counts.get('malformed_body', 0)}")
        print(f"Output: {replay_records_file}")
        print(f"Run `mi-detector analyze {session_dir}` to compute analysis.")
        print("=" * 60)

    except Exception as e:
        logger.error("Replay failed: %s", str(e))
        logger.debug("Traceback:", exc_info=True)
        sys.exit(1)


def register(subparsers):
    parser = subparsers.add_parser(
        "post2get",
        help="Replay captured POST requests as GETs (writes <domain>-replay-records.jsonl)"
    )
    parser.add_argument("url", help="Target website URL (uses most recent crawl session for this domain)")
    parser.add_argument("--threshold", type=float, help="Similarity threshold %% (consumed by `analyze`; default: 95.0)")
    parser.add_argument("--array-style", choices=["php", "numbered", "repeated"],
        help="Array serialization style for non-array-bearing JSON / urlencoded / multipart bodies (default: php)")
    parser.add_argument("--normalize", action="store_true",
        help="Normalization toggle (no-op here; analyze command consumes it)")
    parser.add_argument("--config", help="Path to custom config YAML file")
    parser.add_argument("--output-dir", help="Output directory for results")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    parser.add_argument("--verbose", action="store_true", help="Debug output")
    parser.set_defaults(func=post2get_command)
