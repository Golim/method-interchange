#!/usr/bin/env python3
import time
from datetime import timedelta
from rich.live import Live
from rich.table import Table
from rich.panel import Panel

from urllib.parse import urlparse
from lib.logging_setup import get_shared_console


class ProgressDisplay:
    """
    Real-time progress display for web crawler execution.

    Displays live updating statistics during crawl or simple print-based progress
    in verbose mode. Supports quiet mode where only final summary is shown.
    """

    def __init__(self, target_url: str, max_pages: int, quiet: bool = False, verbose: bool = False):
        """
        Initialize progress display.

        :param str target_url: Target URL being crawled
        :param int max_pages: Maximum pages to crawl
        :param bool quiet: Suppress all output except final summary
        :param bool verbose: Use print-based progress (no Live display)
        """
        self.target_url = target_url
        self.max_pages = max_pages
        self.quiet = quiet
        self.verbose = verbose
        self.pages_visited = 0
        self.posts_found = 0
        self.filtered_posts = 0
        self.current_depth = 0
        self.current_url = ""
        self.start_time = None
        self._live = None
        self._console = get_shared_console()  # Use shared Console for coordination

    def start(self):
        """
        Start the progress display.

        In quiet mode: no-op
        In verbose mode: no Live display (conflicts with log output)
        In normal mode: start Rich Live display with table
        """
        self.start_time = time.time()

        if self.quiet:
            return

        if self.verbose:
            # Don't use Live display - it conflicts with verbose log lines
            self._console.print(f"\n[bold]Starting crawl:[/bold] {self.target_url}\n")
            return

        # Normal mode: Rich Live display with coordinated log output
        # Logs will appear above, progress display updates below (same Console)
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=2
        )
        self._live.start()

    def _render(self) -> Table:
        """
        Render the progress table for Live display.

        :return: Rich Table with current progress statistics
        """
        domain = urlparse(self.target_url).netloc or self.target_url

        # Calculate elapsed time
        elapsed = ""
        if self.start_time:
            elapsed_seconds = int(time.time() - self.start_time)
            elapsed = str(timedelta(seconds=elapsed_seconds))

        # Truncate current URL if too long
        display_url = self.current_url
        if len(display_url) > 50:
            display_url = display_url[:47] + "..."

        # Build table
        table = Table(title=f"MI Detector - Crawling: {domain}", show_header=False)
        table.add_column("Metric", style="cyan", width=20)
        table.add_column("Value", style="green")

        table.add_row("Pages Visited", f"{self.pages_visited} / {self.max_pages}")
        table.add_row("POST Requests", str(self.posts_found))
        table.add_row("Current Depth", str(self.current_depth))
        table.add_row("Elapsed Time", elapsed)
        table.add_row("Current URL", display_url)

        return table

    def update(self, pages: int = None, posts: int = None, depth: int = None, url: str = None, filtered: int = None):
        """
        Update progress statistics.

        :param int pages: Updated pages visited count
        :param int posts: Updated POST requests found count
        :param int depth: Updated current depth
        :param str url: Updated current URL
        :param int filtered: Updated filtered POST requests count
        """
        if pages is not None:
            self.pages_visited = pages
        if posts is not None:
            self.posts_found = posts
        if filtered is not None:
            self.filtered_posts = filtered
        if depth is not None:
            self.current_depth = depth
        if url is not None:
            self.current_url = url

        if self.quiet:
            return

        if self.verbose:
            # Print-based progress for verbose mode
            elapsed = ""
            if self.start_time:
                elapsed_seconds = int(time.time() - self.start_time)
                elapsed = str(timedelta(seconds=elapsed_seconds))

            if url is not None:
                self._console.print(f"[{elapsed}] Page {self.pages_visited}/{self.max_pages}: {url}")
            return

        # Update Live display in normal mode
        if self._live:
            self._live.update(self._render())

    def stop(self):
        """
        Stop the progress display.

        Stops Live display if running. Does not print summary here -
        that's handled by print_summary() which is called even in quiet mode.
        """
        if self._live:
            self._live.stop()
            self._live = None

    def print_summary(self, output_path: str):
        """
        Print final crawl completion summary.

        This is called even in quiet mode to show the final results.

        :param str output_path: Path to output file
        """
        # Calculate total elapsed time
        elapsed = ""
        if self.start_time:
            elapsed_seconds = int(time.time() - self.start_time)
            elapsed = str(timedelta(seconds=elapsed_seconds))

        # Build summary panel content
        filtered_line = f"Filtered POST Requests: {self.filtered_posts}\n" if self.filtered_posts > 0 else ""

        # Print formatted summary panel
        self._console.print("\n")
        self._console.print(Panel(
            f"[bold]Crawl Complete[/bold]\n\n"
            f"Pages: {self.pages_visited}\n"
            f"POST Requests: {self.posts_found}\n"
            f"{filtered_line}"
            f"Time: {elapsed}\n"
            f"Output: {output_path}",
            title="Summary",
            border_style="green"
        ))
