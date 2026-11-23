// Only disjoint bounds prove that independent shells cannot intersect or form a cavity.
export function splitSeparatedBodies(mesh) {
  const { coordinates, diagonal } = mesh;
  const parents = Int32Array.from({ length: coordinates.length / 9 }, (_, index) => index);
  function root(index) {
    while (index !== parents[index]) {
      parents[index] = parents[parents[index]];
      index = parents[index];
    }
    return index;
  }

  const incident = new Map();
  for (let i = 0; i < coordinates.length; i += 3) {
    const key = `${coordinates[i]}:${coordinates[i + 1]}:${coordinates[i + 2]}`;
    const triangle = Math.floor(i / 9);
    const previous = incident.get(key);
    if (previous === undefined) incident.set(key, triangle);
    else parents[root(triangle)] = root(previous);
  }
  const groups = new Map();
  for (let triangle = 0; triangle < parents.length; triangle++) {
    const id = root(triangle);
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(triangle);
    if (groups.size > 32) return null;
  }
  if (groups.size === 1) return [mesh];

  const margin = Math.max(1e-7, diagonal * 1e-8);
  const bodies = [];
  for (const triangles of groups.values()) {
    const part = new Float64Array(triangles.length * 9);
    const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    triangles.forEach((triangle, index) => {
      const vertices = coordinates.subarray(triangle * 9, triangle * 9 + 9);
      part.set(vertices, index * 9);
      for (let i = 0; i < 9; i++) {
        const axis = i % 3;
        min[axis] = Math.min(min[axis], vertices[i]);
        max[axis] = Math.max(max[axis], vertices[i]);
      }
    });
    const overlaps = bodies.some(body => !min.some((value, axis) =>
      value - body.max[axis] > margin || body.min[axis] - max[axis] > margin));
    if (overlaps) return null;
    bodies.push({ coordinates: part, min, max, factor: mesh.factor,
      diagonal: Math.hypot(...max.map((value, axis) => value - min[axis])) });
  }
  return bodies;
}
