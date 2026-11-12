import type { ContractRouterClient } from "@orpc/contract";
import { createRouterClient } from "@orpc/server";
import type { AppContract } from "@superchat/contracts";
import { router } from "../engine/router";

/**
 * The app's API. There is no server of ours behind it: the router runs in the page and each
 * procedure calls the engine's REST API (same origin) directly (web/app/src/engine).
 */
export const rpc: ContractRouterClient<AppContract> = createRouterClient(router);
