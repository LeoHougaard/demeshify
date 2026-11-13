# Frozen isolated conversion adapter

`tools/isolated_conversion.py` adapts one pinned frozen converter artifact and
one hashed STL to `windows_evaluation_sandbox.run_converter`. Its API has no
original STEP, corpus manifest or source-model-name argument. Only the copied
`input.stl`, units and a fresh random job path reach the converter. It uses the
existing profile and unchanged qualified host source `f5d5fc…`, with the original
240-second conversion deadline, bounded cleanup, one CPU and numerical pools of one.

The adapter inventories the pinned converter/runtime before and after execution
and verifies its own, host and verifier source identities. It creates no profiles
and grants nothing to existing runtime, application, reference or source roots.
Only its newly created job receives a modify grant and low-integrity label. The
job's grant is revoked before any converter result is read or retained, including
on setup, timeout and process failures. Filesystem and native process failures
are explicit errors; they never become silently graded results.

The scoped permission helper uses an existing profile's derived SID and bounded
`icacls` calls. Its `/L` flag targets a symbolic link itself, as documented by
[Microsoft](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls).
Before recursive revocation it inspects the owned tree without following links.
If a link, junction, hardlink or other unsafe entry exists, it revokes only the
owned job-root grant and fails closed rather than recursively mutating external
security descriptors. Such an unsafe job is preserved for recovery; it receives
no complete-revocation attestation and cannot be graded or cached.

After the host has drained the whole job and all process handles, the adapter
checks authenticated SID/job/capability/CPU records and confirms the input hash.
It refuses output reparse points, symlinks, junctions, hardlinks, non-regular files
and escaped paths. A fixed whitelist of actual STEP/STL, reports, surface graphs
and diagnostic logs is hashed and retained. Missing required results, incomplete
bootstrap messages and report/input metadata mismatches are protocol errors.
Input metadata is independently parsed; this does not run another conversion or
establish reconstruction fidelity.

The existing caller-owned temporary output directory may contain only an
unchanged `input.stl`. Target files are created exclusively, never overwritten.
Actual retained bytes are checked against sealed hashes during and after copy.
The returned `ReconstructionReport` and `isolation-attestation.json` describe the
one conversion. Conversion latency, identity/output verification, artifact-copy
time and overall adapter time are separate. Failed
jobs, outputs and partial retained files are preserved; no cleanup is performed.

The controller can supply a finite monotonic `case_deadline`. Immediately before
native launch, the adapter clamps the converter allowance to the smaller of
240 seconds and the remaining whole-case budget. This can only shorten the
original timeout. An exhausted deadline never launches the converter and still
executes scoped grant revocation and identity rechecking. The controller owns
the original 360-second total case gate, including grading and final checks;
it must not terminate this adapter in a way that skips its cleanup.

The qualified native host is pinned by its exact source bytes. A repository-only
`.gitattributes` entry keeps that one file at LF on Windows checkouts, so Git's
system `core.autocrlf` setting cannot invalidate its qualification. The host's
source and global Git settings are unchanged. CI checks the checkout's actual
host bytes against the qualified pin.

Errors distinguish `isolation`, `timeout`, `converter_exit` and
`converter_protocol`. An isolation error takes precedence when a frozen identity
or revocation check fails, even if the converter also timed out. Consumers must
not grade or publish a cache record after any adapter error, and must never call
the converter a second time to grade its retained artifact.

The adapter has pure protocol tests with injected host, verifier and permission
services. They exercise real filesystem hashes, exclusive copies, hardlink and
reparse refusal, revocation ordering and failure paths, report consistency and
neutral converter arguments. These tests do not launch an AppContainer, mutate
ACLs, or qualify this new adapter's complete native lifecycle. The host's earlier
native qualification remains evidence for that exact host, not a claim that this
adapter is already a production-certified evaluation backend.

The later [actual controller workflow](isolated-evaluation-workflow-2026-11-13.md)
has exercised this adapter with three real conversions and frozen grading. The
earlier protocol-only qualification above remains distinct from that native
evidence. Full-corpus completion is still a separate requirement.

The integration contract remains:

1. Run a small native end-to-end adapter workflow and denial/cleanup checks using
   only the existing task profile, frozen artifact and fresh owned job grants.
2. Freeze grader source and actual dependency binaries independently; package
   versions alone do not freeze native geometry and distance calculations.
3. Include backend version, adapter/host/verifier source hashes, pinned converter
   manifest, frozen grader/runtime, input hash, units and deadline/pool contract
   in the case identity. Existing non-isolated cache results must not qualify.
4. Grade the retained STEP with all unchanged geometry, topology, no-facet,
   round-trip and timeout gates. Attestation is not a geometry grade or a corpus
   success claim. Publish a cache record only after final identity/output checks.

The full 1,000-source × ten-export baseline and sustained reconstruction hill
climb remain separate, unfinished objectives. This adapter publishes no cache,
relaxes no geometry gate and reports no self-awarded reconstruction score.
