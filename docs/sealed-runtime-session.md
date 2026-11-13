# Live sealed runtime sessions

`tools/sealed_runtime.py` provides `SealedRuntimeSession`. It is a Windows-only,
live context manager, not a timestamp cache, serialized certificate or permission
to skip verification. It creates no ACLs, profiles, services or global settings.
The caller supplies explicitly owned immutable artifact/runtime roots and pinned
SHA-256 catalogs. Do not pass workspace roots, parent directories, fixture CAD,
private inputs, result directories or mutable run/cache folders.

```python
from tools.sealed_runtime import SealedRuntimeSession

with SealedRuntimeSession(
    {runtime_root: pinned_runtime_files, converter_root: pinned_converter_files,
     grader_root: pinned_grader_files},
    module_sha256=pinned_loaded_seal_module_sha256,
) as session:
    session.check()
    # Run a case with the already reviewed sandbox/controller.
    session.check()
    # Repeat only while this exact live session remains open and unchanged.
```

Roots must be absolute, distinct and non-overlapping. Catalog keys use POSIX
relative filenames. Escaped paths, case aliases, compressed CAD extensions,
missing/extra files, hard-linked files, links, junctions and other reparse points
are refused. Named NTFS alternate data streams are also refused. Metadata and
stable volume/file identities are read for root ancestors, but no handles are
held outside the explicitly supplied roots. Filesystems without the required
stable identities, stream enumeration or change notifications fail closed.

On entry the session opens ordinary files with `GENERIC_READ` and
`FILE_SHARE_READ` only. These handles deny write and delete sharing, including
rename/replacement, and remain live until close. Existing writers and writable
memory mappings prevent acquiring the seal. Each expected file is fully hashed
through its own held native handle. Directory handles block root/subdirectory
rename. No expected byte hash is reused without these live OS locks.

Main-stream locks alone do not prevent adding a new filename or an NTFS named
stream. Each root therefore has an asynchronous recursive directory monitor.
Namespace, attribute and security changes latch invalidation, including a file
or alternate stream added and removed between catalog checks. Notification
failure/overflow also invalidates the session. Deferred directory timestamps
from ordinary reads are not interpreted as code changes. `check()` verifies the
pinned loaded seal-module hash, pending notifications, complete names/attributes,
file identities and absence of named streams. It scans metadata, not file bytes;
the existing bytes are protected by the held write-blocking handles. It checks
notifications again after scanning to detect concurrent catalog mutation.

Use `check()` before and after each case, and before committing a batch's
results/cache records. Context exit checks again before releasing locks. Any
change invalidates and closes the session. Check/identity methods then refuse
reuse; the object cannot be copied, pickled or reopened. The `identity` string
records pinned catalogs/code/protocol for evidence, and cannot recreate or
authorize a session in another process. It is not proof after the handles close.

`require_catalogs(required)` combines one live check with exact required-root
catalog comparison and returns the trace identity. It allows other separately
sealed roots but refuses an omitted or different file catalog and invalidates
the session. This is how the converter adapter binds reuse to its complete
manifest/license/application/runtime requirements; see
[locked artifact verification](locked-artifact-verification.md).

Only handles opened by the session are closed, including failure paths. New
read handles and isolated `-I -B` Python bytecode imports remain possible. Callers
must route Python/application/Matplotlib temporary writes outside immutable
roots, and create required grants before sealing. This does not replace process
token/job auditing, input locks, process-tree draining, output sealing, reference
separation or the strict reconstruction gates. It has not been integrated into
the original frozen converter/grader artifact code. The later
[actual controller workflow](isolated-evaluation-workflow-2026-11-13.md) now uses
the live seal in the host while the child grader retains full verification.

Costs are intentional: one handle per catalog file, one per directory, plus a
monitor directory handle/event per root, and one initial full hash pass. A runtime
with roughly 11,000 files therefore needs more than 11,000 live handles and
metadata entries. Per-check work scales with catalog size and NTFS stream
enumeration. A full 11,280-file qualification measured 8.14 seconds for initial
hashing/locking and approximately 2.18 seconds per live check, with 11.3 MB added
RSS and all 12,836 owned handles released. These are session measurements, not
whole-conversion speed claims. A directory-notification cancellation that
fails to settle within two seconds fails closed: owned handles are closed, while
its native IO buffers are retained until process exit to avoid freeing memory
still referenced by the kernel.

Seventeen focused checks pass locally on Windows, including native tiny-file
write/truncate/delete/rename denial, read/import allowance, writer/mapping
refusal, transient file/stream invalidation, rejected hard links/junctions,
exception/hash-mismatch cleanup and post-close refusal. Protocol checks cover
unsafe catalogs, module identity mismatch, pin copying, serialization refusal
and non-Windows failure. These tiny checks alone do not qualify a conversion or
OS isolation; the separate full-runtime and actual-workflow evidence above does.
Existing artifact manifests remain unchanged.

```powershell
python -m pytest tests/test_sealed_runtime.py -q
python -m ruff check tools/sealed_runtime.py tests/test_sealed_runtime.py
```
