# Research findings and reconstruction roadmap

## What the first version got wrong

The original implementation selected one cardinal axis, sampled three
cross-sections, and represented the median section as a polygon or circle. It
could accurately recover constant-section plates and tubes, but it had no
language for sequential bosses, pockets, stepped shafts, or mixed line/arc
profiles. A good surface score on those easy parts did not establish useful CAD
reverse engineering.

## Relevant research

- [Point2Cyl (CVPR
  2022)](https://openaccess.thecvf.com/content/CVPR2022/papers/Uy_Point2Cyl_Reverse_Engineering_3D_Objects_From_Point_Clouds_to_Extrusion_CVPR_2022_paper.pdf)
  models parts as multiple sketch/extrusion cylinders and boolean
  combinations. Its geometry-grounded decomposition—point segmentation, base
  and barrel classification, normals, then closed-form parameter fitting—is
  the right conceptual model for mechanical parts.
- [CAD-SIGNet (CVPR
  2024)](https://openaccess.thecvf.com/content/CVPR2024/html/Khan_CAD-SIGNet_CAD_Language_Inference_from_Point_Clouds_using_Layer-wise_Sketch_CVPR_2024_paper.html)
  autoregressively recovers sketch/extrusion histories and conditions each
  sketch on points near its inferred plane. Its published evaluation includes
  separate line, arc, circle, and extrusion precision/recall; this is far more
  informative than a single shape-overlap score.
- [CAD-Recode (ICCV
  2025)](https://openaccess.thecvf.com/content/ICCV2025/papers/Rukhovich_CAD-Recode_Reverse_Engineering_CAD_Code_from_Point_Clouds_ICCV_2025_paper.pdf)
  maps point clouds to CadQuery code using a small code-pretrained language
  model and a point projector. It reports strong gains from one million
  procedurally generated programs and test-time sampling of multiple
  candidates. Its released implementation is CC BY-NC 4.0, so it must remain
  an optional non-commercial research integration rather than code copied into
  this MIT project.
- [Fusion 360 Gallery
  reconstruction](https://www.research.autodesk.com/publications/fusion-360-gallery/)
  provides 8,625 human design sequences and demonstrates neurally guided
  program search. The paper specifically notes that IoU can score well while
  omitting small holes; exact reconstruction and concise sequences matter.
- [Point2CAD](https://github.com/prs-eth/point2cad) reconstructs B-rep surfaces,
  edges, and corners from surface-segmented points. It is useful for the later
  free-form/B-rep path but requires an upstream segmentation model and its
  released software is non-commercial.

## Architecture

The product path is hybrid:

1. **Geometry analysis:** estimate planar and cylindrical patches, candidate
   sketch planes, symmetry, sharp edges, and extrusion ranges.
2. **Deterministic proposals:** produce analytic sketch/extrusion candidates
   from cross-sections, surface clusters, and closed-form fits.
3. **Learned proposals:** optionally ask a CAD-specific point-cloud model for
   several typed construction sequences.
4. **Safe execution:** validate typed features and build them with OCCT; never
   execute model-written source.
5. **Geometric search:** optimize dimensions and rank candidates using exact
   point-to-triangle distance, volume, topology, validity, feature count, and
   nominal-dimension priors.
6. **Confidence:** high confidence requires a valid solid plus feature-level
   and geometric thresholds. IoU is only a secondary metric.

## Implemented in generation two

- typed circular-arc sketch segments;
- multiple boolean extrusion operations;
- axis-aligned planar-level detection;
- multi-slice layer grouping and reconstruction;
- protection against false arc fits through polygon corners;
- protection against false layer boundaries from cylinder tessellation;
- STEP-to-STL regression fixtures for stepped shafts, blind pockets, mixed
  arcs and lines, four-corner rounded plates, and combined pockets/side holes;
- corpus conversion and batch benchmarking tools.

## Next engineering stages

1. Segment global planar and cylindrical surface patches, then recover through
   and blind holes independently of the main extrusion axis.
2. Add explicit pocket, boss, counterbore, countersink, slot, chamfer, and
   fillet features instead of representing every change as an additive slab.
3. Detect revolutions and lathe profiles for shafts with grooves and tapers.
4. Generate multiple construction histories and use local continuous
   optimization to refine their dimensions.
5. Benchmark thousands of licensed ABC/DeepCAD/Fusion cases by feature count
   and complexity buckets. Preserve a fixed holdout set.
6. Train or fine-tune a proposal model only after the deterministic evaluator
   and corpus metrics are stable, so better-looking but incorrect programs
   cannot pass unnoticed.

## Required benchmark metrics

- valid STEP solid rate;
- exact/high-confidence reconstruction rate;
- median, P95, and maximum surface deviation;
- volume error;
- line, arc, circle, and extrusion precision/recall when histories exist;
- loop count and hole recall;
- extrusion-plane and depth error;
- feature count and construction conciseness;
- results bucketed by number of operations, curves, bodies, and mesh quality.
