import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import {
  mergeVertices,
  toCreasedNormals,
} from "three/examples/jsm/utils/BufferGeometryUtils.js";
import "./styles.css";

const app = document.querySelector("#app");
let selectedFile = null;
let inputViewer;
let outputViewer;
let livePreviewRevision = 0;
let livePreviewLoading = false;
let activeReport = null;
let editablePlan = null;
let selectedFeatureId = null;
let undoStack = [];
let redoStack = [];
let editorBusy = false;

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

/* Previous multi-section layout retained temporarily for migration reference.
  <header class="topbar">
    <a class="brand" href="/">
      <span class="brand-mark"><i></i><i></i><i></i></span>
      <span>STL to STEP <b>Converter</b></span>
    </a>
    <div id="engineStatus" class="status-pill"><span></span> Checking reconstruction engine</div>
  </header>
  <main>
    <section class="hero">
      <div class="eyebrow">Mesh → manufacturing intent</div>
      <h1>Turn mechanical STL files into <em>editable CAD.</em></h1>
      <p>Automatic feature inference, geometric verification, and clean STEP export—without a human reverse-engineering pass.</p>
    </section>

    <section class="workspace">
      <aside class="control-card">
        <div class="step-label"><span>01</span> Source mesh</div>
        <label id="dropzone" class="dropzone">
          <input id="fileInput" type="file" accept=".stl,model/stl" />
          <div class="upload-icon">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M5 14v5h14v-5"/></svg>
          </div>
          <strong>Drop your STL here</strong>
          <span>or choose a file · up to 250 MB</span>
        </label>
        <div id="fileChip" class="file-chip hidden"></div>

        <div class="field-row">
          <label class="field">
            <span>STL units</span>
            <select id="units">
              <option value="mm">Millimetres</option>
              <option value="in">Inches</option>
              <option value="cm">Centimetres</option>
              <option value="m">Metres</option>
            </select>
          </label>
          <label class="field">
            <span>Reconstruction method</span>
            <select id="engine">
              <option value="surface_brep">Recognized surfaces (recommended)</option>
              <option value="feature_tree">Parametric feature history</option>
            </select>
            <small>Surface mode fits analytic and B-spline faces to mesh nodes, then sews them where their boundaries close.</small>
          </label>
        </div>

        <div class="step-label prompt-label"><span>02</span> Optional design intent</div>
        <label class="field">
          <span id="promptLabel">Tell the AI what matters</span>
          <textarea id="prompt" rows="3" placeholder="e.g. Keep the four mounting holes exact and use nominal dimensions"></textarea>
          <small id="promptHelp">Checking whether LLM refinement is configured.</small>
        </label>

        <button id="reconstructButton" class="primary-button" disabled>
          <span>Reconstruct editable CAD</span>
          <svg viewBox="0 0 24 24"><path d="M5 12h14m-5-5 5 5-5 5"/></svg>
        </button>
        <div id="errorBox" class="error-box hidden"></div>
      </aside>

      <div class="visual-card">
        <div class="visual-toolbar">
          <div>
            <span class="visual-title">Geometry preview</span>
            <span id="previewSubtitle" class="visual-subtitle">Waiting for an STL</span>
          </div>
          <div id="viewTabs" class="view-tabs hidden">
            <button data-view="input">Input mesh</button>
            <button class="active" data-view="output">CAD result</button>
          </div>
        </div>
        <div id="viewer" class="viewer">
          <div id="viewerEmpty" class="viewer-empty">
            <div class="grid-cube"><span></span><span></span><span></span></div>
            <p>Your model will appear here</p>
          </div>
          <canvas id="inputCanvas"></canvas>
          <canvas id="outputCanvas" class="hidden"></canvas>
          <div id="surfaceLegend" class="surface-legend hidden">
            <span><i class="fitted-edge"></i>Fitted surface boundary</span>
            <span><i class="faceted-edge"></i>Fallback triangle edges</span>
            <small id="surfaceLegendStatus"></small>
          </div>
          <div id="processing" class="processing hidden">
            <div class="processing-card">
              <div class="processing-heading">
                <div class="scan-object"><span></span></div>
                <div>
                  <small>LIVE · PROVISIONAL CAD</small>
                  <strong id="processingStage">Starting reconstruction</strong>
                </div>
              </div>
              <p id="processingText">Preparing the local geometry engine</p>
              <div class="progress"><i id="progressBar"></i></div>
              <div class="progress-meta">
                <span id="progressPercent">0%</span>
                <span id="progressElapsed">0 seconds</span>
              </div>
              <p id="progressCounts" class="progress-counts">Waiting for geometry data</p>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="results" class="results hidden">
      <div class="result-heading">
        <div>
          <div class="eyebrow">Reconstruction report</div>
          <h2 id="resultTitle">Editable model ready</h2>
        </div>
        <div id="confidence" class="confidence"></div>
      </div>
      <div class="metric-grid">
        <article><span>Surface deviation P95</span><strong id="p95">—</strong><small>millimetres</small></article>
        <article><span>Volume difference</span><strong id="volumeError">—</strong><small>percent</small></article>
        <article><span>Detected construction</span><strong id="construction">—</strong><small id="constructionUnit">CAD representation</small></article>
        <article><span>Processing time</span><strong id="elapsed">—</strong><small>seconds</small></article>
      </div>
      <div class="result-grid">
        <article class="feature-card">
          <h3 id="constructionHeading">Editable feature plan</h3>
          <div id="featurePlan"></div>
        </article>
        <article class="download-card">
          <h3>Export model</h3>
          <p>STEP is compatible with Fusion, SolidWorks, FreeCAD, Onshape, and most mechanical CAD systems.</p>
          <div id="downloads" class="downloads"></div>
        </article>
      </div>
      <article id="featureEditor" class="feature-editor hidden">
        <div class="editor-heading">
          <div>
            <div class="step-label"><span>03</span> Parametric feature tree</div>
            <h3>Edit the reconstruction before export</h3>
            <p>Change measured dimensions, suppress or reorder operations, then rebuild and verify against the original STL.</p>
          </div>
          <div class="editor-history-actions">
            <button id="undoEdit" type="button" title="Undo local edit">Undo</button>
            <button id="redoEdit" type="button" title="Redo local edit">Redo</button>
            <button id="resetAllDimensions" type="button" title="Restore every measured value">Reset measured</button>
          </div>
        </div>
        <div class="editor-layout">
          <aside class="timeline-panel">
            <div class="panel-label">Ordered history <small id="treeRevision"></small></div>
            <div id="featureTree" class="feature-tree"></div>
          </aside>
          <section class="parameter-panel">
            <div id="parameterHeader" class="parameter-header"></div>
            <div id="parameterEditor" class="parameter-editor"></div>
          </section>
        </div>
        <div class="editor-footer">
          <div id="editorFeedback" class="editor-feedback"></div>
          <button id="applyFeatureEdits" class="apply-edit-button" type="button">
            Apply, rebuild &amp; verify
          </button>
        </div>
      </article>
      <div id="warnings"></div>
    </section>
  </main>
  <footer><span>STL to STEP Converter · local-first reverse engineering</span><span>Outputs are verified against the source mesh</span></footer>
*/

