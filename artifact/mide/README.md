# Method Interchangeability Detection Engine (MIDE)

Tool for detecting HTTP method interchangeability vulnerabilities by crawling websites and testing POST-to-GET transformations.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (or `python3-pip`,
  which the installer can use to install uv)
- Docker (optional)

## Setup

From the repository root, use the artifact installer:

```bash
./install.sh
```

It creates `artifact/mide/.venv/`, installs the dependencies pinned by `uv.lock`,
and installs Chromium. Run the commands below from the repository root.

## Usage

The commands in this section contact the supplied target and may trigger
server-side behavior. Run them only against systems for which you have explicit
authorization. Use `./run-demo.sh` from the repository root for the offline-safe
evaluation workflow.

### Crawl a website and capture POST requests

```bash
artifact/mide/.venv/bin/python artifact/mide/main.py crawl https://example.com
```

### Test for method interchangeability

```bash
artifact/mide/.venv/bin/python artifact/mide/main.py post2get https://example.com
```

Uses the most recent crawl session for the domain.

### Test on a batch

```bash
artifact/mide/.venv/bin/python artifact/mide/main.py batch ./list.csv --mode full
```

Runs an interruptable and resumable execution session on a provided list of domains.

By default, the tool runs in `full` mode: each site is crawled and, if any POSTs were captured, it then runs replay and detection on that session.

## Docker

Build and run:

```bash
docker build -t mide artifact/mide
mkdir -p mide-output
docker run --rm -v "$PWD/mide-output:/app/output" mide crawl https://example.com
```

The bind mount preserves MIDE's generated output without hiding the application
code inside the image. For the safe offline artifact evaluation, use
`docker compose run --rm artifact` from the repository root instead.

## Configuration

Edit `artifact/mide/config/default.yaml` or provide a custom config with `--config`:

- Crawling limits (max_pages, max_depth, timeout)
- Browser settings (headless mode, args)
- URL blacklists (analytics, tracking domains)
- Output format and logging
