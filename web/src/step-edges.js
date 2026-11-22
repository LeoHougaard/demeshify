// Closed STEP solids have two distinct faces along a visible topological edge.
// A periodic surface's seam belongs to just one face.
export function sampleSolidEdges(kernel, solid, deflection) {
  try {
    return sample(kernel, solid, deflection);
  } catch {
    // An unavailable overlay must not affect the verified STEP download.
    return [];
  }
}

function sample(kernel, solid, deflection) {
  const adjacency = kernel.edgeToFaceMap(solid, 0x7fffffff);
  const shared = new Set();
  for (let i = 0; i < adjacency.length;) {
    const edge = adjacency[i++];
    const count = adjacency[i++];
    if (count > 1) shared.add(edge);
    i += count;
  }

  // Use the CAD edges' tessellation so their polylines follow the shaded preview.
  const { points, edgeGroups } = kernel.wireframe(solid, { deflection, source: 'triangulation' });
  const curves = [];
  for (let i = 0; i < edgeGroups.length; i += 3) {
    const [start, length, edge] = edgeGroups.subarray(i, i + 3);
    if (!shared.has(edge) || length < 6) continue;
    const curve = [];
    for (let j = start; j < start + length; j += 3) {
      curve.push([points[j], points[j + 1], points[j + 2]]);
    }
    curves.push(curve);
  }
  return curves;
}
