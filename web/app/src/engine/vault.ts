import {
  deleteOmnigentVaultEntry,
  getOmnigentVaultRequest,
  listOmnigentVault,
  type OmnigentVaultEntry,
  saveOmnigentVaultEntry,
} from "@superchat/adapters/omnigent/browser";
import { engineClient, NO_EMAIL } from "./client";
import { os } from "./context";

const iso = (seconds: number | null) =>
  seconds == null ? null : new Date(seconds * 1000).toISOString();

export function mapVaultEntry(entry: OmnigentVaultEntry) {
  return {
    id: entry.id,
    name: entry.name,
    site: entry.site,
    username: entry.username,
    createdAt: iso(entry.created_at) ?? new Date(0).toISOString(),
    lastUsedAt: iso(entry.last_used_at),
  };
}

/** `vault.*`: the person's saved logins. A value goes to the engine once and is never kept here. */
export const vaultRouter = {
  vault: {
    list: os.vault.list.handler(async () => ({
      entries: (await listOmnigentVault(engineClient(), NO_EMAIL)).map(mapVaultEntry),
    })),
    remove: os.vault.remove.handler(async ({ input }) => {
      await deleteOmnigentVaultEntry(engineClient(), NO_EMAIL, input.id);
      return { ok: true as const };
    }),
    request: os.vault.request.handler(async ({ input }) => {
      const request = await getOmnigentVaultRequest(engineClient(), NO_EMAIL, input.requestId);
      return {
        id: request.id,
        name: request.name,
        site: request.site,
        reason: request.reason,
        status: request.status,
      };
    }),
    save: os.vault.save.handler(async ({ input }) =>
      mapVaultEntry(
        await saveOmnigentVaultEntry(engineClient(), NO_EMAIL, {
          name: input.name,
          site: input.site,
          password: input.password,
          ...(input.username ? { username: input.username } : {}),
          ...(input.requestId ? { requestId: input.requestId } : {}),
        }),
      ),
    ),
  },
};
