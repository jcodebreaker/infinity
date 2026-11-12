import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

// A browser with WebGL: the 3D face is loading, so the old flat face must stay hidden.
vi.mock("../webgl", () => ({ supportsWebGL: () => true, readCssColor: () => [0, 0, 0] }));
vi.mock("@superchat/ui-web", () => ({
  MuseAvatar: ({ faceHidden, waitingCount }: { faceHidden?: boolean; waitingCount: number }) => (
    <div data-hidden={String(!!faceHidden)} data-asks={waitingCount} />
  ),
  cn: (...parts: (string | false | undefined)[]) => parts.filter(Boolean).join(" "),
  MUSE_FACE_CHEEK: "#FF8E86",
  MUSE_FACE_INK: "#132320",
  MUSE_FACE_SHINE: "#FFFFFF",
  MUSE_FACE_SPARK: "#F4B63F",
  MUSE_SPARK_PATH: "M0 0",
}));

import { BloopAvatar } from "./BloopAvatar";

describe("BloopAvatar while loading", () => {
  it("shows a soft placeholder and keeps the old flat face hidden, never flashed first", () => {
    const html = renderToString(
      <BloopAvatar color="#0090FF" identity="muse" size={100} state="idle" waitingCount={2} />,
    );

    expect(html).toContain('data-bloop="loading"');
    expect(html).toContain('data-testid="bloop-placeholder"');
    expect(html).toContain('data-hidden="true"');
    // The Ask badge belongs to the static face component and still receives its count.
    expect(html).toContain('data-asks="2"');
  });

  it("tints the placeholder from the identity color, falling back to blue for white", () => {
    const white = renderToString(
      <BloopAvatar color="#FFFFFF" identity="muse" size={100} state="idle" waitingCount={0} />,
    );
    const blue = renderToString(
      <BloopAvatar color="#0090FF" identity="muse" size={100} state="idle" waitingCount={0} />,
    );
    const tint = (html: string) => /background:(#[0-9a-f]{6})/i.exec(html)?.[1];

    expect(tint(white)).toBeDefined();
    expect(tint(white)).toBe(tint(blue));
  });
});
