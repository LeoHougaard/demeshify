const vec = ([x, y, z]) => ({ x, y, z });

function faceFromSegments(kernel, segments) {
  const point = ([x, y]) => ({ x, y, z: 0 });
  const wire = kernel.makeWire(segments.map(({ a, b, mid }) => mid
    ? kernel.makeArcEdge(point(a), point(mid), point(b)) : kernel.makeLineEdge(point(a), point(b))));
  return kernel.makeFace(wire);
}

function boreTools(bores, epsilon) {
  const tolerance = 32 * epsilon;
  const tools = [];
  for (const { center, low, height, r0, r1 } of bores) {
    const tool = tools.find(item => Math.hypot(center[0] - item.center[0], center[1] - item.center[1]) < 4 * epsilon
      && Math.abs(item.radii.at(-1)[1] - low) < epsilon);
    if (!tool) {
      tools.push({ center, radii: [[r0, low], [r1, low + height]] });
      continue;
    }
    const start = tool.radii.at(-1);
    if (Math.abs(start[0] - r0) > tolerance) tool.radii.push([r0, low]);
    const radius = tool.radii.at(-1)[0];
    tool.radii.push([Math.abs(radius - r1) < tolerance ? radius : r1, low + height]);
  }
  for (const tool of tools) {
    const meridian = [];
    for (const point of tool.radii) {
      while (meridian.length >= 2) {
        const [a, b] = meridian.slice(-2);
        if (b[1] <= a[1] || point[1] <= b[1]) break;
        const radius = a[0] + (point[0] - a[0]) * (b[1] - a[1]) / (point[1] - a[1]);
        if (Math.abs(radius - b[0]) > tolerance) break;
        meridian.pop();
      }
      meridian.push(point);
    }
    tool.radii = meridian;
  }
  return tools;
}

function makeBore(kernel, { center: [x, y], radii }) {
  const meridian = [[0, radii[0][1]], ...radii, [0, radii.at(-1)[1]]];
  const edges = meridian.map(([r, h], i) => {
    const [nextR, nextH] = meridian[(i + 1) % meridian.length];
    return kernel.makeLineEdge(vec([x + r, y, h]), vec([x + nextR, y, nextH]));
  });
  return kernel.revolve(kernel.makeFace(kernel.makeWire(edges)), {
    point: vec([x, y, 0]), direction: vec([0, 0, 1]),
  }, 2 * Math.PI);
}

export function constructLayered(kernel, { mesh, profile, height, lowerChamfer, upperChamfer, bores, epsilon }) {
  const outer = profile.find(ring => ring.area > 0);
  const extrude = ring => kernel.extrude(faceFromSegments(kernel, ring.segments), 0, 0, height);
  let solid = extrude(outer);
  for (const slot of profile.filter(ring => ring !== outer)) solid = kernel.cut(solid, extrude(slot));

  const rim = [], widths = [];
  for (const edge of kernel.getSubShapes(solid, 'edge')) {
    const bounds = kernel.getBoundingBox(edge, false);
    if (bounds.zmax - bounds.zmin > epsilon) continue;
    if (Math.abs(bounds.zmin) <= epsilon) {
      rim.push(edge);
      widths.push(lowerChamfer);
    } else if (Math.abs(height - bounds.zmax) <= epsilon) {
      rim.push(edge);
      widths.push(upperChamfer);
    }
  }
  if (!rim.length) return null;
  solid = kernel.chamfer(solid, rim, widths);
  for (const bore of boreTools(bores, epsilon)) solid = kernel.cut(solid, makeBore(kernel, bore));
  return kernel.translate(kernel.unifySameDomain(solid), ...mesh.min);
}
import { meshAgreement } from './mesh-agreement.js';
import { readStl } from './prism-profile.js';


export async function buildLayered(proposal, job, started) {
  const { getKernel } = await import('./prism-step.js');
  const kernel = await getKernel();
  const checkpoint = kernel.checkpoint();
  try {
    const solid = constructLayered(kernel, proposal);
    if (!solid || !kernel.isValid(solid) || !(proposal.volume > 0)) return null;
    const step = kernel.exportStep(solid);
    const reopened = kernel.importStep(step);
    if (!kernel.isValid(reopened) || kernel.subShapeCount(reopened, 'solid') !== 1
        || kernel.subShapeCount(reopened, 'shell') !== 1) return null;
    const volumeError = 100 * Math.abs(kernel.getVolume(reopened) / proposal.volume - 1);
    if (!Number.isFinite(volumeError) || volumeError > 2) return null;

    const { mesh } = proposal;
    const tolerance = Math.min(0.02, mesh.diagonal * 0.0001);
    const preview = kernel.exportStl(reopened, tolerance);
    const sampled = readStl(preview.buffer.slice(preview.byteOffset, preview.byteOffset + preview.byteLength), 'mm');
    const limit = Math.max(0.12, 0.003 * mesh.diagonal) / 2;
    const agreement = sampled && meshAgreement(mesh.coordinates, sampled.coordinates, limit);
    if (!agreement) return null;
    const counts = {};
    for (const face of kernel.getSubShapes(reopened, 'face')) {
      const kind = kernel.surfaceType(face);
      if (kind !== 'plane' && kind !== 'cone' && kind !== 'cylinder') return null;
      const perimeter = kernel.getLength(face);
      const width = 2 * kernel.getSurfaceArea(face) / perimeter;
      if (width < Math.min(0.025, mesh.diagonal * 0.00015) && width * 400 < perimeter) return null;
      counts[kind] = (counts[kind] || 0) + 1;
    }
    const faces = kernel.subShapeCount(reopened, 'face');
    const edges = kernel.subShapeCount(reopened, 'edge');
    const report = {
      id: job.id, engine: 'surface_brep', implementation: 'layered', status: 'complete',
      mesh: { file_name: job.filename, unit_scale: mesh.factor, watertight: true,
        triangle_count: mesh.coordinates.length / 9 },
      surface: { closed: true, solid_count: 1, free_edge_count: 0, brep_face_count: faces,
        recognized_surface_count: faces, surface_counts: counts, faceted_fallback: false,
        topology_vertex_count: kernel.subShapeCount(reopened, 'vertex'), topology_edge_count: edges,
        adjacency_count: edges },
      score: { chamfer_p95_mm: agreement.p95, chamfer_max_mm: agreement.max, local_max_mm: agreement.max,
        volume_error_percent: volumeError, volume_comparable: true, valid_brep: true, valid_solid: true,
        component_count_match: true, step_geometry_verified: true },
      warnings: [], elapsed_seconds: (performance.now() - started) / 1000,
    };
    const text = new TextEncoder();
    return { report, files: {
      'reconstruction.step': text.encode(step), 'reconstruction.stl': preview,
      'report.json': text.encode(JSON.stringify(report, null, 2)),
      'surface_graph.json': text.encode(JSON.stringify({ surface_counts: counts, visualization: {} })),
    } };
  } finally {
    kernel.releaseSince(checkpoint);
  }
}
