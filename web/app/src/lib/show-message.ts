import { createContext, useContext } from "react";

/** Brings one of the person's Conversation messages into view (by its item id) and flashes it
 * once; the shell provides it, so a surface beside the chat (a version menu) can point back at
 * the message it came from. `null` where there is no Conversation to scroll. */
const ShowMessageContext = createContext<((itemId: string) => void) | null>(null);

export const ShowMessageProvider = ShowMessageContext.Provider;
export const useShowMessage = () => useContext(ShowMessageContext);
