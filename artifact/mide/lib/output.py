import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from lib.logging_setup import get_logger


class OutputWriter:
    """
    Incremental JSONL writer for captured POST requests.

    Writes each captured request as a single JSON line immediately after
    capture. Flushes after each write to ensure data persists even if
    the crawler crashes mid-execution.

    Attributes:
        output_base_dir: Base directory for output files
        domain: Domain name for file naming
        session_dir: Session directory path ({output_base_dir}/{domain}/{timestamp}/)
        output_file: Path to the JSONL output file
        count: Number of requests written
        _file_handle: File handle for writing (internal)
        _session_start: Timestamp when writer was initialized

    Example:
        with OutputWriter("output", "example.com") as writer:
            writer.write_request({"url": "https://example.com/api", "method": "POST"})
    """

    def __init__(self, output_base_dir: str = "output", domain: str = ""):
        """
        Initialize output writer with session-based directory hierarchy.

        Args:
            output_base_dir: Base directory path for output files (default: "output")
            domain: Target domain name (used for filename and directory structure)
        """
        self.output_base_dir = Path(output_base_dir)
        self.domain = domain
        self.logger = get_logger("output")

        # Compute session timestamp at construction time (UTC)
        self._session_start = datetime.now(timezone.utc)
        timestamp = self._session_start.strftime("%Y-%m-%d_%H%M%S")

        # Build session directory path: {output_base_dir}/{domain}/{timestamp}/
        self.session_dir = self.output_base_dir / domain / timestamp

        # Output file within session directory
        self.output_file = self.session_dir / f"{domain}-captured-posts.jsonl"

        self.count = 0
        self._file_handle: Optional[Any] = None
        self._write_failed: bool = False
        # Lazy-opened on first write_replay_record() call.
        self._replay_file_handle: Optional[Any] = None

    def open(self) -> None:
        """
        Open output file for writing.

        Creates session directory if it doesn't exist.
        Opens file in append mode to preserve existing data.
        """
        # Create session directory if needed
        self.session_dir.mkdir(parents=True, exist_ok=True)

        # Open file in append mode with UTF-8 encoding
        self._file_handle = open(self.output_file, 'a', encoding='utf-8')

    def write_request(self, request_data: Dict[str, Any]) -> None:
        """
        Write a single request to JSONL file.

        Serializes request_data to JSON on a single line, writes to file,
        and flushes immediately to ensure data persists.

        Args:
            request_data: Dictionary containing request data
                (url, method, headers, postData, timestamp, resourceType)

        Raises:
            ValueError: If file handle is not open
            IOError: If write fails (e.g., disk full)
        """
        if self._file_handle is None:
            raise ValueError("OutputWriter not opened. Use context manager or call open() first.")

        try:
            # Serialize to single-line JSON
            json_line = json.dumps(request_data, ensure_ascii=False)

            # Write line with newline
            self._file_handle.write(json_line + '\n')

            # Flush immediately for crash safety
            self._file_handle.flush()
            os.fsync(self._file_handle.fileno())

            # Increment counter
            self.count += 1

        except OSError as e:
            # Disk full or write failure - critical error
            self.logger.error("CRITICAL: Write failed - %s", e)
            self.logger.error("Disk may be full. Stopping crawl to prevent data loss.")
            self._write_failed = True
            # Re-raise as IOError to stop the crawl immediately
            raise IOError(f"Cannot write to output file (disk full?): {e}")

    def write_replay_record(self, record: Dict[str, Any]) -> None:
        """Append one JSONL line to <domain>-replay-records.jsonl with per-line flush.

        Per-line flush is REQUIRED for mid-replay analysis safety — the
        analyze command may run concurrently against a still-being-written
        replay JSONL. Linux page cache makes flushed lines visible to other
        processes immediately; os.fsync is NOT required for cross-process
        visibility (only for crash-durability).

        Lazy-opens the dedicated `_replay_file_handle` on first call so
        callers do not need to invoke `open()` before writing replay records
        (separate lifecycle from the captured-posts handle).
        """
        if self._replay_file_handle is None:
            replay_path = self.session_dir / f"{self.domain}-replay-records.jsonl"
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self._replay_file_handle = open(replay_path, "a", encoding="utf-8")
        json_line = json.dumps(record, ensure_ascii=False)
        self._replay_file_handle.write(json_line + "\n")
        self._replay_file_handle.flush()
        # f.flush() only — omit os.fsync() to halve syscalls. write_request
        # fsyncs for crash-durability; replay-records can lose the last
        # partial in-memory line on power loss without correctness impact.

    def close(self) -> None:
        """
        Close output file handles.

        Closes both the captured-posts JSONL handle and (if opened) the replay-records
        JSONL handle. Handles already-closed files gracefully.
        """
        if self._file_handle is not None:
            try:
                self._file_handle.close()
            except Exception as e:
                self.logger.warning("Error closing _file_handle: %s", e)
            finally:
                self._file_handle = None
        if self._replay_file_handle is not None:
            try:
                self._replay_file_handle.close()
            except Exception as e:
                self.logger.warning("Error closing _replay_file_handle: %s", e)
            finally:
                self._replay_file_handle = None

    def get_output_path(self) -> str:
        """
        Get path to output file.

        Returns:
            String path to JSONL output file
        """
        return str(self.output_file)

    def get_session_dir(self) -> str:
        """
        Get path to session directory.

        Returns:
            String path to session directory
        """
        return str(self.session_dir)

    def write_summary(self, **kwargs) -> None:
        """
        Write summary.json to session directory with session metadata and statistics.

        Accepts keyword arguments for statistics and fills in timing from construction
        time and current time.

        Args:
            **kwargs: Statistics to include in summary (e.g., target_url, pages_crawled,
                     posts_captured, output_files, etc.)

        Example:
            writer.write_summary(
                target_url="https://example.com",
                pages_crawled=10,
                posts_captured=5
            )
        """
        # Calculate session end time and duration (UTC)
        session_end = datetime.now(timezone.utc)
        duration_seconds = int((session_end - self._session_start).total_seconds())

        # Build summary data
        summary_data = {
            "domain": self.domain,
            "session_start": self._session_start.isoformat(),
            "session_end": session_end.isoformat(),
            "duration_seconds": duration_seconds
        }

        # Add provided kwargs (target_url, pages_crawled, posts_captured, etc.)
        summary_data.update(kwargs)

        # Ensure output_files is included (defaults to captured-posts only)
        if "output_files" not in summary_data:
            summary_data["output_files"] = [
                f"{self.domain}-captured-posts.jsonl",
            ]

        # Write summary.json to session directory
        summary_path = self.session_dir / "summary.json"
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(summary_data, f, indent=2, ensure_ascii=False)

    def write_analysis(self, domain: str, analysis_dict: Dict[str, Any]) -> None:
        """Atomic tmp-then-rename JSON write of <domain>-analysis.json.

        Each `analyze` rerun overrides the previous file via `os.replace`
        (POSIX-atomic on Linux/macOS — no torn writes visible to a concurrent
        reader)."""
        output_path = self.session_dir / f"{domain}-analysis.json"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = str(output_path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(analysis_dict, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(output_path))

    def __enter__(self):
        """
        Context manager entry.

        Opens file and returns self for 'with' statement.
        """
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit.

        Closes file automatically when exiting 'with' block.
        """
        self.close()
        return False