// The converter is deliberately a single-screen tool. Detailed controls and
// diagnostic output live in the Advanced drawer so the primary workflow stays
// focused on import, convert, inspect, and export.
app.innerHTML = `
  <header class="topbar">
    <a class="brand" href="/" aria-label="STL to STEP Converter home">
      <span class="brand-file">STL</span>
      <span>STL to STEP <b>Converter</b></span>
    </a>
    <div class="header-actions">
      <div id="engineStatus" class="status-pill"><span></span> Checking converter</div>
      <button id="advancedToggle" class="advanced-toggle" type="button">Advanced</button>
    </div>
  </header>

  <main class="converter-shell">
    <aside class="converter-sidebar">
      <section class="import-panel">
        <div class="section-heading"><span>1</span><div><strong>Import STL</strong><small>Choose the mesh to convert</small></div></div>
        <label id="dropzone" class="dropzone compact-dropzone">
          <input id="fileInput" type="file" accept=".stl,model/stl" />
          <div class="upload-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M5 14v5h14v-5"/></svg></div>
          <strong>Drop STL here</strong><span>or click to choose a file</span>
        </label>
        <div id="fileChip" class="file-chip hidden"></div>
      </section>

      <button id="reconstructButton" class="primary-button convert-button" disabled>
        <span>Convert to STEP</span><svg viewBox="0 0 24 24"><path d="M5 12h14m-5-5 5 5-5 5"/></svg>
      </button>
      <div id="errorBox" class="error-box hidden"></div>

      <section id="results" class="compact-results hidden">
        <div class="compact-result-heading">
          <div><small>Conversion result</small><strong id="resultTitle">STEP file ready</strong></div>
          <div id="confidence" class="confidence"></div>
        </div>
        <div class="quick-metrics">
          <article><span>Deviation P95</span><strong id="p95">—</strong><small>mm</small></article>
          <article><span>Construction</span><strong id="construction">—</strong><small id="constructionUnit">CAD model</small></article>
          <article><span>Time</span><strong id="elapsed">—</strong><small>seconds</small></article>
        </div>
        <div id="downloads" class="downloads primary-download"></div>
        <button id="openResultDetails" class="text-button" type="button">View conversion details</button>
      </section>
    </aside>

    <section class="viewer-panel">
      <div class="visual-toolbar">
        <div><span class="visual-title">Model viewer</span><span id="previewSubtitle" class="visual-subtitle">Import an STL to begin</span></div>
        <div id="viewTabs" class="view-tabs hidden"><button data-view="input">Input mesh</button><button class="active" data-view="output">STEP result</button></div>
      </div>
      <div id="viewer" class="viewer">
        <div id="viewerEmpty" class="viewer-empty"><div class="empty-file">STL</div><strong>Import a model to inspect it</strong><p>Drag to orbit · scroll to zoom</p></div>
        <canvas id="inputCanvas"></canvas><canvas id="outputCanvas" class="hidden"></canvas>
        <div id="surfaceLegend" class="surface-legend hidden"><span><i class="fitted-edge"></i>Fitted surface boundary</span><span><i class="faceted-edge"></i>Fallback triangle edges</span><small id="surfaceLegendStatus"></small></div>
        <div id="processing" class="processing hidden">
          <div class="processing-card">
            <div class="processing-heading"><div class="scan-object"><span></span></div><div><small>CONVERTING</small><strong id="processingStage">Starting reconstruction</strong></div></div>
            <p id="processingText">Preparing the geometry engine</p><div class="progress"><i id="progressBar"></i></div>
            <div class="progress-meta"><span id="progressPercent">0%</span><span id="progressElapsed">0 seconds</span></div><p id="progressCounts" class="progress-counts">Waiting for geometry data</p>
          </div>
        </div>
      </div>
    </section>
  </main>

  <div id="drawerBackdrop" class="drawer-backdrop hidden"></div>
  <aside id="advancedDrawer" class="advanced-drawer hidden" aria-label="Advanced converter options">
    <header class="drawer-header"><div><small>Optional controls</small><strong>Advanced</strong></div><button id="advancedClose" type="button" aria-label="Close advanced settings">×</button></header>
    <div class="drawer-content">
      <section class="advanced-section">
        <h2>Conversion settings</h2>
        <div class="advanced-settings-grid">
          <label class="field"><span>STL units</span><select id="units"><option value="mm">Millimetres</option><option value="in">Inches</option><option value="cm">Centimetres</option><option value="m">Metres</option></select></label>
          <label class="field"><span>Reconstruction method</span><select id="engine"><option value="surface_brep">Recognized surfaces (recommended)</option><option value="feature_tree">Parametric feature history</option></select><small>Fits analytic and B-spline surfaces, then joins their boundaries.</small></label>
        </div>
        <label class="field advanced-prompt"><span id="promptLabel">Design intent is not needed</span><textarea id="prompt" rows="3" placeholder="For feature-history mode: keep the mounting holes exact"></textarea><small id="promptHelp">Surface mode reconstructs the final boundary directly from mesh geometry.</small></label>
      </section>

      <section class="advanced-section result-details">
        <h2>Conversion details</h2><div class="detail-metrics"><span>Volume difference</span><strong id="volumeError">—</strong><small>percent</small></div>
        <h3 id="constructionHeading">Detected surfaces</h3><div id="featurePlan" class="advanced-feature-plan"></div>
        <h3>Additional files</h3><div id="advancedDownloads" class="downloads advanced-downloads"></div><div id="warnings"></div>
      </section>

      <article id="featureEditor" class="feature-editor hidden">
        <div class="editor-heading"><div><h3>Edit feature history</h3><p>Adjust measured dimensions, reorder operations, then rebuild and verify.</p></div><div class="editor-history-actions"><button id="undoEdit" type="button">Undo</button><button id="redoEdit" type="button">Redo</button><button id="resetAllDimensions" type="button">Reset measured</button></div></div>
        <div class="editor-layout"><aside class="timeline-panel"><div class="panel-label">Ordered history <small id="treeRevision"></small></div><div id="featureTree" class="feature-tree"></div></aside><section class="parameter-panel"><div id="parameterHeader" class="parameter-header"></div><div id="parameterEditor" class="parameter-editor"></div></section></div>
        <div class="editor-footer"><div id="editorFeedback" class="editor-feedback"></div><button id="applyFeatureEdits" class="apply-edit-button" type="button">Apply, rebuild &amp; verify</button></div>
      </article>
    </div>
  </aside>
`;

let aiConfigured = false;

function updateEngineCopy() {
  const surfaceMode = document.querySelector("#engine").value === "surface_brep";
  const prompt = document.querySelector("#prompt");
  prompt.disabled = surfaceMode;
  document.querySelector("#promptLabel").textContent = surfaceMode
    ? "Design intent is not needed"
    : "Tell the AI what matters";
  document.querySelector("#promptHelp").textContent = surfaceMode
    ? "Surface mode reconstructs the final boundary directly from mesh geometry."
    : aiConfigured
      ? "Your instruction may revise the measured feature plan; geometry scoring still gates it."
      : "LLM refinement is off. Instructions are saved, but reconstruction remains deterministic.";
  document.querySelector("#reconstructButton span").textContent = "Convert to STEP";
}

document.querySelector("#engine").addEventListener("change", updateEngineCopy);
updateEngineCopy();

fetch("/api/health")
  .then((response) => response.json())
  .then((health) => {
    aiConfigured = Boolean(health.ai_configured);
    document.querySelector("#engineStatus").innerHTML = "<span></span> Converter ready";
    updateEngineCopy();
  })
  .catch(() => {
    document.querySelector("#engineStatus").innerHTML = "<span></span> Converter offline";
  });

