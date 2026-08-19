# Changelog

## 0.1.0 beta candidate - 2026-08-19

- Renamed the project to STL to STEP Converter.
- Removed the unused language-model integration, prompt controls, and related
  configuration. Both reconstruction modes now use local geometry only.
- Added one-command Windows, macOS, Linux, and private Codespaces startup.
- Added bounded background conversion, child-worker progress, atomic reports,
  stricter request validation, opaque run IDs, and browser security headers.
- Fixed failed jobs being shown as complete and non-millimetre fallback previews
  being exported at the wrong scale.
- Added mobile layout, keyboard-accessible controls, invalid-file recovery,
  polling retries, saved-run links, and a built-in sample conversion.
- Added CI, security and contribution guidance, public beta limits, and tests for
  the asynchronous conversion and download flow.

This is a source release candidate. The Git tag and GitHub release should be
created after the CI workflow passes on the release commit.
