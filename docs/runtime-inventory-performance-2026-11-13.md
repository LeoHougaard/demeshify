# Preserve full identity checks with less path traversal

A retained-output replay of the original frozen grader spent 21.15 seconds
cumulatively in its two full identity checks, compared with 2.00 seconds of
reported geometry verification. The earlier 115.7-second outer grading
observation did not reproduce; this replay took 28.00 seconds under profiling.
No reconstruction, sampling or acceptance changes follow from that timing.

Runtime inventory now traverses checked directory entries with `os.scandir`
and builds relative keys directly. Every regular file still receives a resolved
containment check and a fresh SHA-256 hash. Links, junctions and other reparse
entries are refused before traversal; compound CAD suffixes remain forbidden.
Component-wise ordering matches the previous `pathlib` catalog, including
Windows case normalization. There is no timestamp cache or skipped byte hashing.

Three serial old/new comparisons against the same pinned 11,229-file Python
runtime produced identical dictionaries and identical key order:

| Trial | Previous inventory, seconds | New inventory, seconds |
| --- | ---: | ---: |
| 1 | 7.724 | 5.279 |
| 2 | 7.311 | 5.316 |
| 3 | 7.238 | 5.396 |

The mean fell from 7.424 to 5.330 seconds, about 28 percent for this inventory
operation. The comparisons used one CPU at BelowNormal priority and performed
no conversion. They do not establish whole-pipeline latency improvements. The
old function, exact commands, catalogs and raw timings remain locally under
`benchmarks/multires-1000/experiments/inventory-optimization-20261113/`.

Focused tests cover nested catalog/order equivalence, same-size changed bytes
with restored timestamps, compound CAD suffix rejection, empty/non-directory
roots, resolved escapes, and a real Windows junction whose target is preserved.
Converter and frozen-grader protocol checks also pass. A newly frozen grader
must include this reviewed helper revision; existing frozen artifacts and their
reported identities remain unchanged.
