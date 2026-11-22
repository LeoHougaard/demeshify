import { OcctKernel } from 'occt-wasm';
import wasmUrl from 'occt-wasm/dist/occt-wasm.wasm?url';

let kernelPromise;

export function getKernel() {
  kernelPromise ??= OcctKernel.init({ wasm: wasmUrl }).catch(error => {
    kernelPromise = undefined;
    throw error;
  });
  return kernelPromise;
}

function deviationBound({ outer, holes, epsilon }) {
  return Math.max(epsilon * 8, ...[outer, ...holes].flatMap(({ circle, segments }) =>
    circle ? [circle.deviation] : segments.map(segment => segment.deviation ?? 0)));
}

const axes = ['x', 'y', 'z'];

function constructPrism(kernel, section, deviation) {
  const { axis, width, min, max, outer, holes, frame } = section;
  const loops = [outer, ...holes];
  const normal = [0, 0, 0];
  normal[axis] = 1;
  const vector = normal.map(x => x * width);
  function xyz(point, level = 0) {
    const output = [...min];
    output[axis] += level * width;
    output[(axis + 1) % 3] += point[0];
    output[(axis + 2) % 3] += point[1];
    return output;
  }
  const vec = ([x, y, z]) => ({ x, y, z });
  const rotation = frame && { point: vec([0, 0, 0]), direction: vec(frame.axis) };

  function worldPoint(point) {
    return frame ? frame.origin.map((offset, axis) => offset
      + frame.basis.reduce((sum, basis, index) => sum + point[index] * basis[axis], 0)) : point;
  }

  function solid({ circle, segments }) {
    if (circle) {
      let cylinder = kernel.makeCylinder(circle.radius, width);
      if (axis !== 2) cylinder = kernel.rotate(cylinder, {
        point: vec([0, 0, 0]), direction: vec(axis === 0 ? [0, 1, 0] : [-1, 0, 0]),
      }, Math.PI / 2);
      return kernel.translate(cylinder, ...xyz(circle.center));
    }
    const edges = segments.map(({ a, b, mid }) => mid
      ? kernel.makeArcEdge(vec(xyz(a)), vec(xyz(mid)), vec(xyz(b)))
      : kernel.makeLineEdge(vec(xyz(a)), vec(xyz(b))));
    return kernel.extrude(kernel.makeFace(kernel.makeWire(edges)), ...vector);
  }

  let candidate = solid(outer);
  for (const hole of holes) candidate = kernel.cut(candidate, solid(hole));
  if (!kernel.isValid(candidate)) return null;
  const placed = frame ? kernel.translate(kernel.rotate(candidate, rotation, frame.angle), ...frame.origin) : candidate;

  function verify(reopened) {
    if (kernel.subShapeCount(reopened, 'shell') !== 1) return null;
    const counts = { plane: 0, cylinder: 0 };
    for (const face of kernel.getSubShapes(reopened, 'face')) {
      const type = kernel.surfaceType(face);
      if (!(type in counts)) return null;
      counts[type] += 1;
    }
    const expectedPlanes = 2 + loops.reduce((count, item) =>
      count + item.segments.filter(segment => !segment.mid).length, 0);
    const expectedCylinders = loops.reduce((count, { circle, segments }) =>
      count + (circle ? 1 : segments.filter(segment => segment.mid).length), 0);
    if (counts.plane !== expectedPlanes || counts.cylinder !== expectedCylinders) return null;
    const aligned = frame ? kernel.rotate(kernel.translate(reopened, ...frame.origin.map(x => -x)),
      rotation, -frame.angle) : reopened;
    const box = kernel.getBoundingBox(aligned, false);
    const boundsError = Math.max(...axes.flatMap((key, index) => [
      Math.abs(box[`${key}min`] - min[index]), Math.abs(box[`${key}max`] - max[index]),
    ]));
    const fittedVolume = width * loops.reduce((volume, { signed, circle, segments }) =>
      volume + (circle ? Math.sign(signed) * Math.PI * circle.radius ** 2
        : signed + segments.reduce((sum, segment) => sum + (segment.addedArea ?? 0), 0)), 0);
    const resultVolume = kernel.getVolume(reopened);
    const volumeError = 100 * Math.abs(resultVolume / section.volume - 1);
    if (!Number.isFinite(volumeError) || volumeError > 2 || boundsError > deviation + 1e-6
        || Math.abs(resultVolume / fittedVolume - 1) > 1e-5) return null;
    return { counts, volumeError };
  }

  function boundaries() {
    return loops.flatMap(({ loop, circle, segments }) => {
      const points = circle ? Array.from({ length: 64 }, (_, i) => {
        const a = i * Math.PI / 32;
        return [circle.center[0] + circle.radius * Math.cos(a), circle.center[1] + circle.radius * Math.sin(a)];
      }) : loop;
      const closed = [...points, points[0]];
      return [closed.map(point => worldPoint(xyz(point))), closed.map(point => worldPoint(xyz(point, 1))),
        ...segments.map(({ a }) => [worldPoint(xyz(a)), worldPoint(xyz(a, 1))])];
    });
  }

  return { placed, verify, boundaries, worldMin: section.worldMin, worldMax: section.worldMax };
}

