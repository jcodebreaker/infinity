import { DEFAULT_MUSE_COLOR } from "@superchat/contracts";
import { ACTIVE_RUN_STATUSES } from "@superchat/core";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AvatarStyleProvider } from "./avatar-style.js";
import {
  BotAvatar,
  DEFAULT_GROK_BOT_COLOR,
  GROK_BOT_COLORS,
  GrokShapePreview,
  museAvatarState,
  parseBotAvatar,
  resolvePersonaColorDef,
  resolvePersonaShape,
} from "./bot-avatar.js";
import { type LiveMuseFaceProps, LiveMuseFaceProvider } from "./muse-face.js";

describe("BotAvatar", () => {
  it("renders distinct SVG gradient IDs for concurrent working avatars", () => {
    const html = renderToString(
      <div>
        <BotAvatar color="#8B5CF6" status="running" />
        <BotAvatar color="#10B981" status="running" />
      </div>,
    );

    const gradMatches = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
    expect(gradMatches).toHaveLength(4);
    expect(new Set(gradMatches).size).toBe(4);
    for (const id of gradMatches) {
      expect(id).toBeTruthy();
      expect(html).toContain(`url(#${id})`);
    }
  });

  it.each([...ACTIVE_RUN_STATUSES])("marks active run status %s as working", (status) => {
    const html = renderToString(<BotAvatar color="#3B82F6" status={status} />);
    expect(html).toContain("<svg");
    expect(html).toContain('data-working="true"');
  });

  it("keeps working attribute false when idle", () => {
    const html = renderToString(<BotAvatar color="#F59E0B" status="idle" />);
    expect(html).toContain('data-working="false"');
  });

  it("renders a geometric mascot for plain color values", () => {
    const html = renderToString(
      <BotAvatar color={DEFAULT_GROK_BOT_COLOR} identity="maya" size={28} status="running" />,
    );
    expect(html).toContain("<svg");
    expect(html).toContain("<path");
    expect(html).toContain("<ellipse");
    expect(html).toContain('data-working="true"');
  });

  it("renders distinct shapes for distinct bot identities", () => {
    const maya = renderToString(<BotAvatar color={DEFAULT_GROK_BOT_COLOR} identity="maya" />);
    const github = renderToString(<BotAvatar color={DEFAULT_GROK_BOT_COLOR} identity="github" />);
    expect(maya).not.toEqual(github);
  });

  it("parses shape indexes from encoded color values", () => {
    const parsed = parseBotAvatar(`${DEFAULT_GROK_BOT_COLOR}::shape_3`);
    expect(parsed.color).toBe(DEFAULT_GROK_BOT_COLOR);
    expect(parsed.shapeIndex).toBe(3);
    expect(parsed.isImage).toBe(false);
  });

  it("normalizes malformed shape suffixes to shape 0", () => {
    expect(parseBotAvatar(`${DEFAULT_GROK_BOT_COLOR}::shape_-1`).shapeIndex).toBe(0);
    expect(parseBotAvatar(`${DEFAULT_GROK_BOT_COLOR}::shape_3junk`).shapeIndex).toBe(0);
    expect(parseBotAvatar(`${DEFAULT_GROK_BOT_COLOR}::shape_`).shapeIndex).toBe(0);
  });

  it("exposes the violet identity color as the shared default", () => {
    expect(GROK_BOT_COLORS).toContain(DEFAULT_GROK_BOT_COLOR);
    expect(parseBotAvatar(`${DEFAULT_GROK_BOT_COLOR}::shape_0`).color).toBe(DEFAULT_GROK_BOT_COLOR);
  });

  it("resolves explicit colors and shapes", () => {
    expect(resolvePersonaColorDef("bot", "#10B981").hex.toLowerCase()).toBe("#10b981");
    expect(resolvePersonaColorDef("bot", "#fff").hex).toBe("#fff");
    expect(resolvePersonaShape("bot", "hex")).toContain("M");
    expect(GROK_BOT_COLORS.length).toBeGreaterThan(0);
  });

  it("falls back to the identity palette for invalid custom hex", () => {
    expect(resolvePersonaColorDef("bot", "#zzzzzz")).toEqual(resolvePersonaColorDef("bot"));
    expect(resolvePersonaColorDef("bot", "#ggg")).toEqual(resolvePersonaColorDef("bot"));
  });

  it("renders uploaded images without the geometric svg", () => {
    const html = renderToString(
      <BotAvatar color="data:image/png;base64,abc" identity="maya" size={32} />,
    );
    expect(html).toContain("<img");
    expect(html).not.toContain("<path");
    expect(html).not.toContain("grok-character-eyes");
  });

  it("does not treat arbitrary http(s) color values as remote images", () => {
    const parsed = parseBotAvatar("https://evil.example/track.png");
    expect(parsed.isImage).toBe(false);
    expect(parsed.imageUrl).toBeUndefined();
    const html = renderToString(
      <BotAvatar color="https://evil.example/track.png" identity="maya" size={32} />,
    );
    expect(html).not.toContain("<img");
    expect(html).not.toContain("evil.example");
  });

  it("honors reduced-motion for the working mascot pulse class", () => {
    const html = renderToString(
      <BotAvatar color="#8B5CF6" identity="maya" size={32} status="running" />,
    );
    expect(html).toContain("animate-pulse");
    expect(html).toContain("motion-reduce:animate-none");
  });

  it("exposes shape picker name and pressed state", () => {
    const html = renderToString(
      <GrokShapePreview shapeIndex={0} color="#8B5CF6" selected onClick={() => undefined} />,
    );
    expect(html).toContain('aria-label="hex"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain("focus-visible:ring-2");
  });

  it("renders distinct robot and organic previews for the same identity", () => {
    const robot = renderToString(
      <BotAvatar color={DEFAULT_GROK_BOT_COLOR} identity="avatar-style-preview" variant="robot" />,
    );
    const organic = renderToString(
      <BotAvatar
        color={DEFAULT_GROK_BOT_COLOR}
        identity="avatar-style-preview"
        variant="organic"
      />,
    );
    expect(robot).not.toEqual(organic);
    expect(robot).toContain("grok-character-eyes");
    expect(organic).toContain("superchat-organic-avatar");
    expect(organic).not.toContain("grok-character-eyes");
  });

  it("uses the preferred avatar style when variant is omitted", () => {
    const html = renderToString(
      <AvatarStyleProvider value="organic">
        <BotAvatar color={DEFAULT_GROK_BOT_COLOR} identity="maya" />
      </AvatarStyleProvider>,
    );
    expect(html).toContain("superchat-organic-avatar");
    expect(html).not.toContain("grok-character-eyes");
  });

  it("keeps uploaded images when the organic style is preferred", () => {
    const html = renderToString(
      <BotAvatar color="data:image/png;base64,abc" identity="maya" variant="organic" />,
    );
    expect(html).toContain("<img");
    expect(html).not.toContain("superchat-organic-avatar");
  });

  it("keeps an encoded studio shape when the organic style is preferred", () => {
    const html = renderToString(
      <BotAvatar color={`${DEFAULT_GROK_BOT_COLOR}::shape_3`} identity="maya" variant="organic" />,
    );
    expect(html).toContain("grok-character-eyes");
    expect(html).not.toContain("superchat-organic-avatar");
  });

  it("fills the organic body with the resolved palette hex when the custom color is invalid", () => {
    const fallback = resolvePersonaColorDef("maya", "#zzzzzz");
    const html = renderToString(<BotAvatar color="#zzzzzz" identity="maya" variant="organic" />);
    expect(html).toContain("superchat-organic-avatar");
    expect(html).toContain(`fill="${fallback.hex}"`);
    expect(html).not.toContain("#zzzzzz");
  });
});

describe("Muse face", () => {
  it.each(ACTIVE_RUN_STATUSES.filter((status) => status !== "queued" && status !== "leased"))(
    "derives the working state from active run status %s",
    (status) => {
      expect(museAvatarState(status, 0)).toBe("working");
    },
  );

  it("derives idle when there is no active run and nothing waiting", () => {
    expect(museAvatarState("idle", 0)).toBe("idle");
    expect(museAvatarState(undefined, undefined)).toBe("idle");
  });

  it("derives waiting when there is an open ask, even while working", () => {
    expect(museAvatarState("running", 3)).toBe("waiting");
    expect(museAvatarState("queued", 0)).toBe("thinking");
    expect(museAvatarState("leased", 0)).toBe("thinking");
    expect(museAvatarState(undefined, 1)).toBe("waiting");
  });

  it("renders the muse face with the identity color on the body and the state as a data attribute", () => {
    const html = renderToString(<BotAvatar color="#22C55E" face="muse" status="running" />);
    expect(html).toContain('data-muse-state="working"');
    expect(html).toContain('fill="#22C55E"');
    expect(html).not.toContain("grok-character-eyes");
  });

  it("defaults to DEFAULT_MUSE_COLOR when no color is set", () => {
    const html = renderToString(<BotAvatar color="" face="muse" />);
    expect(html).toContain(`fill="${DEFAULT_MUSE_COLOR}"`);
  });

  it("shows a numeric waiting badge at a legible size, capped at 9+", () => {
    const nine = renderToString(
      <BotAvatar color="#0090FF" face="muse" size={40} waitingCount={9} />,
    );
    expect(nine).toContain("muse-waiting-badge");
    expect(nine).toContain(">9<");

    const ten = renderToString(
      <BotAvatar color="#0090FF" face="muse" size={40} waitingCount={10} />,
    );
    expect(ten).toContain(">9+<");
  });

  it("shows a dot instead of badge text below the legible size", () => {
    const html = renderToString(
      <BotAvatar color="#0090FF" face="muse" size={24} waitingCount={3} />,
    );
    expect(html).toContain("muse-waiting-dot");
    expect(html).not.toContain("muse-waiting-badge");
  });

  it("shows no badge when nothing is waiting", () => {
    const html = renderToString(
      <BotAvatar color="#0090FF" face="muse" size={40} waitingCount={0} />,
    );
    expect(html).not.toContain("muse-waiting-badge");
    expect(html).not.toContain("muse-waiting-dot");
  });

  it("prefers an explicit museState over the status/waitingCount derivation", () => {
    const html = renderToString(
      <BotAvatar color="#0090FF" face="muse" status="running" waitingCount={3} museState="idle" />,
    );
    expect(html).toContain('data-muse-state="idle"');
  });

  it("waiting wins over working in the rendered state", () => {
    const html = renderToString(
      <BotAvatar color="#0090FF" face="muse" status="running" waitingCount={2} />,
    );
    expect(html).toContain('data-muse-state="waiting"');
  });

  it("leaves non-muse avatars unchanged when face is omitted", () => {
    const html = renderToString(<BotAvatar color={DEFAULT_GROK_BOT_COLOR} identity="maya" />);
    expect(html).not.toContain("superchat-muse-avatar");
    expect(html).toContain("grok-character-eyes");
  });

  it("renders a distinct expression marker per state", () => {
    const idle = renderToString(<BotAvatar color="#0090FF" face="muse" status="idle" />);
    const thinking = renderToString(<BotAvatar color="#0090FF" face="muse" status="queued" />);
    const working = renderToString(<BotAvatar color="#0090FF" face="muse" status="running" />);
    const waiting = renderToString(<BotAvatar color="#0090FF" face="muse" waitingCount={1} />);

    expect(idle).toContain("superchat-muse-expression-idle");
    expect(idle).toContain("superchat-muse-mouth-idle");

    expect(thinking).toContain("superchat-muse-expression-thinking");
    expect(thinking).toContain("superchat-muse-mouth-thinking");
    expect(thinking).toContain("superchat-muse-eyebrow-right");

    expect(working).toContain("superchat-muse-expression-working");
    expect(working).toContain("superchat-muse-mouth-working");
    expect(working).toContain("superchat-muse-squint");

    expect(waiting).toContain("superchat-muse-expression-waiting");
    expect(waiting).toContain("superchat-muse-mouth-waiting");
    expect(waiting).toContain("superchat-muse-eyebrow-left");
    expect(waiting).toContain("superchat-muse-eyebrow-right");
    expect(waiting).toContain("superchat-muse-hand");
  });

  it("renders unique depth/shadow gradient ids per muse instance", () => {
    const html = renderToString(
      <div>
        <BotAvatar color="#0090FF" face="muse" />
        <BotAvatar color="#22C55E" face="muse" />
      </div>,
    );

    const gradIds = [...html.matchAll(/id="([^"]+-(?:depth|shadow))"/g)].map((m) => m[1]);
    expect(gradIds).toHaveLength(4);
    expect(new Set(gradIds).size).toBe(4);
    for (const id of gradIds) {
      expect(html).toContain(`url(#${id})`);
    }
  });

  it("keeps the identity color on the body under the new depth overlay", () => {
    const html = renderToString(<BotAvatar color="#9333EA" face="muse" status="idle" />);
    expect(html).toContain('class="superchat-muse-body" fill="#9333EA"');
  });
});

describe("BotAvatar live Muse face", () => {
  const Live = ({ state, size, waitingCount, identity }: LiveMuseFaceProps) => (
    <span
      data-testid="live-face"
      data-state={state}
      data-size={size}
      data-asks={waitingCount}
      data-id={identity}
    />
  );

  it("draws the static Muse face when no live renderer is provided", () => {
    const html = renderToString(<BotAvatar color="#0090FF" face="muse" size={40} />);
    expect(html).toContain("superchat-muse-avatar");
    expect(html).not.toContain("live-face");
  });

  it("hands the derived state, size, asks and identity to a provided live renderer", () => {
    const html = renderToString(
      <LiveMuseFaceProvider value={Live}>
        <BotAvatar
          color="#0090FF"
          face="muse"
          size={40}
          identity="superchat"
          status="running"
          waitingCount={2}
        />
      </LiveMuseFaceProvider>,
    );
    expect(html).toContain('data-testid="live-face"');
    // An open Ask outranks an active run, exactly as in the static face.
    expect(html).toContain('data-state="waiting"');
    expect(html).toContain('data-asks="2"');
    expect(html).toContain('data-id="superchat"');
    expect(html).not.toContain("superchat-muse-avatar");
  });

  it("leaves non-Muse avatars alone even when a live renderer is provided", () => {
    const html = renderToString(
      <LiveMuseFaceProvider value={Live}>
        <BotAvatar color="#8B5CF6" size={36} />
      </LiveMuseFaceProvider>,
    );
    expect(html).not.toContain("live-face");
  });
});
