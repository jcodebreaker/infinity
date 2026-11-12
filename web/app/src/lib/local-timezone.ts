/**
 * The browser's IANA timezone (e.g. "Australia/Sydney"), for timestamp inputs
 * that would otherwise default to UTC. Falls back to UTC when unavailable.
 */
export function localTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

/**
 * A person-readable label for an IANA zone: "Toronto (Eastern Time)". The city is the zone
 * id's last segment; the name comes from `Intl` ("longGeneric" is DST-neutral). Falls back to
 * the raw id when `Intl` can't resolve either part.
 */
export function friendlyTimezone(zone: string = localTimezone(), locale?: string): string {
  const city = (zone.split("/").pop() ?? zone).replace(/_/g, " ");
  let name = "";
  try {
    name =
      new Intl.DateTimeFormat(locale, { timeZone: zone, timeZoneName: "longGeneric" })
        .formatToParts(new Date())
        .find((part) => part.type === "timeZoneName")?.value ?? "";
  } catch {
    // An unknown zone or locale: the city alone still reads fine.
  }
  if (!name || name === city) return city;
  return `${city} (${name})`;
}

/** "8:00 AM" / "10:00 PM" for an "HH:mm" value: 12-hour clock, no leading zero, via `Intl`. */
export function formatClockTime(value: string, locale?: string): string {
  const [h, m] = value.split(":").map(Number);
  if (h === undefined || m === undefined || Number.isNaN(h) || Number.isNaN(m)) return value;
  return new Intl.DateTimeFormat(locale ?? "en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: "UTC",
  }).format(new Date(Date.UTC(2000, 0, 1, h, m)));
}
