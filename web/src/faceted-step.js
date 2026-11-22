import { readStl } from './prism-profile.js';
import { getKernel } from './prism-step.js';

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

function encodeSolid(mesh) {
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

  const rows = [header];
  let last = 12;
  function entity(value) {
    rows.push(`#${++last}=${value};\n`);
    return last;
  }
  const pointIds = points.map(point => entity(`CARTESIAN_POINT('',${coords(point)})`));
  const faces = triangles.map((triangle, i) => {
    const points = (volume > 0 ? triangle : [...triangle].reverse()).map(id => pointIds[id]);
    const loop = entity(`POLY_LOOP('',${refs(points)})`);
    const bound = entity(`FACE_OUTER_BOUND('',#${loop},.T.)`);
    const direction = entity(`DIRECTION('',${coords(normals[i].map(x => x * Math.sign(volume)))})`);
    const placement = entity(`AXIS2_PLACEMENT_3D('',#${points[0]},#${direction},$)`);
    const plane = entity(`PLANE('',#${placement})`);
    return entity(`FACE_SURFACE('',(#${bound}),#${plane},.T.)`);
  });
  const shell = entity(`CLOSED_SHELL('',${refs(faces)})`);
  const solid = entity(`FACETED_BREP('',#${shell})`);
  const representation = entity(`SHAPE_REPRESENTATION('',(#${solid}),#12)`);
  entity(`SHAPE_DEFINITION_REPRESENTATION(#8,#${representation})`);
  rows.push('ENDSEC;\nEND-ISO-10303-21;\n');
  return { step: rows.join(''), volume: Math.abs(volume), area, faces: faces.length,
    vertices: points.length, edges: edges.size };
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

export async function buildFaceted(job, started) {
  if (job.bytes.byteLength > 4 * 1024 * 1024) return null;
  const mesh = readStl(job.bytes, job.units);
  if (!mesh || mesh.coordinates.length / 9 > 20000) return null;
  const encoded = encodeSolid(mesh);
  if (!encoded) return null;
  const kernel = await getKernel();
  const checkpoint = kernel.checkpoint();
  try {
    const reopened = kernel.importStep(encoded.step);
    if (!kernel.isValid(reopened) || kernel.subShapeCount(reopened, 'solid') !== 1
        || kernel.subShapeCount(reopened, 'shell') !== 1
        || kernel.subShapeCount(reopened, 'face') !== encoded.faces
        || kernel.subShapeCount(reopened, 'vertex') !== encoded.vertices
        || kernel.subShapeCount(reopened, 'edge') !== encoded.edges) return null;
    const volumeError = 100 * Math.abs(kernel.getVolume(reopened) / encoded.volume - 1);
    const areaError = Math.abs(kernel.getSurfaceArea(reopened) / encoded.area - 1);
    if (!Number.isFinite(volumeError + areaError) || volumeError > 0.001 || areaError > 1e-5) return null;
    const report = {
      id: job.id, engine: 'surface_brep', implementation: 'faceted', status: 'best_effort',
      mesh: { file_name: job.filename, unit_scale: mesh.factor, watertight: true,
        triangle_count: encoded.faces },
      surface: { closed: true, solid_count: 1, free_edge_count: 0, brep_face_count: encoded.faces,
        recognized_surface_count: 0, surface_counts: {}, faceted_fallback: true,
        source_mesh_fallback: true, fallback_geometry_verified: true, faceted_face_count: encoded.faces,
        faceted_patch_count: 1, topology_vertex_count: encoded.vertices, topology_edge_count: encoded.edges,
        adjacency_count: encoded.edges },
      score: { volume_error_percent: volumeError,
        volume_comparable: true, valid_brep: true, valid_solid: true, component_count_match: true,
        step_geometry_verified: true },
      warnings: ['The STEP preserves the STL triangles as CAD faces.'],
      elapsed_seconds: (performance.now() - started) / 1000,
    };
    const text = new TextEncoder();
    return { report, files: {
      'reconstruction.step': text.encode(encoded.step), 'reconstruction.stl': previewStl(mesh),
      'report.json': text.encode(JSON.stringify(report, null, 2)),
      'surface_graph.json': text.encode(JSON.stringify({ visualization: { global_faceted_fallback: true } })),
    } };
  } finally {
    kernel.releaseSince(checkpoint);
  }
}
