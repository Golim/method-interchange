#!/usr/bin/env python3
import logging
import os
from rich.console import Console
from rich.theme import Theme
from rich.logging import RichHandler

# Shared console instance for coordinated output between logs and Live display
_shared_console: Console = None


def get_shared_console() -> Console:
    """Get the shared Console instance used for logging and progress display."""
    global _shared_console
    if _shared_console is None:
        custom_theme = Theme({
            "logging.level.debug": "orange1",
            "logging.level.info": "blue",
            "logging.level.warning": "yellow",
            "logging.level.error": "red",
            "logging.level.critical": "red bold",
        })
        _shared_console = Console(theme=custom_theme)
    return _shared_console


def setup_logging(domain: str, verbosity: str = "normal", log_dir="logs") -> None:
    """
    Configure root logger with console and file handlers based on verbosity level.

    :param str domain: Target domain (used for error log filename)
    :param str verbosity: Logging verbosity - "quiet", "normal", or "verbose"
    :param str log_dir: Directory for error log files (defaults to "logs" if None)
    """
    # Create log directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)

    # Determine console log level based on verbosity
    level_map = {
        "quiet": logging.CRITICAL + 1,  # Effectively disable console output
        "normal": logging.INFO,  # Show INFO logs below Live display
        "verbose": logging.DEBUG
    }
    console_level = level_map.get(verbosity, logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture all levels, handlers will filter

    # Remove any existing handlers to avoid duplicates
    # Close handlers before removing to prevent file handle leaks
    for handler in root_logger.handlers[:]:
        handler.close()
        root_logger.removeHandler(handler)

    # Get shared console instance (used by both logging and Live display)
    console = get_shared_console()

    # Add RichHandler for console output with custom colors
    # When used with Live display on same Console, logs appear above Live content
    console_handler = RichHandler(
        console=console,
        markup=True,
        rich_tracebacks=True,
        show_time=True
    )
    console_handler.setLevel(console_level)
    console_formatter = logging.Formatter("%(message)s")
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # Add FileHandler for error log
    error_log_path = os.path.join(log_dir, f"{domain}-errors.log")
    file_handler = logging.FileHandler(error_log_path, mode='a')
    file_handler.setLevel(logging.WARNING)  # Capture WARNING, ERROR, CRITICAL
    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Suppress noisy third-party loggers
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('playwright').setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the specified name.

    :param str name: Logger name (e.g., 'crawler', 'output', 'cli')
    :return: Logger instance
    """
    return logging.getLogger(name)
