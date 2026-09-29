# InstSci browser execution layer

This document records the boundaries of the current checkout. The earlier
execution-layer design can be found at Git revision `a24cad5`; commands and
features described there must be checked against the current CLI before use.

## Runtime ownership

- `PublisherBatchDownloader` owns publisher navigation, capture, diagnostics,
  and PDF verification in the visible CloakBrowser runtime.
- The default `papers` and `publisher-batch` routes submit to a long-lived
  publisher broker. `run_records_with_context` reuses its existing context.
- Standalone `run_records` owns the contexts it launches and closes them.
- An attached diagnostic controller must preserve browser identity, storage,
  lifecycle, and download ownership. Institution credentials and human
  verification remain user actions in the visible browser.
- Final publisher capability claims require the screenshot-backed browser
  evidence described in `AGENTS.md`; an HTTP response is preflight evidence.

## Retry and recovery

`retry_policy.failure_category` classifies failures as `transient`,
`human_action`, `access_denied`, or `attention_required`. Unknown reasons need
inspection. The existing mixed `challenge_or_viewer_timeout` state requires
human attention because it can represent a CAPTCHA or WAF checkpoint.

Automatic retry is one additional pass over known transient failures. It waits
30 seconds before the pass, uses one worker, and waits 5 seconds between items.
The broker route retains the exact context object throughout this process.
`--no-retry` disables the pass and its cooldown. Final summaries distinguish
eligible records, actual retry attempts, successful retries, and remaining
failure categories; the last retry outcome supplies the current reason.

`instsci jobs resume JOB_ID` selects unfinished records, including records absent
from a partial final manifest. `--transient-only` limits selection to classified
temporary failures. Explicit recovery disables the old skip-attempted flag so
its selected records actually run. Complete login or verification in the
visible browser before resuming human-action records.

This policy controls retry scheduling. It does not solve verification prompts,
add a second browser runtime, or certify publisher access. Local mocked tests
cover scheduling, context identity and recovery selection; actual access
checks still follow `AGENTS.md`.
