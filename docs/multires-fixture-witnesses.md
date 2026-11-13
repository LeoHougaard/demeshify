# Verify fixture metadata before freezing the corpus

The assembler previously compared only four fields with a materialization
record. Matching hashes could therefore conceal changed units, tessellation
settings, exporter versions, source/license metadata or ground-truth dimensions.

Each selected case must now retain all witnessed metadata. Units must remain
millimetres, tessellation must match its exact linear/angular preset, and exports
must remain absolute and serial. Exporter versions and source/license references
must also match the frozen collection contract. Triangle counts and source
dimensions, volume, solid counts and face-type metadata must match their records.
Missing historical fields produce an explicit error; settings are never inferred.
Source and STL bytes are still rehashed, and ten distinct matching case witnesses
are required. Selected record paths and byte hashes are retained in the report.

Historical exposure is a separate controller annotation: it can conservatively
move a witnessed split to development, never to validation or holdout. Indexed
public family/provenance annotations are applied only after raw export metadata
has passed witness verification. Their source path, actual Git blob hash, pinned
commit format and unchanged source/license identity are checked. This local check
does not independently re-query the public repository's commit tree.

Both existing collections store a license digest, label and URL in each record.
The primary collection does not store the license file path. A repeatable
`--license-file` option rehashes the available license text against its pinned
digest; public source indexes already provide their license path. Reports state
whether actual license bytes were checked. A complete `--freeze` requires those
bytes, rather than treating an unavailable historical path as verified.

```powershell
python tools/assemble_multires_corpus.py --collection PRIMARY-MANIFEST.json 980 --collection PUBLIC-MANIFEST.json 20 --source-index PUBLIC-INDEX.json --license-file PRIMARY-LICENSE.md --license-file PUBLIC-LICENSE.md --output NEW-SNAPSHOT.json
```

Existing snapshots and geometry are preserved. Atomic metadata snapshots now use
short unique temporary names, fixing a real Windows path-length failure where
the final SHA-256 filename fit but a repeated hash plus UUID exceeded the limit.
This changes no export geometry, reconstruction gate, source quota or exclusion.

The existing pass15 data was reassembled with this verifier: 869 distinct source
models, 8,690 STL inputs and 869 materialization receipts still qualify. Both
collections' actual license texts were rehashed successfully. Every case row is
identical to the preceding pass15 snapshot. This remains a partial 1,000-source /
10,000-input corpus, with no new exports or reconstruction evaluations performed.
Thirty-three pure metadata/controller checks and Ruff pass.
