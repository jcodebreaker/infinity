import * as z from "zod";
import { Id } from "../ids.js";

export const botId = z.object({ botId: Id });
