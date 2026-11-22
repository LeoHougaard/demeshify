import { circle, profileSegments, readStl } from './prism-profile.js';

function indexMesh(mesh) {
  const points = [], triangles = [], indices = new Map(), edges = new Map();
  for (let i = 0; i < mesh.coordinates.length; i += 9) {
    const face = [0, 3, 6].map(j => {
      const point = mesh.min.map((value, axis) => mesh.coordinates[i + j + axis] - value);
      const key = point.join(':');
      if (!indices.has(key)) {
        indices.set(key, points.length);
        points.push(point);
      }
      return indices.get(key);
    });
    if (new Set(face).size !== 3) return null;
    for (let j = 0; j < 3; j++) {
      const a = face[j], b = face[(j + 1) % 3];
      const key = a < b ? `${a}:${b}` : `${b}:${a}`;
      const mask = a < b ? 1 : 2;
      const old = edges.get(key) || 0;
      if (old & mask) return null;
      edges.set(key, old | mask);
    }
    triangles.push(face);
  }
  return [...edges.values()].every(mask => mask === 3) ? { points, triangles } : null;
}

function simplify(loop, epsilon) {
  const points = [];
  for (const p of loop) {
    const previous = points.at(-1);
    if (!previous || Math.hypot(p[0] - previous[0], p[1] - previous[1]) > epsilon) points.push(p);
  }
  const last = points.at(-1);
  if (Math.hypot(points[0][0] - last[0], points[0][1] - last[1]) <= epsilon) points.pop();
  return points.filter(([x, y], i) => {
    const [ax, ay] = points[(i + points.length - 1) % points.length];
    const [bx, by] = points[(i + 1) % points.length];
    const deviation = Math.abs((x - ax) * (by - ay) - (y - ay) * (bx - ax));
    return (x - ax) * (x - bx) + (y - ay) * (y - by) > 0
      || deviation > epsilon * Math.hypot(bx - ax, by - ay);
  });
}

function fitBore(points, epsilon) {
  const exact = circle(points, epsilon);
  if (exact || points.length < 12) return exact;
  // A staggered tessellation can leave some crossings slightly inside a bore's circle.
  const [cx, cy] = points.reduce(([x, y], p) => [x + p[0], y + p[1]], [0, 0]).map(x => x / points.length);
  let xx = 0, xy = 0, yy = 0, rhsX = 0, rhsY = 0;
  for (const [a, b] of points) {
    const x = a - cx, y = b - cy, halfNorm = (x * x + y * y) / 2;
    xx += x * x;
    xy += x * y;
    yy += y * y;
    rhsX += x * halfNorm;
    rhsY += y * halfNorm;
  }
  const det = xx * yy - xy * xy;
  if (det <= epsilon ** 4) return null;
  const center = [cx + (yy * rhsX - xy * rhsY) / det, cy + (xx * rhsY - xy * rhsX) / det];
  const radii = points.map(([x, y]) => Math.hypot(x - center[0], y - center[1]));
  const radius = Math.max(...radii);
  return radius - Math.min(...radii) <= 32 * epsilon ? { center, radius } : null;
}

function slice({ points, triangles }, height, epsilon) {
  const next = new Map(), xy = new Map();
  for (const face of triangles) {
    const cut = [];
    for (let j = 0; j < 3; j++) {
      const a = face[j], b = face[(j + 1) % 3];
      const start = points[a], end = points[b];
      if ((start[2] > height) === (end[2] > height)) continue;
      const key = a < b ? `${a}:${b}` : `${b}:${a}`;
      if (!xy.has(key)) {
        const t = (height - start[2]) / (end[2] - start[2]);
        xy.set(key, [start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1])]);
      }
      cut[start[2] > height ? 0 : 1] = key;
    }
    if (cut.length) {
      if (!cut[0] || !cut[1] || next.has(cut[0])) return null;
      next.set(cut[0], cut[1]);
    }
  }
  const rings = [];
  while (next.size) {
    const start = next.keys().next().value;
    let current = start;
    const loop = [];
    do {
      loop.push(xy.get(current));
      const neighbor = next.get(current);
      if (!neighbor) return null;
      next.delete(current);
      current = neighbor;
    } while (current !== start);
    const outline = simplify(loop, epsilon);
    if (outline.length < 3) return null;
    const area = outline.reduce((sum, [x, y], i) => {
      const [a, b] = outline[(i + 1) % outline.length];
      return sum + x * b - y * a;
    }, 0) / 2;
    rings.push({ outline, area, round: area < 0 ? fitBore(outline, epsilon) : null });
  }
  return rings;
}

