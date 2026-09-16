#!/usr/bin/env python3
# Load .env file if present so environment variables referenced elsewhere (e.g. in lib modules)
# pick up values when modules import at runtime.
try:
    from dotenv import load_dotenv, find_dotenv
    dotenv_path = find_dotenv()
    if dotenv_path:
        load_dotenv(dotenv_path)
    else:
        # Attempt a best-effort load (no-op if no .env exists)
        load_dotenv()
except Exception:
    # If python-dotenv isn't available, continue without failing; env vars must be set externally.
    pass

from lib.commands import (
    analyze,
    analyze_batch,
    backfill_no_data_get,
    batch,
    crawl,
    post2get,
    save_session,
)


def main() -> None:
    """Main entry point — thin dispatcher for CLI subcommands."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="mide", description="Method Interchangeability Detection Engine"
    )

    subparsers = parser.add_subparsers(
        dest="command", required=True, help="Command to execute"
    )

    # Register all commands
    for cmd in [
        crawl,
        post2get,
        batch,
        save_session,
        backfill_no_data_get,
        analyze,
        analyze_batch,
    ]:
        cmd.register(subparsers)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
