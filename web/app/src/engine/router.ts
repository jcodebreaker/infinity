import { accountRouter } from "./account";
import { activityRouter } from "./activity";
import { approvalsRouter } from "./approvals";
import { appsRouter } from "./apps";
import { artifactsRouter } from "./artifacts";
import { automationsRouter } from "./automations";
import { briefRouter } from "./brief";
import { chatsRouter } from "./chats";
import { os } from "./context";
import { dailyNotesRouter } from "./daily-notes";
import { decksRouter } from "./decks";
import { filesRouter } from "./files";
import { hostRouter } from "./host";
import { knowledgeRouter } from "./knowledge";
import { liveRouter } from "./live";
import { memoryRouter } from "./memory";
import { engineModelsRouter } from "./models";
import { museRouter } from "./muse";
import { outboxRouter } from "./outbox";
import { rhythmRouter } from "./rhythm";
import { searchRouter } from "./search";
import { sheetsRouter } from "./sheets";
import { topicsRouter } from "./topics";
import { vaultRouter } from "./vault";

/** Every procedure the app calls, answered by the engine's REST API from this page. */
export const router = os.router({
  ...accountRouter,
  ...activityRouter,
  ...appsRouter,
  ...approvalsRouter,
  ...artifactsRouter,
  ...automationsRouter,
  ...briefRouter,
  ...chatsRouter,
  ...hostRouter,
  ...dailyNotesRouter,
  ...decksRouter,
  ...engineModelsRouter,
  ...filesRouter,
  ...knowledgeRouter,
  ...liveRouter,
  ...memoryRouter,
  ...museRouter,
  ...outboxRouter,
  ...rhythmRouter,
  ...searchRouter,
  ...sheetsRouter,
  ...topicsRouter,
  ...vaultRouter,
});
