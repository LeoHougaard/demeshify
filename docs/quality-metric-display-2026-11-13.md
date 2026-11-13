# Display measured precision and missing quality honestly

The real reconstructed V41 result has a positive P95 distance near 0.000013 mm.
Rounding that to `0.000` hid its nonzero value, while a measured local maximum
near 0.0845 mm was absent from the product details.

Positive values below display resolution now show a bound: `<0.001` mm for
surface distances and `<0.01` percent for volume difference. Exact zero remains
zero; missing, nonfinite, negative or nonnumeric measurements remain unavailable.
Advanced details show the maximum available measured deviation from the report's
global and local samples. It is a sampled result against the imported STL, not a
certified global bound or a comparison to unavailable original CAD.

New imports clear these metrics along with prior results and exports. Rendering
a report with no score also clears the distance and volume fields, preserving
`N/A` when volume comparison is explicitly unavailable. No geometry, scoring,
quality acceptance, navigation, layout or backend behavior changes.

Actual saved-run browser checks cover the recovered smooth result, truthful
faceted fallback, unavailable scores and new-input invalidation. The faceted
fallback remains labelled incomplete CAD recovery even when its measured STL
distance is tiny. Native controls were used; no conversion ran for this display
change. Local screenshots/action counts are retained in
`benchmarks/multires-1000/experiments/product-flow-agent/metric-truthfulness-20261113/`.
The later shared formatter also makes sub-resolution volume values explicit.

JavaScript syntax, formatter edge cases and the production build pass. The full
repository CI must qualify the committed UI source. No generated bundle, private
fixtures or downloaded geometry are included in the commit.