class MeshViewer {
  constructor(canvas, color) {
    this.canvas = canvas;
    this.color = color;
    this.cadStyle = true;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.scene = new THREE.Scene();
    if (this.cadStyle) this.scene.background = new THREE.Color(0x303234);
    this.camera = new THREE.PerspectiveCamera(38, 1, 0.01, 100000);
    this.camera.position.set(4, 3, 4);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.scene.add(
      new THREE.HemisphereLight(0xffffff, 0x243044, this.cadStyle ? 1.65 : 2.2),
    );
    const key = new THREE.DirectionalLight(0xffffff, this.cadStyle ? 2.3 : 3.2);
    key.position.set(3, 5, 4);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(
      this.cadStyle ? 0xaad8eb : 0x4db7ff,
      this.cadStyle ? 0.85 : 2,
    );
    rim.position.set(-4, 1, -3);
    this.scene.add(rim);
    this.animate();
  }

  async load(buffer, { resetCamera = true } = {}) {
    const rawGeometry = new STLLoader().parse(buffer);
    rawGeometry.computeBoundingBox();
    const rawBox = rawGeometry.boundingBox;
    const rawSize = rawBox.getSize(new THREE.Vector3());
    const topologyGeometry = rawGeometry.clone();
    topologyGeometry.deleteAttribute("normal");
    const weldedGeometry = mergeVertices(
      topologyGeometry,
      Math.max(rawSize.length() * 1e-7, 1e-8),
    );
    // STL stores independent triangle normals, so computeVertexNormals alone
    // still makes exact CAD cylinders look polygonal. Average only across
    // shallow tessellation edges; real mechanical creases remain sharp.
    const geometry = toCreasedNormals(rawGeometry, THREE.MathUtils.degToRad(30));
    const hadMesh = Boolean(this.mesh);
    this.clearSurfaceOverlay();
    if (this.mesh) {
      this.scene.remove(this.mesh);
      this.mesh.geometry.dispose();
      this.mesh.material.dispose();
    }
    if (this.cadEdges) {
      this.scene.remove(this.cadEdges);
      this.cadEdges.geometry.dispose();
      this.cadEdges.material.dispose();
      this.cadEdges = null;
    }
    const material = new THREE.MeshStandardMaterial({
      color: this.color,
      metalness: this.cadStyle ? 0.04 : 0.18,
      roughness: this.cadStyle ? 0.56 : 0.34,
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    });
    this.mesh = new THREE.Mesh(geometry, material);
    geometry.computeBoundingBox();
    const box = geometry.boundingBox;
    const center = box.getCenter(new THREE.Vector3());
    this.modelCenter = center.clone();
    this.mesh.position.sub(center);
    this.scene.add(this.mesh);
    if (this.cadStyle) {
      this.cadEdges = new THREE.LineSegments(
        new THREE.EdgesGeometry(weldedGeometry, 1),
        new THREE.LineBasicMaterial({ color: 0x17242c, depthTest: true }),
      );
      this.cadEdges.position.sub(center);
      this.cadEdges.renderOrder = 2;
      this.scene.add(this.cadEdges);
    }
    topologyGeometry.dispose();
    weldedGeometry.dispose();
    if (resetCamera || !hadMesh) {
      const size = box.getSize(new THREE.Vector3());
      const radius = Math.max(size.length() * 0.65, 1);
      this.camera.near = radius / 1000;
      this.camera.far = radius * 100;
      this.camera.position.set(radius, radius * 0.72, radius);
      this.camera.updateProjectionMatrix();
      this.controls.target.set(0, 0, 0);
      this.controls.update();
    }
  }

  clearSurfaceOverlay() {
    if (!this.surfaceOverlay) return;
    this.scene.remove(this.surfaceOverlay);
    this.surfaceOverlay.traverse((item) => {
      if (!item.userData.sharedGeometry) item.geometry?.dispose();
      item.material?.dispose();
    });
    this.surfaceOverlay = null;
  }

  showSurfaceOverlay(surfaceGraph, sourceBuffer, unitScale = 1) {
    this.clearSurfaceOverlay();
    if (!this.mesh || !this.modelCenter) return { boundaryCount: 0, triangleCount: 0 };

    const visualization = surfaceGraph?.visualization || {};
    const group = new THREE.Group();
    group.position.copy(this.modelCenter).multiplyScalar(-1);
    let boundaryCount = 0;
    let triangleCount = 0;

    if (!visualization.global_faceted_fallback) {
      const boundaryPositions = [];
      for (const curve of visualization.surface_boundaries || []) {
        if (!Array.isArray(curve) || curve.length < 2) continue;
        for (let index = 0; index < curve.length - 1; index += 1) {
          const first = curve[index];
          const second = curve[index + 1];
          if (first?.length !== 3 || second?.length !== 3) continue;
          boundaryPositions.push(...first, ...second);
        }
        boundaryCount += 1;
      }
      if (boundaryPositions.length) {
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute(
          "position",
          new THREE.Float32BufferAttribute(boundaryPositions, 3),
        );
        group.add(
          new THREE.LineSegments(
            geometry,
            new THREE.LineBasicMaterial({ color: 0x17242c, depthTest: true }),
          ),
        );
      }
    }

    const sourceGeometry = new STLLoader().parse(sourceBuffer);
    const sourcePositions = sourceGeometry.getAttribute("position");
    const sourceFaceCount = Math.floor(sourcePositions.count / 3);
    if (visualization.global_faceted_fallback) {
      const wireframe = new THREE.Mesh(
        this.mesh.geometry,
        new THREE.MeshBasicMaterial({
          color: 0x111a20,
          wireframe: true,
          transparent: true,
          opacity: 0.92,
          depthTest: true,
        }),
      );
      wireframe.userData.sharedGeometry = true;
      group.add(wireframe);
      triangleCount = sourceFaceCount;
    } else {
      const requestedFaces = visualization.faceted_source_face_indices || [];
      const trianglePositions = [];
      for (const rawFaceIndex of requestedFaces) {
        const faceIndex = Number(rawFaceIndex);
        if (!Number.isInteger(faceIndex) || faceIndex < 0 || faceIndex >= sourceFaceCount) continue;
        const points = [0, 1, 2].map((offset) => {
          const vertexIndex = faceIndex * 3 + offset;
          return [
            sourcePositions.getX(vertexIndex) * unitScale,
            sourcePositions.getY(vertexIndex) * unitScale,
            sourcePositions.getZ(vertexIndex) * unitScale,
          ];
        });
        trianglePositions.push(
          ...points[0], ...points[1],
          ...points[1], ...points[2],
          ...points[2], ...points[0],
        );
        triangleCount += 1;
      }
      if (trianglePositions.length) {
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute(
          "position",
          new THREE.Float32BufferAttribute(trianglePositions, 3),
        );
        group.add(
          new THREE.LineSegments(
            geometry,
            new THREE.LineBasicMaterial({ color: 0x111a20, depthTest: true }),
          ),
        );
      }
    }
    sourceGeometry.dispose();

    this.surfaceOverlay = group;
    this.scene.add(group);
    return { boundaryCount, triangleCount };
  }

  resize() {
    const parent = this.canvas.parentElement;
    const width = parent.clientWidth;
    const height = parent.clientHeight;
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.renderer.setSize(width, height, false);
      this.camera.aspect = width / Math.max(height, 1);
      this.camera.updateProjectionMatrix();
    }
  }

  animate() {
    requestAnimationFrame(() => this.animate());
    if (!this.canvas.classList.contains("hidden")) {
      this.resize();
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    }
  }
}

