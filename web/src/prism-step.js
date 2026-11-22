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
  const { axis, width, min, max, outer, holes, epsilon } = section;
  const loops = [outer, ...holes];
  const deviation = Math.max(epsilon * 8, ...loops.map(loop => loop.circle?.deviation || 0));
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

  function solid({ loop, circle }) {
    if (circle) {
      let cylinder = kernel.makeCylinder(circle.radius, width);
      if (axis !== 2) cylinder = kernel.rotate(cylinder, {
        point: vec([0, 0, 0]), direction: vec(axis === 0 ? [0, 1, 0] : [-1, 0, 0]),
      }, Math.PI / 2);
      return kernel.translate(cylinder, ...xyz(circle.center));
    }
    const edges = loop.map((point, i) => kernel.makeLineEdge(
      vec(xyz(point)), vec(xyz(loop[(i + 1) % loop.length])),
    ));
    return kernel.extrude(kernel.makeFace(kernel.makeWire(edges)), ...vector);
  }

  try {
    let candidate = solid(outer);
    for (const hole of holes) candidate = kernel.cut(candidate, solid(hole));
    if (!kernel.isValid(candidate)) return null;
    const step = kernel.exportStep(candidate);
    const reopened = kernel.importStep(step);
    if (!kernel.isValid(reopened) || kernel.subShapeCount(reopened, 'solid') !== 1
        || kernel.subShapeCount(reopened, 'shell') !== 1) return null;
    const counts = { plane: 0, cylinder: 0 };
    for (const face of kernel.getSubShapes(reopened, 'face')) {
      const type = kernel.surfaceType(face);
      if (!(type in counts)) return null;
      counts[type] += 1;
    }
    const expectedPlanes = 2 + loops.reduce((count, item) => count + (item.circle ? 0 : item.loop.length), 0);
    if (counts.plane !== expectedPlanes || counts.cylinder !== loops.filter(item => item.circle).length) return null;
    const box = kernel.getBoundingBox(reopened, false);
    const boundsError = Math.max(...['x', 'y', 'z'].flatMap((key, index) => [
      Math.abs(box[`${key}min`] - min[index]), Math.abs(box[`${key}max`] - max[index]),
    ]));
    const fittedVolume = width * loops.reduce((volume, { signed, circle }) =>
      volume + (circle ? Math.sign(signed) * Math.PI * circle.radius ** 2 : signed), 0);
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
    const surfaceBoundaries = loops.flatMap(({ loop, circle }) => {
      const points = circle ? Array.from({ length: 64 }, (_, i) => {
        const a = i * Math.PI / 32;
        return [circle.center[0] + circle.radius * Math.cos(a), circle.center[1] + circle.radius * Math.sin(a)];
      }) : loop;
      const closed = [...points, points[0]];
      return [closed.map(point => xyz(point)), closed.map(point => xyz(point, 1)),
        ...(circle ? [] : points.map(point => [xyz(point), xyz(point, 1)]))];
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
