# Keep product results attached to their input

Reopening a saved nine-face smooth result and selecting a different STL left the
old quality result, STEP tab and download links visible. Reloading restored the
old run from the URL. Those outputs belonged to a different model.

Selecting or clearing an input now immediately clears result/export/editor
state and the saved-run URL. A selection version guards late conversion progress,
report, preview and edit responses. New input dimensions and conversion readiness
belong only to the current selection. Saved artifacts remain on disk and the
original saved-run URL still reopens them.

Fit uses the viewer's existing camera framing through an Advanced control,
the F key and double-click. The shortcut ignores text, selection and editable
controls. STL and STEP viewers retain the same navigation implementation.

The production bundle was tested with a real copied saved success, a truthful
2,072-facet fallback and malformed input, without launching another conversion.
The downloaded success STEP had the same SHA-256 as the saved file and reopened
independently as one valid solid with nine faces. New-input and clear-input flows
remove exports/results/tabs/run URL; delayed progress and delayed preview fetches
cannot resurrect them. Reload and saved-run reopening were checked. Fit was
exercised through Advanced, native F and double-click; a focused selection control
did not trigger the shortcut.

Local reproducible flows, action counts, screenshots and downloaded-output hashes
are retained under `benchmarks/multires-1000/experiments/product-flow-agent/`.
The available drag tool did not support right/middle buttons; those navigation
checks used explicit synthetic pointer/wheel events and are not claimed as native
mouse automation. Some final repeat selector clicks timed out in the automation
host despite a hit-testable button; that limitation is recorded in the evidence.
The task-owned browser/server were closed. Build and JavaScript syntax checks
pass; repository CI separately qualifies the committed source.
