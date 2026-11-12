import { base64ToBytes, bytesToBase64 } from "./base64.js";
import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";

export interface OmnigentHostState {
  /** The session's runner is connected (Files and tools can reach it). */
  ready: boolean;
  /** The managed launch's progress while it is in flight or failed; `null` once it is up. */
  launch: { stage: string; error: string | null } | null;
}

export function hostStatusUrl(config: OmnigentClientConfig, sessionId: string): URL {
  return new URL(`/v1/sessions/${encodeURIComponent(sessionId)}/host`, config.baseUrl);
}

/** `GET /v1/sessions/{id}/host` — whether the session's host is ready and how its launch
 * is going (engine/omnigent/superchat/host_status/routes.py). */
export async function getOmnigentHost(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<OmnigentHostState> {
  const response = await fetch(hostStatusUrl(config, sessionId), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "get host status", config.secrets);
  return (await response.json()) as OmnigentHostState;
}

// The Muse's host workspace on the Omnigent side: the session filesystem routes
// (`/v1/sessions/{id}/resources/environments/default/filesystem`) backed by the runner's os_env.
// Read-only listing and file reads, with every path confined to the workspace before it is sent.

export interface OmnigentFileEntry {
  name: string;
  /** Path relative to the workspace root. */
  path: string;
  type: "file" | "directory";
  /** Bytes, files only. */
  size: number | null;
  /** Unix epoch seconds. */
  modifiedAt: number;
}

export interface OmnigentFileContent {
  path: string;
  size: number;
  /** The engine cut the read short (its own cap). */
  truncated: boolean;
  bytes: Uint8Array;
}

/** Biggest file Superchat previews or saves. */
export const WORKSPACE_FILE_MAX_BYTES = 5 * 1024 * 1024;

export class WorkspacePathError extends Error {
  constructor(message = "Path is outside the workspace") {
    super(message);
    this.name = "WorkspacePathError";
  }
}

/** A workspace-relative path with no `.`/`..`/empty segments; throws for anything absolute or
 * escaping. `""` is the workspace root. */
export function confineWorkspacePath(path: string): string {
  if (path.includes("\0") || path.includes("\\")) throw new WorkspacePathError();
  if (path.startsWith("/") || /^[a-zA-Z]:/.test(path) || path.startsWith("~")) {
    throw new WorkspacePathError();
  }
  const parts: string[] = [];
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") throw new WorkspacePathError();
    parts.push(part);
  }
  return parts.join("/");
}

function filesystemUrl(config: OmnigentClientConfig, sessionId: string, path: string): URL {
  const segments = path ? `/${path.split("/").map(encodeURIComponent).join("/")}` : "";
  return new URL(
    `/v1/sessions/${encodeURIComponent(sessionId)}/resources/environments/default/filesystem${segments}`,
    config.baseUrl,
  );
}

interface RawEntry {
  name: string;
  path: string;
  type: string;
  bytes: number | null;
  modified_at: number;
}

/** One directory of the workspace, newest first. */
export async function listOmnigentFiles(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  path: string,
): Promise<OmnigentFileEntry[]> {
  const url = filesystemUrl(config, sessionId, confineWorkspacePath(path));
  url.searchParams.set("limit", "1000");
  const response = await fetch(url, { headers: omnigentHeaders(config, email) });
  await throwOnError(response, "list files", config.secrets);
  const body = (await response.json()) as { data?: RawEntry[] };
  return (body.data ?? [])
    .map((entry) => ({
      name: entry.name,
      path: entry.path,
      type: entry.type === "directory" ? ("directory" as const) : ("file" as const),
      size: entry.type === "directory" ? null : (entry.bytes ?? 0),
      modifiedAt: entry.modified_at,
    }))
    .sort((a, b) => b.modifiedAt - a.modifiedAt);
}

/** One workspace file's bytes (the engine sends text inline and anything else as base64). */
export async function readOmnigentFile(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  path: string,
): Promise<OmnigentFileContent> {
  const clean = confineWorkspacePath(path);
  if (!clean) throw new WorkspacePathError("Not a file");
  const response = await fetch(filesystemUrl(config, sessionId, clean), {
    headers: omnigentHeaders(config, email),
  });
  await throwOnError(response, "read file", config.secrets);
  const body = (await response.json()) as {
    path?: string;
    encoding?: string;
    content?: string;
    bytes?: number;
    truncated?: boolean;
    object?: string;
  };
  if (body.object !== "session.environment.filesystem.file_content") {
    throw new WorkspacePathError("Not a file");
  }
  const content = body.content ?? "";
  const bytes =
    body.encoding === "base64" ? base64ToBytes(content) : new TextEncoder().encode(content);
  return {
    path: body.path ?? clean,
    size: body.bytes ?? bytes.length,
    truncated: Boolean(body.truncated),
    bytes,
  };
}

/** `PUT .../filesystem/{path}` — writes bytes into the workspace (base64 on the wire, parent
 * folders created; `exclusive` fails instead of replacing). Wakes a sleeping host; a 503 means it could not start. */
export async function writeOmnigentFile(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  path: string,
  bytes: Uint8Array,
  options: { exclusive?: boolean } = {},
): Promise<{ path: string; bytesWritten: number }> {
  const clean = confineWorkspacePath(path);
  if (!clean) throw new WorkspacePathError("Not a file");
  const response = await fetch(filesystemUrl(config, sessionId, clean), {
    method: "PUT",
    headers: { ...omnigentHeaders(config, email), "content-type": "application/json" },
    body: JSON.stringify({
      content: bytesToBase64(bytes),
      encoding: "base64",
      create_parents: true,
      // An exclusive create fails (`already_exists`) rather than replace a file of that name.
      ...(options.exclusive ? { if_exists: "fail" } : {}),
    }),
  });
  await throwOnError(response, "write file", config.secrets);
  const body = (await response.json().catch(() => ({}))) as {
    path?: string;
    bytes_written?: number;
  };
  return { path: body.path ?? clean, bytesWritten: body.bytes_written ?? bytes.length };
}

const MIME_BY_EXTENSION: Record<string, string> = {
  html: "text/html",
  htm: "text/html",
  md: "text/markdown",
  markdown: "text/markdown",
  txt: "text/plain",
  csv: "text/csv",
  json: "application/json",
  pdf: "application/pdf",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  svg: "image/svg+xml",
};

/** Preview kind by extension: a known mime, `text/plain` for other text, else octet-stream. */
export function workspaceMimeType(name: string, isText: boolean): string {
  const extension = name.includes(".") ? (name.split(".").pop() ?? "").toLowerCase() : "";
  return MIME_BY_EXTENSION[extension] ?? (isText ? "text/plain" : "application/octet-stream");
}

/** A buffer that is not text: a NUL byte in the first 8 KiB, or invalid UTF-8. */
export function looksBinary(bytes: Uint8Array): boolean {
  const head = bytes.subarray(0, 8192);
  if (head.includes(0)) return true;
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return false;
  } catch {
    return true;
  }
}
