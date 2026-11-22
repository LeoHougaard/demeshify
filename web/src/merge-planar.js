const edgeKey = (a, b) => a < b ? `${a}:${b}` : `${b}:${a}`;
const dot = ([a, b, c], [x, y, z]) => a * x + b * y + c * z;
const difference = ([a, b, c], [x, y, z]) => [a - x, b - y, c - z];
const cross = ([a, b, c], [x, y, z]) => [b * z - c * y, c * x - a * z, a * y - b * x];

function boundaryLoops(triangles, group, points, normal) {
  const border = new Map();
  for (const index of group) {
    const face = triangles[index];
    for (let i = 0; i < 3; i++) {
      const a = face[i], b = face[(i + 1) % 3];
      const key = edgeKey(a, b);
      if (border.has(key)) border.delete(key);
      else border.set(key, [a, b]);
    }
  }
  const next = new Map(border.values());
  if (next.size !== border.size) return null;
  const loops = [];
  let perimeter = 0;
  while (next.size) {
    const start = next.keys().next().value;
    const loop = [];
    let at = start, area = 0;
    do {
      const to = next.get(at);
      if (to === undefined) return null;
      next.delete(at);
      loop.push(at);
      const a = difference(points[at], points[start]), b = difference(points[to], points[start]);
      area += dot(cross(a, b), normal);
      perimeter += Math.hypot(...difference(points[at], points[to]));
      at = to;
    } while (at !== start);
    if (loop.length < 3 || !area) return null;
    loops.push({ loop, area: area / 2 });
  }
  const outside = loops.filter(loop => loop.area > 0);
  if (outside.length !== 1 || !perimeter) return null;
  const area = loops.reduce((sum, loop) => sum + loop.area, 0);
  if (area <= 0 || 2 * area / perimeter < 0.0005) return null;
  return [outside[0].loop, ...loops.filter(loop => loop.area < 0).map(loop => loop.loop)];
}

export function mergePlanar(points, triangles, normals) {
  const adjacency = triangles.map(() => []);
  const edges = new Map();
  triangles.forEach((face, index) => {
    for (let i = 0; i < 3; i++) {
      const key = edgeKey(face[i], face[(i + 1) % 3]);
      const neighbor = edges.get(key);
      if (neighbor === undefined) edges.set(key, index);
      else {
        adjacency[index].push(neighbor);
        adjacency[neighbor].push(index);
      }
    }
  });
  const protectedVertices = new Set();
  const areas = triangles.map(([a, b, c]) => {
    const ab = difference(points[b], points[a]);
    const ac = difference(points[c], points[a]);
    const area = Math.hypot(...cross(ab, ac));
    const perimeter = Math.hypot(...ab) + Math.hypot(...ac)
      + Math.hypot(...difference(points[c], points[b]));
    // A nearly coplanar neighbor can increase the STEP tolerance on a shared
    // vertex enough to swallow a narrow source face on import.
    if (area / perimeter < 0.0001) {
      protectedVertices.add(a);
      protectedVertices.add(b);
      protectedVertices.add(c);
    }
    return area;
  });
  const protectedFaces = triangles.map(face => face.some(vertex => protectedVertices.has(vertex)));
  const order = areas.map((_, i) => i).sort((a, b) => areas[b] - areas[a]);
  const visited = new Uint8Array(triangles.length);
  const faces = [];
  // Group triangles against a fixed support plane so near-coplanarity cannot accumulate.
  for (const seed of order) {
    if (visited[seed]) continue;
    visited[seed] = 1;
    if (protectedFaces[seed]) {
      faces.push({ loops: [triangles[seed]], normal: normals[seed] });
      continue;
    }
    const normal = normals[seed], origin = points[triangles[seed][0]];
    const group = [seed];
    for (let i = 0; i < group.length; i++) {
      for (const next of adjacency[group[i]]) {
        if (visited[next] || protectedFaces[next] || dot(normal, normals[next]) < 1 - 2e-8) continue;
        if (triangles[next].some(v => Math.abs(dot(normal, difference(points[v], origin))) > 1e-5)) continue;
        visited[next] = 1;
        group.push(next);
      }
    }
    const loops = group.length > 1 && boundaryLoops(triangles, group, points, normal);
    // The STEP plane must use the same origin as the distance checks above.
    if (loops) faces.push({ loops, normal, origin: triangles[seed][0] });
    else for (const index of group) faces.push({ loops: [triangles[index]], normal: normals[index] });
  }
  return faces;
}
