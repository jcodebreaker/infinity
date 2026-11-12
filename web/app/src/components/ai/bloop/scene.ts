import type { MuseState } from "@superchat/contracts";
import type * as ThreeNamespace from "three";
import {
  type BloopPose,
  bloopPalette,
  FAST_KEYS,
  NEUTRAL_POSE,
  POSE_KEYS,
  PROP_KEYS,
  poseFor,
} from "./expression";
import type { BloopPropTextures } from "./props";

type Three = typeof ThreeNamespace;

// The body is a soft translucent jelly: view-space light, a gentle depth gradient, a glow at the
// rim and a small bright highlight. `uShear` lags the tip behind the body's tilt and `uWob` ripples
// the surface; limbs use a local gradient instead.
const VERTEX_SHADER = `
uniform float uTime;
uniform vec2 uShear;
uniform float uWob;
uniform float uLocal;
varying vec3 vN;
varying vec3 vV;
varying float vH;
void main() {
  vec3 p = position;
  float h = uLocal > 0.5 ? clamp((position.y + 1.0) * 0.5, 0.0, 1.0) : clamp((position.y + 0.92) / 2.5, 0.0, 1.0);
  if (uLocal < 0.5) {
    p.x += uShear.x * h * h * 1.2;
    p.z += uShear.y * h * h * 1.2;
    p += normal * sin(position.y * 5.0 + uTime * 9.0) * uWob * (0.3 + h);
  }
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  vN = normalize(normalMatrix * normal);
  vV = -mv.xyz;
  vH = h;
  gl_Position = projectionMatrix * mv;
}
`;

const FRAGMENT_SHADER = `
uniform vec3 uTop;
uniform vec3 uBot;
uniform vec3 uRim;
varying vec3 vN;
varying vec3 vV;
varying float vH;
void main() {
  vec3 N = normalize(vN);
  vec3 V = normalize(vV);
  float nv = max(dot(N, V), 0.0);
  float fres = pow(1.0 - nv, 2.3);
  vec3 L = normalize(vec3(-0.45, 0.8, 0.6));
  float diff = max(dot(N, L), 0.0);
  vec3 base = mix(uBot, uTop, smoothstep(0.0, 1.0, vH));
  vec3 col = base * (0.72 + 0.3 * diff) + uRim * fres * 0.5;
  col += uBot * 0.14 * (1.0 - vH) * (1.0 - nv);
  vec3 Hh = normalize(L + V);
  col += vec3(1.0) * pow(max(dot(N, Hh), 0.0), 60.0) * 0.55;
  col += vec3(1.0) * smoothstep(0.88, 0.95, dot(N, normalize(vec3(-0.55, 0.55, 0.63)))) * 0.32;
  gl_FragColor = vec4(col, mix(0.9, 0.98, fres));
}
`;

export interface BloopFaceColors {
  ink: string;
  cheek: string;
  shine: string;
  /** The gold spark on the Muse's head, and its SVG path (a 120-unit box). */
  spark: string;
  sparkPath: string;
}

export interface BloopSceneOptions {
  /** The Muse's identity color; the body is the only part that takes it. */
  color: string;
  state: MuseState;
  /** Scales bodily motion; lower for reduced motion. */
  amp: number;
  face: BloopFaceColors;
  /** Shared prop textures, or null for small faces where props would be unreadable. */
  props: BloopPropTextures | null;
}

export interface BloopScene {
  setState(state: MuseState): void;
  setColor(color: string): void;
  setProps(props: BloopPropTextures | null): void;
  /** Pointer in -1..1 across the viewport; Bloop's gaze follows it. */
  setPointer(x: number, y: number): void;
  /** Advances the pose to `nowMs` (a `performance.now()` clock). */
  update(nowMs: number): void;
  /** Draws the current pose into the active viewport of `renderer`. */
  render(renderer: ThreeNamespace.WebGLRenderer): void;
  dispose(): void;
}

// The body is a droplet: a lathe of this profile, so the face can be placed on its surface.
const H0 = -0.92;
const HT = 2.5;
const RW = 1.05;

function profile(y: number): number {
  const t = Math.min(0.9999, Math.max(0.0001, (y - H0) / HT));
  return RW * Math.sin(Math.PI * t ** 0.75);
}

