# Evaluation timeout, resource and resume semantics

The controller now preserves a conversion's validated native lifecycle
attestation and timings before host grading starts. If host grading exhausts
the original 360-second case deadline, timeout classification precedes execution
certification. The original failure category remains in `failure_origin`.
Accepted remains false; source readiness must be established independently
before an execution timeout can become a scored strict failure. This preparation
does not reconstruct again or extend the original solve deadline. Isolation,
fixture and non-timeout grader protocol errors remain uncertified.

A warm grader's explicit `grader_resource_limit` has separate semantics.
Stable retained conversion outputs and a confirmed drained grader worker may
certify execution for resume, but `accepted`, `certified` and
`solver_outcome_scored` always remain false. A readiness receipt from another
export cannot turn this host resource failure into a solver failure. A partial
child result suggesting an invalid fixture is not consumed after the resource
exception, and no fixture exclusion is created. Missing drain evidence prevents
execution certification and completed caching.

Resource-limited reference-only preparation likewise leaves source readiness
unknown. Its evidence is retained, and ordinary resume does not repeat that
expensive preparation or conversion. Retrying grading/preparation requires an
explicit newly qualified workflow; it cannot silently reuse a fresh solve
budget or overwrite the original outcome.

Already scored, source-ready consecutive cache records can now be read as one
transaction with full live identity checks at its boundaries. Every record
still checks its checksum, frozen case identity, both fixture hashes and actual
retained output hashes. The live seal continues to block writes/deletes and
monitor transient catalog changes. The transaction performs no grading,
conversion or readiness work and stops at the first missing, unscored or unready
record. Fresh-case limits therefore retain their existing meaning. Reused
measurements remain explicitly historical.

The controller source hash changes cache identity. Older records are preserved;
they are not recertified under this implementation. The historical unscored
resource-heavy attempt remains unchanged. A fresh full conversion/reference
grading workflow and an independent-process cached-prefix resume subsequently
passed on the final pinned warm pipeline: one strict pass, then zero fresh
conversions/grades with identical retained outputs and native attestation.
This is a one-case workflow proof, not a new full-corpus evaluation.

Focused controller checks exercise late host timeouts, known/unknown/unready
sources, retained attestation, no reconversion on resume, resource failure with
and without drain evidence, misleading partial child results, ready-source
receipts, cache tamper/fixture checks and transient native catalog changes.
No reconstruction, validity, deviation, dimension, volume, no-facet or original
120/240/360-second acceptance gate changes.
