# Preserve completed evaluations on Windows paths

The first progressive full-corpus pilot completed isolated conversion, original
reference grading and final per-case checks successfully. Its attempt record
reports a strict pass. Saving the completed-cache envelope then failed because
the atomic temporary copied the target's 64-character fingerprint into its own
name: the valid target path was 232 characters, but its temporary was 270.

Atomic writes now use a short random sibling name, preserving exclusive creation,
flush/fsync and atomic publication. Existing-target protection, path ownership,
cache fingerprints, original reconstruction gates and deadlines are unchanged.
No global Windows setting or long-path policy is changed.

A regression exercises a real 245-character destination with an enforced Windows
path-length boundary, checks the full JSON round trip, refuses replacement of an
existing exclusive target and verifies ordinary atomic replacement. The original
pilot outputs, attempt record and failed-launch trace remain local. A storage
failure is not a converter solve failure or a completed full-corpus baseline.