const fileInput = document.querySelector("#fileInput");
const dropzone = document.querySelector("#dropzone");
const fileChip = document.querySelector("#fileChip");
const button = document.querySelector("#reconstructButton");
const viewerEmpty = document.querySelector("#viewerEmpty");
const processing = document.querySelector("#processing");
const inputCanvas = document.querySelector("#inputCanvas");
const outputCanvas = document.querySelector("#outputCanvas");
const errorBox = document.querySelector("#errorBox");
const viewTabs = document.querySelector("#viewTabs");
const surfaceLegend = document.querySelector("#surfaceLegend");
const surfaceLegendStatus = document.querySelector("#surfaceLegendStatus");
const featureEditor = document.querySelector("#featureEditor");
const featureTree = document.querySelector("#featureTree");
const parameterHeader = document.querySelector("#parameterHeader");
const parameterEditor = document.querySelector("#parameterEditor");
const editorFeedback = document.querySelector("#editorFeedback");
const applyFeatureEdits = document.querySelector("#applyFeatureEdits");
const advancedDrawer = document.querySelector("#advancedDrawer");
const drawerBackdrop = document.querySelector("#drawerBackdrop");

function setAdvancedOpen(open) {
  advancedDrawer.classList.toggle("hidden", !open);
  drawerBackdrop.classList.toggle("hidden", !open);
  document.body.classList.toggle("drawer-open", open);
}

document.querySelector("#advancedToggle").addEventListener("click", () => setAdvancedOpen(true));
document.querySelector("#advancedClose").addEventListener("click", () => setAdvancedOpen(false));
document.querySelector("#openResultDetails").addEventListener("click", () => setAdvancedOpen(true));
drawerBackdrop.addEventListener("click", () => setAdvancedOpen(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setAdvancedOpen(false);
});

const cloneValue = (value) =>
  typeof structuredClone === "function"
    ? structuredClone(value)
    : JSON.parse(JSON.stringify(value));

const fieldLabels = {
  name: "Feature name",
  axis: "Axis",
  mode: "Operation",
  start: "Start position",
  depth: "Depth",
  center: "Center",
  radius: "Radius",
  inner_radius: "Inner radius",
  diameter: "Diameter",
  start_diameter: "Start diameter",
  end_diameter: "End diameter",
  origin: "Origin",
  plane_normal: "Sketch plane normal",
  direction: "Axis direction",
  x_direction: "Sketch X direction",
  support_patch_id: "Sketch support surface",
  terminating_patch_id: "Termination surface",
  extent_kind: "Feature extent",
  outer: "Outer profile",
  profile: "Sketch profile",
  holes: "Profile openings",
  additional_regions: "Additional regions",
  start_outer: "Start profile",
  end_outer: "End profile",
  intermediate_offsets: "Intermediate offsets",
  intermediate_profiles: "Intermediate profiles",
  smooth: "Smooth loft",
  through: "Through hole",
  size: "Size",
  size2: "Second distance",
  selector: "Edge selection",
  position: "Axis position",
  end: "Feature end",
  target_feature_id: "Target feature",
  points: "Sketch points",
  segments: "Path segments",
  mid: "Arc midpoint",
  start_tangent: "Start tangent direction",
  end_tangent: "End tangent direction",
};

const enumValues = {
  axis: ["X", "Y", "Z"],
  mode: ["add", "cut", "fillet", "chamfer"],
  selector: ["all", "circle", "outer", "nearest"],
  end: ["start", "end", "both"],
  extent_kind: ["distance", "through_all", "up_to_patch"],
};

const internalFields = new Set([
  "feature_id",
  "tree_index",
  "depends_on",
  "confidence",
  "feature_index",
  "kind",
  "suppressed",
]);

function orderedFeatures(plan = editablePlan) {
  return plan ? [plan.base, ...(plan.operations || [])] : [];
}

function selectedFeature() {
  return orderedFeatures().find((feature) => feature.feature_id === selectedFeatureId);
}

function normalizeClientTimeline() {
  const features = orderedFeatures();
  features.forEach((feature, index) => {
    feature.tree_index = index;
    feature.depends_on = index ? [features[index - 1].feature_id] : [];
    if (
      feature.kind === "edge_finish" &&
      feature.target_feature_id &&
      !feature.depends_on.includes(feature.target_feature_id)
    ) {
      feature.depends_on.push(feature.target_feature_id);
    }
  });
}

function snapshotForUndo() {
  undoStack.push(JSON.stringify(editablePlan));
  if (undoStack.length > 80) undoStack.shift();
  redoStack = [];
}

function getAtPath(object, path) {
  return path.split(".").reduce((current, part) => current?.[Number.isNaN(Number(part)) ? part : Number(part)], object);
}

function setAtPath(object, path, value) {
  const parts = path.split(".");
  const final = parts.pop();
  const parent = parts.reduce(
    (current, part) => current[Number.isNaN(Number(part)) ? part : Number(part)],
    object,
  );
  parent[Number.isNaN(Number(final)) ? final : Number(final)] = value;
}

function labelFor(key, index = null) {
  if (index !== null) return `Item ${index + 1}`;
  return fieldLabels[key] || key.replaceAll("_", " ").replace(/^./, (value) => value.toUpperCase());
}

function isDimensionPath(path) {
  return !path.includes("direction") && !path.includes("normal") && !path.endsWith("feature_index");
}

function numberStep(value, path) {
  if (!isDimensionPath(path)) return "0.0001";
  const magnitude = Math.abs(Number(value));
  if (magnitude >= 100) return "0.1";
  if (magnitude >= 1) return "0.01";
  return "0.001";
}

function dimensionMeta(path, value) {
  const key = `${selectedFeatureId}.${path}`;
  const measured = editablePlan.measured_values?.[key];
  if (typeof measured !== "number") return "";
  const delta = Number(value) - measured;
  const changed = Math.abs(delta) > 1e-10;
  const unit = isDimensionPath(path) ? " mm" : "";
  const source = editablePlan.parameter_sources?.[key] || "measured";
  const locked = editablePlan.locked_parameters?.includes(key);
  return `<div class="parameter-field-meta">
    <span>measured ${measured.toFixed(4)}${unit}</span>
    <span class="${changed ? "changed" : ""}">${changed ? `Δ ${delta >= 0 ? "+" : ""}${delta.toFixed(4)}${unit}` : "unchanged"}</span>
    <span class="source">${escapeHtml(source)}</span>
    <label title="Protect this parameter from AI revisions">lock <input class="parameter-lock" type="checkbox" data-lock-key="${escapeHtml(key)}" ${locked ? "checked" : ""}></label>
  </div>`;
}

function numericField(label, value, path, wide = false) {
  const unit = isDimensionPath(path) ? " · mm" : "";
  return `<div class="parameter-field ${wide ? "wide" : ""}">
    <label><span>${escapeHtml(label)}${unit}</span></label>
    <input type="number" data-param-path="${escapeHtml(path)}" data-value-type="number"
      step="${numberStep(value, path)}" value="${Number(value)}">
    ${dimensionMeta(path, value)}
  </div>`;
}

function vectorField(label, values, path) {
  const names = values.length === 2 ? ["X", "Y"] : ["X", "Y", "Z", "W"];
  return `<div class="parameter-field wide">
    <label><span>${escapeHtml(label)}${isDimensionPath(path) ? " · mm" : ""}</span></label>
    <div class="vector-fields ${values.length === 2 ? "two" : ""}">
      ${values.map((value, index) => `<div class="vector-component"><b>${names[index] || index + 1}</b><input type="number" data-param-path="${escapeHtml(`${path}.${index}`)}" data-value-type="number" step="${numberStep(value, path)}" value="${Number(value)}"></div>`).join("")}
    </div>
  </div>`;
}

