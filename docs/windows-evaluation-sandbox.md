# Windows evaluation process boundary

`tools/windows_evaluation_sandbox.py` is a reusable host for a frozen converter
in an **existing** task-specific AppContainer. It creates no profiles, ACL grants,
checkouts or global configuration. Native qualification is still required; its
protocol tests are not proof that a real Windows converter was isolated.

The controller supplies a task-owned interpreter, compiled entrypoint, fresh job
containing only the copied `input.stl`, units, owned root and existing profile.
Runtime/application permissions must already be read/execute only; the job needs
its scoped modify grant and low-integrity label. All numerical pools are one,
the complete process job has one CPU and below-normal priority, and no inherited
handles or capabilities are supplied. Source and original STEP paths never go
into converter arguments or environment.

The initial process is created suspended, assigned to a kill-on-close job with
no breakaway permission, and independently audited by the host before resume.
The host queries its job membership, AppContainer token flag, exact profile SID
and zero capability count. Completion-port child notifications receive the same
host audit. Converter-writable audit files are not trusted. A missing process
notification or an exited child that cannot be audited fails qualification;
the audit count must match the job's total process count.

The host holds a read-only, non-inheritable input handle without write/delete
sharing until cleanup finishes. It returns only after the job reports no active
processes **and every retained process handle is signaled**, retaining the actual
root exit code and authenticated process records. Native probes showed that job
accounting can reach zero before process termination completes; zero alone is
insufficient to protect grading from existing output handles.
A root exit notification does not finish the run: descendants must still drain
within the original deadline. A failed audit, timeout or native error terminates
the job and waits for zero active processes before releasing the input.
Failure to drain is an isolation failure, never a graded conversion result.
This closes the prototype's root-exit/descendant-write race: ACL revocation alone
does not invalidate handles a still-running child already opened.

Before production integration, the controller must:

1. Verify the pinned converter manifest and every runtime/application file before
   execution, and check them again before publishing a case result. Never grant
   reference roots or reuse a writable runtime. Preserve existing artifacts.
2. Provision and track grants only on new, explicitly task-owned job directories;
   revoke their AppContainer grants after the job drains and before grading.
   Refuse output links/junctions and seal/hash actual retained outputs.
3. Freeze the grader's source **and dependency binaries** independently. The
   current `runtime_identity()` package-version inventory does not hash OCP,
   Point Cloud Utils, SciPy or other installed dependency files.
4. Include backend, host source, pinned converter manifest, sealed grader/runtime,
   CPU/pool/deadline contract and input hashes in case/cache identity. The existing
   non-isolated cache must not qualify as a new isolated evaluation.
5. Add an explicit conversion adapter to `benchmark_multires_corpus.evaluate`.
   Grade the retained output only; do not run a second conversion. Keep original
   geometry gates and publish cache records only after every identity and cleanup
   check passes. Record crashes/timeouts/isolation errors separately.

The pure protocol checks cover host-audit ordering, wrong root/child tokens,
root exit before job zero, orphan timeout, missing notifications, failure to drain, input-lock
lifetime, nonzero exits, owned paths and bounded arguments. The native checks
below qualify the tested process-boundary paths. Production controller integration,
an independently frozen grader and complete isolated STEP-to-ten-STL-to-grading
flows remain required before claiming the reference-isolation workflow is complete.

## Native qualification so far

The final host source SHA-256 is
`f5d5fc02427af7e0d8a5df1a5b14372e930809dac96f257cd141a7b0da43db36`.
An actual frozen conversion of development `5b7f2dd6f8dee0946745-q00`
completed with unchanged strict/reference acceptance, no facets and an unchanged
input hash. The host authenticated eight job processes: three Python workers,
three Windows console hosts and two command helpers. All had the exact expected
AppContainer SID, job membership and zero capabilities. The job grant was revoked
before host-side grading of the retained STEP, without a second conversion or
shared cache publication; the sealed converter/runtime hashes were unchanged.
This was one export of one source, not an overall recovery rate.
Conversion took 12.16 seconds in this observation. Independent reference grading
measured 0.07442 mm maximum deviation, 0.26121% volume error and 0.03770 mm maximum
dimension error against the unchanged strict gates. These observations describe
this one case, not a broader performance or yield claim.

Separate compiled access probes confirmed original STEP and host-only sentinel
reads denied with WinError 5, native OCP STEP read returning `IFSelect_RetError`,
and input write/delete denied with WinError 32 while the host read lock remained.
An earlier orphan timeout probe exposed the accounting-zero/unsignaled-process
race described above. The final source was retested after retaining synchronization
handles: its two-second diagnostic timeout terminated the job, observed zero
active processes, and waited for all four actual process observer handles to
signal before input unlock and grant revocation. The held-file child's delayed
write did not occur. Both final native probes preserve unchanged input and sealed
converter/runtime hashes; neither creates a shared benchmark-cache record.

All native attempts, outputs, exact probe source snapshots and prior host versions
remain local under `experiments/agent-native-host-qualification/`, with a concise
`HANDOFF.json`. Seventeen protocol tests pass; a symlink-creation check is skipped
where Windows requires unavailable authority, and Ruff passes. No runtime or
application grants, existing profiles or global configuration were changed.
