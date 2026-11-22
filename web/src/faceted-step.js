import { readStl } from './prism-profile.js';
import { getKernel } from './prism-step.js';
import { splitSeparatedBodies } from './mesh-bodies.js';
import { mergePlanar } from './merge-planar.js';

const header = `ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('Faceted solid'),'2;1');
FILE_NAME('reconstruction.step','',(''),(''),'Demeshify','','');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));
ENDSEC;
DATA;
#1=APPLICATION_CONTEXT('automotive design');
#2=APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#1);
#3=PRODUCT_CONTEXT('',#1,'mechanical');
#4=PRODUCT('solid','solid','',(#3));
#5=PRODUCT_DEFINITION_FORMATION('','',#4);
#6=PRODUCT_DEFINITION_CONTEXT('part definition',#1,'design');
#7=PRODUCT_DEFINITION('design','',#5,#6);
#8=PRODUCT_DEFINITION_SHAPE('','',#7);
#9=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));
#10=(NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.));
#11=(NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT());
#12=(GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNIT_ASSIGNED_CONTEXT((#9,#10,#11)) REPRESENTATION_CONTEXT('','3D'));
`;

const refs = ids => `(${ids.map(id => `#${id}`).join(',')})`;
const coords = point => `(${point.map(value => value.toExponential(15).toUpperCase()).join(',')})`;
const subtract = (a, b) => a.map((value, i) => value - b[i]);
const dot = (a, b) => a.reduce((sum, value, i) => sum + value * b[i], 0);
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];

function encodeSolid(mesh, entity, merge) {
  const triangles = [], points = [], normals = [], edges = new Map(), ids = new Map();
  let volume = 0, area = 0;
  for (let offset = 0; offset < mesh.coordinates.length; offset += 9) {
    const vertices = [0, 3, 6].map(i => Array.from(mesh.coordinates.subarray(offset + i, offset + i + 3)));
    const normal = cross(subtract(vertices[1], vertices[0]), subtract(vertices[2], vertices[0]));
    const length = Math.hypot(...normal);
    if (!length) return null;
    normals.push(normal.map(value => value / length));
    area += length / 2;
    volume += dot(subtract(vertices[0], mesh.min), normal) / 6;
    const triangle = vertices.map(point => {
      const key = point.join(',');
      if (!ids.has(key)) {
        ids.set(key, points.length);
        points.push(point);
      }
      return ids.get(key);
    });
    for (let i = 0; i < 3; i++) {
      const a = triangle[i], b = triangle[(i + 1) % 3];
      const key = a < b ? `${a}:${b}` : `${b}:${a}`;
      const mask = a < b ? 1 : 2;
      const old = edges.get(key) || 0;
      if (old & mask) return null;
      edges.set(key, old | mask);
    }
    triangles.push(triangle);
  }
  if (!Math.abs(volume) || [...edges.values()].some(mask => mask !== 3)) return null;

  const pointIds = points.map(point => entity(`CARTESIAN_POINT('',${coords(point)})`));
  const regions = merge ? mergePlanar(points, triangles, normals)
    : triangles.map((triangle, i) => ({ loops: [triangle], normal: normals[i] }));
  const planar = regions.filter(region => region.origin !== undefined).length;
  const retainedEdges = new Map();
  const faces = regions.map(({ loops, normal, origin = loops[0][0] }) => {
    const bounds = loops.map((vertices, i) => {
      if (merge) {
        for (let j = 0; j < vertices.length; j++) {
          const a = vertices[j], b = vertices[(j + 1) % vertices.length];
          const key = a < b ? `${a}:${b}` : `${b}:${a}`;
          retainedEdges.set(key, [points[a], points[b]]);
        }
      }
      const oriented = volume > 0 ? vertices : [...vertices].reverse();
      const loop = entity(`POLY_LOOP('',${refs(oriented.map(id => pointIds[id]))})`);
      return entity(`${i ? 'FACE_BOUND' : 'FACE_OUTER_BOUND'}('',#${loop},.T.)`);
    });
    const direction = entity(`DIRECTION('',${coords(normal.map(x => x * Math.sign(volume)))})`);
    const placement = entity(`AXIS2_PLACEMENT_3D('',#${pointIds[origin]},#${direction},$)`);
    const plane = entity(`PLANE('',#${placement})`);
    return entity(`FACE_SURFACE('',${refs(bounds)},#${plane},.T.)`);
  });
  const shell = entity(`CLOSED_SHELL('',${refs(faces)})`);
  return {
    solid: entity(`FACETED_BREP('',#${shell})`), volume: Math.abs(volume), area, faces: faces.length,
    vertices: points.length, edges: edges.size, planar, boundaries: [...retainedEdges.values()],
  };
}

function encodeSolids(meshes, merge = false) {
  const rows = [header];
  let last = 12;
  function entity(value) {
    rows.push(`#${++last}=${value};\n`);
    return last;
  }
  const bodies = meshes.map(mesh => encodeSolid(mesh, entity, merge));
  if (bodies.some(body => !body)) return null;
  const representation = entity(`SHAPE_REPRESENTATION('',${refs(bodies.map(body => body.solid))},#12)`);
  entity(`SHAPE_DEFINITION_REPRESENTATION(#8,#${representation})`);
  rows.push('ENDSEC;\nEND-ISO-10303-21;\n');
  return { step: rows.join(''), bodies };
}