function primitiveField(key, value, path) {
  const label = labelFor(key);
  if (typeof value === "number") return numericField(label, value, path);
  if (typeof value === "boolean") {
    return `<div class="parameter-field wide"><label><span>${escapeHtml(label)}</span><input type="checkbox" data-param-path="${escapeHtml(path)}" data-value-type="boolean" ${value ? "checked" : ""}></label></div>`;
  }
  if (typeof value === "string") {
    if (key === "name") {
      return `<div class="parameter-field wide"><label><span>${escapeHtml(label)}</span></label><input type="text" data-param-path="${escapeHtml(path)}" data-value-type="string" value="${escapeHtml(value)}"></div>`;
    }
    if (key === "target_feature_id") {
      const options = orderedFeatures()
        .filter((feature) => feature.feature_id !== selectedFeatureId)
        .map((feature) => `<option value="${escapeHtml(feature.feature_id)}" ${feature.feature_id === value ? "selected" : ""}>${escapeHtml(feature.name)}</option>`)
        .join("");
      return `<div class="parameter-field"><label><span>${escapeHtml(label)}</span></label><select data-param-path="${escapeHtml(path)}" data-value-type="string">${options}</select></div>`;
    }
    if (enumValues[key]) {
      const choices = key === "mode"
        ? (selectedFeature()?.kind === "edge_finish" ? ["fillet", "chamfer"] : ["add", "cut"])
        : enumValues[key];
      const options = choices
        .map((option) => `<option value="${option}" ${option === value ? "selected" : ""}>${escapeHtml(option)}</option>`)
        .join("");
      return `<div class="parameter-field"><label><span>${escapeHtml(label)}</span></label><select data-param-path="${escapeHtml(path)}" data-value-type="string">${options}</select></div>`;
    }
    return `<div class="parameter-field"><label><span>${escapeHtml(label)}</span></label><div class="readonly-value">${escapeHtml(value)}</div></div>`;
  }
  return `<div class="parameter-field"><label><span>${escapeHtml(label)}</span></label><div class="readonly-value">Not set</div></div>`;
}

function renderParameterValue(key, value, path, depth = 0) {
  const label = labelFor(key);
  if (value === null || typeof value !== "object") return primitiveField(key, value, path);
  if (Array.isArray(value) && value.length > 0 && value.every((item) => typeof item === "number")) {
    return vectorField(label, value, path);
  }
  if (Array.isArray(value) && value.length === 0) {
    return `<div class="parameter-field"><label><span>${escapeHtml(label)}</span></label><div class="readonly-value">None</div></div>`;
  }

  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item, labelFor(key, index)])
    : Object.entries(value)
      .filter(([childKey]) => childKey !== "kind")
      .map(([childKey, childValue]) => [childKey, childValue, labelFor(childKey)]);
  const profileKind = !Array.isArray(value) && value.kind ? ` · ${value.kind}` : "";
  return `<details class="parameter-section" ${depth < 1 ? "open" : ""}>
    <summary>${escapeHtml(label)}<small>${escapeHtml(profileKind)}${Array.isArray(value) ? ` · ${value.length}` : ""}</small></summary>
    <div class="parameter-grid">
      ${entries.map(([childKey, childValue, childLabel]) => {
        const childPath = `${path}.${childKey}`;
        if (childValue !== null && typeof childValue === "object") {
          return renderParameterValue(childLabel, childValue, childPath, depth + 1);
        }
        return primitiveField(childLabel, childValue, childPath);
      }).join("")}
    </div>
  </details>`;
}

function renderFeatureTree() {
  const features = orderedFeatures();
  document.querySelector("#treeRevision").textContent = `revision ${editablePlan.revision}`;
  featureTree.innerHTML = features.map((feature, index) => {
    const operationIndex = index - 1;
    const canMoveUp = index > 1;
    const canMoveDown = index > 0 && operationIndex < editablePlan.operations.length - 1;
    return `<div class="feature-row ${feature.feature_id === selectedFeatureId ? "selected" : ""} ${feature.suppressed ? "suppressed" : ""}" data-feature-id="${escapeHtml(feature.feature_id)}">
      <span class="feature-order">${index + 1}</span>
      <span><strong>${escapeHtml(feature.name)}</strong><small>${escapeHtml(feature.kind.replaceAll("_", " "))}</small></span>
      <span class="feature-row-controls">
        <input class="feature-toggle" type="checkbox" data-action="toggle-feature" title="Enable feature" ${!feature.suppressed ? "checked" : ""} ${index === 0 ? "disabled" : ""}>
        <button type="button" data-action="move-up" title="Move earlier" ${canMoveUp ? "" : "disabled"}>↑</button>
        <button type="button" data-action="move-down" title="Move later" ${canMoveDown ? "" : "disabled"}>↓</button>
      </span>
    </div>`;
  }).join("");
}

function renderParameterEditor() {
  const feature = selectedFeature();
  if (!feature) {
    parameterHeader.innerHTML = "";
    parameterEditor.innerHTML = '<div class="parameter-empty">Select a feature to edit it.</div>';
    return;
  }
  parameterHeader.innerHTML = `<div><h4>${escapeHtml(feature.name)}</h4><p>${escapeHtml(feature.feature_id)} · position ${feature.tree_index + 1}</p></div><div class="parameter-header-actions"><button class="reset-feature-button" id="resetFeatureDimensions" type="button">Reset feature</button></div>`;
  const fields = Object.entries(feature)
    .filter(([key]) => !internalFields.has(key))
    .map(([key, value]) => renderParameterValue(key, value, key))
    .join("");
  parameterEditor.innerHTML = `<div class="parameter-grid">${fields}</div>`;
  document.querySelector("#resetFeatureDimensions")?.addEventListener("click", () => resetMeasuredValues(selectedFeatureId));
}

function plansDiffer() {
  return Boolean(activeReport?.plan && JSON.stringify(editablePlan) !== JSON.stringify(activeReport.plan));
}

function renderEditorFeedback(message = "") {
  const dirty = plansDiffer();
  editorFeedback.className = `editor-feedback ${dirty ? "dirty" : ""}`;
  if (message) {
    editorFeedback.innerHTML = escapeHtml(message);
  } else if (dirty) {
    editorFeedback.innerHTML = "Unsaved parameter changes · rebuild to update the solid and fit metrics.";
  } else if (activeReport?.score) {
    editorFeedback.innerHTML = `<strong>Verified revision ${editablePlan.revision}</strong> · P95 ${activeReport.score.chamfer_p95_mm.toFixed(3)} mm · volume ${activeReport.score.volume_error_percent.toFixed(2)}%`;
  }
  document.querySelector("#undoEdit").disabled = !undoStack.length || editorBusy;
  document.querySelector("#redoEdit").disabled = !redoStack.length || editorBusy;
  applyFeatureEdits.disabled = !dirty || editorBusy;
  applyFeatureEdits.textContent = editorBusy ? "Rebuilding…" : "Apply, rebuild & verify";
}

function renderFeatureEditor(message = "") {
  if (!editablePlan) {
    featureEditor.classList.add("hidden");
    return;
  }
  normalizeClientTimeline();
  featureEditor.classList.remove("hidden");
  renderFeatureTree();
  renderParameterEditor();
  renderEditorFeedback(message);
}

function initializeFeatureEditor(report) {
  activeReport = report;
  editablePlan = report.plan ? cloneValue(report.plan) : null;
  selectedFeatureId = editablePlan?.base?.feature_id || null;
  undoStack = [];
  redoStack = [];
  renderFeatureEditor();
}

