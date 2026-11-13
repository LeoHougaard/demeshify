# Multiresolution outcome reporting

The runner preserves its existing summary keys and adds explicit coverage,
source-model denominators, failure categories, facet totals, per-export-quality
measurements and fresh/reused performance groups. These describe the actual
retained results; they do not change any conversion, grading or acceptance gate.

A source passes only when all ten distinct presets pass. The rate among fully
evaluated sources is separate from the full expected-source rate, which remains
absent until all expected sources have all ten results. Partial case rates are
not full-corpus rates. Each numeric distribution reports its available and
missing/invalid case counts. Missing measurements are never replaced by zero.
Whole-run summaries refresh after the first case, every 100 cases, the final
case and each explicit batch boundary. The summary's evaluated count states its
actual coverage; individual result records remain durable after every case.

Reused latency and RSS values were measured in the earlier conversion. They do
not measure the current cache lookup. Unmarked historical rows have a separate
group. Memory is a one-second sampled controller process-tree RSS measurement,
including its grader/converter children, not an exact converter peak.

Reference-only failures and outer-worker `crash` statuses are categorized
explicitly. Failure categories may overlap. The reference comparison still does
not establish exact feature history or general topological equivalence.

Saved reports can be summarized without reconversion:

```powershell
python -m tools.multires_summary old-results.json --manifest frozen-manifest.json --output historical-summary.json
```

The new report pins input hashes and declares that it neither performed a
conversion nor certified a cache identity. Existing outputs are preserved.
Adding reporting code to the live runner naturally changes its existing full
source fingerprint. The identity algorithm is unchanged; historical outcomes
must not be relabeled as fresh or as matching the new converter identity.
