import type { MuseState } from "@superchat/contracts";
import { DEFAULT_MUSE_COLOR } from "@superchat/contracts";
import type { GrokColorDef } from "@superchat/core";
import {
  ACTIVE_RUN_STATUSES,
  avatarIdentitySeed,
  DEFAULT_GROK_BOT_COLOR,
  GROK_BOT_COLORS,
  GROK_COLOR_LIST,
  organicAvatarPath,
  resolvePersonaColorDef,
  SHIPPED_BOT_AVATAR_CENTER,
  SHIPPED_BOT_AVATAR_SHAPE_KEYS,
  SHIPPED_BOT_AVATAR_SHAPES,
  SHIPPED_BOT_AVATAR_VIEWBOX,
  shippedBotAvatarShapePath,
  shippedHash,
} from "@superchat/core";
import { tokens } from "@superchat/ui-tokens";
import type { CSSProperties } from "react";
import { memo, useId, useMemo, useSyncExternalStore } from "react";
import type { AvatarStyle } from "./avatar-style.js";
import { useAvatarStyle } from "./avatar-style.js";
import { cn } from "./lib/utils.js";
import { useLiveMuseFace } from "./muse-face.js";
import "./styles.css";

export type { GrokColorDef };
export { DEFAULT_GROK_BOT_COLOR, GROK_BOT_COLORS, GROK_COLOR_LIST, resolvePersonaColorDef };

export const GROK_SHAPES = SHIPPED_BOT_AVATAR_SHAPES;
export const SHIPPED_SHAPE_KEYS = SHIPPED_BOT_AVATAR_SHAPE_KEYS;
const VIEWBOX = SHIPPED_BOT_AVATAR_VIEWBOX;
const CENTER = SHIPPED_BOT_AVATAR_CENTER;

export const GROK_MASCOT_SHAPES = SHIPPED_SHAPE_KEYS.map(
  (k) => GROK_SHAPES[k] ?? FALLBACK_SHAPE_PATH,
);

const FALLBACK_SHAPE_PATH = GROK_SHAPES.hex ?? "";

export function resolvePersonaShape(identity: string, explicitShape?: string | null): string {
  if (explicitShape) {
    const explicit = GROK_SHAPES[explicitShape];
    if (explicit) return explicit;
  }
  let hash = shippedHash(identity);
  hash = Math.imul(hash ^ (hash >>> 16), 73244475);
  hash = Math.imul(hash ^ (hash >>> 13), 3266489909);
  const shapeIndex = ((hash ^ (hash >>> 16)) >>> 0) % SHIPPED_SHAPE_KEYS.length;
  const key = SHIPPED_SHAPE_KEYS[shapeIndex] ?? "hex";
  return GROK_SHAPES[key] ?? FALLBACK_SHAPE_PATH;
}

export function parseBotAvatar(
  rawColor: string,
  _identity?: string,
): {
  color: string;
  shapeIndex?: number;
  isImage: boolean;
  imageUrl?: string;
} {
  if (!rawColor) return { color: "#F97316", isImage: false };
  // Only data: image URLs are rendered. Arbitrary http(s)/blob values in `color`
  // must not become <img src> (SSRF / tracking when other members view the bot).
  if (rawColor.startsWith("data:image/")) {
    return { color: "#F97316", isImage: true, imageUrl: rawColor };
  }
  if (rawColor.includes("::shape_")) {
    const parts = rawColor.split("::shape_");
    const rawShapeIdx = parts[1] ?? "0";
    const parsedShapeIdx = /^\d+$/.test(rawShapeIdx) ? Number(rawShapeIdx) : 0;
    const shapeIdx = Number.isSafeInteger(parsedShapeIdx) ? parsedShapeIdx : 0;
    return {
      color: parts[0] || "#F97316",
      shapeIndex: shapeIdx % SHIPPED_SHAPE_KEYS.length,
      isImage: false,
    };
  }
  return { color: rawColor, isImage: false };
}