function applyLocalParameterChange(input) {
  const feature = selectedFeature();
  if (!feature) return;
  const path = input.dataset.paramPath;
  let value;
  if (input.dataset.valueType === "number") {
    value = Number(input.value);
    if (!Number.isFinite(value)) return;
  } else if (input.dataset.valueType === "boolean") {
    value = input.checked;
  } else {
    value = input.value;
  }
  if (getAtPath(feature, path) === value) return;
  snapshotForUndo();
  setAtPath(feature, path, value);
  if (typeof value === "number") {
    const key = `${feature.feature_id}.${path}`;
    editablePlan.parameter_sources ||= {};
    editablePlan.parameter_sources[key] = "user";
  }
  renderFeatureEditor();
}

function resetMeasuredValues(featureId = null) {
  if (!editablePlan?.measured_values) return;
  snapshotForUndo();
  for (const [key, measured] of Object.entries(editablePlan.measured_values)) {
    const separator = key.indexOf(".");
    const ownerId = key.slice(0, separator);
    if (featureId && ownerId !== featureId) continue;
    const feature = orderedFeatures().find((item) => item.feature_id === ownerId);
    if (!feature) continue;
    const path = key.slice(separator + 1);
    try {
      if (typeof getAtPath(feature, path) === "number") {
        setAtPath(feature, path, measured);
        editablePlan.parameter_sources[key] = "measured";
      }
    } catch {
      // A removed optional parameter has no current location to restore.
    }
  }
  renderFeatureEditor();
}

featureTree.addEventListener("click", (event) => {
  const row = event.target.closest(".feature-row");
  if (!row || !editablePlan || editorBusy) return;
  selectedFeatureId = row.dataset.featureId;
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "toggle-feature") return;
  const features = orderedFeatures();
  const index = features.findIndex((feature) => feature.feature_id === selectedFeatureId);
  if (action === "move-up" || action === "move-down") {
    snapshotForUndo();
    const operationIndex = index - 1;
    const target = action === "move-up" ? operationIndex - 1 : operationIndex + 1;
    [editablePlan.operations[operationIndex], editablePlan.operations[target]] = [
      editablePlan.operations[target],
      editablePlan.operations[operationIndex],
    ];
  }
  renderFeatureEditor();
});

featureTree.addEventListener("change", (event) => {
  if (event.target.dataset.action !== "toggle-feature" || editorBusy) return;
  const row = event.target.closest(".feature-row");
  const feature = orderedFeatures().find((item) => item.feature_id === row.dataset.featureId);
  if (!feature || feature.tree_index === 0) return;
  snapshotForUndo();
  feature.suppressed = !event.target.checked;
  selectedFeatureId = feature.feature_id;
  renderFeatureEditor();
});

parameterEditor.addEventListener("change", (event) => {
  const input = event.target.closest("[data-param-path]");
  if (input && !editorBusy) applyLocalParameterChange(input);
  const lock = event.target.closest("[data-lock-key]");
  if (lock && !editorBusy) {
    snapshotForUndo();
    const values = new Set(editablePlan.locked_parameters || []);
    lock.checked ? values.add(lock.dataset.lockKey) : values.delete(lock.dataset.lockKey);
    editablePlan.locked_parameters = [...values].sort();
    renderFeatureEditor();
  }
});

document.querySelector("#undoEdit").addEventListener("click", () => {
  if (!undoStack.length || editorBusy) return;
  redoStack.push(JSON.stringify(editablePlan));
  editablePlan = JSON.parse(undoStack.pop());
  if (!selectedFeature()) selectedFeatureId = editablePlan.base.feature_id;
  renderFeatureEditor();
});

document.querySelector("#redoEdit").addEventListener("click", () => {
  if (!redoStack.length || editorBusy) return;
  undoStack.push(JSON.stringify(editablePlan));
  editablePlan = JSON.parse(redoStack.pop());
  if (!selectedFeature()) selectedFeatureId = editablePlan.base.feature_id;
  renderFeatureEditor();
});

document.querySelector("#resetAllDimensions").addEventListener("click", () => resetMeasuredValues());

applyFeatureEdits.addEventListener("click", async () => {
  if (!activeReport?.plan || !editablePlan || editorBusy || !plansDiffer()) return;
  editorBusy = true;
  renderEditorFeedback("Rebuilding the ordered feature tree and verifying it against the STL…");
  try {
    const response = await fetch(`/api/runs/${activeReport.id}/plan`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_revision: activeReport.plan.revision,
        plan: editablePlan,
      }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = Array.isArray(payload.detail)
        ? payload.detail.map((item) => item.msg).join("; ")
        : payload.detail;
      throw new Error(detail || `Rebuild failed (${response.status})`);
    }
    const report = await response.json();
    editorBusy = false;
    await renderResult(report, { scroll: false });
  } catch (error) {
    editorBusy = false;
    renderEditorFeedback(error.message);
    editorFeedback.classList.add("error");
  }
});

const formatBytes = (bytes) => {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

async function chooseFile(file) {
  if (!file || !file.name.toLowerCase().endsWith(".stl")) {
    showError("Please choose an STL file.");
    return;
  }
  selectedFile = file;
  surfaceLegend.classList.add("hidden");
  errorBox.classList.add("hidden");
  fileChip.classList.remove("hidden");
  fileChip.innerHTML = `
    <span class="file-type">STL</span>
    <span><strong>${escapeHtml(file.name)}</strong><small>${formatBytes(file.size)}</small></span>
    <button id="clearFile" aria-label="Remove file">×</button>
  `;
  document.querySelector("#clearFile").addEventListener("click", clearFile);
  dropzone.classList.add("hidden");
  viewerEmpty.classList.add("hidden");
  inputCanvas.classList.remove("hidden");
  outputCanvas.classList.add("hidden");
  inputViewer ||= new MeshViewer(inputCanvas, 0x8ebbd2);
  await inputViewer.load(await file.arrayBuffer());
  document.querySelector("#previewSubtitle").textContent = file.name;
  button.disabled = false;
}

function clearFile(event) {
  event?.preventDefault();
  selectedFile = null;
  fileInput.value = "";
  fileChip.classList.add("hidden");
  dropzone.classList.remove("hidden");
  viewerEmpty.classList.remove("hidden");
  inputCanvas.classList.add("hidden");
  outputCanvas.classList.add("hidden");
  viewTabs.classList.add("hidden");
  surfaceLegend.classList.add("hidden");
  button.disabled = true;
}

fileInput.addEventListener("change", (event) => chooseFile(event.target.files[0]));
["dragenter", "dragover"].forEach((name) =>
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  }),
);
["dragleave", "drop"].forEach((name) =>
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
  }),
);
dropzone.addEventListener("drop", (event) => chooseFile(event.dataTransfer.files[0]));

viewTabs.addEventListener("click", (event) => {
  const tab = event.target.closest("button");
  if (!tab) return;
  const output = tab.dataset.view === "output";
  inputCanvas.classList.toggle("hidden", output);
  outputCanvas.classList.toggle("hidden", !output);
  [...viewTabs.children].forEach((item) => item.classList.toggle("active", item === tab));
});

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
}

const formatElapsed = (seconds) => {
  const rounded = Math.max(0, Math.round(seconds || 0));
  if (rounded < 60) return `${rounded} second${rounded === 1 ? "" : "s"}`;
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return `${minutes}m ${remainder.toString().padStart(2, "0")}s`;
};

