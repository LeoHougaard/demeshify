import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { splitSeparatedBodies } from './mesh-bodies.js';

const edgeKey = (a, b) => a < b ? `${a}:${b}` : `${b}:${a}`;
const cross = (a, b) => a[0] * b[1] - a[1] * b[0];
const delta = (a, b) => [a[0] - b[0], a[1] - b[1]];
const pairs = (loop) => loop.map((point, i) => [point, loop[(i + 1) % loop.length]]);
const area = (loop) => pairs(loop).reduce((sum, [a, b]) => sum + cross(a, b), 0) / 2;

function inside([x, y], loop) {
  return pairs(loop).reduce((found, [[ax, ay], [bx, by]]) =>
    (ay > y) !== (by > y) && x < ax + (y - ay) * (bx - ax) / (by - ay) ? !found : found, false);
}

function circumcircle(a, middle, end, epsilon) {
  const b = delta(middle, a);
  const c = delta(end, a);
  const d = 2 * cross(b, c);
  if (Math.abs(d) <= epsilon ** 2) return null;
  const u = (Math.hypot(...b) ** 2 * c[1] - Math.hypot(...c) ** 2 * b[1]) / d;
  const v = (b[0] * Math.hypot(...c) ** 2 - c[0] * Math.hypot(...b) ** 2) / d;
  const center = [a[0] + u, a[1] + v];
  const radius = Math.hypot(u, v);
  return { center, radius };
}

function arcAngle(a, b, center) {
  const x = delta(a, center), y = delta(b, center);
  return Math.atan2(cross(x, y), x[0] * y[0] + x[1] * y[1]);
}

function circle(loop, epsilon) {
  if (loop.length < 12) return null;
  const fit = circumcircle(loop[0], loop[Math.floor(loop.length / 3)],
    loop[Math.floor(2 * loop.length / 3)], epsilon);
  if (!fit) return null;
  const { center, radius } = fit;
  if (loop.some(point => Math.abs(Math.hypot(...delta(point, center)) - radius) > epsilon * 4)) return null;
  let turn = 0;
  let maxAngle = 0;
  const orientation = Math.sign(area(loop));
  for (const [p, q] of pairs(loop)) {
    const angle = arcAngle(p, q, center);
    if (Math.sign(angle) !== orientation) return null;
    turn += angle;
    maxAngle = Math.max(maxAngle, Math.abs(angle));
  }
  if (Math.abs(Math.abs(turn) - 2 * Math.PI) > 1e-6 || maxAngle > Math.PI / 4) return null;
  return { center, radius, deviation: radius * (1 - Math.cos(maxAngle / 2)) + epsilon * 8 };
}

function fitArc(points, start, end, epsilon) {
  const a = points[start], b = points[end], mid = points[Math.floor((start + end) / 2)];
  const fit = circumcircle(a, mid, b, epsilon);
  if (!fit) return null;
  const { center, radius } = fit;
  let turn = 0, maxAngle = 0, addedArea = 0;
  for (let i = start; i < end; i++) {
    const angle = arcAngle(points[i], points[i + 1], center);
    if (Math.abs(angle) > Math.PI / 12 || !angle || turn * angle < 0
        || Math.abs(Math.hypot(...delta(points[i], center)) - radius) > epsilon * 4) return null;
    turn += angle;
    maxAngle = Math.max(maxAngle, Math.abs(angle));
    addedArea += radius ** 2 * (angle - Math.sin(angle)) / 2;
  }
  if (Math.abs(turn) < Math.PI / 12 || Math.abs(turn) > 1.9 * Math.PI) return null;
  return { a, b, mid, addedArea, deviation: radius * (1 - Math.cos(maxAngle / 2)) + epsilon * 8 };
}

function profileSegments(loop, epsilon) {
  if (loop.length > 256) return null;
  let corner = 0, strongestTurn = 0;
  for (let i = 0; i < loop.length; i++) {
    const a = loop[(i + loop.length - 1) % loop.length], b = loop[i], c = loop[(i + 1) % loop.length];
    const turn = Math.PI - Math.abs(arcAngle(a, c, b));
    if (turn > strongestTurn) {
      corner = i;
      strongestTurn = turn;
    }
  }
  const points = [...loop.slice(corner), ...loop.slice(0, corner), loop[corner]];
  const segments = [];
  for (let start = 0; start < loop.length && segments.length < 8;) {
    let arc;
    let end = loop.length;
    for (; end >= start + 6; end--) {
      arc = fitArc(points, start, end, epsilon);
      if (arc) break;
    }
    if (!arc) end = start + 1;
    segments.push(arc || { a: points[start], b: points[end] });
    start = end;
    if (start === loop.length) return segments;
  }
  return null;
}

