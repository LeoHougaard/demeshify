import { Box3, Triangle, Vector3 } from 'three';

function tree(items) {
  const bounds = items.reduce((box, item) => box.union(item.bounds), new Box3());
  if (items.length <= 8) return { bounds, items };
  const span = bounds.getSize(new Vector3());
  const axis = span.x >= span.y && span.x >= span.z ? 'x' : span.y >= span.z ? 'y' : 'z';
  items.sort((a, b) => a.center[axis] - b.center[axis]);
  const cut = Math.floor(items.length / 2);
  return { bounds, children: [tree(items.slice(0, cut)), tree(items.slice(cut))] };
}

function triangles(coordinates) {
  const items = [];
  for (let i = 0; i < coordinates.length; i += 9) {
    const vertices = [0, 3, 6].map(offset => new Vector3().fromArray(coordinates, i + offset));
    const triangle = new Triangle(...vertices);
    if (!triangle.getArea()) return null;
    items.push({ triangle, center: triangle.getMidpoint(new Vector3()), bounds: new Box3().setFromPoints(vertices) });
  }
  return items;
}

function squaredDistance(node, point, nearest, best = Infinity) {
  if (node.bounds.distanceToPoint(point) ** 2 >= best) return best;
  if (node.items) {
    for (const { triangle, bounds } of node.items) {
      if (bounds.distanceToPoint(point) ** 2 >= best) continue;
      triangle.closestPointToPoint(point, nearest);
      best = Math.min(best, nearest.distanceToSquared(point));
    }
    return best;
  }
  const [a, b] = node.children;
  return a.bounds.distanceToPoint(point) <= b.bounds.distanceToPoint(point)
    ? squaredDistance(b, point, nearest, squaredDistance(a, point, nearest, best))
    : squaredDistance(a, point, nearest, squaredDistance(b, point, nearest, best));
}

function probe(samples, index, limit, deviations) {
  const point = new Vector3(), nearest = new Vector3();
  for (const { triangle: { a, b, c }, center } of samples) {
    const probes = [a, b, c, center,
      point.copy(a).lerp(b, 0.5).clone(), point.copy(b).lerp(c, 0.5).clone(), point.copy(c).lerp(a, 0.5).clone()];
    for (const sample of probes) {
      const distance = Math.sqrt(squaredDistance(index, sample, nearest));
      if (!Number.isFinite(distance) || distance > limit) return false;
      deviations.push(distance);
    }
  }
  return true;
}

export function meshAgreement(input, output, limit) {
  if (output.length / 9 > 30_000) return null;
  const source = triangles(input), result = triangles(output);
  if (!source || !result) return null;
  const deviations = [];
  if (!probe(source, tree(result), limit, deviations) || !probe(result, tree(source), limit, deviations)) return null;
  deviations.sort((a, b) => a - b);
  return { p95: deviations[Math.floor(0.95 * (deviations.length - 1))], max: deviations.at(-1) };
}
