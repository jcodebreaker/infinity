// The Project a session is working in (docs/adr/0008): the engine reports it on the session's
// Super Chat block (`superchat.project`, ADR 0009), name included, with the folder it works in.
import type { OmnigentClientConfig } from "./client/core.js";
import { omnigentHeaders, throwOnError } from "./client/core.js";
import { getOmnigentSession, setOmnigentSessionWorkspace } from "./client/sessions.js";

export interface WorkingProject {
  slug: string;
  name: string;
}

/** Where a session works: its Project, its folder (workspace-relative, `null` at the root), and
 * whether the person chose it. */
export interface WorkingPlace {
  project: WorkingProject | null;
  folder: string | null;
  chosen: boolean;
}

/** Session label: `"person"` while the person's own folder choice stands (the engine's
 * `superchat/projects/choice.py`); `""` clears it. */
export const WORKSPACE_CHOSEN_LABEL_KEY = "omnigent.superchat.workspace.chosen";
/** Session label: the folder the person just moved the chat to, told to the Muse once. */
export const WORKSPACE_MOVED_LABEL_KEY = "omnigent.superchat.workspace.moved";

/** The Project the session has open, or null. One session read. */
export async function getOmnigentWorkingProject(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<WorkingProject | null> {
  return (await getOmnigentWorkingPlace(config, email, sessionId)).project;
}

/** Where the session works. One session read. */
export async function getOmnigentWorkingPlace(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<WorkingPlace> {
  const block = (await getOmnigentSession(config, email, sessionId)).superchat;
  return {
    project: block?.project ?? null,
    folder: block?.folder ?? null,
    chosen: block?.workspace_chosen ?? false,
  };
}

/** The workspace root (absolute) of a session: its working directory minus the folder it is in. */
export async function getOmnigentWorkspaceRoot(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
): Promise<string | null> {
  const session = await getOmnigentSession(config, email, sessionId);
  const workspace =
    typeof session.workspace === "string" ? session.workspace.replace(/\/+$/, "") : "";
  if (!workspace) return null;
  const folder = session.superchat?.folder;
  return folder && workspace.endsWith(`/${folder}`)
    ? workspace.slice(0, workspace.length - folder.length - 1)
    : workspace;
}

/**
 * The person moves a session to `workspace` (absolute): the working directory changes, the
 * session is marked as the person's choice (`chosen`: false clears the mark, e.g. back to the
 * whole workspace) and the Muse is told once at its next turn.
 */
export async function moveOmnigentSessionForPerson(
  config: OmnigentClientConfig,
  email: string,
  sessionId: string,
  workspace: string,
  chosen: boolean,
): Promise<string> {
  const canonical = await setOmnigentSessionWorkspace(config, email, sessionId, workspace);
  const response = await fetch(
    new URL(`/v1/sessions/${encodeURIComponent(sessionId)}`, config.baseUrl),
    {
      method: "PATCH",
      headers: omnigentHeaders(config, email),
      body: JSON.stringify({
        labels: {
          [WORKSPACE_CHOSEN_LABEL_KEY]: chosen ? "person" : "",
          [WORKSPACE_MOVED_LABEL_KEY]: canonical,
        },
      }),
    },
  );
  await throwOnError(response, "mark the person's folder", config.secrets);
  return canonical;
}