function profile(positions, min, max, axis, epsilon, frame) {
  const u = (axis + 1) % 3, v = (axis + 2) % 3;
  const width = max[axis] - min[axis];
  if (width <= epsilon * 10) return null;
  const vertices = new Map();
  const points = [];
  const caps = [new Map(), new Map()];
  const capAreas = [0, 0];
  const manifold = new Map();
  const sides = new Map();
  for (let offset = 0; offset < positions.length; offset += 9) {
    const ids = [], levels = [];
    for (let j = offset; j < offset + 9; j += 3) {
      const along = positions[j + axis];
      const level = along - min[axis] <= epsilon ? 0 : max[axis] - along <= epsilon ? 1 : -1;
      if (level < 0) return null;
      const point = [positions[j + u] - min[u], positions[j + v] - min[v]];
      const [x, y] = point.map(value => Math.round(value / epsilon));
      const key = `${x}:${y}`;
      let id = vertices.get(key);
      // A rigid projection can put opposite vertices on either side of a grid line.
      if (frame && id === undefined) {
        for (const [dx, dy] of [[-1, -1], [-1, 0], [-1, 1], [0, -1], [0, 1], [1, -1], [1, 0], [1, 1]]) {
          const neighbor = vertices.get(`${x + dx}:${y + dy}`);
          if (neighbor !== undefined && Math.hypot(...delta(points[neighbor], point)) <= epsilon) {
            id = neighbor;
            break;
          }
        }
      }
      if (id === undefined) {
        id = points.length;
        vertices.set(key, id);
        points.push(point);
      }
      ids.push(id);
      levels.push(level);
    }
    const welded = ids.map((id, i) => 2 * id + levels[i]);
    if (new Set(welded).size < 3) return null;
    for (const [a, b] of pairs(welded)) {
      const k = edgeKey(a, b);
      const mask = a < b ? 1 : 2;
      const old = manifold.get(k) || 0;
      if (old & mask) return null;
      manifold.set(k, old | mask);
    }
    if (levels.every(level => level === levels[0])) {
      const cap = caps[levels[0]];
      const projected = ids.map(id => points[id]);
      const signed = cross(delta(projected[1], projected[0]), delta(projected[2], projected[0])) / 2;
      if (levels[0] ? signed <= 0 : signed >= 0) return null;
      capAreas[levels[0]] += Math.abs(signed);
      for (const [a, b] of pairs(ids)) {
        const k = edgeKey(a, b);
        if (cap.has(k)) cap.delete(k);
        else cap.set(k, [a, b]);
      }
    } else {
      const distinct = [...new Set(ids)];
      if (distinct.length !== 2) return null;
      const k = edgeKey(...distinct);
      sides.set(k, (sides.get(k) || 0) + 1);
    }
  }
  if (!caps[1].size || caps[0].size !== caps[1].size || sides.size !== caps[1].size
      || [...manifold.values()].some(mask => mask !== 3)
      || [...caps[1].keys()].some(key => !caps[0].has(key) || sides.get(key) !== 2)) return null;
  const next = new Map([...caps[1].values()]);
  if (next.size !== caps[1].size) return null;
  const loops = [];
  while (next.size) {
    const start = next.keys().next().value;
    const loop = [];
    let id = start;
    do {
      loop.push(points[id]);
      const neighbor = next.get(id);
      if (neighbor === undefined) return null;
      next.delete(id);
      id = neighbor;
    } while (id !== start);
    if (loop.length < 3) return null;
    const round = circle(loop, epsilon);
    const segments = round ? [] : profileSegments(loop, epsilon);
    if (!segments) return null;
    loops.push({ loop, signed: area(loop), circle: round, segments });
  }
  const outer = loops.find(item => item.signed > 0);
  if (!outer || loops.some(item => item !== outer && (item.signed >= 0 || !inside(item.loop[0], outer.loop)))) return null;
  const section = loops.reduce((sum, loop) => sum + loop.signed, 0);
  if (section <= 0 || capAreas.some(a => Math.abs(a - section) > section * 1e-5)) return null;
  return { axis, width, min, max, epsilon, frame, volume: section * width, outer,
    holes: loops.filter(item => item !== outer) };
}

