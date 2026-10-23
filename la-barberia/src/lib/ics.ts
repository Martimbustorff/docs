import type { EntryRow, Settings } from "./types";

function esc(s: string): string {
  return s.replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n");
}

function fold(line: string): string {
  // Linhas ICS não devem passar de 75 octetos; dobramos por caracteres de forma conservadora.
  const out: string[] = [];
  let cur = "";
  for (const ch of line) {
    if (Buffer.byteLength(cur + ch) > 73) {
      out.push(cur);
      cur = " " + ch;
    } else cur += ch;
  }
  out.push(cur);
  return out.join("\r\n");
}

function utc(d: Date): string {
  return d.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
}

export function buildIcs(entry: Pick<EntryRow, "id" | "startsAt" | "endsAt" | "serviceName">, settings: Pick<Settings, "shopName" | "address">, now = new Date()): string {
  const lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//La barberia//Marcacoes//PT",
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
    "BEGIN:VEVENT",
    `UID:${entry.id}@marcacoes.la-barberia`,
    `DTSTAMP:${utc(now)}`,
    `DTSTART:${utc(entry.startsAt)}`,
    `DTEND:${utc(entry.endsAt)}`,
    `SUMMARY:${esc(`${settings.shopName} – ${entry.serviceName ?? "Marcação"}`)}`,
    `LOCATION:${esc(settings.address)}`,
    `DESCRIPTION:${esc("Marcação na " + settings.shopName + ". Para alterar ou cancelar, use o link privado da marcação.")}`,
    "END:VEVENT",
    "END:VCALENDAR",
  ];
  return lines.map(fold).join("\r\n") + "\r\n";
}

export function googleCalendarUrl(entry: Pick<EntryRow, "startsAt" | "endsAt" | "serviceName">, settings: Pick<Settings, "shopName" | "address">): string {
  const p = new URLSearchParams({
    action: "TEMPLATE",
    text: `${settings.shopName} – ${entry.serviceName ?? "Marcação"}`,
    dates: `${utc(entry.startsAt)}/${utc(entry.endsAt)}`,
    location: settings.address,
  });
  return `https://calendar.google.com/calendar/render?${p.toString()}`;
}