function rimSetback(points, height, mesh, epsilon) {
  const rim = points.filter(point => Math.abs(point[2] - height) <= epsilon);
  const inset = [0, 1].flatMap(axis => [
    Math.min(...rim.map(point => point[axis])),
    mesh.max[axis] - mesh.min[axis] - Math.max(...rim.map(point => point[axis])),
  ]).sort((a, b) => a - b);
  const measured = (inset[1] + inset[2]) / 2;
  return measured > epsilon && inset[3] - inset[0] <= Math.max(epsilon * 8, measured * 0.1) ? measured : null;
}

export function recognizeLayered(data, units) {
  if (data.byteLength > 512 * 1024) return null;
  const mesh = readStl(data, units);
  if (!mesh || mesh.coordinates.length / 9 > 10_000) return null;
  const epsilon = Math.max(mesh.diagonal * 5e-7, 1e-7);
  const levels = [];
  const heightSamples = [...new Set(mesh.coordinates.filter((_, i) => i % 3 === 2))].sort((a, b) => a - b);
  for (const z of heightSamples) {
    const height = z - mesh.min[2];
    if (!levels.length || height - levels.at(-1) > epsilon) levels.push(height);
    if (levels.length > 24) return null;
  }
  if (levels.length < 4) return null;
  const indexed = indexMesh(mesh);
  if (!indexed) return null;
  const bores = [];
  const bands = [];
  for (let band = 0; band < levels.length - 1; band++) {
    const low = levels[band], high = levels[band + 1], height = high - low;
    const first = slice(indexed, low + height / 4, epsilon);
    const second = slice(indexed, high - height / 4, epsilon);
    if (!first || !second || first.filter(ring => ring.area > 0).length !== 1
        || second.filter(ring => ring.area > 0).length !== 1) return null;
    const profileArea = rings => rings.reduce((sum, ring) => sum + (ring.round ? 0 : ring.area), 0);
    bands.push({ low, high, ramps: Math.abs(profileArea(first) - profileArea(second)) > epsilon * mesh.diagonal });
    const roundHoles = first.flatMap(ring => ring.round ? [ring.round] : []);
    const matchedHoles = second.flatMap(ring => ring.round ? [ring.round] : []);
    const near = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]) < 4 * epsilon;
    if (roundHoles.length !== matchedHoles.length) return null;
    for (const a of roundHoles) {
      const b = matchedHoles.find(hole => near(a.center, hole.center));
      if (!b) return null;
      const diff = (b.radius - a.radius) / 2;
      const r0 = a.radius - diff, r1 = b.radius + diff;
      if (Math.min(r0, r1) <= epsilon || Math.abs(r1 - r0) > 4 * height) return null;
      bores.push({ center: a.center, low, height, r0, r1 });
    }
  }
  const straight = bands.findIndex(band => !band.ramps);
  const lastStraight = bands.findLastIndex(band => !band.ramps);
  if (straight <= 0 || lastStraight === bands.length - 1
      || bands.slice(straight, lastStraight + 1).some(band => band.ramps)) return null;
  const centerBand = bands.slice(straight, lastStraight + 1).reduce((a, b) =>
    a.high - a.low >= b.high - b.low ? a : b);
  const center = slice(indexed, (centerBand.low + centerBand.high) / 2, epsilon);
  if (!center) return null;
  const profile = center.filter(ring => !ring.round).map(ring => ({
    ...ring, segments: profileSegments(ring.outline, epsilon, 40),
  }));
  if (profile.some(ring => !ring.segments)) return null;
  const volume = indexed.triangles.reduce((sum, [a, b, c]) => {
    const [x, y, z] = indexed.points[a], [u, v, w] = indexed.points[b], [p, q, r] = indexed.points[c];
    return sum + (x * (v * r - w * q) + y * (w * p - u * r) + z * (u * q - v * p)) / 6;
  }, 0);
  const height = levels.at(-1);
  const lowerChamfer = rimSetback(indexed.points, 0, mesh, epsilon);
  const upperChamfer = rimSetback(indexed.points, height, mesh, epsilon);
  if (!lowerChamfer || !upperChamfer) return null;
  return { mesh, height, profile, bores, epsilon, volume, lowerChamfer, upperChamfer };
}