export async function buildPrism({ sections, factor, triangles }, job, started) {
  const deviations = sections.map(deviationBound);
  if (sections.some((section, i) => deviations[i] > Math.max(0.12, section.diagonal * 0.003) / 2)) return null;
  const kernel = await getKernel();
  const checkpoint = kernel.checkpoint();
  try {
    const bodies = sections.map((section, i) => constructPrism(kernel, section, deviations[i]));
    if (bodies.some(body => !body)) return null;
    const model = bodies.length === 1 ? bodies[0].placed : kernel.makeCompound(bodies.map(body => body.placed));
    const step = kernel.exportStep(model);
    const reopened = kernel.importStep(step);
    if (!kernel.isValid(reopened)) return null;
    const solids = kernel.getSubShapes(reopened, 'solid');
    if (solids.length !== bodies.length || kernel.subShapeCount(reopened, 'shell') !== bodies.length) return null;

    const pending = new Set(bodies);
    const counts = { plane: 0, cylinder: 0 };
    let volumeError = 0;
    for (const solid of solids) {
      const center = kernel.getCenterOfMass(solid);
      const body = [...pending].find(({ worldMin, worldMax }) => axes.every((key, i) =>
        center[key] >= worldMin[i] && center[key] <= worldMax[i]));
      const check = body?.verify(solid);
      if (!check) return null;
      pending.delete(body);
      counts.plane += check.counts.plane;
      counts.cylinder += check.counts.cylinder;
      volumeError = Math.max(volumeError, check.volumeError);
    }

    const deviation = Math.max(...deviations);
    const faceCount = counts.plane + counts.cylinder;
    const report = {
      id: job.id, engine: 'surface_brep', implementation: 'prism', status: 'complete',
      mesh: { file_name: job.filename, unit_scale: factor, watertight: true,
        triangle_count: triangles },
      surface: {
        recognized_surface_count: faceCount, surface_counts: counts, brep_face_count: faceCount,
        solid_count: solids.length, free_edge_count: 0, closed: true, faceted_fallback: false,
        topology_vertex_count: kernel.subShapeCount(reopened, 'vertex'),
        topology_edge_count: kernel.subShapeCount(reopened, 'edge'),
        adjacency_count: kernel.subShapeCount(reopened, 'edge'),
      },
      score: { deviation_bound_mm: deviation, volume_error_percent: volumeError,
        volume_comparable: true, valid_brep: true, valid_solid: true,
        component_count_match: true, step_geometry_verified: true },
      warnings: [], elapsed_seconds: (performance.now() - started) / 1000,
    };
    const surfaceBoundaries = bodies.flatMap(body => body.boundaries());
    const text = new TextEncoder();
    const files = {
      'reconstruction.step': text.encode(step),
      'reconstruction.stl': kernel.exportStl(reopened, Math.min(deviation / 2, 0.03)),
      'report.json': text.encode(JSON.stringify(report, null, 2)),
      'surface_graph.json': text.encode(JSON.stringify({
        surface_counts: counts, visualization: { surface_boundaries: surfaceBoundaries },
      })),
    };
    return { report, files };
  } finally {
    kernel.releaseSince(checkpoint);
  }
}
