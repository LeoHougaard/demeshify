# Qualified isolated conversion and frozen grading workflow

The actual controller completed three fresh development cases with pinned
converter and grader artifacts, one numerical thread and one CPU. Every case
finished conversion, drained all audited converter processes, revoked its scoped
job grant, retained the actual output, then graded that output against host-only
reference CAD. No second conversion supplied the score. All three passed the
unchanged strict gates, with zero fallback facets and matching supported topology.

This qualifies a small complete workflow, not the full 10,000-case baseline or
the experimental converter's promotion. Two passing controls were included.
The matched original converter scored two strict passes on these same three
cases. The new recovery is a 55-face analytic solid replacing a 2,072-facet
carrier; the carrier remains a geometry-preserving failure under the strict
no-facet gate. Both controls retain exactly the same reference maximum deviation.

| Development case | Candidate conversion | Whole case | Strict | Topology |
| --- | ---: | ---: | --- | --- |
| New difficult middle export | 17.25 s | 49.71 s | Pass | Match |
| Smooth-surface control | 14.11 s | 48.17 s | Pass | Match |
| Existing fine-export control | 34.46 s | 79.84 s | Pass | Match |

The converter artifact is source `032fae4fdff92fc82a4d88b13e1df90f2824e274`,
manifest SHA-256 `8e61535f08f9f86877f5f8a29aea514ac4870fa247f8ff934a45ab7135c4565f`.
It remains an unpromoted reconstruction experiment. The frozen grader is source
`e9e2497f272e97bd8d1b413a42b87bd33bcba799`, manifest
`9c136ef66d53fea52f1c0c3198f89426050872b2e234d9b0dafc8dcb680d928f`.
The grader source is reachable through
[PR #15](https://github.com/LeoHougaard/demeshify/pull/15), main
`1024313e58b06904c2f0eac649f0648efbb1468b`, whose
[actual merged-SHA CI passed](https://github.com/LeoHougaard/demeshify/actions/runs/43381623927).

The pinned original converter is source
`c84f7ac864a1a251bc944f1333c7f81105db96dc`, manifest
`abdbaa623da4241bbcb09bf09f5162df2ef8a630c10492771d0ace13bb0fbad9`.
Both runs used the same grader, tool hashes, pool/affinity settings, original
120-second analytic / 240-second conversion / 360-second whole-case limits,
input/settings identities and development cases. The recovered target's
reference maximum/local deviations are 0.069515 / 0.084552 mm, dimension error
0.097552 mm against 0.12 mm, and volume error 0.005589 percent. The two control
conversion observations are slower than baseline; these few measurements do not
establish a broad performance claim. Per-case times exclude initial session
construction. Sampled process-tree RSS peaked at approximately 1.16 GiB and
includes controller/grader processes, not just conversion.

The controller's real execution found a Windows native path failure that the
short protocol fixtures missed. Conversion outputs were complete, but deeply
nested grader scratch paths could not create `reconstruction.step`. All three
failed attempts remain intact and unscored. The corrected workflow binds a
short existing owned `scratch_root` into both normal and source-readiness request
hashes. It changes no global path settings, fixture or acceptance gate.

The live seal hashes every artifact/runtime file once under write/delete-blocking
handles and checks exact catalogs and directory notifications throughout the
case. The child grader still performs its own independent full verification.
The existing native process host retains exact source SHA-256
`f5d5fc02427af7e0d8a5df1a5b14372e930809dac96f257cd141a7b0da43db36`.
A repository-scoped LF attribute preserves those qualified bytes on Windows;
no global Git configuration or host source changed. Earlier native original-STEP
denial and orphan termination tests remain evidence for that exact host.

Source/grader readiness receipts allow a failed execution to become a scored
strict failure without reconstructing again. Unready sources populate an
exclusion ledger and remain unscored; unavailable readiness is retryable on
resume. These paths are covered by protocol tests. A new actual native converter
crash/readiness-upgrade scenario has not been deliberately induced in this run.

Reproduction and per-case records remain local under
`benchmarks/multires-1000/experiments/controller-v2/`:
`v41-inner.json`, `c84-inner.json`, `matched-inner-decision.json`, contracts,
checksummed completed records, source-readiness receipts and retained outputs.
The earlier failing controller-v1 attempts are preserved. See
[the controller command](frozen-evaluation-controller.md) for resumable CLI use.
No private geometry or generated corpus files are committed.

Validation: 113 focused controller/adapter/locked-identity/native-seal checks
pass, as does Ruff. The full local suite passed before the final scratch-root
correction, whose 23 grader protocol checks also pass. Repository CI must qualify
the reviewed controller commit separately. Artifact builder qualification flags
remain false because a generic artifact build does not certify a whole corpus.
