// `files.*`: the host workspace browser. Browse the Muse's Conversation workspace on the
// engine (the engine session's filesystem routes), preview one file, save one to the Library, and
// take composer attachments into the workspace (`your_files/uploads/<date>/`) and read them.
import { ORPCError } from "@orpc/server";
import {
  bytesToBase64,
  confineWorkspacePath,
  createOmnigentArtifact,
  findOmnigentKnowledge,
  ingestOmnigentKnowledge,
  listOmnigentFiles,
  looksBinary,
  OmnigentApiError,
  type OmnigentFileEntry,
  readOmnigentFile,
  WORKSPACE_FILE_MAX_BYTES,
  WorkspacePathError,
  workspaceMimeType,
  writeOmnigentFile,
} from "@superchat/adapters/omnigent/browser";
import {
  AttachmentValidationError,
  attachmentExtensionForMimeType,
  decodeAttachmentBase64,
  isIngestableAttachmentMimeType,
  validateAttachmentMimeType,
  WORKSPACE_UPLOADS_PREFIX,
} from "@superchat/core";
import { toArtifact } from "./artifact-shape";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { os } from "./context";

/** The engine answered with this error code (`OmnigentApiError.code`, from the error body). */
function hasEngineCode(error: unknown, ...codes: string[]): boolean {
  return (
    error instanceof OmnigentApiError && error.code !== undefined && codes.includes(error.code)
  );
}

/** The host's runner is still (re)connecting; the panel shows "starting" and retries. */
function isHostStarting(error: unknown): boolean {
  return hasEngineCode(error, "runner_unavailable", "runner_capability_mismatch");
}

/** Confinement errors are the caller's mistake; engine "not found" stays a 404. */
function mapError(error: unknown): never {
  if (error instanceof WorkspacePathError) {
    throw new ORPCError("BAD_REQUEST", { message: error.message });
  }
  if (hasEngineCode(error, "not_found")) {
    throw new ORPCError("NOT_FOUND", { message: "File not found" });
  }
  if (isHostStarting(error)) {
    throw new ORPCError("SERVICE_UNAVAILABLE", { message: "Starting your machine" });
  }
  throw error;
}

/** A file name that is safe as one path segment: no separators or control characters, no leading
 * dots, bounded length; an extension is added from the mime type when the name has none. */
export function safeUploadName(name: string, mimeType: string): string {
  const base = (name.split(/[\\/]/).pop() ?? "")
    // biome-ignore lint/suspicious/noControlCharactersInRegex: stripping control characters is the point
    .replace(/[\u0000-\u001f\u007f]/g, "")
    // A screenshot's name has U+202F before "PM"; the model retypes it as a plain space.
    .replace(/\u202f/g, " ")
    .replace(/^\.+/, "")
    .trim();
  let result = base || "file";
  if (result.length > 120) {
    const dot = result.lastIndexOf(".");
    const ext = dot > 0 && result.length - dot <= 12 ? result.slice(dot) : "";
    result = result.slice(0, 120 - ext.length) + ext;
  }
  if (!/\.[A-Za-z0-9]{1,12}$/.test(result)) result += attachmentExtensionForMimeType(mimeType);
  return result;
}

async function listOrEmpty(sessionId: string, dir: string): Promise<OmnigentFileEntry[]> {
  // A folder that does not exist yet (404) simply has nothing in it.
  return listOmnigentFiles(engineClient(), NO_EMAIL, sessionId, dir).catch((error: unknown) => {
    if (hasEngineCode(error, "not_found")) return [];
    throw error;
  });
}

/** How many times an upload picks a name again when another upload took it first. */
const UPLOAD_NAME_ATTEMPTS = 5;

/** Whether `name` is `base` or one of its numbered copies (`base (2).ext`). */
export function isCopyOf(name: string, base: string): boolean {
  const stemAndExt = (value: string): [string, string] => {
    const dot = value.lastIndexOf(".");
    return dot > 0 ? [value.slice(0, dot), value.slice(dot).toLowerCase()] : [value, ""];
  };
  const [stem, ext] = stemAndExt(base);
  const [candidateStem, candidateExt] = stemAndExt(name);
  return ext === candidateExt && candidateStem.replace(/ \(\d+\)$/, "") === stem;
}

