# ACSAC artifact guide

## Inventory

| Path | Contents |
| --- | --- |
| `artifact/mide/` | MIDE source code, pinned `uv.lock`, configuration, and Dockerfile |
| `artifact/analysis/` | Offline analysis and opt-in security follow-up scripts |
| `artifact/data/demo/` | Small synthetic replay-record fixture; no real-site data |
| `claims/` | One runnable guide per major artifact claim |
| `install.sh` | Installs the pinned Python environment and Chromium |
| `run-demo.sh` | Runs a safe end-to-end offline demonstration |
| `metadata.toml` | Metadata file produced for the ACSAC artifact submission |

## Platform and requirements

The artifact is designed for an Ubuntu 22.04-or-newer x86-64 VM on public research
infrastructure such as Chameleon, CloudLab, FABRIC, or SPHERE. A standard two-core
VM with 4 GB RAM and 10 GB free disk is sufficient for the offline demonstration.
No special hardware is required. Python 3.11+, outbound access to PyPI and the
Playwright browser download during installation, and a current Chromium-compatible
Linux environment are required. Docker is optional; `artifact/mide/Dockerfile` provides an
alternative MIDE runtime.

Run `./install.sh`, then `./run-demo.sh`. Installation normally takes several
minutes because Chromium is downloaded; the demo completes in under two minutes.
As a container alternative, run `docker compose run --rm artifact`; see
`infrastructure/README.txt` for individual claim commands.

## Claim-to-paper mapping

The bundled claims are deliberately small, deterministic demonstrations of the
paper's methods. They do not regenerate the paper's Tranco counts.

| Artifact entry | Paper support | What the evaluator should verify |
| --- | --- | --- |
| `claims/claim1/run.sh` | Sections 5--6; baseline-control method and Table `baseline-classes` | The comparator separates classes A/B/C/D and handles form, JSON, and multipart inputs. |
| `claims/claim2/run.sh` | Sections 5--6 and Appendix; conversion/replay analysis, endpoint taxonomy, and validation workflow | The analysis scripts produce non-empty summaries and a stratified validation sample. |
| `claims/claim3/run.sh` | Section 7; CSRF token-replay and WCD follow-up methodology | Offline plans and recorded synthetic outcomes are generated, while the WCD dry run performs no live requests. |
| `artifact/mide/main.py` | Sections 5 and 6; browser crawl, POST capture, conversion, replay, and batch execution | The live workflow is available for authorized targets; it is not required for the safe offline badge evaluation. |

## Data and reproducibility scope

This archive includes source code and synthetic example data only. It does not
include the raw large-scale crawl corpus, because captured live-web request and
response records can contain target-specific or sensitive material. Thus, it is
documented, complete for exercising the software, and consistent with the paper,
but cannot independently regenerate the paper's exact large-scale counts. See
`artifact/data/README.md` for the included JSONL schema and data limitations.

## Public release

The authors intend to release the entire artifact evaluated here—including the
source code, analysis scripts, synthetic fixture, claim runners, and documentation—in
a permanent public repository after evaluation. The raw large-scale crawl corpus is
not part of the evaluated artifact and will not be released for the privacy and
sensitivity reasons above.

## Safety

All bundled examples are offline. Crawling and live CSRF/WCD replays can interact
with remote systems and must be used only with explicit authorization. The default
MIDE configuration includes conservative crawl limits and filters, but those do not
replace authorization.