function renderReconstructionProgress(progress) {
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  document.querySelector("#processingStage").textContent = progress.stage || "Reconstructing";
  document.querySelector("#processingText").textContent = progress.detail || "Working locally";
  document.querySelector("#progressBar").style.width = `${percent}%`;
  document.querySelector("#progressPercent").textContent = `${Math.round(percent)}%`;
  document.querySelector("#progressElapsed").textContent = formatElapsed(progress.elapsed_seconds);
  const counts = [];
  if (progress.candidates_total > 0) {
    counts.push(`Candidates tested: ${progress.candidates_done} / ${progress.candidates_total}`);
  }
  if (progress.current_features > 0) {
    counts.push(`Current feature tree: ${progress.current_features} features`);
  }
  if (progress.seconds_since_update > 8 && progress.status === "running") {
    counts.push("Geometry calculation still active");
  }
  document.querySelector("#progressCounts").textContent =
    counts.join(" · ") || "Discovering the first editable features";
  updateLivePreview(progress);
}

async function updateLivePreview(progress) {
  const revision = Number(progress.preview_revision) || 0;
  if (!progress.preview_url || revision <= livePreviewRevision || livePreviewLoading) return;
  livePreviewLoading = true;
  try {
    const response = await fetch(progress.preview_url, { cache: "no-store" });
    if (!response.ok) return;
    const buffer = await response.arrayBuffer();
    if (revision <= livePreviewRevision) return;
    outputViewer ||= new MeshViewer(outputCanvas, 0x8ebbd2);
    await outputViewer.load(buffer, { resetCamera: livePreviewRevision === 0 });
    livePreviewRevision = revision;
    viewerEmpty.classList.add("hidden");
    inputCanvas.classList.add("hidden");
    outputCanvas.classList.remove("hidden");
    document.querySelector("#previewSubtitle").textContent =
      `Live provisional CAD · revision ${revision}`;
  } catch {
    // A candidate can be superseded between polling and download. The next
    // progress event will provide a newer stable preview.
  } finally {
    livePreviewLoading = false;
  }
}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitForReconstruction(runId) {
  while (true) {
    const response = await fetch(`/api/runs/${runId}/progress`, { cache: "no-store" });
    const progress = await response.json();
    if (!response.ok) throw new Error(progress.detail || "Could not read reconstruction progress.");
    renderReconstructionProgress(progress);
    if (progress.status === "failed") {
      throw new Error(progress.error || "Reconstruction failed.");
    }
    if (progress.status === "complete") {
      const reportResponse = await fetch(`/api/runs/${runId}`, { cache: "no-store" });
      const report = await reportResponse.json();
      if (!reportResponse.ok) throw new Error(report.detail || "Could not load the result.");
      return report;
    }
    await wait(650);
  }
}