export interface BotAvatarProps {
  color: string;
  size?: number;
  status?: string;
  identity?: string;
  className?: string;
  variant?: AvatarStyle;
  /** Renders the Muse face (the old design notes) instead of the shipped mascot shapes. */
  face?: "muse";
  /** Open-Ask count for the `waiting` Muse state; ignored unless `face="muse"`. */
  waitingCount?: number;
  /**
   * Forces the Muse's expression regardless of `status`/`waitingCount` — either
   * precomputed live state (derived from the active run data, e.g. `deriveMuseState`
   * in web/app; callers that already track live run state should pass this instead
   * of a bot row's `status`, which is a snapshot that rarely reflects an in-flight
   * run) or a one-time wave (`state="waiting"`, then `"idle"`) in the first-run
   * welcome. Muse-only; ignored unless `face="muse"`. Does not affect the Ask-count
   * badge, which still only shows for a real `waitingCount`.
   */
  museState?: MuseState;
}

/**
 * `idle` / `working` / `waiting` are derived, never passed as free strings:
 * an open Ask always wins over an active run.
 */
export function museAvatarState(
  status: string | undefined,
  waitingCount: number | undefined,
): MuseState {
  if ((waitingCount ?? 0) > 0) return "waiting";
  // A run that is queued or just claimed is the Muse thinking; a running one is working.
  if (status === "queued" || status === "leased") return "thinking";
  if (ACTIVE_RUN_STATUSES.some((s) => s === status)) return "working";
  return "idle";
}

