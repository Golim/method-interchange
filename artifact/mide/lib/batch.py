import json
import os
import time
from pathlib import Path
from typing import List, Optional, Dict, Any

from lib.config import Config
from lib.checkpoint import CheckpointManager
from lib.crawler import CrawlerEngine
from lib.replay import ReplayEngine
from lib.output import OutputWriter
from lib.logging_setup import get_logger
from lib.utils import validate_url, extract_filename_from_domain


class BatchRunner:
    """
    Orchestrates sequential batch processing of multiple websites.

    For each site, runs the requested pipeline (crawl, detect, or full),
    handles failures gracefully by catching exceptions and continuing,
    tracks per-site outcomes, and saves checkpoint state after each site.

    Attributes:
        config: Configuration instance with batch and crawl parameters
        batch_dir: Output directory for this batch run
        sites: List of URLs to process
        mode: Pipeline mode - 'crawl', 'detect', or 'full'
        checkpoint: CheckpointManager for resumability state
        completed: Count of successfully completed sites
        failed: Count of failed sites (exceptions caught)
        no_posts: Count of sites where no POST requests were captured
        captured_posts_sites: List of URLs where POSTs were captured
    """

    def __init__(
        self,
        config: Config,
        batch_dir: str,
        sites: List[str],
        mode: str,
        checkpoint: CheckpointManager,
    ) -> None:
        """
        Initialize BatchRunner.

        Args:
            config: Configuration instance with all settings
            batch_dir: Output directory for this batch run
            sites: List of URLs to process sequentially
            mode: Pipeline mode - 'crawl', 'detect', or 'full'
            checkpoint: CheckpointManager instance for state tracking
        """
        self.config = config
        self.batch_dir = batch_dir
        self.sites = sites
        self.mode = mode
        self.checkpoint = checkpoint
        self.logger = get_logger("batch")

        # Counters
        self.completed: int = 0
        self.failed: int = 0
        self.no_posts: int = 0

        # Site lists
        self.captured_posts_sites: List[str] = []

    def run(self) -> Dict[str, Any]:
        """
        Main batch processing loop.

        Processes each site sequentially. Resumes from checkpoint index if
        a previous run was interrupted. Handles failures by catching exceptions,
        logging the error, and continuing to the next site.

        Returns:
            Summary dictionary with batch statistics
        """
        resume_index = self.checkpoint.get_resume_index()
        total = len(self.sites)

        # On resume, restore counters from existing incremental summary
        if resume_index > 0:
            self._load_existing_summary()

        for i, url in enumerate(self.sites):
            if i < resume_index:
                # Already processed in a previous (interrupted) run
                self._print_status(i + 1, total, url, "SKIPPED (already processed)")
                continue

            self._print_status(i + 1, total, url, "STARTING")

            try:
                result = self._process_site(url)

                if result["status"] == "completed":
                    self.checkpoint.mark_done()
                    self.completed += 1
                    if result.get("posts_captured", 0) > 0:
                        self.captured_posts_sites.append(url)
                    self._print_status(
                        i + 1, total, url,
                        f"DONE posts={result.get('posts_captured', 0)}"
                    )

                elif result["status"] == "no_posts":
                    self.checkpoint.mark_done()
                    self.no_posts += 1
                    self._print_status(i + 1, total, url, "NO POSTs FOUND")

            except Exception as e:
                self.checkpoint.mark_failed()
                self.failed += 1
                self._print_status(i + 1, total, url, f"FAILED: {str(e)[:80]}")
                self.logger.error("Site failed: %s - %s", url, str(e))

            # Write incremental summary after each site
            self._write_incremental_summary()

            # Inter-site politeness delay (skip after last site)
            if i < total - 1:
                time.sleep(self.config.inter_site_delay)

        return self._build_batch_summary()

    def _process_site(self, url: str) -> Dict[str, Any]:
        """
        Run the pipeline for a single site.

        Validates URL, creates site output directory, and executes the
        requested mode (crawl/detect/full). Raises exceptions on failure
        so the outer loop can mark the site as failed.

        Args:
            url: Site URL to process

        Returns:
            Dict with 'status' (completed/no_posts) and 'posts_captured'.

        Raises:
            ValueError: If URL is invalid
            Any exception raised by CrawlerEngine or ReplayEngine
        """
        # Validate URL
        if not validate_url(url):
            raise ValueError(f"Invalid URL: {url}")

        # Extract filesystem-safe domain name
        domain = extract_filename_from_domain(url)

        # Ensure site output directory exists
        site_dir = os.path.join(self.batch_dir, domain)
        os.makedirs(site_dir, exist_ok=True)

        if self.mode == "crawl":
            return self._run_crawl(url, domain)

        elif self.mode == "detect":
            return self._run_replay(url, domain)

        elif self.mode == "full":
            crawl_result = self._run_crawl(url, domain)
            if crawl_result["status"] == "no_posts":
                return crawl_result
            # Only run replay if posts were captured
            if crawl_result.get("posts_captured", 0) > 0:
                self._run_replay(url, domain, session_dir=crawl_result.get("session_dir"))
                return {
                    "status": "completed",
                    "posts_captured": crawl_result.get("posts_captured", 0),
                    "session_dir": crawl_result.get("session_dir"),
                }
            return crawl_result

        else:
            raise ValueError(f"Unknown mode: {self.mode}")

    def _run_crawl(self, url: str, domain: str) -> Dict[str, Any]:
        """
        Run crawl pipeline for a single site.

        Creates an OutputWriter (which establishes session directory structure),
        instantiates CrawlerEngine, wires the on_post_captured callback, runs
        the crawl, writes session summary, and closes the writer.

        Args:
            url: Site URL to crawl
            domain: Filesystem-safe domain name for output naming

        Returns:
            Dict with status, posts_captured count, and session_dir path
        """
        # OutputWriter creates {batch_dir}/{domain}/{timestamp}/ internally
        writer = OutputWriter(output_base_dir=self.batch_dir, domain=domain)
        writer.open()

        try:
            # Create CrawlerEngine and wire callback
            engine = CrawlerEngine(url, self.config)
            engine.on_post_captured = writer.write_request

            # Run crawl
            posts_captured = engine.crawl()

            # Write session summary
            writer.write_summary(
                target_url=url,
                pages_crawled=engine.pages_crawled,
                posts_captured=posts_captured,
            )

            session_dir = writer.get_session_dir()
        finally:
            writer.close()

        if posts_captured == 0:
            return {
                "status": "no_posts",
                "posts_captured": 0,
                "session_dir": session_dir,
            }

        return {
            "status": "completed",
            "posts_captured": posts_captured,
            "session_dir": session_dir,
        }

    def _run_replay(self, url: str, domain: str, session_dir: Optional[str] = None) -> Dict[str, Any]:
        """
        Run replay pipeline for a single site.

        Finds the most recent session directory for the domain within batch_dir,
        locates the captured-posts JSONL file, replays each captured POST as a
        GET, and appends one record per POST to <domain>-replay-records.jsonl.
        Analysis is offline — run `analyze` separately on the session_dir.

        Args:
            url: Site URL (used for logging)
            domain: Filesystem-safe domain name for output naming
            session_dir: Optional explicit session directory to use. If None,
                         discovers the most recent session for the domain.

        Returns:
            Dict with status and (when applicable) session_dir.
        """
        # Find session directory
        if session_dir is None:
            session_dir = self._find_latest_session(domain)

        if session_dir is None:
            self.logger.warning("No session directory found for %s - cannot run replay", domain)
            return {"status": "no_posts"}

        # Find JSONL input file
        jsonl_file = os.path.join(session_dir, f"{domain}-captured-posts.jsonl")

        if not os.path.exists(jsonl_file):
            self.logger.warning("No JSONL file found at %s - no posts to replay", jsonl_file)
            return {"status": "no_posts"}

        # Check if file is empty
        if os.path.getsize(jsonl_file) == 0:
            self.logger.warning("JSONL file is empty: %s", jsonl_file)
            return {"status": "no_posts"}

        from pathlib import Path
        rep_writer = OutputWriter(output_base_dir=self.batch_dir, domain=domain)
        rep_writer.session_dir = Path(session_dir)
        engine = ReplayEngine(self.config)
        engine.run(input_file=jsonl_file, writer=rep_writer)
        rep_writer.close()

        return {
            "status": "completed",
            "session_dir": session_dir,
        }

    def _find_latest_session(self, domain: str) -> Optional[str]:
        """
        Find the most recent session directory for a domain in batch_dir.

        Session directories are named by timestamp ({domain}/{YYYY-MM-DD_HHMMSS}/),
        so sorting by name descending gives the most recent.

        Args:
            domain: Filesystem-safe domain name

        Returns:
            Path to most recent session directory, or None if not found
        """
        domain_dir = os.path.join(self.batch_dir, domain)

        if not os.path.isdir(domain_dir):
            return None

        # List subdirectories (timestamp dirs) and sort descending for most recent
        try:
            subdirs = [
                os.path.join(domain_dir, d)
                for d in os.listdir(domain_dir)
                if os.path.isdir(os.path.join(domain_dir, d))
            ]
            if not subdirs:
                return None
            # Sort descending by name - timestamps sort correctly as strings
            subdirs.sort(reverse=True)
            return subdirs[0]
        except OSError:
            return None

    def _print_status(self, index: int, total: int, url: str, status: str) -> None:
        """
        Print one-liner status to console (scrolling log format).

        Args:
            index: Current site index (1-based)
            total: Total number of sites
            url: Site URL being processed
            status: Status string to display
        """
        print(f"[{index}/{total}] {url} -- {status}")

    def _build_batch_summary(self) -> Dict[str, Any]:
        """
        Build batch summary dictionary with all statistics.

        Returns:
            Dict with total_sites, completed, failed, no_posts_found,
            sites_with_captured_posts counts and the captured_posts_sites list.
        """
        return {
            "total_sites": len(self.sites),
            "completed": self.completed,
            "failed": self.failed,
            "no_posts_found": self.no_posts,
            "sites_with_captured_posts": len(self.captured_posts_sites),
            "captured_posts_sites": self.captured_posts_sites,
        }

    def _write_incremental_summary(self) -> None:
        """Write current batch summary atomically to batch-summary.json."""
        summary = self._build_batch_summary()
        output_path = os.path.join(self.batch_dir, "batch-summary.json")
        tmp_path = output_path + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, output_path)

    def _load_existing_summary(self) -> None:
        """Load counters from existing batch-summary.json for resume accumulation."""
        summary_path = os.path.join(self.batch_dir, "batch-summary.json")
        if not os.path.exists(summary_path):
            return
        try:
            with open(summary_path, 'r', encoding='utf-8') as f:
                summary = json.load(f)
            self.completed = summary.get("completed", 0)
            self.failed = summary.get("failed", 0)
            self.no_posts = summary.get("no_posts_found", 0)
            self.captured_posts_sites = summary.get("captured_posts_sites", [])
            self.logger.debug(
                "Loaded existing summary: completed=%d, failed=%d, no_posts=%d",
                self.completed, self.failed, self.no_posts
            )
        except (json.JSONDecodeError, OSError) as e:
            self.logger.warning("Could not load existing summary, starting fresh: %s", str(e))

    @staticmethod
    def parse_csv(filepath: str, start: int = 0, limit: Optional[int] = None) -> List[str]:
        """
        Read URLs from a CSV file (one URL per line, no header).

        Handles bare domains by prepending 'https://' if no scheme present.
        Strips whitespace, skips empty lines and comment lines (starting with '#').
        Applies start/limit slicing for processing subsets.

        Args:
            filepath: Path to CSV file containing URLs
            start: Index offset to start from (default: 0)
            limit: Maximum number of URLs to return after start (default: all)

        Returns:
            List of URL strings with proper schemes
        """
        urls = []

        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()

                # Skip empty lines
                if not line:
                    continue

                # Skip comment lines
                if line.startswith('#'):
                    continue

                # Prepend https:// if no scheme present
                if '://' not in line:
                    line = 'https://' + line

                urls.append(line)

        # Apply start/limit slicing
        if limit is not None:
            return urls[start:start + limit]
        return urls[start:]

    @staticmethod
    def write_success_csv(batch_dir: str, urls: List[str]) -> None:
        """
        Write successful site URLs to success-sites.csv in batch_dir.

        Output format matches input CSV format (one URL per line, no header),
        making it re-runnable as batch input for re-processing successful sites.

        Args:
            batch_dir: Batch output directory to write file into
            urls: List of URLs that completed successfully
        """
        output_path = os.path.join(batch_dir, "success-sites.csv")
        with open(output_path, 'w', encoding='utf-8') as f:
            for url in urls:
                f.write(url + '\n')

    @staticmethod
    def write_batch_summary_json(batch_dir: str, summary: Dict[str, Any]) -> None:
        """
        Write batch summary statistics to batch-summary.json in batch_dir.

        Args:
            batch_dir: Batch output directory to write file into
            summary: Summary dictionary from _build_batch_summary()
        """
        output_path = os.path.join(batch_dir, "batch-summary.json")
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
