from pathlib import Path


def save_session_command(args) -> None:
    """Open a headed browser, wait for user to log in, then save session to JSON."""
    from playwright.sync_api import sync_playwright
    output_path = args.output
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        existing = Path(output_path) if Path(output_path).exists() else None
        if existing:
            # Sanitize CHIPS-format cookies (partitionKey as object) written by newer Chrome/browser-use
            # Playwright expects partitionKey to be a string or absent
            import json as _json
            state = _json.loads(existing.read_text())
            for cookie in state.get('cookies', []):
                if isinstance(cookie.get('partitionKey'), dict):
                    del cookie['partitionKey']
            context = browser.new_context(storage_state=state)
        else:
            context = browser.new_context()
        page = context.new_page()
        page.goto("about:blank")
        input("Log in to your account in the browser window, then press Enter to save session > ...")
        context.storage_state(path=output_path)
        browser.close()
    print(output_path)


def register(subparsers):
    parser = subparsers.add_parser(
        "save-session",
        help="Save browser session to JSON (used by auth-crawl and batch flows)"
    )
    parser.add_argument(
        "--output",
        default="./session.json",
        help="Output path for session JSON (default: ./session.json)"
    )
    parser.set_defaults(func=save_session_command)
