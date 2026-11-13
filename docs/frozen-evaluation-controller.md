# Frozen reconstruction evaluation controller

`tools/frozen_evaluation_controller.py` runs a resumable, serial pipeline:
one isolated conversion, retained-output sealing, then the frozen grader's actual
CLI against those outputs. It never grades by running a second conversion and
never loads a corpus case into the converter API. The converter receives only a
neutral copied `input.stl`, its hash, units and an owned output directory.

Launch production runs with the **pinned grader runtime's Python**, `-I -B`:

```powershell
& '<PINNED-GRADER-RUNTIME>/python.exe' -I -B tools/frozen_evaluation_controller.py '<FROZEN-MANIFEST>' --converter '<CONVERTER-ARTIFACT>' --converter-sha256 '<PIN>' --grader '<GRADER-ARTIFACT>' --grader-sha256 '<PIN>' --profile 'Leo.Demeshify.Evaluation.20261112' --owned-root '<OWNED-EVALUATION-ROOT>' --output '<OWNED-EVALUATION-ROOT>/baseline.json' --batch-cases 3
```

The pinned grader must contain the current retained-output ENTRY contract,
including independent reference-fixture readiness. Historical grader artifacts
without that guard remain historical evidence; the controller refuses their
incompatible ENTRY before loading application code. `--split` selects development,
validation or holdout without changing source/family assignments. All variants
and duplicate-geometry families are checked for split consistency before filtering.
No controller logic tunes against holdout outcomes.

Both normal grading and source-only readiness bind `scratch_root` to the existing,
unlinked owned evaluation root in their hashed configuration. The grader creates
its temporary work directly there, avoiding the excessive nested Windows paths
of per-attempt grader work. This changes neither fixture paths nor acceptance gates.

The bootstrap uses standard-library imports until it verifies the grader manifest,
all runtime files, application bytecode and dependency declarations. It requires
the declared runtime interpreter and isolated/no-bytecode flags. Only then does
it prefer the sealed grader application path and import the adapter's `app.schemas`,
`app.mesh` and bounded-process helper. Every loaded `app` module must originate in
that artifact before and after each case. Working `app.surface_graph` or
`app.surface_brep` edits therefore do not change the frozen baseline.

Controller, adapter, host, verifier, resource sampler and summary code must remain
fixed. Case fingerprints include their hashes, full case settings and units,
pinned converter/grader manifests, actual runtime/dependency inventory hashes,
platform, numerical pools, profile and deadline contract. Package version strings
are not substituted for binary inventories. A live `SealedRuntimeSession` hashes
every catalog file while holding read handles that deny write/delete sharing,
covering both whole artifact roots (including manifests/licenses/declarations)
and every declared runtime. Shared runtime catalogs must agree exactly. Subsequent
before/after-case checks use that same non-transferable live object, exact catalogs
and directory notifications. A digest or serialized trace is never authority to
skip verification. The adapter receives the actual session object; the frozen
grader child retains its full independent verification. All owned session handles
close in `finally`, including initialization, cache and resume-contract failures.
This policy is explicit in the fingerprint and is not an mtime-only cache.

One monotonic **360-second whole-case deadline** covers conversion and grading.
The adapter receives that deadline and can only shorten its existing 240-second
native conversion maximum. Its cleanup still drains the converter job, waits for
all process handles and revokes the job grant in `finally`; the controller never
hard-kills the adapter in a way that skips revocation. Only after confirmed cleanup
and sealed output checks may the reference grader start, with the remaining case
budget, capped at 360 seconds. Final checks crossing the original case deadline
produce a timeout rather than acceptance. Cleanup and durable failure evidence
may finish after expiry; they do not create an accepted result.

Actual STEP, report and permitted diagnostics are pinned before grading. The
grader's result must attest the exact grader manifest/runtime/case hash, fixture
hashes and retained output hashes, and confirm no second conversion. Original
STL/STEP hashes, tool/artifact identities, neutral input and retained outputs are
checked again before publication. The existing strict geometry, topology,
no-facet, round-trip and timeout gates remain inside the pinned grader.

Reference readiness errors are **fixture errors**, not Demeshify solve failures.
Their diagnostic records populate `*.fixture-exclusions.json`, keyed by source
hash and grader pin, so deficient fixtures can be excluded and replaced while
preserving the original input and evidence. Failed conversions without a reference
readiness proof remain explicitly unscored execution outcomes. An execution can
be integrity-certified and resumed without pretending its solver result was scored.
Normal grades seed an immutable per-source/per-grader readiness receipt. A failed
conversion first reuses a matching receipt; otherwise the controller invokes the
same frozen CLI's `--reference-readiness` mode, which reads only the source STEP,
performs no conversion and uses no retained reconstruction. This independently
qualifies the fixture for an already-failed execution. It never changes a failed
conversion into a pass or gives it additional solver time.

Fixture preparation after an attempt is timed separately from the original
360-second case and converter latency. If preparation is unavailable or times out,
the verified execution remains explicitly unscored. Resume retries only readiness,
using the preserved execution and outputs rather than reconstructing again.
Successful upgrades publish a new immutable `.ready.json` companion, preserving
the original unscored cache record. Fresh preparation counts toward the batch
work limit, while `reused=true` keeps historical converter timings honest.
Source readiness receipts are shared across the source's ten variants and across
converter candidates only when the source hash, expected topology and pinned
grader/runtime/process conditions match. Changed or conflicting receipts are refused.

The summary separates execution coverage, scored coverage and unscored cases;
strict source-model rates use only scored cases. A full baseline is incomplete
while fixture readiness or scoring remains unresolved.

Each fresh case has a unique attempt directory. Completed records use a new
`frozen-controller-completed-v1` namespace, checksummed envelopes and atomic,
exclusive publication. No legacy non-isolated cache is reused. Changed records,
inputs, retained bytes or runtime/code identities are refused. Resume preserves
historical conversion/grade timings and labels them reused; lookup time is separate.
Failures and new fixture preparations count toward `--batch-cases`; unchanged reused
prefixes do not. Partial or interrupted
attempts never block a clean retry and are never silently treated as completed.

Per-case records and compact summaries are durable after each case; full snapshots
are written for the first case, each hundred, final and batch boundary. The current
correctness-first version recomputes detailed summaries each case. It samples
owned-process-tree RSS with the existing sampler and keeps conversion, retained-copy,
grader-copy, grading and whole-case latency separate. It stops at the existing
15 GiB storage floor and preserves all jobs, attempts and failed evidence.

Reporting uses `aggregate_details` for actual case/export outcomes, performance,
failure categories and source-model rates. Ten successful exports count as one
source design. An entirely evaluated small manifest does not satisfy the explicit
1,000-distinct-STEP / 10,000-STL objective. Artifact workflow flags remain false;
unit protocol checks do not claim native controller qualification or full-corpus
completion. Root must review and exercise the actual pinned CLI before promotion.

The later [actual workflow qualification](isolated-evaluation-workflow-2026-11-13.md)
records three fresh native conversions and frozen grades, the matched original
converter run, process/reference boundaries and the Windows scratch-path repair.
The explicit 10,000-case baseline remains incomplete.
