import signal
import sys
from typing import Dict, Any, Optional

from lib.config import load_config
from lib.crawler import CrawlerEngine
from lib.logging_setup import setup_logging, get_logger
from lib.output import OutputWriter
from lib.progress import ProgressDisplay
from lib.utils import validate_url, extract_filename_from_domain

logger = None


def crawl_command(args) -> None:
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

    if args.output_dir is not None:
        cli_overrides.setdefault('output', {})['directory'] = args.output_dir

    # Load configuration with CLI overrides
    config = load_config(config_path=args.config, cli_overrides=cli_overrides)

    # Validate target URL
    if not validate_url(args.url):
        print(f"Error: Invalid URL '{args.url}'", file=sys.stderr)
        print("URL must have http:// or https:// scheme and a valid domain", file=sys.stderr)
        sys.exit(1)

    # Extract domain for output file naming (filesystem-safe)
    domain = extract_filename_from_domain(args.url)
    if not domain:
        print(f"Error: Could not extract domain from URL '{args.url}'", file=sys.stderr)
        sys.exit(1)

    # Determine verbosity level
    verbosity = "quiet" if args.quiet else ("verbose" if args.verbose else "normal")

    # Create OutputWriter
    writer: Optional[OutputWriter] = None
    engine: Optional[CrawlerEngine] = None
    progress: Optional[ProgressDisplay] = None

    try:
        # Initialize output writer FIRST to establish session directory
        writer = OutputWriter(config.output_directory, domain)
        writer.open()

        # Get session directory for logging
        session_dir = writer.get_session_dir()

        # Setup logging with session directory
        setup_logging(domain, verbosity, log_dir=session_dir)
        logger = get_logger("cli")

        # Initialize crawler engine
        engine = CrawlerEngine(args.url, config)

        # Wire callback: on_post_captured -> writer.write_request
        engine.on_post_captured = writer.write_request

        # Create progress display
        progress = ProgressDisplay(
            target_url=args.url,
            max_pages=config.max_pages,
            quiet=args.quiet,
            verbose=args.verbose
        )

        # Wire progress callback (also passes filtered count from engine)
        engine.on_progress = lambda **kwargs: progress.update(filtered=engine.filtered_post_count, **kwargs)

        # Set up SIGINT handler for graceful shutdown
        def handle_sigint(signum, frame):
            if logger:
                logger.info("Received interrupt signal, shutting down gracefully...")
            if engine:
                engine.request_shutdown()

        signal.signal(signal.SIGINT, handle_sigint)

        # Start progress display
        progress.start()

        # Run crawl (launches browser, crawls, and cleans up)
        captured_requests = engine.crawl()

    except KeyboardInterrupt:
        # Backup handler if SIGINT doesn't work
        if logger:
            logger.info("Interrupted by user")
        else:
            print("Interrupted by user", file=sys.stderr)

    except Exception as e:
        if logger:
            logger.error("Fatal error: %s", str(e))
            logger.debug("Traceback:", exc_info=True)
        else:
            print(f"Fatal error: {str(e)}", file=sys.stderr)
        sys.exit(1)

    finally:
        # Stop progress display and print summary
        if progress:
            progress.stop()
            if writer:
                progress.print_summary(writer.get_output_path())

                # Write summary.json with session statistics
                try:
                    writer.write_summary(
                        target_url=args.url,
                        pages_crawled=progress.pages_visited,
                        posts_captured=writer.count,
                        filtered_posts=engine.filtered_post_count if engine else 0,
                    )
                except Exception as e:
                    logger.warning(f"Failed to write summary.json: {e}")

        # Clean up resources
        # Note: engine.crawl() handles browser cleanup internally
        if writer:
            try:
                writer.close()
            except Exception:
                pass


def register(subparsers):
    parser = subparsers.add_parser(
        "crawl",
        help="Crawl website and capture POST requests"
    )
    parser.add_argument("url", help="Target URL to crawl")
    parser.add_argument("--max-pages", type=int, help="Maximum pages to crawl (default: 10)")
    parser.add_argument("--max-depth", type=int, help="Maximum BFS depth (default: 5)")
    parser.add_argument("--timeout", type=int, help="Page load timeout in seconds (default: 30)")
    parser.add_argument("--max-posts", type=int, help="Maximum POST requests to capture per site (default: 100, 0 = unlimited)")
    parser.add_argument(
        "--max-clickable",
        type=int,
        help="Maximum standalone buttons to click per page (default: 10, 0 = disable)"
    )
    parser.add_argument("--config", help="Path to custom config YAML file")
    parser.add_argument("--output-dir", help="Output directory for captured requests")
    parser.add_argument("--quiet", action="store_true", help="Minimal output (final summary only)")
    parser.add_argument("--verbose", action="store_true", help="Debug-level output")
    parser.set_defaults(func=crawl_command)