export function readStl(data, units) {
  const geometry = new STLLoader().parse(data);
  const coordinates = geometry.getAttribute('position').array;
  geometry.dispose();
  if (!coordinates.length || coordinates.length % 9) return null;
  const rawMin = [Infinity, Infinity, Infinity], rawMax = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < coordinates.length; i++) {
    if (!Number.isFinite(coordinates[i])) return null;
    rawMin[i % 3] = Math.min(rawMin[i % 3], coordinates[i]);
    rawMax[i % 3] = Math.max(rawMax[i % 3], coordinates[i]);
  }
  const spans = rawMax.map((x, i) => x - rawMin[i]);
  const factor = { mm: 1, cm: 10, m: 1000, in: 25.4, auto: Math.max(...spans) < 1 ? 1000 : 1 }[units];
  if (!factor) return null;
  const min = rawMin.map(x => x * factor), max = rawMax.map(x => x * factor);
  const scaled = factor === 1 ? coordinates : Float64Array.from(coordinates, x => x * factor);
  const diagonal = Math.hypot(...spans) * factor;
  return { coordinates: scaled, min, max, factor, diagonal };
}

const dot3 = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross3 = ([ax, ay, az], [bx, by, bz]) => [ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx];

function* capNormals(coordinates) {
  const faces = [];
  for (let offset = 0; offset < coordinates.length; offset += 9) {
    const d = [0, 1, 2].map(i => coordinates[offset + 3 + i] - coordinates[offset + i]);
    const e = [0, 1, 2].map(i => coordinates[offset + 6 + i] - coordinates[offset + i]);
    const normal = cross3(d, e);
    const area = Math.hypot(...normal);
    if (area) faces.push({ normal, area });
  }
  const tried = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  for (const { normal, area } of faces.sort((a, b) => b.area - a.area)) {
    const unit = normal.map(value => value / (normal[2] < 0 ? -area : area));
    if (tried.some(previous => Math.abs(dot3(previous, unit)) > 1 - 1e-8)) continue;
    tried.push(unit);
    yield unit;
    if (tried.length === 9) return;
  }
}

function rotatedProfile(mesh) {
  const { coordinates, min: origin, diagonal } = mesh;
  if (coordinates.length / 9 > 5000) return null;
  const worldMagnitude = Math.max(...[...mesh.min, ...mesh.max].map(Math.abs));
  const epsilon = Math.max(1e-7, diagonal * 4e-7 + worldMagnitude * 2 ** -22);
  if (epsilon > Math.max(0.12, diagonal * 0.003) / 16) return null;
  const projected = new Float64Array(coordinates.length);
  for (const normal of capNormals(coordinates)) {
    const [nx, ny, nz] = normal;
    const s = 1 / (1 + nz);
    const basis = [[1 - nx * nx * s, -nx * ny * s, -nx], [-nx * ny * s, 1 - ny * ny * s, -ny], normal];
    const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < coordinates.length; i += 3) {
      const p = [coordinates[i] - origin[0], coordinates[i + 1] - origin[1], coordinates[i + 2] - origin[2]];
      for (let j = 0; j < 3; j++) {
        const value = dot3(p, basis[j]);
        min[j] = Math.min(min[j], value);
        max[j] = Math.max(max[j], value);
        projected[i + j] = value;
      }
    }
    const frame = { basis, origin, axis: [-ny, nx, 0], angle: Math.atan2(Math.hypot(nx, ny), nz) };
    const section = profile(projected, min, max, 2, epsilon, frame);
    if (section) return section;
  }
  return null;
}

function recognizeBody(mesh) {
  const { coordinates, min, max, diagonal } = mesh;
  const epsilon = Math.max(diagonal * 2e-7, 1e-7);
  for (const axis of [0, 1, 2]) {
    const section = profile(coordinates, min, max, axis, epsilon);
    if (section) return section;
  }
  return rotatedProfile(mesh);
}

export function recognizePrisms(data, units) {
  // Keep attempts bounded on inputs likely to need the full reconstruction engine.
  if (data.byteLength > 1024 * 1024) return null;
  const mesh = readStl(data, units);
  if (!mesh) return null;
  const single = recognizeBody(mesh);
  const bodies = single ? [mesh] : splitSeparatedBodies(mesh);
  if (!bodies || (!single && bodies.length === 1)) return null;
  const sections = bodies.map(body => {
    const section = single || recognizeBody(body);
    return section && { ...section, diagonal: body.diagonal, worldMin: body.min, worldMax: body.max };
  });
  return sections.every(Boolean) ? { sections, factor: mesh.factor, triangles: mesh.coordinates.length / 9 } : null;
}