export const BotAvatar = memo(function BotAvatar({
  color,
  size = 36,
  status,
  identity = "",
  className,
  variant,
  face,
  waitingCount,
  museState,
}: BotAvatarProps) {
  const id = useId().replace(/[^a-zA-Z0-9-_]/g, "");
  const isWorking = ACTIVE_RUN_STATUSES.some((s) => s === status);
  const preferredVariant = useAvatarStyle();
  const LiveMuseFace = useLiveMuseFace();

  const parsed = useMemo(() => parseBotAvatar(color, identity), [color, identity]);
  const effectiveId = identity || parsed.color || "agent";

  const colorDef = useMemo(
    () => resolvePersonaColorDef(effectiveId, parsed.color),
    [effectiveId, parsed.color],
  );

  const shapePath = useMemo(() => {
    if (parsed.shapeIndex !== undefined) {
      return shippedBotAvatarShapePath(parsed.shapeIndex);
    }
    return resolvePersonaShape(effectiveId);
  }, [parsed.shapeIndex, effectiveId]);

  if (parsed.isImage && parsed.imageUrl) {
    return (
      <div
        className={cn(
          "superchat-bot-avatar relative overflow-hidden rounded-full flex items-center justify-center select-none bg-secondary shrink-0 border border-border",
          className,
        )}
        data-working={isWorking}
        style={{
          width: size,
          height: size,
          boxShadow: isWorking
            ? "0 0 0 2px #3B82F6, 0 0 10px rgba(59,130,246,0.6)"
            : "0 2px 5px rgba(0,0,0,0.5)",
        }}
      >
        {isWorking ? (
          <svg
            className="superchat-bot-avatar-ring absolute pointer-events-none"
            style={{
              inset: -4,
              width: size + 8,
              height: size + 8,
            }}
            viewBox="0 0 48 48"
            fill="none"
            aria-hidden="true"
          >
            <circle
              cx="24"
              cy="24"
              r="22"
              stroke="#3B82F6"
              strokeWidth="3.2"
              strokeLinecap="round"
              strokeDasharray="45 80"
            />
          </svg>
        ) : null}
        <img src={parsed.imageUrl} alt="" className="h-full w-full object-cover" />
      </div>
    );
  }

  if (face === "muse") {
    const museColor = color ? parsed.color : DEFAULT_MUSE_COLOR;
    const state = museState ?? museAvatarState(status, waitingCount);
    return LiveMuseFace ? (
      <LiveMuseFace
        color={museColor}
        size={size}
        state={state}
        waitingCount={waitingCount ?? 0}
        identity={identity}
        className={className}
      />
    ) : (
      <MuseAvatar
        color={museColor}
        size={size}
        state={state}
        waitingCount={waitingCount ?? 0}
        className={className}
      />
    );
  }

  if (parsed.shapeIndex === undefined && (variant ?? preferredVariant) === "organic") {
    return (
      <OrganicAvatar
        color={colorDef.hex}
        identity={effectiveId}
        size={size}
        isWorking={isWorking}
        className={className}
      />
    );
  }

  return (
    <div
      className={cn(
        "superchat-bot-avatar grok-avatar-container relative inline-flex items-center justify-center shrink-0 select-none",
        className,
      )}
      style={{
        width: size,
        height: size,
      }}
      data-working={isWorking}
    >
      <svg
        className="superchat-bot-avatar-ring absolute pointer-events-none"
        style={{
          inset: -4,
          width: size + 8,
          height: size + 8,
          filter: `drop-shadow(0 0 6px ${colorDef.light}) drop-shadow(0 0 10px #ffffff)`,
        }}
        viewBox="0 0 48 48"
        fill="none"
        aria-hidden="true"
      >
        <circle
          cx="24"
          cy="24"
          r="22"
          stroke={`url(#${id}-ring)`}
          strokeWidth="3.2"
          strokeLinecap="round"
          strokeDasharray="45 80"
        />
        <circle cx="43" cy="24" r="2.8" fill="#ffffff" />
        <defs>
          <linearGradient id={`${id}-ring`} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="1" />
            <stop offset="60%" stopColor={colorDef.light} stopOpacity="0.9" />
            <stop offset="100%" stopColor={colorDef.light} stopOpacity="0" />
          </linearGradient>
        </defs>
      </svg>
      <svg
        viewBox={VIEWBOX}
        width={size}
        height={size}
        aria-hidden="true"
        className={cn(
          "overflow-visible transition-transform duration-300",
          isWorking
            ? "animate-pulse scale-[1.04] motion-reduce:animate-none"
            : "hover:scale-[1.03] motion-reduce:hover:scale-100",
        )}
        style={{
          filter: isWorking
            ? `drop-shadow(0 0 8px ${colorDef.light}) drop-shadow(0 0 2px #ffffff)`
            : "drop-shadow(0 2px 4px rgba(0,0,0,0.45))",
        }}
      >
        <defs>
          <linearGradient id={`grok-ink-${id}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor={colorDef.light} />
            <stop offset="100%" stopColor={colorDef.dark} />
          </linearGradient>
        </defs>
        <g>
          <path d={shapePath} fill={`url(#grok-ink-${id})`} />
          <g fill={colorDef.eyeColor} className="grok-character-eyes">
            <ellipse cx={CENTER - 29} cy={CENTER - 8} rx={10} ry={7} />
            <ellipse cx={CENTER + 29} cy={CENTER - 8} rx={10} ry={7} />
          </g>
        </g>
      </svg>
    </div>
  );
});

function OrganicAvatar({
  color,
  identity,
  size,
  isWorking,
  className,
}: {
  color: string;
  identity?: string;
  size: number;
  isWorking: boolean;
  className?: string;
}) {
  const reducedMotion = useSyncExternalStore(
    subscribeToReducedMotion,
    reducedMotionSnapshot,
    () => false,
  );
  const seed = avatarIdentitySeed(identity || color || DEFAULT_GROK_BOT_COLOR);
  const duration = `${4.8 + (seed % 24) / 10}s`;
  const shapeA = organicAvatarPath(seed);
  const shapeB = organicAvatarPath(seed, 0.42);

  return (
    <svg
      viewBox="-60 -60 120 120"
      aria-hidden="true"
      className={cn("superchat-organic-avatar overflow-visible select-none", className)}
      data-working={isWorking}
      data-shape-family={seed % 10}
      data-eye-pattern={seed % 4}
      style={{
        width: size,
        height: size,
        flex: "none",
      }}
    >
      {(["idle", "working"] as const).map((mode) => (
        <path
          key={mode}
          className={`superchat-organic-avatar-body superchat-organic-avatar-body-${mode}`}
          d={shapeA}
          fill={color}
          style={
            {
              "--superchat-organic-path": `path("${shapeA}")`,
              filter:
                mode === "working"
                  ? `drop-shadow(0 0 ${Math.round(size * 0.16)}px ${color})`
                  : "drop-shadow(0 2px 3px rgba(0,0,0,.34))",
            } as CSSProperties
          }
        >
          {!reducedMotion ? (
            <animate
              attributeName="d"
              values={`${shapeA};${shapeB};${shapeA}`}
              dur={duration}
              repeatCount="indefinite"
            />
          ) : null}
        </path>
      ))}
      <g transform={`rotate(${(seed % 9) - 4})`}>
        {(["idle", "working"] as const).map((mode) => (
          <g
            key={mode}
            className={`superchat-organic-avatar-eyes superchat-organic-avatar-eyes-${mode}`}
            fill={tokens.background}
          >
            <rect x="-14" y="-12" width="7" height="24" rx="3.5" />
            <rect x="7" y="-12" width="7" height="24" rx="3.5" />
          </g>
        ))}
      </g>
    </svg>
  );
}

/**
 * Fixed accents for the Muse face (the old design notes). The body is the only part that
 * takes the bot's identity color; everything else — including the gradient/rim-light
 * overlays used for depth — is built from these constants or from the color prop, never
 * a new hardcoded hex.
 */
export const MUSE_FACE_INK = "#132320";
export const MUSE_FACE_CHEEK = "#FF8E86";
export const MUSE_FACE_SPARK = "#F4B63F";
export const MUSE_FACE_SHINE = "#FFFFFF";
const MUSE_BODY_PATH =
  "M60 24C90 24 104 44 104 68C104 94 86 108 60 108C34 108 16 94 16 68C16 44 30 24 60 24Z";
export const MUSE_SPARK_PATH = "M60 3L63 13L73 16L63 19L60 29L57 19L47 16L57 13Z";

/** Below this size a numeric waiting badge stops being legible; show a dot instead. */
const MUSE_BADGE_TEXT_MIN_SIZE = 32;

/** Small mouth per state; `waiting` gets an open-mouth ellipse instead (drawn separately). */
const MUSE_MOUTH_PATHS: Record<Exclude<MuseState, "waiting">, string> = {
  idle: "M54 82Q60 87 66 82",
  thinking: "M55 83Q60 80 65 83",
  working: "M56 82Q60 85 64 82",
};

/** The static SVG Muse face with its Ask badge. `faceHidden` keeps the badge but fades the face. */
export function MuseAvatar({
  color,
  size,
  state,
  waitingCount,
  className,
  faceHidden = false,
}: {
  color: string;
  size: number;
  state: MuseState;
  waitingCount: number;
  className?: string;
  faceHidden?: boolean;
}) {
  const showBadge = waitingCount > 0;
  const showBadgeText = size >= MUSE_BADGE_TEXT_MIN_SIZE;
  const badgeLabel = waitingCount > 9 ? "9+" : String(waitingCount);
  const rawGradId = useId();
  const gradId = rawGradId.replace(/[^a-zA-Z0-9-_]/g, "");
  const isThinking = state === "thinking";
  const isWorking = state === "working";
  const isWaiting = state === "waiting";

  return (
    <div
      className={cn(
        "superchat-muse-avatar-container relative inline-flex items-center justify-center shrink-0 select-none",
        className,
      )}
      style={{ width: size, height: size }}
    >
      <svg
        viewBox="0 0 120 120"
        width={size}
        height={size}
        aria-hidden="true"
        data-muse-state={state}
        className={cn(
          "superchat-muse-avatar overflow-visible transition-opacity duration-300 motion-reduce:transition-none",
          faceHidden && "opacity-0",
        )}
      >
        <defs>
          {/* Depth: lighter top-left, slightly deeper bottom-right, built only from the
              illustration's ink/shine constants layered over the flat identity-color fill. */}
          <linearGradient id={`${gradId}-depth`} x1="12%" y1="8%" x2="88%" y2="96%">
            <stop offset="0%" stopColor={MUSE_FACE_SHINE} stopOpacity={0.32} />
            <stop offset="45%" stopColor={MUSE_FACE_SHINE} stopOpacity={0} />
            <stop offset="100%" stopColor={MUSE_FACE_INK} stopOpacity={0.16} />
          </linearGradient>
          <radialGradient id={`${gradId}-shadow`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={MUSE_FACE_INK} stopOpacity={0.18} />
            <stop offset="100%" stopColor={MUSE_FACE_INK} stopOpacity={0} />
          </radialGradient>
        </defs>
        <ellipse cx={60} cy={112} rx={32} ry={6} fill={`url(#${gradId}-shadow)`} />
        <circle
          className="superchat-muse-glow"
          cx={60}
          cy={66}
          r={46}
          fill="none"
          stroke={color}
          strokeWidth={7}
          pointerEvents="none"
        />
        <g className="superchat-muse-all">
          <path className="superchat-muse-body" fill={color} d={MUSE_BODY_PATH} />
          <path
            className="superchat-muse-depth"
            fill={`url(#${gradId}-depth)`}
            d={MUSE_BODY_PATH}
            pointerEvents="none"
          />
          <path
            className="superchat-muse-rim"
            d="M28 38Q60 14 96 40"
            fill="none"
            stroke={MUSE_FACE_SHINE}
            strokeOpacity={0.4}
            strokeWidth={3}
            strokeLinecap="round"
          />
          <ellipse
            fill={MUSE_FACE_SHINE}
            opacity={0.28}
            cx={42}
            cy={42}
            rx={14}
            ry={8}
            transform="rotate(-24 42 42)"
          />
          <g className="superchat-muse-eyes">
            <g className="superchat-muse-pupils">
              <ellipse fill={MUSE_FACE_INK} cx={46} cy={66} rx={5.5} ry={7.5} />
              <ellipse fill={MUSE_FACE_INK} cx={74} cy={66} rx={5.5} ry={7.5} />
              <circle fill={MUSE_FACE_SHINE} cx={48} cy={63} r={1.8} />
              <circle fill={MUSE_FACE_SHINE} cx={76} cy={63} r={1.8} />
              {isWorking ? (
                <g className="superchat-muse-squint" fill={color}>
                  <ellipse cx={46} cy={61.5} rx={6.2} ry={3.4} />
                  <ellipse cx={74} cy={61.5} rx={6.2} ry={3.4} />
                </g>
              ) : null}
            </g>
            {isThinking ? (
              <path
                className="superchat-muse-eyebrow superchat-muse-eyebrow-right"
                d="M69 55Q76 49 83 54"
                stroke={MUSE_FACE_INK}
                strokeWidth={3}
                strokeLinecap="round"
                fill="none"
              />
            ) : null}
            {isWaiting ? (
              <>
                <path
                  className="superchat-muse-eyebrow superchat-muse-eyebrow-left"
                  d="M38 53Q46 46 54 52"
                  stroke={MUSE_FACE_INK}
                  strokeWidth={3}
                  strokeLinecap="round"
                  fill="none"
                />
                <path
                  className="superchat-muse-eyebrow superchat-muse-eyebrow-right"
                  d="M66 52Q74 46 82 53"
                  stroke={MUSE_FACE_INK}
                  strokeWidth={3}
                  strokeLinecap="round"
                  fill="none"
                />
              </>
            ) : null}
          </g>
          <ellipse fill={MUSE_FACE_CHEEK} opacity={0.5} cx={36} cy={80} rx={7} ry={4} />
          <ellipse fill={MUSE_FACE_CHEEK} opacity={0.5} cx={84} cy={80} rx={7} ry={4} />
          <g className={cn("superchat-muse-expression", `superchat-muse-expression-${state}`)}>
            {state === "waiting" ? (
              <ellipse
                className="superchat-muse-mouth superchat-muse-mouth-waiting"
                fill={MUSE_FACE_INK}
                cx={60}
                cy={85}
                rx={8}
                ry={6}
              />
            ) : (
              <path
                className={`superchat-muse-mouth superchat-muse-mouth-${state}`}
                d={MUSE_MOUTH_PATHS[state]}
                stroke={MUSE_FACE_INK}
                strokeWidth={3}
                strokeLinecap="round"
                fill="none"
              />
            )}
            {isWaiting ? (
              <g className="superchat-muse-hand">
                <path
                  d="M100 66Q112 60 110 50"
                  stroke={MUSE_FACE_INK}
                  strokeWidth={3}
                  strokeLinecap="round"
                  fill="none"
                />
                <circle
                  className="superchat-muse-hand-palm"
                  fill={color}
                  stroke={MUSE_FACE_INK}
                  strokeWidth={2}
                  cx={110}
                  cy={48}
                  r={7}
                />
              </g>
            ) : null}
          </g>
          <path className="superchat-muse-spark" fill={MUSE_FACE_SPARK} d={MUSE_SPARK_PATH} />
        </g>
        <g className="superchat-muse-thought" fill={color}>
          <circle className="superchat-muse-dot" cx={92} cy={22} r={5} />
          <circle className="superchat-muse-dot superchat-muse-dot-2" cx={104} cy={12} r={5} />
          <circle className="superchat-muse-dot superchat-muse-dot-3" cx={116} cy={2} r={5} />
        </g>
      </svg>
      {showBadge ? (
        showBadgeText ? (
          <span
            data-testid="muse-waiting-badge"
            className="absolute -top-1 -right-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] leading-none font-semibold text-destructive-foreground"
          >
            {badgeLabel}
          </span>
        ) : (
          <span
            data-testid="muse-waiting-dot"
            className="absolute top-0 right-0 size-2 rounded-full bg-destructive"
          />
        )
      ) : null}
    </div>
  );
}

const reducedMotionMedia = "(prefers-reduced-motion: reduce)";

function reducedMotionSnapshot(): boolean {
  return window.matchMedia(reducedMotionMedia).matches;
}

function subscribeToReducedMotion(onChange: () => void): () => void {
  const media = window.matchMedia(reducedMotionMedia);
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

export function GrokShapePreview({
  shapeIndex,
  color,
  selected,
  onClick,
}: {
  shapeIndex: number;
  color: string;
  selected?: boolean;
  onClick?: () => void;
}) {
  const key = SHIPPED_SHAPE_KEYS[shapeIndex % SHIPPED_SHAPE_KEYS.length] ?? "hex";
  const path = shippedBotAvatarShapePath(shapeIndex);
  const colorDef = resolvePersonaColorDef("preview", color);

  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={key}
      aria-pressed={selected ?? false}
      className={cn(
        "relative flex size-11 items-center justify-center rounded-xl transition-transform hover:scale-105 active:scale-95 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-popover",
        selected
          ? "ring-2 ring-primary ring-offset-2 ring-offset-popover bg-white/10"
          : "hover:bg-white/5",
      )}
    >
      <svg viewBox={VIEWBOX} className="size-8 overflow-visible" aria-hidden="true">
        <path d={path} fill={colorDef.light} />
        <g fill={colorDef.eyeColor}>
          <ellipse cx={CENTER - 29} cy={CENTER - 8} rx={10} ry={7} />
          <ellipse cx={CENTER + 29} cy={CENTER - 8} rx={10} ry={7} />
        </g>
      </svg>
    </button>
  );
}

export function Wordmark({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-3", className)}>
      <div className="flex h-11 w-11 items-center justify-center gap-1.5 rounded-full bg-card">
        <span className="h-4 w-[7px] rounded-full bg-primary" />
        <span className="h-4 w-[7px] rounded-full bg-primary" />
      </div>
      <span className="font-[Aeonik,ui-sans-serif] text-[28px] tracking-tight text-foreground">
        Superchat
      </span>
    </div>
  );
}
