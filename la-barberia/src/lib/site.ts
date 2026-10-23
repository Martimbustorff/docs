import { hhmm, weekdayName } from "./time";
import type { Settings, WorkingHoursRow } from "./types";

export const waLink = (s: Pick<Settings, "whatsappE164">) => `https://wa.me/${s.whatsappE164.replace(/\D/g, "")}`;
export const mapsLink = (s: Pick<Settings, "address">) =>
  `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(s.address)}`;
export const instagramLink = (s: Pick<Settings, "instagram">) => `https://www.instagram.com/${s.instagram}/`;

/** Formata +351912345678 como +351 912 345 678. */
export function prettyPhone(e164: string): string {
  const m = /^\+351(\d{3})(\d{3})(\d{3})$/.exec(e164);
  return m ? `+351 ${m[1]} ${m[2]} ${m[3]}` : e164;
}

/** Horário semanal público: junta os intervalos de todos os profissionais ativos, por dia. */
export function weeklySummary(rows: WorkingHoursRow[]): { day: string; text: string }[] {
  const out: { day: string; text: string }[] = [];
  for (let wd = 1; wd <= 7; wd++) {
    const iv = rows
      .filter((r) => r.weekday === wd)
      .map((r) => [hhmm(r.startTime), hhmm(r.endTime)] as [string, string])
      .sort((a, b) => a[0].localeCompare(b[0]));
    const merged: [string, string][] = [];
    for (const [s, e] of iv) {
      const last = merged.at(-1);
      if (last && s <= last[1]) last[1] = e > last[1] ? e : last[1];
      else merged.push([s, e]);
    }
    out.push({ day: weekdayName(wd), text: merged.length ? merged.map(([s, e]) => `${s}–${e}`).join("  ·  ") : "Fechado" });
  }
  return out;
}
