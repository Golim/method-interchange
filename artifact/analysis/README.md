# Method Interchangeability Analysis

This document describes what the analysis scripts compute, what input they expect, and how to interpret each output table and figure.

Run `./install.sh` once from the repository root before executing these scripts.
The scripts import MIDE's shared comparator directly from `../mide/lib`; no copying
or manual path changes are required. Invoke them from the repository root with
`artifact/mide/.venv/bin/python artifact/analysis/<script>.py ...`.

## Prevalence Analysis

To compute method interchangeability prevalence, run:

```bash
artifact/mide/.venv/bin/python artifact/analysis/prevalence_analysis.py output/batch-<timestamp> --latex
```

The positional argument may be a full batch directory, a single session directory, or a single `*-replay-records.jsonl` file. When a batch directory contains `batch-summary.json`, the script uses it to report crawl coverage and site-level denominators relative to the full tested population.

Important options:

- `--out-dir results/prevalence-analysis`: where tables, plots, CSV labels, and JSON summary are written.
- `--latex`: also writes `.tex` versions of all tables.
- `--threshold 95.0`: response similarity threshold used by the comparator.
- `--bootstrap-samples 5000`: number of site-level bootstrap samples.
- `--no-charts`: skip PNG chart generation.

### Unit Definitions

The script reports method interchangeability at three levels.

- **Site level**: a domain/site from the batch, such as `example.com`.
- **Route level**: unique `(scheme, host, path, method, normalized content type)` among replayed POST records.
- **Request level**: an individual captured POST instance in a replay JSONL file.

The main prevalence denominators are intentionally strict:

- `sites_with_replayed_POSTs`: sites with at least one POST that was converted and replayed as GET.
- `unique_replayed_POST_routes`: unique route keys among replayed POST records.
- `replayed_POST_requests`: POST records with at least one converted GET candidate.

Sites with no POSTs, conversion-skipped POSTs, and missing replay files are reported separately in coverage tables, not silently folded into the main denominator.

### Core Classification

For each replayed POST, the script compares:

- the original POST response with the converted GET response; and
- the original POST response with the no-query GET control response.

It assigns the baseline-control class:

- **A. Strong interchangeability**: converted GET matches POST, no-query GET does not.
- **B. Ambiguous**: converted GET matches POST, no-query GET also matches POST.
- **C. Baseline collision**: converted GET does not match POST, no-query GET matches POST.
- **D. Not interchangeable**: neither GET response matches POST.

The headline number uses **A only**. The broader upper bound uses **A+B**.

The route label is:

- `route_reached`: converted GET returned `2xx`, `3xx`, or `400`.
- `route_rejected`: converted GET returned `401`, `403`, `404`, `405`, `415`, `422`, `5xx`, timeout, or network failure.

The script also records `accepted_for_equivalence = route_reached AND source_equivalent`. A `400` is evidence that the route/handler may have been reached, but is not counted as interchangeability unless the response comparison is equivalent.

### Output Tables

All tables are written as `.txt`; with `--latex`, matching `.tex` files are also written.

- `batch-coverage`: shows the analysis population from the full batch down to replayed requests. This is the table to cite when explaining how many of the tested sites had no POSTs, had captured POSTs, had replay files, had replayed POSTs, and how many POST records were conversion-skipped.
- `site-prevalence-denominators`: reports A-only and A+B site prevalence against several denominators: all tested sites, completed pipeline sites, sites with captured POSTs, sites with replay files, and sites with replayed POSTs. This is useful for distinguishing “prevalence among all tested Tranco sites” from “prevalence among sites where interchangeability could actually be tested.”
- `main-prevalence`: the headline A-only prevalence at site, route, and request levels, with site-level bootstrap confidence intervals.
- `upper-bound-prevalence`: the broader A+B prevalence at site, route, and request levels.
- `baseline-classes`: request-level counts for A/B/C/D among replayed POSTs.
- `conversion-outcomes-all`: conversion labels for every captured POST record in replay JSONL files.
- `conversion-outcomes-interchangeable`: conversion labels only for high-confidence A requests.
- `replay-outcomes`: replay outcome labels among replayed POST records.
- `route-outcomes`: `route_reached`/`route_rejected` counts among replayed POST records.
- `content-type-breakdown`: captured POSTs, replayed POSTs, and high-confidence A requests by source content-type bucket.

### Output Data Files

- `request-labels.csv`: one row per captured POST record, with domain, URL, conversion outcome, replay outcome, route outcome, A/B/C/D baseline class, high-confidence flag, upper-bound flag, and similarity scores.
- `summary.json`: machine-readable copy of the major counts, batch summary, prevalence metrics, confidence intervals, and threshold used.

### Output Figures

Unless `--no-charts` is used, the script writes:

- `site-analysis-funnel.png`: site counts through the analysis pipeline: tested, completed, captured POSTs, replayed POSTs, high-confidence sites, and upper-bound sites.
- `prevalence-levels.png`: bar chart comparing A-only site, route, and request prevalence with bootstrap error bars.
- `similarity-scatter.png`: scatter plot with x-axis `similarity(POST, converted GET)` and y-axis `similarity(POST, no-query GET)`. Dashed lines mark the similarity threshold. The quadrants correspond to strong interchangeability, ambiguity, baseline collision, and non-interchangeability.

