# Experimental STEP preview tessellation

`app.step_preview.export_stl_preview(shape, destination, *,
linear_deflection=0.01, angular_deflection=0.04)` writes a binary STL and
returns its triangle count. The product exporter uses it for valid, closed
reconstructions after STEP export and round-trip validation. It remains
independent of reconstruction and strict grading.

The exporter requests absolute deflection of half the existing sewing
tolerance, capped at 0.01 mm, with angle 0.04 radians. An unchanged source-mesh
fallback still copies its original STL when its units permit; incomplete or
invalid diagnostic outputs retain their previous export path. Preview export
does not alter the delivered STEP, repair geometry, or qualify a reconstruction.

Coordinates and native face orientation are preserved, including an intentional
reversal. There is no implicit scaling, centering, unit conversion, orientation
repair, or tolerance modification. Deflection is absolute in the supplied
shape's coordinate units. The default is 0.01 mm when the caller already uses
millimetres; callers control other units through their shape and deflection.

The helper meshes a separate complete native copy with copied geometry and no
copied mesh cache. It sets `ForceFaceDeflection` and
`EnableControlSurfaceDeflectionAllSurfaces`, retains surface-deflection control,
and disables parallel meshing. These existing
[OCCT 7.9.3 parameters](https://github.com/Open-Cascade-SAS/OCCT/blob/V7_9_3/src/IMeshTools/IMeshTools_Parameters.hxx)
allow analytic face interiors to honor the requested deflection despite large
boundary uncertainties; the
[implementation](https://github.com/Open-Cascade-SAS/OCCT/blob/V7_9_3/src/BRepMesh/BRepMesh_Deflection.cxx)
still accounts for mismatched edge endpoints.

Every face must contribute finite, nondegenerate triangles with valid indices.
This includes checking their actual binary STL coordinate precision. Loose
geometry outside faces is refused instead of discarded. Only after this check
does the helper stage an STL. A false native writer result, truncated file or
triangle-count mismatch raises an error. An existing destination survives a
failure; successful publication is atomic.

Full repository CI exposed a valid spherical-cavity regression: the native
mesher emitted four exactly zero-area pole triangles, causing the preview guard
to reject the otherwise valid analytic result. The helper now removes only
artifacts with repeated, exactly identical native coordinates and a certified
collapsed UV edge. Five native surface/derivative checks establish a constant
3D image and null edge direction. Ordinary periodic seams, collinear triangles
and positive-area triangles lost at binary precision remain refused. Pruning
affects only the separate copied triangulation; no native geometry or tolerance
changes. Each face must still retain valid triangles, and writer completeness
is checked against that cleaned mesh.

The original unchanged spherical-cavity conversion test and sixteen preview
checks pass on an exact original-head application overlay with only this helper
recompiled. New controls retain both shell orientations, watertight winding and
the original BREP bytes, and refuse regular-seam and collinear degeneracies.
The failed CI, original child traceback and current/legacy comparison remain
preserved separately. Full CI must qualify the updated PR head.

Eleven small checks cover an analytic cylinder with a preexisting mesh and
nonzero native tolerances, a through-hole, two separate solids, translated and
explicitly scaled coordinates, both face orientations, an incomplete mesher,
false/truncated writer results, loose geometry, invalid parameters and geometry
that collapses at binary coordinate precision. Tests reopen actual STL exports
and check winding, volume, components and hole topology. The caller's original
native shape, including its cached tessellation, remains byte-identical in the
source-preservation checks.

The one-face preparation experiment is separate from repository qualification.
At 3,530 identical native-face locations, absolute 0.01 mm with both flags
reduced maximum preview deviation from 0.073055 to 0.000229 mm. That face grew
from 101 to 3,126 triangles, about 4 to 38 ms in one observation. Boundary
triangle errors around 0.045 mm remained because native boundary geometry and
tolerances were unchanged. Relative deflection with both flags was rejected:
its triangle area greatly exceeded the native face area.

Raw BREP bytes for an isolated meshed copy can differ because unused neighboring
surface representations and bookkeeping change. The experiment checked 701
in-face geometry values, entity tolerances, area and wire/edge counts directly;
it does not establish byte identity of the meshed copy. The original delivered
STEP was never modified.

One reopened 55-face reconstruction was also compared with the production
preview at numeric deflection 0.01 and angle 0.04, with parallel meshing disabled.
Production uses relative deflection; this helper intentionally uses absolute
deflection. Both exports contained every face and reopened as watertight,
consistently wound meshes with one component and Euler characteristic 2.
The caller's whole BREP bytes remained identical, as did 8,898 sampled native
surface/curve/vertex values, all entity tolerances, area and volume.

At the same 8,892 valid native surface locations, maximum preview distance fell
from 0.068817 to 0.013538 mm. At 169 fixed locations on the under-resolved corner,
it fell from 0.068817 to 0.0000418 mm. Source-STL local coverage improved from
0.084500 to 0.044713 mm; reverse local distance improved from 0.073127 to
0.052247 mm. Both meshes retained identical dimensions. Export grew from
34,166 triangles / 1.71 MB / 0.209 s to 49,916 / 2.50 MB / 0.839 s in one
observation, including diagnostic instrumentation on the helper. Native volume
discrepancy increased slightly, from 0.00468% to 0.00548%. Directly inspected
whole and section renders show the corner chord replaced by the curved outline.
An instrumented repeat used CPU 0 and BelowNormal priority, with successful
Windows API calls checked. The combined qualification, mesh-analysis and render
process sampled a 539 MB peak working set; that is not helper-only memory.
These are preview measurements on one saved reconstruction, not converter
acceptance, guaranteed deflection bounds, or broad performance claims.

A serial saved-STEP check added two distinct source controls: a dense curved
plate with ten cylinders and a genus-one hole, and a stepped conical/cylindrical
part with multiple planar loops. Both variants retained all native faces,
identical dimensions, watertight coherent winding, one component and the original
Euler characteristics (0 and 2). Caller BREP bytes and native geometry/tolerances
remained unchanged. The plate's maximum native surface distance stayed
0.001144 mm; reverse STL local distance improved from 0.004220 to 0.003482 mm,
while its source-to-preview RMS and volume discrepancy increased slightly.
Its triangle count grew from 106,446 to 108,722; the stepped part retained
4,130 triangles and identical measured distances. The dense worker peaked at
565 MB combined working set. All three serial cases took 30.94 s including
imports; failed plotting evidence was retained and recovered from saved STL
without rerunning native meshing. Recovered whole/section PNGs were directly
inspected. Preview fidelity is measured independently of core reconstruction.

An additional integration check exports a translated through-hole solid through
the actual product exporter and reopens both STEP and STL. It checks native
validity and volume, one component, watertight winding, hole topology, unchanged
dimensions and placement, and that the original shape remains unmeshed.

One fresh browser workflow then used the intended V21 reconstruction core and
the integrated exporter on a real curved-profile development STL. Import,
conversion/progress, input/result views, projection controls, Fit view, quality
details, STEP download, independent native reopening and saved-run reopening
passed. Exactly one conversion produced a valid one-solid, one-shell STEP with
19 faces and zero facets, plus a complete 28,406-triangle binary preview. The
download hash matched the generated STEP; reopening transferred every STEP root.
The UI retained the three residual B-spline warning and accurately displayed
P95 0.019 mm, maximum 0.055 mm and volume difference 0.37%. These are application
checks against the STL, not reference-CAD grading. Both fresh and saved views
had empty browser error/warning logs. Task-owned tabs and server were closed.

Native orbit, pan and wheel zoom remain unverified: the primary browser tool
delivered plain-left drags and no wheel events, while the fallback browser host
was unavailable. No synthetic input or control remapping manufactured a pass.
The original UI controls and bindings are unchanged by this exporter change.

Broader output density and separate helper memory remain unqualified. Per-face
presence and triangle validity alone do not certify those properties. This
helper cannot qualify reconstruction or change any strict acceptance gate.
