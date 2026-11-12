// An assistant message that is really a raw engine or model failure (the model SDK hands
// these back as ordinary text). People never see the original: it renders as one calm
// note. The engine marks no such message, so detection is by pattern, and only for short
// texts: a failure notice is a line or two, never a long answer that mentions an error.

export const ENGINE_ERROR_NOTE = "Something went wrong on my side. Try again.";

const MAX_ERROR_LENGTH = 600;

const ENGINE_ERROR_PATTERNS: readonly RegExp[] = [
  /issue with the selected model/i,
  /may not exist or you may not have access/i,
  /\$\{[^}]*\}/,
  /\bHTTP\s*[45]\d\d\b/i,
  /\b(?:API|request|status)(?: error| code)?:?\s*[45]\d\d\b/i,
  /\b(?:invalid_request|overloaded|rate_limit|authentication|api)_error\b/i,
  /Traceback \(most recent call last\)/,
  /^\s*at \S+ \(.*:\d+:\d+\)/m,
  /^\s*(?:API Error|[A-Za-z]+(?:Error|Exception))\b\s*:/,
];

export function isEngineErrorText(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed || trimmed.length > MAX_ERROR_LENGTH || trimmed.includes("```")) return false;
  return ENGINE_ERROR_PATTERNS.some((pattern) => pattern.test(trimmed));
}

/**
 * What a person reads for an engine error code in the model layer, worded here (the engine
 * sends only the code). Shared so a failed turn's stored text can be told apart again:
 * `modelErrorCode` maps it back, which is how the Conversation offers the fix.
 */
export const MODEL_ERROR_COPY = {
  model_key_required: "Add your API key to continue.",
  model_not_supported: "This model isn't available — pick another.",
  model_budget_exhausted: "You've reached your monthly model budget.",
  model_provider_unreachable: "Couldn't reach the model provider. Try again.",
  account_suspended: "Your account is paused. Contact your admin.",
} as const;

export type ModelErrorCode = keyof typeof MODEL_ERROR_COPY;

/** The model-layer code a stored failure text came from, or `null` for any other text. */
export function modelErrorCode(text: string): ModelErrorCode | null {
  const trimmed = text.trim();
  for (const [code, copy] of Object.entries(MODEL_ERROR_COPY)) {
    if (copy === trimmed) return code as ModelErrorCode;
  }
  return null;
}