## Endpoint Taxonomy Analysis

Run it with:

```bash
artifact/mide/.venv/bin/python artifact/analysis/endpoint_taxonomy_analysis.py analyze output/batch-<timestamp> --latex
```

## Conversion and Replay Failure Analysis

`artifact/analysis/conversion_replay_failure_analysis.py` explains what the system misses before and after POST-to-GET conversion. It is complementary to the prevalence analysis: the prevalence script asks how often we see high-confidence method interchangeability, while this script asks why captured POSTs fail to become clean, replayable, equivalent candidates.

Run it with:

```bash
artifact/mide/.venv/bin/python artifact/analysis/conversion_replay_failure_analysis.py output/batch-20260511_134923 --latex
```

Important options:

- `--out-dir results/conversion-replay-failure-analysis`: where tables, plots, CSV labels, and JSON summary are written.
- `--threshold 95.0`: structural response-similarity threshold used for converted-GET equivalence.
- `--oversized-query-threshold 8192`: query-string length threshold used to mark a converted GET as oversized.
- `--latex`: also writes `.tex` versions of all tables.
- `--no-charts`: skip PNG chart generation.

Important outputs:

- `conversion-success-by-content-type`: how many captured POST records produced at least one converted GET candidate, grouped by source content type.
- `replay-success-by-content-type`: among converted candidates, how many received an HTTP response rather than only replay failures, grouped by source content type.
- `equivalence-by-content-type`: among replay successes, how many converted GET responses matched the original POST response. This table does not apply the no-query baseline control; read it as conversion/replay equivalence, not as the headline high-confidence interchangeability result.
- `lossy-multipart-confirmation`: equivalent multipart records where file uploads were removed. These are explicitly weaker evidence because the converted GET omitted data that was present in the original POST.
- `query-length-distribution`: converted query-string length percentiles by content type, using the maximum query length when a record has multiple candidates.
- `primary-miss-reasons`: one primary reason per captured POST record. This is a priority-ordered explanation, so a record with several issues is counted under the first matching reason.
- `json-shapes`: non-exclusive JSON body-shape labels. Shares can sum above 100 percent because one JSON body can be both a top-level object and contain nested arrays, for example.
- `multipart-shapes`: multipart body-shape labels based on whether parsed parts contain fields, files, or both.
- `replay-failures`: replay failure subtypes among records that were converted but did not receive a usable replay response.
- `request-failure-labels.csv`: one row per captured POST record with all labels used by the tables.
- `summary.json`: machine-readable aggregate counts, threshold settings, and miss-reason counts.
- `query-length-distribution.png`: histogram of converted query-string lengths.
- `primary-miss-reasons.png`: bar chart of the most common primary miss reasons.

## Response-Equivalence Validation Study

`artifact/analysis/response_validation_platform.py` prepares and serves a manual validation study for the response-equivalence detector. It samples replayed records stratified by baseline class, using finite-population sample sizes for the requested confidence and margin of error. By default it samples classes A and D at 90% confidence and 15% margin so the study stays small enough for practical manual review; add class B or tighten the confidence and margin options only when you explicitly want a larger study.

Prepare a study with:

```bash
artifact/mide/.venv/bin/python artifact/analysis/response_validation_platform.py prepare output --out-dir results/response-validation --latex
```

Serve the local annotation UI with:

```bash
artifact/mide/.venv/bin/python artifact/analysis/response_validation_platform.py serve --study-dir results/response-validation --host 127.0.0.1 --port 8766
```

Generate report artifacts after annotating with:

```bash
artifact/mide/.venv/bin/python artifact/analysis/response_validation_platform.py report --study-dir results/response-validation --latex
```

The annotation UI shows the original POST URL, the converted GET URL, the no-query GET baseline URL, status summaries, compact body-only response diffs, and optional body previews. Each item records whether the converted GET is manually equivalent to the POST, whether the no-query baseline is equivalent, and optional free-form notes.

Important outputs:

- `sample.json`: sampled validation items and their source replay-record references.
- `annotations.json`: editable/persistent annotation state used by the UI.
- `annotations.csv`: flat export of completed annotations.
- `validation-sample-summary`: finite-population sample sizes by class.
- `threshold-sensitivity`: class counts and prevalence at thresholds 0.90, 0.95, and 0.98.
- `manual-validation-results`: manual equivalence rates and Wilson confidence intervals by class and comparison target.
- `validation-summary.json` and `validation-report.md`: machine-readable and prose summaries for paper drafting.

For the paper, class-A precision should be computed from class-A items where `POST vs converted GET` is marked `equivalent` or `different`; uncertain labels should be reported but excluded from the point estimate. Class-D manual-equivalence rate provides an empirical estimate of missed equivalent cases in the sampled D stratum.

## CSRF Token Replay Check

