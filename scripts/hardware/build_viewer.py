"""Assemble docs/hardware/viewer.html: one self-contained page with three.js, the
glTF model and the part descriptions embedded, so it opens from a file or a
phone with no network.

    python scripts/hardware/build_viewer.py --three <dir with three.module.min.js, OrbitControls.js, GLTFLoader.js>
                                            --model docs/hardware/ira_watch.glb --parts docs/hardware/parts.json
                                            --out docs/hardware/viewer.html

The three.js files come from the `three` package on npm (MIT licence; the licence
text is embedded). Rotate and zoom, an explode and assemble slider, layer toggles,
and tap a part to read its name, what it does for the person and which IRA CARE
feature uses it.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>IRA CARE watch</title>
<meta name="description" content="The IRA CARE watch, part by part: rotate, zoom, explode, and tap a part to see what it does for the person.">
<style>
  :root { --bg: #f4f2ee; --panel: #ffffff; --ink: #1f2a24; --muted: #5b6b63; --line: #e1ded8; --accent: #2f5d8a; --help: #b3261e; }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #14181b; --panel: #1d2327; --ink: #eef1f0; --muted: #a6b3ac; --line: #2c343a; --accent: #8ab6e0; } }
  :root[data-theme="dark"] { --bg: #14181b; --panel: #1d2327; --ink: #eef1f0; --muted: #a6b3ac; --line: #2c343a; --accent: #8ab6e0; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--ink); font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  #stage { position: fixed; inset: 0; touch-action: none; }
  canvas { display: block; }
  header { position: fixed; top: 0; left: 0; right: 0; padding: 12px 16px; pointer-events: none; }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; }
  header p { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
  #controls { position: fixed; left: 0; right: 0; bottom: 0; background: var(--panel); border-top: 1px solid var(--line); padding: 10px 16px calc(10px + env(safe-area-inset-bottom)); display: grid; gap: 8px; grid-template-columns: 1fr; max-height: 48vh; overflow: auto; }
  @media (min-width: 720px) { #controls { left: auto; right: 16px; bottom: 16px; width: 340px; border: 1px solid var(--line); border-radius: 14px; max-height: calc(100vh - 32px); } }
  label.slider { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px; font-size: 14px; }
  input[type=range] { width: 100%; accent-color: var(--accent); }
  .layers { display: flex; flex-wrap: wrap; gap: 6px; }
  .layers button { font: inherit; font-size: 13px; padding: 6px 10px; border-radius: 999px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); min-height: 32px; }
  .layers button[aria-pressed="false"] { opacity: .45; text-decoration: line-through; }
  #info { border-top: 1px solid var(--line); padding-top: 8px; min-height: 64px; }
  #info h2 { margin: 0 0 4px; font-size: 16px; }
  #info p { margin: 0 0 4px; font-size: 14px; }
  #info .muted { color: var(--muted); }
  .row { display: flex; gap: 8px; flex-wrap: wrap; }
  .row button { font: inherit; font-size: 14px; padding: 8px 12px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); min-height: 40px; }
  .row button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  #loading { position: fixed; inset: 0; display: grid; place-items: center; color: var(--muted); background: var(--bg); }
</style>
</head>
<body>
<div id="stage"></div>
<header><h1>IRA CARE watch</h1><p>Drag to turn, pinch or scroll to zoom, tap a part to read about it.</p></header>
<div id="loading">Loading the watch…</div>
<section id="controls" aria-label="Viewer controls">
  <label class="slider">Explode <input id="explode" type="range" min="0" max="1" step="0.01" value="0" aria-label="Explode or assemble"> <span id="explodeValue" aria-hidden="true">0%</span></label>
  <div class="row"><button id="assemble">Assemble</button><button id="explodeAll">Explode</button><button id="reset">Reset view</button><button id="spin" aria-pressed="true">Spin</button></div>
  <div class="layers" id="layers" aria-label="Show or hide layers"></div>
  <div id="info"><h2>Tap a part</h2><p class="muted">Each part says what it does for the person and which IRA CARE feature uses it.</p></div>
</section>
<script type="importmap">{"imports":{"three":"data:text/javascript;base64,__THREE__"}}</script>
<script type="module">
import * as THREE from 'three';
__ORBIT__
__GLTF__
const PARTS = __PARTS__;
const LAYERS = PARTS.layers;
const META = Object.fromEntries(PARTS.parts.map(p => [p.name, p]));
const MODEL = 'data:model/gltf-binary;base64,__MODEL__';

const stage = document.getElementById('stage');
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
stage.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(32, 1, 0.001, 10);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true; controls.dampingFactor = 0.08; controls.minDistance = 0.06; controls.maxDistance = 0.6;
scene.add(new THREE.HemisphereLight(0xffffff, 0x667788, 1.1));
const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(0.3, -0.3, 0.5); scene.add(key);
const fill = new THREE.DirectionalLight(0xffffff, 0.8); fill.position.set(-0.4, -0.2, 0.3); scene.add(fill);
const rim = new THREE.DirectionalLight(0xffffff, 1.0); rim.position.set(0.1, 0.5, 0.4); scene.add(rim);

const root = new THREE.Group(); root.rotation.x = -Math.PI / 2; scene.add(root);  // Blender's Z-up to three's Y-up is handled by glTF; the group lets us orbit nicely
const parts = []; // {mesh, layer, index, home}
const layerIndex = Object.fromEntries(LAYERS.map((l, i) => [l, i]));
let explodeAmount = 0;
const EXPLODE_STEP = 0.0065; // 6.5 mm per layer, in metres

function setup(gltf) {
  const model = gltf.scene;
  model.traverse(o => {
    if (!o.isMesh) return;
    const name = (o.userData && o.userData.part) || o.name.replace(/\.\d+$/, '');
    const layer = (o.userData && o.userData.layer) || name;
    const idx = layerIndex[layer] ?? 0;
    o.userData.partName = name; o.userData.layerName = layer;
    o.castShadow = false;
    parts.push({ mesh: o, layer, index: idx, home: o.position.clone(), name });
  });
  scene.add(model);
  // frame on the case, not the strap that hangs below it
  const caseBox = new THREE.Box3();
  for (const p of parts) if (p.layer !== 'Strap' && p.layer !== 'Clasp') caseBox.expandByObject(p.mesh);
  modelSphere = caseBox.getBoundingSphere(new THREE.Sphere());
  frame();
  controls.saveState();
  document.getElementById('loading').remove();
  buildLayerButtons();
}

function applyExplode() {
  for (const p of parts) {
    if (p.layer === 'Strap' || p.layer === 'Clasp') continue;
    p.mesh.position.copy(p.home);
    p.mesh.position.y += p.index * EXPLODE_STEP * explodeAmount;   // glTF is Y-up: Blender's Z became Y
  }
  document.getElementById('explodeValue').textContent = Math.round(explodeAmount * 100) + '%';
}
const slider = document.getElementById('explode');
slider.addEventListener('input', () => { explodeAmount = parseFloat(slider.value); applyExplode(); });
document.getElementById('assemble').addEventListener('click', () => { slider.value = 0; explodeAmount = 0; applyExplode(); });
document.getElementById('explodeAll').addEventListener('click', () => { slider.value = 1; explodeAmount = 1; applyExplode(); });
let spinning = true;
const spinBtn = document.getElementById('spin');
spinBtn.addEventListener('click', () => { spinning = !spinning; spinBtn.setAttribute('aria-pressed', String(spinning)); });
document.getElementById('reset').addEventListener('click', () => { frame(); });

function buildLayerButtons() {
  const box = document.getElementById('layers');
  for (const l of LAYERS) {
    const b = document.createElement('button');
    b.textContent = l; b.setAttribute('aria-pressed', 'true');
    b.addEventListener('click', () => {
      const on = b.getAttribute('aria-pressed') !== 'true';
      b.setAttribute('aria-pressed', String(on));
      for (const p of parts) if (p.layer === l) p.mesh.visible = on;
    });
    box.appendChild(b);
  }
}

const raycaster = new THREE.Raycaster(); const pointer = new THREE.Vector2();
let downAt = null;
renderer.domElement.addEventListener('pointerdown', e => { downAt = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener('pointerup', e => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6) return;
  const r = renderer.domElement.getBoundingClientRect();
  pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(parts.filter(p => p.mesh.visible).map(p => p.mesh), false)[0];
  if (hit) describe(hit.object.userData.partName);
});
let highlighted = null;
function describe(name) {
  const m = META[name] || { name, what: '', feature: '' };
  document.getElementById('info').innerHTML = `<h2>${escapeHtml(m.name)}</h2><p>${escapeHtml(m.what)}</p><p class="muted"><strong>Used by:</strong> ${escapeHtml(m.feature)}</p>`;
  if (highlighted) highlighted.forEach(({ mesh, emissive }) => { if (mesh.material.emissive) mesh.material.emissive.copy(emissive); });
  highlighted = parts.filter(p => p.name === name).map(p => {
    const mat = p.mesh.material; const emissive = mat.emissive ? mat.emissive.clone() : new THREE.Color(0);
    if (!mat.userData.cloned) { p.mesh.material = mat.clone(); p.mesh.material.userData.cloned = true; }
    if (p.mesh.material.emissive) p.mesh.material.emissive.setHex(0x2f5d8a);
    return { mesh: p.mesh, emissive };
  });
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

let modelSphere = null;
function frame() {
  if (!modelSphere) return;
  const aspect = stage.clientWidth / stage.clientHeight;
  const vfov = THREE.MathUtils.degToRad(camera.fov);
  const hfov = 2 * Math.atan(Math.tan(vfov / 2) * aspect);
  const fov = Math.min(vfov, hfov);
  // the case fills about three fifths of the canvas; the exploded stack grows upward from it
  const d = (modelSphere.radius * 1.7) / Math.sin(fov / 2);
  const centre = modelSphere.center.clone();
  centre.y += modelSphere.radius * 0.35;
  controls.target.copy(centre);
  const dir = new THREE.Vector3(0.55, 0.5, 0.7).normalize();
  camera.position.copy(centre).addScaledVector(dir, d);
  camera.lookAt(centre);
}
function resize() {
  // on a phone the controls panel sits over the bottom of the screen: the canvas ends where the panel starts
  const phone = window.innerWidth < 720;
  const panel = document.getElementById('controls').getBoundingClientRect();
  stage.style.bottom = phone ? Math.max(0, window.innerHeight - panel.top) + 'px' : '0px';
  const w = stage.clientWidth, h = stage.clientHeight;
  renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
  frame();
}
window.addEventListener('resize', resize); resize(); setTimeout(resize, 50);

const loader = new GLTFLoader();
loader.load(MODEL, setup, undefined, err => { document.getElementById('loading').textContent = 'The model could not be loaded: ' + err; });

renderer.setAnimationLoop(() => {
  if (spinning && !controls.userRotating) { controls.autoRotate = true; controls.autoRotateSpeed = 1.2; } else { controls.autoRotate = false; }
  controls.update(); renderer.render(scene, camera);
});
window.__viewer = { parts, describe, get count() { return parts.length; } };
</script>
<footer style="position:fixed;left:16px;bottom:4px;font-size:11px;color:var(--muted);pointer-events:none">three.js, MIT licence; model built by scripts/hardware/build_watch.py</footer>
</body>
</html>
"""


