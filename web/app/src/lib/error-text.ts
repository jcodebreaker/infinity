/** The error's own message (the API already worded it for the person), or `fallback`. */
export function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
