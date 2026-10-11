# Changelog

## Unreleased

- Preserve enclosed STL cavities and islands when normalizing mesh orientation and
  assembling analytic or faceted STEP solids. Verification now checks both shell
  and solid counts, so nested positive-volume bodies cannot pass as a hollow part.
- Reject NaN and infinite STL vertices before mesh processing can alter them,
  while keeping cleanup overlays tied to original input triangle numbers.

- Exposed STL export units and output dimensions before conversion, with a
  metre-unit suggestion for tiny imports and clearer upload connection errors.
- Recovered small tangent cylinders using smaller local fitting samples and
  fixed cone fitting when the estimated apex lies far outside the mesh.
- Added conical-bore recovery for thin layered parts, including countersinks at
  both ends. The existing STEP geometry and curved-surface checks still apply.
- Recovered spherical caps with a circular rim when ordinary face trimming
  fails, retaining the sphere as an analytic CAD surface.
- Completed the September 18 evaluation of 556 distinct STL files. The original
  500 retain 497 geometry passes and improve to 383 strict CAD passes. On 56
  additional files, verified non-faceted results improve from 13 to 19, with
  strict CAD recovery increasing from 5 to 7 of 36 reference-backed cases.
  No previous pass is lost. See the [additional-design report](docs/design-evaluation-2026-09-18.md).
- Added constant-section and layered-extrusion recovery with checks that retain
  recognized curved surfaces. Corrected shallow cylinder fits that absorbed flat
  walls beside small fillets.
- Added adaptive simplification for dense faceted fallbacks. Geometry checks use
  the original input, and the browser distinguishes verified facets from recovered
  CAD surfaces.
- Kept conversion progress running until the supervising worker saves its final
  result. Saved-run pages resume polling while a conversion is active.
- Added local hostname and browser-origin checks and shared queue limits for
  background conversions, synchronous conversions, and feature edits.
- Corrected repository links and the minimum supported Node.js version.
- Completed a reproducible 500-file release evaluation: 497 geometry passes
  (99.4%) and 382 strict CAD passes (76.4%), up from 461 and 335 respectively.
  No previous geometry or CAD pass was lost. Three malformed inputs remain
  unverified; near-100% CAD recovery remains unmet. See the
  [release report](docs/release-report.md) and its per-file results.

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
