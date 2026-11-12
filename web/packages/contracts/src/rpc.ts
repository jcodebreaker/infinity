import { accountContract } from "./rpc/account.js";
import { activityContract } from "./rpc/activity.js";
import { approvalsContract } from "./rpc/approvals.js";
import { appsContract } from "./rpc/apps.js";
import { artifactsContract } from "./rpc/artifacts.js";
import { automationsContract } from "./rpc/automations.js";
import { botsContract } from "./rpc/bots.js";
import { briefContract } from "./rpc/brief.js";
import { chatsContract } from "./rpc/chats.js";
import { dailyNotesContract } from "./rpc/daily-notes.js";
import { decksContract } from "./rpc/decks.js";
import { filesContract } from "./rpc/files.js";
import { knowledgeContract } from "./rpc/knowledge.js";
import { liveContract } from "./rpc/live.js";
import { memoryContract } from "./rpc/memory.js";
import { engineModelsContract } from "./rpc/models.js";
import { museContract } from "./rpc/muse.js";
import { outboxContract } from "./rpc/outbox.js";
import { rhythmContract } from "./rpc/rhythm.js";
import { sheetsContract } from "./rpc/sheets.js";
import { topicsContract } from "./rpc/topics.js";
import { vaultContract } from "./rpc/vault.js";

export const appContract = {
  ...accountContract,
  ...activityContract,
  ...appsContract,
  ...approvalsContract,
  ...artifactsContract,
  ...automationsContract,
  ...botsContract,
  ...briefContract,
  ...chatsContract,
  ...filesContract,
  ...dailyNotesContract,
  ...decksContract,
  ...engineModelsContract,
  ...knowledgeContract,
  ...liveContract,
  ...memoryContract,
  ...museContract,
  ...outboxContract,
  ...rhythmContract,
  ...sheetsContract,
  ...topicsContract,
  ...vaultContract,
};

export type AppContract = typeof appContract;
