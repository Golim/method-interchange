# Method Interchangeability Detection Engine (MIDE)

Tool for detecting HTTP method interchangeability vulnerabilities by crawling websites and testing POST-to-GET transformations.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (package manager)
- Docker (optional)

## Setup

Install `uv` if you don't have it, Then install dependencies:

```bash
uv sync
uv run playwright install chromium
```

> `uv sync` creates `.venv/` and installs all dependencies from `uv.lock`. Run it once after cloning.

## Usage

### Crawl a website and capture POST requests

```bash
uv run python main.py crawl https://example.com
```

### Test for method interchangeability

```bash
uv run python main.py post2get https://example.com
```

Uses the most recent crawl session for the domain.

### Test on a batch

```bash
uv run python main.py batch ./list.csv --mode full
```

Runs an interruptable and resumable execution session on a provided list of domains.

By default, the tool runs in `full` mode: each site is crawled and, if any POSTs were captured, it then runs replay and detection on that session.

## Docker

Build and run:

```bash
docker build -t mide .
docker run --rm -v path/to/the/project:/app mide crawl https://example.com
```

## Configuration

Edit `config/default.yaml` or provide a custom config with `--config`:

- Crawling limits (max_pages, max_depth, timeout)
- Browser settings (headless mode, args)
- URL blacklists (analytics, tracking domains)
- Output format and logging