function profileSlope(y: number): number {
  const t = Math.min(0.995, Math.max(0.005, (y - H0) / HT));
  return (RW * Math.cos(Math.PI * t ** 0.75) * Math.PI * 0.75 * t ** -0.25) / HT;
}

export function createBloopScene(THREE: Three, options: BloopSceneOptions): BloopScene {
  const { face } = options;
  // Props (cloud, bubble, spinner) need room above and beside the body; small faces frame tight.
  const framed = options.props !== null;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100);
  if (framed) {
    camera.position.set(1.3, 3.0, 10.6);
    camera.lookAt(0, 1.7, 0);
  } else {
    camera.position.set(0.9, 2.2, 8.0);
    camera.lookAt(0, 1.3, 0);
  }
  scene.add(new THREE.HemisphereLight(0xffffff, 0xc9bdf0, 0.7));

  const disposables: { dispose(): void }[] = [];
  const track = <T extends { dispose(): void }>(item: T): T => {
    disposables.push(item);
    return item;
  };

  // ---- materials ----
  const palette = bloopPalette(options.color);
  const jelly = (local: boolean) =>
    track(
      new THREE.ShaderMaterial({
        vertexShader: VERTEX_SHADER,
        fragmentShader: FRAGMENT_SHADER,
        transparent: true,
        depthWrite: false,
        uniforms: {
          uTop: { value: new THREE.Color(palette.top) },
          uBot: { value: new THREE.Color(palette.bot) },
          uRim: { value: new THREE.Color(palette.rim) },
          uTime: { value: 0 },
          uShear: { value: new THREE.Vector2() },
          uWob: { value: 0 },
          uLocal: { value: local ? 1 : 0 },
        },
      }),
    );
  const bodyMaterial = jelly(false);
  const limbMaterial = jelly(true);
  const coreMaterial = track(
    new THREE.MeshStandardMaterial({
      color: palette.core,
      roughness: 0.5,
      transparent: true,
      opacity: 0.55,
    }),
  );
  const inkMaterial = track(new THREE.MeshBasicMaterial({ color: face.ink }));
  const eyeMaterial = track(
    new THREE.MeshStandardMaterial({ color: face.ink, roughness: 0.15, metalness: 0.15 }),
  );
  const shineMaterial = track(new THREE.MeshBasicMaterial({ color: face.shine }));
  const mouthMaterial = track(
    new THREE.MeshBasicMaterial({ color: face.ink, side: THREE.DoubleSide }),
  );
  const tongueMaterial = track(
    new THREE.MeshBasicMaterial({ color: face.cheek, side: THREE.DoubleSide }),
  );
  const cheekMaterial = track(
    new THREE.MeshBasicMaterial({ color: face.cheek, transparent: true, opacity: 0.8 }),
  );

  function ellipsoid(
    material: ThreeNamespace.Material,
    sx: number,
    sy: number,
    sz: number,
    segments = 24,
  ): ThreeNamespace.Mesh {
    const geometry = track(new THREE.SphereGeometry(1, segments, Math.round(segments * 0.75)));
    const mesh = new THREE.Mesh(geometry, material);
    mesh.scale.set(sx, sy, sz);
    return mesh;
  }

  /** Places `mesh` on the droplet's surface at height `y` / angle `az`, facing outward. */
  function placeOn(mesh: ThreeNamespace.Object3D, y: number, az: number, lift: number) {
    const r = profile(y);
    const slope = profileSlope(y);
    const norm = Math.sqrt(1 + slope * slope);
    const nx = Math.sin(az) / norm;
    const nz = Math.cos(az) / norm;
    const ny = -slope / norm;
    const x = r * Math.sin(az) + nx * lift;
    const py = y + ny * lift;
    const z = r * Math.cos(az) + nz * lift;
    mesh.position.set(x, py, z);
    mesh.lookAt(x + nx, py + ny, z + nz);
  }

  // ---- rig ----
  const root = new THREE.Group();
  const bodyGroup = new THREE.Group();
  const attach = new THREE.Group();
  const faceGroup = new THREE.Group();
  bodyGroup.position.y = 0.95;
  root.add(bodyGroup);
  bodyGroup.add(attach);
  attach.add(faceGroup);
  scene.add(root);

  const profilePoints: ThreeNamespace.Vector2[] = [];
  for (let i = 0; i <= 90; i++) {
    const y = H0 + (HT * i) / 90;
    profilePoints.push(new THREE.Vector2(Math.max(0.0001, profile(y)), y));
  }
  const body = new THREE.Mesh(track(new THREE.LatheGeometry(profilePoints, 72)), bodyMaterial);
  body.renderOrder = 5;
  bodyGroup.add(body);

  const core = ellipsoid(coreMaterial, 0.34, 0.3, 0.3, 20);
  core.position.set(0, -0.28, -0.2);
  core.renderOrder = 1;
  bodyGroup.add(core);

  const bubbles: ThreeNamespace.Mesh[] = [];
  for (let i = 0; i < 4; i++) {
    const bubble = ellipsoid(
      track(new THREE.MeshBasicMaterial({ color: face.shine, transparent: true, opacity: 0.5 })),
      0.07,
      0.07,
      0.07,
      10,
    );
    bubble.renderOrder = 2;
    bodyGroup.add(bubble);
    bubbles.push(bubble);
  }

  // Face: eyes (with a happy-arc spare), brows, cheeks, and a live mouth ribbon.
  const eyes: { ball: ThreeNamespace.Group }[] = [];
  const brows: { arc: ThreeNamespace.Mesh; side: number }[] = [];
  const cheeks: ThreeNamespace.Mesh[] = [];
  for (const side of [-1, 1]) {
    const eye = new THREE.Group();
    const ball = new THREE.Group();
    const er = 0.17;
    ball.add(ellipsoid(eyeMaterial, er * 0.86, er * 1.12, er * 0.5, 20));
    const glint = ellipsoid(shineMaterial, er * 0.32, er * 0.32, er * 0.3, 10);
    glint.position.set(er * 0.28, er * 0.5, er * 0.45);
    ball.add(glint);
    const glint2 = ellipsoid(shineMaterial, er * 0.16, er * 0.16, er * 0.15, 8);
    glint2.position.set(-er * 0.3, -er * 0.42, er * 0.45);
    ball.add(glint2);
    eye.add(ball);
    placeOn(eye, 0.16, side * 0.4, 0.02);
    faceGroup.add(eye);
    eyes.push({ ball });

    const brow = new THREE.Mesh(
      track(new THREE.TorusGeometry(0.13, 0.013, 8, 20, Math.PI * 0.8)),
      inkMaterial,
    );
    const browGroup = new THREE.Group();
    browGroup.add(brow);
    placeOn(browGroup, 0.52, side * 0.4, 0.02);
    faceGroup.add(browGroup);
    brows.push({ arc: brow, side });

    const cheek = new THREE.Mesh(track(new THREE.CircleGeometry(0.14, 24)), cheekMaterial);
    cheek.scale.set(1.25, 0.8, 1);
    placeOn(cheek, -0.12, side * 0.66, 0.03);
    faceGroup.add(cheek);
    cheeks.push(cheek);
  }

  const MOUTH_SEGMENTS = 20;
  const mouthPositions = new Float32Array((MOUTH_SEGMENTS + 1) * 2 * 3);
  const mouthGeometry = track(new THREE.BufferGeometry());
  mouthGeometry.setAttribute("position", new THREE.BufferAttribute(mouthPositions, 3));
  const mouthIndex: number[] = [];
  for (let i = 0; i < MOUTH_SEGMENTS; i++) {
    const a = i * 2;
    mouthIndex.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
  }
  mouthGeometry.setIndex(mouthIndex);
  const mouthGroup = new THREE.Group();
  const mouthMesh = new THREE.Mesh(mouthGeometry, mouthMaterial);
  mouthMesh.frustumCulled = false;
  mouthGroup.add(mouthMesh);
  const tongue = new THREE.Mesh(
    track(new THREE.CircleGeometry(0.055, 16, Math.PI, Math.PI)),
    tongueMaterial,
  );
  mouthGroup.add(tongue);
  const tongueOut = ellipsoid(tongueMaterial, 0.045, 0.035, 0.01, 12);
  mouthGroup.add(tongueOut);
  placeOn(mouthGroup, -0.05, 0, 0.03);
  mouthGroup.position.y += 0.03;
  faceGroup.add(mouthGroup);

  function updateMouth(smile: number, open: number, width: number, wobble: number, time: number) {
    for (let i = 0; i <= MOUTH_SEGMENTS; i++) {
      const u = (i / MOUTH_SEGMENTS) * 2 - 1;
      const curve = 1 - u * u;
      const x = u * 0.15 * width;
      const top = -smile * 0.075 * curve + wobble * 0.022 * Math.sin(u * 8 + time * 6);
      const bottom = top - (0.014 + open * 0.15 * curve ** 0.6);
      mouthPositions[i * 6] = x;
      mouthPositions[i * 6 + 1] = top;
      mouthPositions[i * 6 + 3] = x;
      mouthPositions[i * 6 + 4] = bottom;
    }
    const attribute = mouthGeometry.getAttribute("position");
    attribute.needsUpdate = true;
    tongue.visible = open > 0.3;
    tongue.position.set(0, -smile * 0.075 - (0.014 + open * 0.15) + 0.05, 0.002);
    tongue.scale.set(1, Math.min(1, open * 1.4), 1);
  }

  function makeArm(side: number): ThreeNamespace.Group {
    const pivot = new THREE.Group();
    pivot.position.set(side * 0.98, -0.18, 0.1);
    const arm = ellipsoid(limbMaterial, 0.17, 0.3, 0.17, 20);
    arm.position.set(side * 0.03, -0.26, 0);
    arm.renderOrder = 6;
    pivot.add(arm);
    attach.add(pivot);
    return pivot;
  }
  const armRight = makeArm(1);
  const armLeft = makeArm(-1);

  const FOOT_Y = 0.15;
  function makeFoot(side: number): ThreeNamespace.Mesh {
    const foot = ellipsoid(limbMaterial, 0.27, 0.15, 0.32, 18);
    foot.position.set(side * 0.4, FOOT_Y, 0.16);
    foot.renderOrder = 6;
    root.add(foot);
    return foot;
  }
  const footRight = makeFoot(1);
  const footLeft = makeFoot(-1);

  // Floor: a soft contact shadow.
  const shadowTexture = (() => {
    const c = document.createElement("canvas");
    c.width = 128;
    c.height = 128;
    const g = c.getContext("2d");
    if (g) {
      const gradient = g.createRadialGradient(64, 64, 4, 64, 64, 62);
      gradient.addColorStop(0, `${face.ink}59`);
      gradient.addColorStop(1, `${face.ink}00`);
      g.fillStyle = gradient;
      g.fillRect(0, 0, 128, 128);
    }
    return track(new THREE.CanvasTexture(c));
  })();
  const shadow = new THREE.Mesh(
    track(new THREE.PlaneGeometry(3.6, 3.6)),
    track(
      new THREE.MeshBasicMaterial({ map: shadowTexture, transparent: true, depthWrite: false }),
    ),
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = 0.004;
  scene.add(shadow);
  // ---- props: thought cloud and "…" bubble (textures are shared, see props.ts) ----
  let props = options.props;
  function makeSprite(): ThreeNamespace.Sprite {
    const sprite = new THREE.Sprite(
      track(new THREE.SpriteMaterial({ transparent: true, depthWrite: false })),
    );
    sprite.visible = false;
    scene.add(sprite);
    return sprite;
  }
  const cloud = makeSprite();
  const chat = makeSprite();
  const thoughtTrail = [makeSprite(), makeSprite(), makeSprite()];

  const sparkTexture = (() => {
    const c = document.createElement("canvas");
    c.width = 128;
    c.height = 128;
    const g = c.getContext("2d");
    if (g) {
      g.translate(64, 64);
      g.scale(2.6, 2.6);
      g.translate(-60, -16);
      g.fillStyle = face.spark;
      g.fill(new Path2D(face.sparkPath));
    }
    return track(new THREE.CanvasTexture(c));
  })();
  const sparkSprite = new THREE.Sprite(
    track(new THREE.SpriteMaterial({ map: sparkTexture, transparent: true, depthWrite: false })),
  );
  sparkSprite.renderOrder = 8;
  attach.add(sparkSprite);

  function applyProps(next: BloopPropTextures | null) {
    props = next;
    for (const dot of thoughtTrail) dot.material.map = next?.circle ?? null;
    cloud.material.map = next?.cloud[0] ?? null;
    chat.material.map = next?.chat[0] ?? null;
    for (const sprite of [cloud, chat, ...thoughtTrail]) {
      sprite.material.needsUpdate = true;
    }
  }
  applyProps(props);

  // ---- state ----
  const pose: BloopPose = { ...NEUTRAL_POSE };
  const targets: BloopPose = { ...NEUTRAL_POSE };
  let state = options.state;
  let stateStart = performance.now() / 1000;
  const look = { x: 0, y: 0, tx: 0, ty: 0 };
  const shear = { x: 0, y: 0, vx: 0, vy: 0 };
  let prevTilt = 0;
  let prevLean = 0;
  let lastTime = 0;
  let blinkAt = 2.2;
  let blinkStart = -10;

  function applyPalette(color: string) {
    const next = bloopPalette(color);
    coreMaterial.color.set(next.core);
    for (const material of [bodyMaterial, limbMaterial]) {
      material.uniforms.uTop?.value.set(next.top);
      material.uniforms.uBot?.value.set(next.bot);
      material.uniforms.uRim?.value.set(next.rim);
    }
  }

  function update(nowMs: number) {
    const now = nowMs / 1000;
    const dt = lastTime ? Math.min(0.05, now - lastTime) : 0.016;
    lastTime = now;
    const t = now - stateStart;

    Object.assign(targets, poseFor(state, t, options.amp));
    for (const key of POSE_KEYS) {
      const rate = FAST_KEYS.has(key) ? 20 : PROP_KEYS.has(key) ? 7 : 11;
      pose[key] += (targets[key] - pose[key]) * (1 - Math.exp(-dt * rate));
    }

    // Jelly spring: the tip lags the body's tilt.
    const tiltRate = dt > 0 ? (pose.tilt - prevTilt) / dt : 0;
    const leanRate = dt > 0 ? (pose.lean - prevLean) / dt : 0;
    prevTilt = pose.tilt;
    prevLean = pose.lean;
    shear.vx += (-140 * shear.x - 9 * shear.vx - 7 * tiltRate) * dt;
    shear.vy += (-140 * shear.y - 9 * shear.vy - 7 * leanRate) * dt;
    shear.x = Math.max(-0.7, Math.min(0.7, shear.x + shear.vx * dt));
    shear.y = Math.max(-0.7, Math.min(0.7, shear.y + shear.vy * dt));
    const sx = shear.x + pose.tipX;
    const sy = shear.y + pose.tipZ;

    root.position.y = pose.hop;
    bodyGroup.scale.set(1 + pose.squash, 1 - pose.squash, 1 + pose.squash);
    bodyGroup.rotation.set(pose.lean, 0, pose.tilt);
    armRight.rotation.set(pose.xR, 0, pose.aR);
    armLeft.rotation.set(pose.xL, 0, -pose.aL);
    footRight.position.y = FOOT_Y + pose.fR;
    footLeft.position.y = FOOT_Y;
    attach.position.set(sx * 0.24, 0, sy * 0.24);
    for (const material of [bodyMaterial, limbMaterial]) {
      const uniforms = material.uniforms;
      if (uniforms.uTime) uniforms.uTime.value = now;
    }
    bodyMaterial.uniforms.uShear?.value.set(sx, sy);
    if (bodyMaterial.uniforms.uWob) bodyMaterial.uniforms.uWob.value = pose.wob;

    core.position.y = -0.28 + 0.03 * Math.sin(now * 2);
    bubbles.forEach((bubble, i) => {
      const phase = (now * (0.1 + 0.03 * i) + i * 0.37) % 1;
      const y = -0.7 + phase * 1.2;
      const r = profile(y) * 0.55;
      bubble.position.set(
        Math.sin(now * 0.7 + i * 2.1) * r,
        y,
        Math.cos(now * 0.7 + i * 2.1) * r * 0.6 - 0.1,
      );
      const material = bubble.material as ThreeNamespace.MeshBasicMaterial;
      material.opacity = 0.5 * Math.sin(Math.PI * phase);
    });

    // The gold spark floats above the tip, twinkles, and spins while the Muse is working.
    sparkSprite.position.set(0, 1.95 + 0.05 * Math.sin(now * 1.6), 0);
    const twinkle = 1 + 0.08 * Math.sin(now * 3);
    sparkSprite.scale.set(0.62 * twinkle, 0.62 * twinkle, 1);
    sparkSprite.material.rotation += dt * 4 * pose.pSpin;

    // Face.
    look.x += (look.tx - look.x) * Math.min(1, dt * 6);
    look.y += (look.ty - look.y) * Math.min(1, dt * 6);
    const gx = pose.gx * (1 - pose.follow) + look.x * pose.follow;
    const gy = pose.gy * (1 - pose.follow) + look.y * 0.8 * pose.follow;
    faceGroup.rotation.y = gx * 0.18;
    faceGroup.rotation.x = -gy * 0.12;
    if (now > blinkAt) {
      blinkStart = now;
      blinkAt = now + 2.2 + ((now * 7.3) % 2.8);
    }
    const blink = now - blinkStart < 0.13 ? 0.08 : 1;
    for (const eye of eyes) {
      eye.ball.scale.set(pose.eyeW, Math.max(0.02, pose.eye * blink), 1);
      eye.ball.position.set(gx * 0.055, gy * 0.045, 0);
    }
    for (const brow of brows) {
      const lift = brow.side < 0 ? pose.bRL : pose.bRR;
      const tilt = brow.side < 0 ? pose.bTL : pose.bTR;
      brow.arc.position.y = -0.1 + pose.bY + lift;
      brow.arc.rotation.z = Math.PI * 0.1 + brow.side * tilt;
    }
    cheekMaterial.opacity = Math.min(1, pose.blush);
    updateMouth(pose.smile, pose.open, pose.mW, pose.sq, now);
    tongueOut.visible = pose.tongue > 0.3;
    tongueOut.position.set(0.06 * pose.mW, -pose.smile * 0.075 * 0.4 - 0.03, 0.002);
    tongueOut.scale.set(0.045 * pose.tongue, 0.035 * pose.tongue, 0.01);

    // Props (only when shared textures were provided).
    const frame = Math.floor(now * 2.2) % 3;
    const thinkOn = props !== null && pose.pThink > 0.03;
    cloud.visible = thinkOn;
    cloud.position.set(-1.9, 3.9, 0.3);
    cloud.scale.set(1.6 * pose.pThink, 1.1 * pose.pThink, 1);
    if (props) cloud.material.map = props.cloud[frame] ?? null;
    thoughtTrail.forEach((dot, i) => {
      dot.visible = thinkOn;
      const pulse = 1 + 0.12 * Math.sin(now * 3 + i);
      dot.position.set(-0.95 - i * 0.28, 2.65 + i * 0.4, 0.3);
      const size = [0.16, 0.24, 0.34][i] ?? 0.2;
      dot.scale.set(size * pulse * pose.pThink, size * pulse * pose.pThink, 1);
    });
    chat.visible = props !== null && pose.pChat > 0.03;
    chat.position.set(1.8, 3.1 + 0.06 * Math.sin(now * 2), 0.3);
    chat.scale.set(1.3 * pose.pChat, 0.9 * pose.pChat, 1);
    if (props) chat.material.map = props.chat[frame] ?? null;
    // Floor.
    const lift = Math.min(1, pose.hop / 1.1);
    const shadowScale = 1 - lift * 0.35;
    shadow.scale.set(shadowScale, shadowScale, shadowScale);
  }

  return {
    setState(next) {
      if (next === state) return;
      state = next;
      stateStart = performance.now() / 1000;
    },
    setColor: applyPalette,
    setProps: applyProps,
    setPointer(x, y) {
      look.tx = x;
      look.ty = y;
    },
    update,
    render(renderer) {
      renderer.render(scene, camera);
    },
    dispose() {
      for (const item of disposables) item.dispose();
    },
  };
}
