import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lib.logging_setup import get_logger


class CheckpointManager:
    """
    Manages batch run state via a checkpoint.json file in the batch output directory.

    Enables resumability: if a batch is interrupted, re-running with --resume picks
    up from current_index rather than reprocessing already-completed sites.

    Atomic write pattern:
        Write to checkpoint.json.tmp, then os.replace() to checkpoint.json.
        This ensures checkpoint.json is never partially written on crash.
    """

    def __init__(self, batch_dir: str) -> None:
        """
        Initialize CheckpointManager for the given batch output directory.

        Args:
            batch_dir: Absolute or relative path to the batch output directory.
                       The checkpoint file will be written at {batch_dir}/checkpoint.json.
        """
        self.logger = get_logger("checkpoint")
        self.batch_dir = str(Path(batch_dir).resolve())
        self.checkpoint_path = os.path.join(self.batch_dir, "checkpoint.json")
        self._state: dict = {}

    def create(
        self,
        input_file: str,
        mode: str,
        start_index: int,
        limit: Optional[int],
        total_sites: int,
    ) -> None:
        """
        Initialize a fresh checkpoint for a new batch run.

        Sets current_index=0 and all counts to 0. Saves immediately to disk.

        Args:
            input_file: Path to the input CSV file containing sites.
            mode: Batch mode ('crawl', 'detect', or 'full').
            start_index: The logical index in the input CSV where processing starts
                         (used when --start-index CLI flag is provided).
            limit: Maximum number of sites to process, or None for all sites.
            total_sites: Total number of sites that will be processed in this batch.
        """
        self._state = {
            "input_file": str(Path(input_file).resolve()),
            "mode": mode,
            "start_index": start_index,
            "limit": limit,
            "batch_dir": self.batch_dir,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "current_index": 0,
            "completed_count": 0,
            "failed_count": 0,
            "total_sites": total_sites,
        }
        self.save()
        self.logger.debug(
            f"Checkpoint created: {total_sites} sites, mode={mode}, "
            f"start_index={start_index}, limit={limit}"
        )

    def load(self) -> dict:
        """
        Load existing checkpoint from checkpoint.json.

        Returns:
            Dictionary containing the checkpoint state.

        Raises:
            FileNotFoundError: If checkpoint.json does not exist in batch_dir.
        """
        if not os.path.exists(self.checkpoint_path):
            raise FileNotFoundError(
                f"Checkpoint file not found: {self.checkpoint_path}. "
                "Use --resume only when a previous batch was interrupted."
            )

        with open(self.checkpoint_path, "r") as f:
            self._state = json.load(f)

        self.logger.debug(
            f"Checkpoint loaded: current_index={self._state.get('current_index', 0)}, "
            f"completed={self._state.get('completed_count', 0)}, "
            f"failed={self._state.get('failed_count', 0)}"
        )
        return self._state

    def exists(self) -> bool:
        """
        Check whether a checkpoint file exists in the batch directory.

        Returns:
            True if checkpoint.json exists, False otherwise.
        """
        return os.path.exists(self.checkpoint_path)

    def mark_done(self) -> None:
        """
        Mark the current site as successfully completed.

        Increments current_index (moves to next site) and completed_count,
        then saves checkpoint atomically.
        """
        self._state["current_index"] = self._state.get("current_index", 0) + 1
        self._state["completed_count"] = self._state.get("completed_count", 0) + 1
        self.save()
        self.logger.debug(
            f"Site marked done: index={self._state['current_index']}, "
            f"completed={self._state['completed_count']}"
        )

    def mark_failed(self) -> None:
        """
        Mark the current site as failed (error during processing).

        Increments current_index (moves to next site) and failed_count,
        then saves checkpoint atomically.
        """
        self._state["current_index"] = self._state.get("current_index", 0) + 1
        self._state["failed_count"] = self._state.get("failed_count", 0) + 1
        self.save()
        self.logger.debug(
            f"Site marked failed: index={self._state['current_index']}, "
            f"failed={self._state['failed_count']}"
        )

    def get_resume_index(self) -> int:
        """
        Get the index from which to resume processing.

        The caller slices the sites list from this index: sites[:current_index]
        are already processed and should be skipped.

        Returns:
            current_index from the checkpoint state.
        """
        return self._state.get("current_index", 0)

    def save(self) -> None:
        """
        Write current checkpoint state to disk using atomic write.

        Write pattern:
            1. Write state JSON to checkpoint.json.tmp
            2. os.replace() to checkpoint.json (atomic on POSIX)

        This ensures checkpoint.json is never corrupted by partial writes.
        The batch_dir is created if it does not yet exist.
        """
        os.makedirs(self.batch_dir, exist_ok=True)

        tmp_path = self.checkpoint_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self._state, f, indent=2)

        os.replace(tmp_path, self.checkpoint_path)
        self.logger.debug(f"Checkpoint saved: {self.checkpoint_path}")

    def get_state(self) -> dict:
        """
        Return a copy of the current checkpoint state dictionary.

        Returns:
            Dictionary with all checkpoint fields.
        """
        return dict(self._state)
