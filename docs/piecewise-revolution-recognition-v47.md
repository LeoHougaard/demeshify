# V47 candidate: elementary supports at source meridian breakpoints

The new development failure a561 produces geometrically accepted, valid smooth
CAD, but two generic revolutions hide its elementary conical supports. Source
STL inspection shows constant-radius and distinct straight sloped meridian runs
with existing shared ring breakpoints. Original source STEP files and CAD
parameters were not used to design this candidate.

V47 starts from V41 graph recognition and V21 BRep construction. V46 and all
boundary/anchor candidates remain parked and preserved. It acts only when the
existing whole-region torus/revolution stage has accepted a generic revolution.
Source adjacency groups plateaus in the normal's axial component, proposing
straight meridian runs. Every whole proposed region must pass the unchanged
cone/cylinder node and normal validators; adoption is atomic across the original
region, so a failed piece cannot fragment an otherwise working revolution.

The proposal uses the caller's existing normal envelope. A first 0.5-degree
proposal was rejected because unequal angular tessellation rows fragmented the
real source into 297/149 components. Its source and failed evidence are retained.
The final primitive normal gate remains unchanged. Additional conservative
classification requires node error at most one tenth of the original fit
tolerance and an independently sampled interior axial level for each cone.
Two-ring conical chords alone cannot establish a straight meridian. Original
minimum area and primitive fit gates remain fixed; the number of new fits uses
the existing conservative local-consensus bound. No final quality gate changes.

All partitions contain only original source faces. No mesh coordinate or
breakpoint is moved, and normal graph construction recreates shared boundaries
from those exact source interfaces. IDs are allocated above existing primitive
IDs, preserving other recognized owners. The source graph cache version changes
with recognition behavior.

The bounded, source-only replay of a561's saved regions gives:

| Original region | New primitive regions | Source faces | Maximum node error |
| --- | --- | --- | --- |
| revolution-001 | cone, cone | 296 + 222 | 0.000000527 mm |
| revolution-002 | cylinder, cone | 148 + 222 | 0.000000352 mm |

Each original region's source ownership is exhaustive and disjoint. The pure
helper work took 0.0082 seconds in one observation; this is not a conversion
speed claim. It does not establish native STEP validity or strict recovery.

Eight focused tests pass: cone/cylinder transitions in both directions, two
distinct cone slopes, exact source shared rings and ownership, staggered angular
rows, curved meridians, a tangent cylinder/fillet requiring atomic rejection,
single-cone preservation, and refusal to bypass existing validation on a coarser
staggered source. The coarse staggered case remains deliberately unpromoted;
its original primitive fitters refuse it. Ruff passes.

Native isolated conversion and the unchanged frozen V5 grader subsequently
confirmed a strict recovery on this development model. V41 used two generic
revolutions and missed its conical supports; V47 produced three cones, four
cylinders and three planes, with zero faceted faces. Maximum reference distance
fell from 0.069330 to 0.000285 mm, and maximum dimension error from 0.033012 to
0.000329 mm. Conversion took 18.20 versus 13.89 seconds in one observation;
this is not a broad speed claim.

A matched five-model development cohort then improved from **3/5 to 4/5 strict
passes**, with no lost passes. The independent multi-loop trim failure remained
unsolved, and three existing successes passed both versions. One pair reused
its earlier exact-context result; four pairs were fresh. These five distinct
models do not establish an all-ten source-model rate or a full-corpus result.
The candidate stays **provisional**, with V21 retained as the product default
until broader matched regressions and the required frozen-corpus evaluation
justify promotion. No acceptance gate was changed.

Saved input/output STL renders were also inspected. The recovered conical lip
is straight instead of bowed, while the bore and end rims remain present.
Preview measurements are separate from the native reference metrics above.
The exact five-model development cohort, source/input
hashes, rejected proposal, parked V46 source, candidate patches and bounded
probes are retained under
`benchmarks/multires-1000/experiments/new-dev-failures-20261113/`.