/** `name.ext` -> `name (2).ext`, `name (3).ext`, ... until it is not in `taken`. */
export function dedupeUploadName(name: string, taken: ReadonlySet<string>): string {
  if (!taken.has(name)) return name;
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  for (let n = 2; ; n += 1) {
    const candidate = `${stem} (${n})${ext}`;
    if (!taken.has(candidate)) return candidate;
  }
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** Writes a composer attachment into the Muse's workspace under
 * `your_files/uploads/<date>/` and returns where it landed. Wakes the host. */
export async function uploadAttachment(
  input: { name: string; mimeType: string; contentBase64: string },
  now: Date = new Date(),
): Promise<{ path: string; name: string; mimeType: string; size: number }> {
  let bytes: Uint8Array;
  try {
    validateAttachmentMimeType(input.mimeType);
    bytes = decodeAttachmentBase64(input.contentBase64);
  } catch (error) {
    if (error instanceof AttachmentValidationError) {
      throw new ORPCError("BAD_REQUEST", { message: error.message });
    }
    throw error;
  }
  const client = engineClient();
  const sessionId = await museSessionId();
  const dir = `${WORKSPACE_UPLOADS_PREFIX}${now.toISOString().slice(0, 10)}`;
  try {
    const chosen = safeUploadName(input.name, input.mimeType);
    // The same file attached again reuses the stored copy and its index entry. Only a file the
    // host indexes is looked up (an image never is), and only a copy that carries the name the
    // person chose is reused, so the name they see is the name they picked.
    if (isIngestableAttachmentMimeType(input.mimeType)) {
      const sha256 = await sha256Hex(bytes);
      const duplicate = await findOmnigentKnowledge(client, NO_EMAIL, sessionId, { sha256 });
      if (duplicate.found && duplicate.path && duplicate.name && isCopyOf(duplicate.name, chosen)) {
        return {
          path: duplicate.path,
          name: duplicate.name,
          mimeType: input.mimeType,
          size: bytes.byteLength,
        };
      }
    }
    // The name is picked from the folder and written with an exclusive create: two uploads that
    // chose the same free name cannot overwrite each other, the loser picks the next number.
    for (let attempt = 0; attempt < UPLOAD_NAME_ATTEMPTS; attempt += 1) {
      const existing = await listOrEmpty(sessionId, dir);
      const name = dedupeUploadName(chosen, new Set(existing.map((entry) => entry.name)));
      const path = `${dir}/${name}`;
      try {
        await writeOmnigentFile(client, NO_EMAIL, sessionId, path, bytes, { exclusive: true });
      } catch (error) {
        if (hasEngineCode(error, "already_exists")) continue;
        throw error;
      }
      return { path, name, mimeType: input.mimeType, size: bytes.byteLength };
    }
    throw new ORPCError("CONFLICT", { message: "Could not pick a free name for this file" });
  } catch (error) {
    if (isHostStarting(error)) {
      throw new ORPCError("SERVICE_UNAVAILABLE", {
        message: "Your machine is starting. Try again in a moment.",
      });
    }
    return mapError(error);
  }
}

/** Reads an uploaded attachment in the host and answers when it is searchable. One awaited
 * call: the composer keeps Send disabled until it returns. Wakes the host. */
export async function ingestAttachment(input: {
  path: string;
}): Promise<{ fileId: string; name: string; pages: number; chars: number }> {
  let path: string;
  try {
    path = confineWorkspacePath(input.path);
  } catch (error) {
    return mapError(error);
  }
  if (path !== input.path || !path.startsWith(WORKSPACE_UPLOADS_PREFIX)) {
    throw new ORPCError("BAD_REQUEST", { message: "Attachment is not an uploaded file" });
  }
  try {
    const read = await ingestOmnigentKnowledge(engineClient(), NO_EMAIL, await museSessionId(), {
      path,
    });
    return { fileId: read.file_id, name: read.name, pages: read.pages, chars: read.chars };
  } catch (error) {
    if (isHostStarting(error)) {
      throw new ORPCError("SERVICE_UNAVAILABLE", {
        message: "Your machine is starting. Try again in a moment.",
      });
    }
    // The host is awake but the read did not finish in time: its own message, never "starting".
    if (hasEngineCode(error, "knowledge_timeout")) {
      throw new ORPCError("GATEWAY_TIMEOUT", {
        message: "Reading this file is taking too long. Try again in a moment.",
      });
    }
    if (hasEngineCode(error, "invalid_input", "not_found")) {
      throw new ORPCError("BAD_REQUEST", { message: "Superchat couldn't read this file." });
    }
    return mapError(error);
  }
}

export const filesRouter = {
  files: {
    list: os.files.list.handler(async ({ input }) => {
      const entries = await listOmnigentFiles(
        engineClient(),
        NO_EMAIL,
        await museSessionId(),
        input.path,
      ).catch(mapError);
      return { entries };
    }),
    read: os.files.read.handler(async ({ input }) => {
      const file = await readOmnigentFile(
        engineClient(),
        NO_EMAIL,
        await museSessionId(),
        input.path,
      ).catch(mapError);
      const name = confineWorkspacePath(input.path).split("/").pop() ?? input.path;
      const binary = looksBinary(file.bytes);
      const mimeType = workspaceMimeType(name, !binary);
      const tooLarge = file.size > WORKSPACE_FILE_MAX_BYTES || file.truncated;
      const previewable = !binary || mimeType !== "application/octet-stream";
      return {
        path: file.path,
        name,
        mimeType,
        size: file.size,
        tooLarge,
        binary,
        contentBase64: tooLarge || !previewable ? null : bytesToBase64(file.bytes),
      };
    }),
    uploadAttachment: os.files.uploadAttachment.handler(({ input }) => uploadAttachment(input)),
    ingestAttachment: os.files.ingestAttachment.handler(({ input }) => ingestAttachment(input)),
    saveToLibrary: os.files.saveToLibrary.handler(async ({ input }) => {
      const sessionId = await museSessionId();
      const file = await readOmnigentFile(engineClient(), NO_EMAIL, sessionId, input.path).catch(
        mapError,
      );
      if (file.truncated || file.size > WORKSPACE_FILE_MAX_BYTES) {
        throw new ORPCError("BAD_REQUEST", { message: "File is too large to save" });
      }
      const name = confineWorkspacePath(input.path).split("/").pop() ?? input.path;
      const item = await createOmnigentArtifact(engineClient(), NO_EMAIL, {
        parentSessionId: sessionId,
        name,
        bytes: file.bytes,
      }).catch((error: unknown) => {
        if (error instanceof Error && /\(400\)|\(422\)/.test(error.message)) {
          throw new ORPCError("BAD_REQUEST", {
            message: "This file type can't be saved to the Library",
          });
        }
        throw error;
      });
      return toArtifact(item, sessionId);
    }),
  },
};
