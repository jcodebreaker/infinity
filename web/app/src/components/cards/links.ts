/** Card links come from model output: only http(s) is ever made clickable. */
export function safeHttpUrl(raw: string | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

const EMAIL = /^[^\s@<>"']+@[^\s@<>"']+\.[^\s@<>"']+$/;
const PHONE = /^\+?[0-9 ().-]{5,24}$/;

/** `mailto:` only for a plain address, never one carrying a query (cc/body injection). */
export function mailtoHref(email: string): string | null {
  return EMAIL.test(email) ? `mailto:${email}` : null;
}

export function telHref(phone: string): string | null {
  return PHONE.test(phone) ? `tel:${phone.replace(/[^0-9+]/g, "")}` : null;
}