function previewStl(mesh) {
  const bytes = new Uint8Array(84 + mesh.coordinates.length / 9 * 50);
  const view = new DataView(bytes.buffer);
  view.setUint32(80, mesh.coordinates.length / 9, true);
  for (let i = 0; i < mesh.coordinates.length; i += 9) {
    const start = 96 + i / 9 * 50;
    for (let j = 0; j < 9; j++) view.setFloat32(start + 4 * j, mesh.coordinates[i + j], true);
  }
  return bytes;
}

function recoverPlanes(kernel, { step, bodies }, original, maxFaces) {
  const merged = kernel.importStep(step);
  if (!kernel.isValid(merged) || kernel.subShapeCount(merged, 'solid') !== bodies.length
      || kernel.subShapeCount(merged, 'shell') !== bodies.length) return null;
  const faceCount = kernel.subShapeCount(merged, 'face');
  if (faceCount >= maxFaces || faceCount !== bodies.reduce((sum, body) => sum + body.faces, 0)) return null;
  const volumeError = 100 * Math.abs(kernel.getVolume(merged) / original.volume - 1);
  const areaError = Math.abs(kernel.getSurfaceArea(merged) / original.area - 1);
  if (!Number.isFinite(volumeError + areaError) || volumeError > 0.001 || areaError > 1e-5) return null;

  // The output uses the source boundary loops. The same tessellation can be used
  // for display, with only the retained STEP edges drawn over it.
  return { step, faceCount, volumeError, planarCount: bodies.reduce((sum, body) => sum + body.planar, 0),
    vertices: kernel.subShapeCount(merged, 'vertex'), edges: kernel.subShapeCount(merged, 'edge'),
    stepEdges: bodies.flatMap(body => body.boundaries) };
}

export async function buildFaceted(job, started, reason) {
  if (job.bytes.byteLength > 4 * 1024 * 1024) return null;
  const mesh = readStl(job.bytes, job.units);
  const automatic = Boolean(reason) && job.mode !== 'faceted';
  if (!mesh || mesh.coordinates.length / 9 > (automatic ? 25_000 : 20_000)) return null;
  const meshes = splitSeparatedBodies(mesh);
  const encoded = meshes && encodeSolids(meshes);
  if (!encoded) return null;
  const kernel = await getKernel();
  const checkpoint = kernel.checkpoint();
  try {
    const count = encoded.bodies.length;
    const total = (field) => encoded.bodies.reduce((sum, body) => sum + body[field], 0);
    const faces = total('faces'), vertices = total('vertices'), edges = total('edges');
    let recovered;
    if (automatic) {
      try {
        const compact = encodeSolids(meshes, true);
        if (compact && compact.bodies.reduce((sum, body) => sum + body.faces, 0) < faces) {
          recovered = recoverPlanes(kernel, compact,
            { volume: total('volume'), area: total('area') }, faces);
        }
      } catch {
        // Keep the verified source geometry if an optional recovery fails.
      }
    }
    let volumeError = recovered?.volumeError;
    if (!recovered) {
      const reopened = kernel.importStep(encoded.step);
      if (!kernel.isValid(reopened) || kernel.subShapeCount(reopened, 'solid') !== count
          || kernel.subShapeCount(reopened, 'shell') !== count
          || kernel.subShapeCount(reopened, 'face') !== faces
          || kernel.subShapeCount(reopened, 'vertex') !== vertices
          || kernel.subShapeCount(reopened, 'edge') !== edges) return null;
      volumeError = 100 * Math.abs(kernel.getVolume(reopened) / total('volume') - 1);
      const areaError = Math.abs(kernel.getSurfaceArea(reopened) / total('area') - 1);
      if (!Number.isFinite(volumeError + areaError) || volumeError > 0.001 || areaError > 1e-5) return null;
    }
    const report = {
      id: job.id, engine: 'surface_brep', implementation: recovered ? 'hybrid' : 'faceted', status: 'best_effort',
      mesh: { file_name: job.filename, unit_scale: mesh.factor, watertight: true,
        triangle_count: faces },
      surface: { closed: true, solid_count: count, free_edge_count: 0, brep_face_count: recovered?.faceCount ?? faces,
        recognized_surface_count: recovered?.planarCount ?? 0,
        surface_counts: recovered ? { plane: recovered.planarCount } : {}, faceted_fallback: true,
        source_mesh_fallback: true, fallback_geometry_verified: true,
        faceted_face_count: recovered ? recovered.faceCount - recovered.planarCount : faces,
        faceted_patch_count: count, topology_vertex_count: recovered?.vertices ?? vertices,
        topology_edge_count: recovered?.edges ?? edges, adjacency_count: recovered?.edges ?? edges },
      score: { volume_error_percent: volumeError,
        volume_comparable: true, valid_brep: true, valid_solid: true, component_count_match: true,
        step_geometry_verified: true },
      warnings: [...(reason ? [reason] : []), recovered
        ? 'Coplanar triangles were merged into CAD faces. Other regions retain the source facets.'
        : 'The STEP preserves the STL triangles as CAD faces.'],
      elapsed_seconds: (performance.now() - started) / 1000,
    };
    const text = new TextEncoder();
    return { report, files: {
      'reconstruction.step': text.encode(recovered?.step ?? encoded.step),
      'reconstruction.stl': previewStl(mesh),
      'report.json': text.encode(JSON.stringify(report, null, 2)),
      'surface_graph.json': text.encode(JSON.stringify({ visualization: recovered
        ? { step_edges: recovered.stepEdges } : { global_faceted_fallback: true } })),
    } };
  } finally {
    kernel.releaseSince(checkpoint);
  }
}