def inline_module(path: str, strip_imports: bool, export: str = "") -> str:
    """OrbitControls and GLTFLoader import from 'three' and from sibling files; the
    import map resolves 'three', and the two sibling imports are rewritten to the names
    already in scope."""
    src = open(path, encoding="utf-8").read()
    src = re.sub(r"^import\s+\{([^}]*)\}\s+from\s+'three';", lambda m: "const {" + m.group(1) + "} = THREE;", src, flags=re.M)
    src = re.sub(r"^import\s+\*\s+as\s+\w+\s+from\s+'three';", "", src, flags=re.M)
    src = re.sub(r"^import\s+.*?from\s+'\.\./utils/BufferGeometryUtils\.js';", "", src, flags=re.M | re.S)
    src = re.sub(r"^export\s+\{[^}]*\};?", "", src, flags=re.M)
    src = re.sub(r"^export\s+(class|const|function)\s", r"\1 ", src, flags=re.M)
    # each module in its own scope, so the two share no top-level names
    return f"const {export} = (() => {{\n{src}\nreturn {export};\n}})();"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--three", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--parts", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    three = open(os.path.join(a.three, "three.module.min.js"), "rb").read()
    orbit = inline_module(os.path.join(a.three, "OrbitControls.js"), True, "OrbitControls")
    gltf = inline_module(os.path.join(a.three, "GLTFLoader.js"), True, "GLTFLoader")
    # GLTFLoader uses BufferGeometryUtils.toTrianglesDrawMode for non-triangle draw modes; the model is all triangles.
    gltf = gltf.replace("toTrianglesDrawMode(", "(g=>g)(")
    model = base64.b64encode(open(a.model, "rb").read()).decode()
    parts = json.load(open(a.parts))
    html = (TEMPLATE.replace("__THREE__", base64.b64encode(three).decode()).replace("__ORBIT__", orbit).replace("__GLTF__", gltf)
            .replace("__PARTS__", json.dumps(parts)).replace("__MODEL__", model))
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(a.out, round(len(html) / 1e6, 2), "MB")


if __name__ == "__main__":
    main()
