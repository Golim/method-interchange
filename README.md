# MIDE: Method Interchangeability Detection Engine

This is the artifact for *When POST Becomes GET: Investigating HTTP Method
Interchangeability and Its Security Implications*. MIDE captures browser-generated
POST requests, converts supported request bodies into GET candidates, replays those
candidates with a no-query control, and compares the observed responses.

For the initial setup and platform requirements, see [`infrastructure/README.txt`](infrastructure/README.txt).
The generated submission metadata is [`metadata.toml`](metadata.toml); replace its repository URL
and citation only if the submission repository or accepted-paper metadata changes.

## Quick start: safe offline demonstration

On Ubuntu 22.04 or newer (x86-64), with Python 3.11+ and outbound installation
network access:

```bash
./install.sh
./run-demo.sh
```

The demo is fully offline after installation and finishes in under two minutes on a
two-core VM with 4 GB RAM. It uses only synthetic records in `artifact/data/demo/` and writes
results to `results/demo/`.

## Docker Compose

If Docker and the Docker Compose plugin are installed, the same offline demo can run
without a host Python setup:

```bash
docker compose run --rm artifact
```

Run an individual claim with, for example,

```bash
docker compose run --rm artifact ./claims/claim1/run.sh
```

## Google Colab

The safe offline demonstrations can also run on a standard Google Colab CPU
runtime; no GPU is needed. Open a new notebook and run these cells:

```python
!git clone https://github.com/Golim/method-interchange.git
%cd method-interchange
!UV_CACHE_DIR=/tmp/mide-uv-cache ./install.sh
!./run-demo.sh
!./claims/claim1/run.sh
!./claims/claim2/run.sh
!./claims/claim3/run.sh
```

Colab runtimes are temporary, so download or copy the generated `results/`
directory before disconnecting. Installation requires outbound network access.
Use Colab for the bundled offline demonstrations only; live crawling, CSRF
replay, and WCD testing require explicit authorization. See
`infrastructure/README.txt` for public-infrastructure limitations.

## Repository layout

| Path | Purpose |
| --- | --- |
| `artifact/` | MIDE source, analyses, configuration, lockfile, and synthetic data |
| `infrastructure/` | Public-infrastructure requirements and container guidance |
| `claims/` | Instructions for each major artifact claim |
| `ARTIFACT.md` | Submission inventory, requirements, safety, and limitations |
| `metadata.toml` | ACSAC/artmeta metadata file submitted with the artifact |

## Live use

MIDE can collect a new crawl and replay supported POST requests:

```bash
artifact/mide/.venv/bin/python artifact/mide/main.py crawl https://example.org
artifact/mide/.venv/bin/python artifact/mide/main.py post2get https://example.org
```

Live crawling, CSRF checks, and WCD checks contact remote systems. Use them only on
systems for which you have explicit authorization. The raw Internet-scale
measurement data are not part of this repository; see `artifact/data/README.md` for the
reproducibility implications.