button.addEventListener("click", async () => {
  if (!selectedFile) return;
  button.disabled = true;
  errorBox.classList.add("hidden");
  processing.classList.remove("hidden");
  livePreviewRevision = 0;
  livePreviewLoading = false;
  document.querySelector("#results").classList.add("hidden");
  renderReconstructionProgress({
    stage: "Uploading mesh",
    detail: "Saving the STL locally",
    percent: 2,
    elapsed_seconds: 0,
    candidates_done: 0,
    candidates_total: 0,
    current_features: 0,
    seconds_since_update: 0,
    status: "queued",
  });
  const form = new FormData();
  form.append("file", selectedFile);
  form.append("input_units", document.querySelector("#units").value);
  form.append("prompt", document.querySelector("#prompt").value);
  form.append("engine", document.querySelector("#engine").value);
  try {
    const response = await fetch("/api/reconstruct/start", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Reconstruction failed.");
    renderReconstructionProgress(payload);
    const report = await waitForReconstruction(payload.id);
    await renderResult(report);
  } catch (error) {
    showError(error.message);
  } finally {
    processing.classList.add("hidden");
    button.disabled = false;
  }
});

async function renderResult(report, options = {}) {
  surfaceLegend.classList.add("hidden");
  const complete = report.status === "complete";
  const surfaceMode = report.engine === "surface_brep" && report.surface;
  const semantic = report.plan?.representation !== "sampled_approximation";
  document.querySelector("#resultTitle").textContent = complete
    ? surfaceMode
      ? "Surface B-rep ready"
      : "Editable model ready"
    : report.status === "best_effort"
      ? surfaceMode
        ? report.surface.closed
          ? "Best fitted surface solid produced"
          : "Surface recognition result ready"
        : semantic
        ? "Best editable model produced"
        : "Geometric approximation produced"
      : "Automatic reconstruction stopped";
  const confidence = document.querySelector("#confidence");
  confidence.className = `confidence ${complete ? "good" : "caution"}`;
  confidence.textContent = complete
    ? surfaceMode
      ? "Watertight verified solid"
      : "High-confidence match"
    : surfaceMode
      ? report.surface.closed
        ? "Best effort"
        : "Valid fitted surface set"
      : semantic
        ? "Best effort"
        : "Approximation only";

  if (report.score) {
    document.querySelector("#p95").textContent = report.score.chamfer_p95_mm.toFixed(3);
    document.querySelector("#volumeError").textContent = report.score.volume_comparable !== false
      ? report.score.volume_error_percent.toFixed(2)
      : "N/A";
  }
  document.querySelector("#elapsed").textContent = report.elapsed_seconds.toFixed(1);
  const featureCount = report.plan
    ? 1 + (report.plan.operations || []).filter((operation) => !operation.suppressed).length
    : 0;
  const baseLabels = {
    cylinder: "Cylinder",
    extrude: "Extrusion",
    oriented_extrude: "3D-axis extrusion",
    revolve: "Revolve",
  };
  document.querySelector("#construction").textContent =
    surfaceMode
      ? `${report.surface.recognized_surface_count} surfaces`
      : featureCount > 1
      ? `${featureCount} features`
      : baseLabels[report.plan?.base?.kind] || "Parametric solid";
  document.querySelector("#constructionUnit").textContent = surfaceMode
    ? `${report.surface.brep_face_count} fitted B-rep faces`
    : "parametric feature construction";
  document.querySelector("#constructionHeading").textContent = surfaceMode
    ? "Recognized surface model"
    : "Editable feature plan";

  const base = report.plan?.base;
  const plan = document.querySelector("#featurePlan");
  if (base) {
    const vector = (values) =>
      `(${values.map((value) => Number(value).toFixed(3)).join(", ")})`;
    const profiles = [
      base.outer,
      base.profile,
      ...(base.holes || []),
      ...(report.plan.operations || []).flatMap((operation) => [
        operation.outer,
        operation.start_outer,
        operation.end_outer,
        ...(operation.holes || []),
      ]),
    ].filter(Boolean);
    const arcCount = profiles.reduce(
      (count, profile) =>
        count +
        (profile.segments?.filter((segment) => segment.kind === "arc").length || 0),
      0,
    );
    const splineSegmentCount = profiles.reduce(
      (count, profile) =>
        count +
        (profile.segments?.filter((segment) => segment.kind === "spline").length || 0),
      0,
    );
    const analyticFeatureCount = (report.plan.operations || []).filter((operation) =>
      [
        "round_hole",
        "conical_hole",
        "conical_add",
        "oriented_cylinder",
        "tapered_add",
        "edge_finish",
      ].includes(operation.kind),
    ).length;
    let details;
    if (base.kind === "cylinder") {
      details = [
        ["Base feature", "Analytic cylinder"],
        ["Axis", base.axis],
        ["Outer diameter", `${(base.radius * 2).toFixed(3)} mm`],
        ["Depth", `${base.depth.toFixed(3)} mm`],
        ["Bore", base.inner_radius ? `${(base.inner_radius * 2).toFixed(3)} mm` : "None"],
      ];
    } else if (base.kind === "oriented_extrude") {
      details = [
        ["Base feature", "Arbitrary-plane sketch extrusion"],
        ["3D axis", vector(base.direction)],
        ["Profile", base.outer.kind],
        ["Depth", `${base.depth.toFixed(3)} mm`],
        ["Profile openings", String(base.holes?.length || 0)],
      ];
    } else if (base.kind === "revolve") {
      details = [
        ["Base feature", "Revolved sketch"],
        ["Axis", base.axis],
        ["Profile", base.profile.kind],
        ["Analytic profile curves", `${arcCount} arcs · ${splineSegmentCount} splines`],
      ];
    } else {
      details = [
        ["Base feature", "Sketch extrusion"],
        ["Axis", base.axis],
        ["Profile", base.outer.kind],
        ["Depth", `${base.depth.toFixed(3)} mm`],
        ["Profile openings", String(base.holes?.length || 0)],
      ];
    }
    details.push(
      ["Additional features", String((report.plan.operations || []).filter((operation) => !operation.suppressed).length)],
      ["Analytic curves / features", `${arcCount + splineSegmentCount} / ${analyticFeatureCount}`],
    );
    plan.innerHTML = details
      .map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");
  } else if (surfaceMode) {
    const surfaceLabels = {
      plane: "Planes",
      cylinder: "Cylinders",
      cone: "Cones",
      sphere: "Spheres",
      torus: "Tori",
      extrusion: "Profile extrusions",
      revolution: "Profile revolutions",
      freeform: "Freeform residuals",
    };
    const details = Object.entries(report.surface.surface_counts)
      .filter(([, count]) => count > 0)
      .map(([kind, count]) => [surfaceLabels[kind] || kind, String(count)]);
    details.push(
      [
        "STEP representation",
        report.surface.closed
          ? report.surface.faceted_fallback
            ? report.surface.source_mesh_fallback
              ? "Closed exact source-mesh faceted solid"
              : "Closed hybrid analytic/conforming-facet solid"
            : "Closed analytic/B-spline solid"
          : report.surface.faceted_fallback
            ? "Hybrid analytic/conforming-facet face set"
            : "Recognized analytic/B-spline face set",
      ],
      ["Node-fitted B-splines", String(report.surface.point_fitted_face_count || 0)],
      [
        "Conforming facet fallback",
        report.surface.faceted_fallback
          ? `${report.surface.faceted_patch_count || 0} regions / ${report.surface.faceted_face_count || 0} faces`
          : "Not needed",
      ],
      ["Canonical vertices", String(report.surface.topology_vertex_count || 0)],
      ["Canonical edges", String(report.surface.topology_edge_count || 0)],
      ["Adjacent surface pairs", String(report.surface.adjacency_count)],
      ["Sewn solids", String(report.surface.solid_count)],
      ["Free edges", String(report.surface.free_edge_count)],
    );
    plan.innerHTML = details
      .map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");
  } else {
    plan.innerHTML = "<p>No CAD representation was produced.</p>";
  }

  const fileBase = `/api/runs/${report.id}/files`;
  const sourceStem = (report.mesh.file_name || "model.stl")
    .replace(/\.[^.]+$/, "")
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^[._]+|[._]+$/g, "") || "model";
  const reconstructedStepName = `${sourceStem}_reconstructed.step`;
  document.querySelector("#downloads").innerHTML = `
    <a class="download-primary" href="${fileBase}/reconstruction.step" download="${escapeHtml(reconstructedStepName)}">
      <span><strong>Export STEP file</strong><small>${surfaceMode && report.surface.closed ? "Watertight reconstructed B-rep" : "Reconstructed CAD model"}</small></span><b>↓</b>
    </a>
  `;
  document.querySelector("#advancedDownloads").innerHTML = surfaceMode
    ? `
      <a href="${fileBase}/surface_graph.json" download>Surface graph JSON <b>↓</b></a>
      ${report.surface.closed ? "" : `<a href="${fileBase}/joined_surfaces.step" download>Joined-shell diagnostic <b>↓</b></a>`}
      <a href="${fileBase}/report.json" download>Verification report <b>↓</b></a>
    `
    : `
      <a href="${fileBase}/reconstruction.py" download>CadQuery source <b>↓</b></a>
      <a href="${fileBase}/plan.json" download>Feature plan JSON <b>↓</b></a>
      <a href="${fileBase}/report.json" download>Verification report <b>↓</b></a>
    `;
  const warnings = document.querySelector("#warnings");
  warnings.innerHTML = (report.warnings || [])
    .map((warning) => `<div class="warning"><b>!</b><span>${escapeHtml(warning)}</span></div>`)
    .join("");

  if (report.status !== "failed") {
    if (report.plan) {
      initializeFeatureEditor(report);
    } else {
      activeReport = report;
      editablePlan = null;
      featureEditor.classList.add("hidden");
    }
    const response = await fetch(`${fileBase}/reconstruction.stl?revision=${report.plan?.revision || 0}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("The reconstructed preview could not be loaded.");
    const outputBuffer = await response.arrayBuffer();
    outputViewer ||= new MeshViewer(outputCanvas, 0x8ebbd2);
    await outputViewer.load(outputBuffer);
    let sourceMeshBuffer = null;
    try {
      const sourceResponse = await fetch(`${fileBase}/input.stl`, { cache: "no-store" });
      if (sourceResponse.ok) {
        sourceMeshBuffer = await sourceResponse.arrayBuffer();
        inputViewer ||= new MeshViewer(inputCanvas, 0x8ebbd2);
        await inputViewer.load(sourceMeshBuffer.slice(0));
      }
    } catch {
      // The STEP result remains usable if an older saved run has no source preview.
    }
    if (surfaceMode) {
      try {
        const graphResponse = await fetch(`${fileBase}/surface_graph.json`, { cache: "no-store" });
        if (graphResponse.ok && sourceMeshBuffer) {
          const surfaceGraph = await graphResponse.json();
          const overlay = outputViewer.showSurfaceOverlay(
            surfaceGraph,
            sourceMeshBuffer,
            Number(report.mesh.unit_scale) || 1,
          );
          surfaceLegendStatus.textContent = overlay.triangleCount
            ? `${overlay.triangleCount.toLocaleString()} fallback triangles shown`
            : "No triangle fallback used";
          surfaceLegend.classList.remove("hidden");
        }
      } catch {
        surfaceLegend.classList.add("hidden");
      }
    }
    inputCanvas.classList.add("hidden");
    outputCanvas.classList.remove("hidden");
    viewTabs.classList.remove("hidden");
    [...viewTabs.children].forEach((item) =>
      item.classList.toggle("active", item.dataset.view === "output"),
    );
  } else {
    activeReport = report;
    editablePlan = null;
    featureEditor.classList.add("hidden");
  }
  document.querySelector("#results").classList.remove("hidden");
}

const requestedRun = new URLSearchParams(window.location.search).get("run");
if (/^[a-f0-9]{12}$/.test(requestedRun || "")) {
  fetch(`/api/runs/${requestedRun}`, { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) throw new Error("Saved reconstruction not found");
      return response.json();
    })
    .then((report) => renderResult(report, { scroll: false }))
    .catch((error) => showError(error.message));
}
