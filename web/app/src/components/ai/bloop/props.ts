import type * as ThreeNamespace from "three";

type Three = typeof ThreeNamespace;

const TAU = Math.PI * 2;

export interface BloopTheme {
  /** CSS color strings read from the semantic tokens (`--card`, `--border`, `--foreground`). */
  card: string;
  border: string;
  foreground: string;
}

/** The thought cloud, "…" bubble and trail circle, drawn once per theme and shared. */
export interface BloopPropTextures {
  cloud: ThreeNamespace.CanvasTexture[];
  chat: ThreeNamespace.CanvasTexture[];
  circle: ThreeNamespace.CanvasTexture;
  dispose(): void;
}

export function buildPropTextures(THREE: Three, theme: BloopTheme): BloopPropTextures {
  const all: ThreeNamespace.CanvasTexture[] = [];
  const draw = (
    width: number,
    height: number,
    paint: (g: CanvasRenderingContext2D) => void,
  ): ThreeNamespace.CanvasTexture => {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const g = canvas.getContext("2d");
    if (g) paint(g);
    const texture = new THREE.CanvasTexture(canvas);
    all.push(texture);
    return texture;
  };
  const dots = (
    g: CanvasRenderingContext2D,
    lit: number,
    x0: number,
    y: number,
    gap: number,
    r: number,
  ) => {
    for (let d = 0; d < 3; d++) {
      g.fillStyle = d < lit ? theme.foreground : theme.border;
      g.beginPath();
      g.arc(x0 + d * gap, y, r, 0, TAU);
      g.fill();
    }
  };

  const cloud: ThreeNamespace.CanvasTexture[] = [];
  const chat: ThreeNamespace.CanvasTexture[] = [];
  for (let lit = 1; lit <= 3; lit++) {
    cloud.push(
      draw(320, 220, (g) => {
        const circles: [number, number, number][] = [
          [90, 120, 56],
          [150, 90, 66],
          [215, 112, 58],
          [160, 140, 62],
          [110, 150, 48],
          [205, 150, 48],
        ];
        g.strokeStyle = theme.border;
        g.lineWidth = 8;
        for (const [x, y, r] of circles) {
          g.beginPath();
          g.arc(x, y, r, 0, TAU);
          g.stroke();
        }
        g.fillStyle = theme.card;
        for (const [x, y, r] of circles) {
          g.beginPath();
          g.arc(x, y, r - 1, 0, TAU);
          g.fill();
        }
        dots(g, lit, 112, 124, 48, 14);
      }),
    );
    chat.push(
      draw(256, 176, (g) => {
        g.fillStyle = theme.card;
        g.strokeStyle = theme.border;
        g.lineWidth = 7;
        g.beginPath();
        g.moveTo(40, 150);
        g.lineTo(58, 110);
        g.lineTo(30, 100);
        g.quadraticCurveTo(14, 100, 14, 84);
        g.lineTo(14, 36);
        g.quadraticCurveTo(14, 14, 36, 14);
        g.lineTo(220, 14);
        g.quadraticCurveTo(242, 14, 242, 36);
        g.lineTo(242, 84);
        g.quadraticCurveTo(242, 106, 220, 106);
        g.lineTo(96, 106);
        g.closePath();
        g.fill();
        g.stroke();
        dots(g, lit, 88, 60, 40, 13);
      }),
    );
  }
  const circle = draw(64, 64, (g) => {
    g.fillStyle = theme.card;
    g.strokeStyle = theme.border;
    g.lineWidth = 5;
    g.beginPath();
    g.arc(32, 32, 26, 0, TAU);
    g.fill();
    g.stroke();
  });

  return {
    cloud,
    chat,
    circle,
    dispose() {
      for (const texture of all) texture.dispose();
    },
  };
}