`artifact/analysis/csrf_token_replay_check.py` is a live replay checker for the token-bearing high-confidence candidates found by the CSRF analysis. Passing `--execute` sends the planned live requests.

Dry-run example:

```bash
artifact/mide/.venv/bin/python artifact/analysis/csrf_token_replay_check.py output/batch-20260511_134923 --limit 10 --latex
```

Live replay example:

```bash
artifact/mide/.venv/bin/python artifact/analysis/csrf_token_replay_check.py output/batch-20260511_134923 --execute --repeat-post --test-invalid-post-token
```

The checker can send these variants:

- `post_original_1`: original captured POST.
- `post_original_2`: same POST again, when `--repeat-post` is used.
- `post_without_token` and `post_changed_token`: only when `--test-invalid-post-token` is used.
- `get_converted_original`: converted GET with original token carriers.
- `get_without_token`: converted GET with CSRF-like query/header token carriers removed.
- `get_changed_token`: converted GET with CSRF-like query/header token carriers replaced by a sentinel invalid value.

The important evidence is differential. A valid token working more than once is not, by itself, a CSRF bypass: many CSRF tokens are reusable session tokens. Stronger evidence is: POST rejects a missing or changed token, while the converted GET still matches the original POST response with the token missing or changed. That pattern is reported as a `GET token validation gap` in live replay results.

The live results table includes separate `POST Token Evidence`, `GET Token Evidence`, and `Method Gap Evidence` columns. Use these columns before citing a gap:

- `POST rejects missing token and changed token`: the live POST baseline worked, and invalid-token POST variants did not match it.
- `POST baseline non-success; no POST-token evidence`: the original POST replay failed, so the run cannot tell whether POST-side CSRF validation works.
- `no POST token enforcement observed`: missing/changed-token POST variants still matched the original POST response.
- `GET accepts missing token and changed token`: invalid-token converted GET variants still matched the original POST response.
- `GET bypasses POST rejection for missing token and changed token`: the strongest pairwise evidence; the same invalid-token condition is rejected by POST but accepted by converted GET.
- `no pairwise GET-over-POST token gap observed`: GET may still accept an invalid token, but not for a token variant that POST rejected in the same run.

For a strong CSRF method-gap claim, prefer cases where POST evidence shows rejection of invalid tokens and GET evidence shows acceptance of invalid tokens. If GET accepts invalid tokens but POST evidence says `no POST token enforcement observed`, the endpoint may still be interesting, but the run does not show that the weakness is GET-specific.

## WCD Confirmation Test

`artifact/analysis/wcd_confirmed_test.py` uses the bundled
`artifact/mide/lib/wcde.py` module against high-confidence interchangeable
converted GET endpoints. For each endpoint, it generates WCD payload URLs with the
`.css` extension across every `WCDE.MODES` entry. It does not stop after the first
confirmed mode; every mode is tested so the output can show which path-confusion
variants worked.

Run a dry plan/check with:

```bash
artifact/mide/.venv/bin/python artifact/analysis/wcd_confirmed_test.py output/batch-<timestamp> --dry-run --limit 5 --latex
```

Run the live confirmation pass with:

```bash
artifact/mide/.venv/bin/python artifact/analysis/wcd_confirmed_test.py output/batch-<timestamp> --latex
```

Important outputs:

- `wcd-mode-results.csv`: one row per endpoint and WCDE mode, including the generated attack URL, response statuses, cache-status heuristics, route-match similarity, body-match result, cache-hit result, and output class.
- `wcd-endpoint-summary.csv`: one row per endpoint, with whether any mode confirmed WCD and which modes confirmed it.
- `domains/<domain>.json`: detailed per-domain artifact containing every normal, inducing, and probing request/response exchange, including request URL, request headers, request body, response status, response headers, response body, redirect chain, and per-mode classification metadata.
- `wcd-output-classes.{txt,tex}`: distribution of the WCD output labels across all mode results.
- `wcd-confirmed-modes.{txt,tex}`: confirmed result counts by WCDE mode.
- `wcd-endpoint-summary.{txt,tex}`: compact endpoint-level table.
- `summary.json`: machine-readable aggregate counts, tested modes, extension, and dry-run flag.

The output classes are used as follows:

- `not_tested_safety`: dry-run rows that were planned but not executed.
- `not_dynamic`: the live normal converted-GET response could not be fetched, so WCD could not be evaluated.
- `normal_url_already_cacheable`: the unmodified converted GET already appears cacheable, so WCD-specific attribution is ambiguous.
- `wcd_payload_not_routed`: the WCD payload URL did not reach an equivalent dynamic response.
- `wcd_payload_static_error`: the WCD payload URL returned a static-looking error response.
- `wcd_miss_no_hit`: the payload routed, but the probe response did not show cache-hit evidence.
- `wcd_hit_but_body_mismatch`: cache-hit evidence was present, but the cached body did not match the induced body.
- `wcd_confirmed_*`: cache-hit evidence was present and the probe body matched the induced payload response; the suffix describes the sensitivity markers observed in the cached response.
