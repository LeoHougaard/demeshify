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

export async function buildPrism(section, job, started) {
  const { axis, width, min, max, outer, holes, epsilon, frame } = section;
  const loops = [outer, ...holes];
  const deviation = Math.max(epsilon * 8, ...loops.flatMap(({ circle, segments }) =>
    circle ? [circle.deviation] : segments.map(segment => segment.deviation ?? 0)));
  if (deviation > Math.max(0.12, section.diagonal * 0.003) / 2) return null;
  const kernel = await getKernel();
  const checkpoint = kernel.checkpoint();
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

  try {
    let candidate = solid(outer);
    for (const hole of holes) candidate = kernel.cut(candidate, solid(hole));
    if (!kernel.isValid(candidate)) return null;
    const placed = frame ? kernel.translate(kernel.rotate(candidate, rotation, frame.angle), ...frame.origin) : candidate;
    const step = kernel.exportStep(placed);
    const reopened = kernel.importStep(step);
    if (!kernel.isValid(reopened) || kernel.subShapeCount(reopened, 'solid') !== 1
        || kernel.subShapeCount(reopened, 'shell') !== 1) return null;
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
    const boundsError = Math.max(...['x', 'y', 'z'].flatMap((key, index) => [
      Math.abs(box[`${key}min`] - min[index]), Math.abs(box[`${key}max`] - max[index]),
    ]));
    const fittedVolume = width * loops.reduce((volume, { signed, circle, segments }) =>
      volume + (circle ? Math.sign(signed) * Math.PI * circle.radius ** 2
        : signed + segments.reduce((sum, segment) => sum + (segment.addedArea ?? 0), 0)), 0);
    const resultVolume = kernel.getVolume(reopened);
    const volumeError = 100 * Math.abs(resultVolume / section.volume - 1);
    if (!Number.isFinite(volumeError) || volumeError > 2 || boundsError > deviation + 1e-6
        || Math.abs(resultVolume / fittedVolume - 1) > 1e-5) return null;

    const faceCount = counts.plane + counts.cylinder;
    const report = {
      id: job.id, engine: 'surface_brep', implementation: 'prism', status: 'complete',
      mesh: { file_name: job.filename, unit_scale: section.factor, watertight: true,
        triangle_count: section.triangles },
      surface: {
        recognized_surface_count: faceCount, surface_counts: counts, brep_face_count: faceCount,
        solid_count: 1, free_edge_count: 0, closed: true, faceted_fallback: false,
        topology_vertex_count: kernel.subShapeCount(reopened, 'vertex'),
        topology_edge_count: kernel.subShapeCount(reopened, 'edge'),
        adjacency_count: kernel.subShapeCount(reopened, 'edge'),
      },
      score: { deviation_bound_mm: deviation, volume_error_percent: volumeError,
        volume_comparable: true, valid_brep: true, valid_solid: true,
        component_count_match: true, step_geometry_verified: true },
      warnings: [], elapsed_seconds: (performance.now() - started) / 1000,
    };
    const surfaceBoundaries = loops.flatMap(({ loop, circle, segments }) => {
      const points = circle ? Array.from({ length: 64 }, (_, i) => {
        const a = i * Math.PI / 32;
        return [circle.center[0] + circle.radius * Math.cos(a), circle.center[1] + circle.radius * Math.sin(a)];
      }) : loop;
      const closed = [...points, points[0]];
      return [closed.map(point => worldPoint(xyz(point))), closed.map(point => worldPoint(xyz(point, 1))),
        ...segments.map(({ a }) => [worldPoint(xyz(a)), worldPoint(xyz(a, 1))])];
    });
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
