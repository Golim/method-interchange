import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from lib.batch import BatchRunner
from lib.checkpoint import CheckpointManager
from lib.config import load_config
from lib.logging_setup import setup_logging, get_logger

logger = None


def batch_command(args) -> None:
    """Process multiple websites from a CSV input file."""
    global logger

    # Build CLI overrides dictionary
    cli_overrides: Dict[str, Any] = {}

    if args.max_pages is not None:
        cli_overrides.setdefault('crawling', {})['max_pages'] = args.max_pages

    if args.max_depth is not None:
        cli_overrides.setdefault('crawling', {})['max_depth'] = args.max_depth

    if args.timeout is not None:
        # Convert seconds to milliseconds for config
        cli_overrides.setdefault('crawling', {})['page_load_timeout'] = args.timeout * 1000

    if args.max_posts is not None:
        cli_overrides.setdefault('capture', {})['max_posts'] = args.max_posts

    if args.max_clickable is not None:
        cli_overrides.setdefault('crawling', {})['max_clickable'] = args.max_clickable

    if args.threshold is not None:
        cli_overrides.setdefault('detection', {})['similarity_threshold'] = args.threshold

    if args.normalize:
        cli_overrides.setdefault('detection', {})['enable_normalization'] = True

    if args.output_dir is not None:
        cli_overrides.setdefault('output', {})['directory'] = args.output_dir

    # Map --mode to batch settings
    mode = args.mode

    # Load configuration with CLI overrides
    config = load_config(config_path=args.config, cli_overrides=cli_overrides)

    # Determine verbosity level
    verbosity = "quiet" if args.quiet else ("verbose" if args.verbose else "normal")

    if args.resume:
        # ---- RESUME PATH ----
        batch_dir = str(Path(args.resume).resolve())
        checkpoint = CheckpointManager(batch_dir)

        if not checkpoint.exists():
            print(f"Error: No checkpoint found in '{args.resume}'", file=sys.stderr)
            print("Use --resume only with a directory from a previous interrupted batch run.", file=sys.stderr)
            sys.exit(1)

        # Load checkpoint state
        state = checkpoint.load()
        input_file = state["input_file"]
        mode = state["mode"]
        start_index = state.get("start_index", 0)
        limit = state.get("limit")
        total_sites = state.get("total_sites", 0)
        current_index = state.get("current_index", 0)

        # Re-parse CSV from the original input file
        sites = BatchRunner.parse_csv(input_file, start_index, limit)

        remaining = total_sites - current_index
        print(f"Resuming batch from {batch_dir}, {remaining} sites remaining")

    else:
        # ---- FRESH RUN PATH ----
        if not args.input_file:
            print("Error: input_file is required for a fresh batch run", file=sys.stderr)
            print("Usage: python main.py batch <input_file.csv>", file=sys.stderr)
            print("       python main.py batch --resume <batch_dir/>", file=sys.stderr)
            sys.exit(1)

        # Validate input file exists
        input_path = Path(args.input_file)
        if not input_path.exists():
            print(f"Error: Input file not found: '{args.input_file}'", file=sys.stderr)
            sys.exit(1)

        # Parse sites from CSV
        sites = BatchRunner.parse_csv(str(input_path), args.start, args.limit)
        if not sites:
            print(f"Error: No sites found in '{args.input_file}' (start={args.start}, limit={args.limit})", file=sys.stderr)
            sys.exit(1)

        # Create batch output directory: {output_dir}/batch-{YYYYMMDD_HHMMSS}/
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_output = config.output_directory
        batch_dir = str(Path(base_output) / f"batch-{timestamp}")
        os.makedirs(batch_dir, exist_ok=True)

        # Create checkpoint for this fresh run
        checkpoint = CheckpointManager(batch_dir)
        checkpoint.create(
            input_file=str(input_path.resolve()),
            mode=mode,
            start_index=args.start,
            limit=args.limit,
            total_sites=len(sites),
        )

        print(f"Starting batch: {len(sites)} sites, mode={mode}")
        input_file = str(input_path.resolve())

    # Setup logging in batch directory
    setup_logging("batch", verbosity, log_dir=batch_dir)
    logger = get_logger("cli")

    # Setup SIGINT handler - print resume instructions
    runner = None

    def handle_sigint(signum, frame):
        print("\nInterrupted! Checkpoint saved. Resume with:")
        print(f"  python main.py batch --resume {batch_dir}")
        sys.exit(1)

    signal.signal(signal.SIGINT, handle_sigint)

    # Create and run BatchRunner
    runner = BatchRunner(
        config=config,
        batch_dir=batch_dir,
        sites=sites,
        mode=mode,
        checkpoint=checkpoint,
    )
    summary = runner.run()

    # Write output files
    BatchRunner.write_batch_summary_json(batch_dir, summary)
    BatchRunner.write_success_csv(batch_dir, summary.get("captured_posts_sites", []))

    # Print final summary to console
    total = summary.get("total_sites", 0)
    completed = summary.get("completed", 0)
    failed = summary.get("failed", 0)
    no_posts = summary.get("no_posts_found", 0)
    with_posts = summary.get("sites_with_captured_posts", 0)

    print("\n" + "=" * 60)
    print("BATCH COMPLETE")
    print("=" * 60)
    print(f"Total sites:              {total}")
    print(f"Completed:                {completed}")
    print(f"Failed:                   {failed}")
    print(f"No POSTs found:           {no_posts}")
    print(f"Sites with POSTs:         {with_posts}")
    print()
    print(f"Output:       {batch_dir}")
    print(f"Summary:      {batch_dir}/batch-summary.json")
    print(f"Success CSV:  {batch_dir}/success-sites.csv")
    print("=" * 60)


def register(subparsers):
    parser = subparsers.add_parser(
        "batch",
        help="Process multiple websites from CSV input file"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="CSV file with one URL per line (not needed with --resume)"
    )
    parser.add_argument(
        "--mode",
        choices=["crawl", "detect", "full"],
        default="full",
        help="Pipeline mode per site: crawl only, detect only, or full (default: full)"
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Start index in CSV (0-based, default: 0)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of sites to process from start index"
    )
    parser.add_argument(
        "--resume",
        metavar="BATCH_DIR",
        help="Resume interrupted batch from checkpoint directory (e.g., output/batch-20260218_143000/)"
    )
    parser.add_argument("--max-pages", type=int, help="Max pages per site (default: 10)")
    parser.add_argument("--max-depth", type=int, help="Max BFS depth per site (default: 5)")
    parser.add_argument("--timeout", type=int, help="Page load timeout in seconds (default: 30)")
    parser.add_argument("--max-posts", type=int, help="Max POST requests per site (default: 100, 0 = unlimited)")
    parser.add_argument(
        "--max-clickable",
        type=int,
        help="Maximum standalone buttons to click per page (default: 10, 0 = disable)"
    )
    parser.add_argument("--threshold", type=float, help="Similarity threshold %% (default: 95.0)")
    parser.add_argument("--normalize", action="store_true", help="Normalize dynamic content before comparison")
    parser.add_argument("--config", help="Path to custom config YAML file")
    parser.add_argument("--output-dir", help="Base output directory (default: output)")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    parser.add_argument("--verbose", action="store_true", help="Debug output")
    parser.set_defaults(func=batch_command)
