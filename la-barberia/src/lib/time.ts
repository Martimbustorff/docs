import { DateTime } from "luxon";

/** Fuso horário da barbearia. Todas as regras de calendário usam este fuso, incluindo a mudança de hora. */
export const TZ = "Europe/Lisbon";

export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export const LOCAL_DT_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

export function lisbon(d: Date | number | string): DateTime {
  const dt = d instanceof Date ? DateTime.fromJSDate(d) : typeof d === "number" ? DateTime.fromMillis(d) : DateTime.fromISO(d);
  return dt.setZone(TZ);
}

/** 'YYYY-MM-DD' do dia (em Lisboa) a que o instante pertence. */
export function localDate(d: Date): string {
  return lisbon(d).toFormat("yyyy-MM-dd");
}

export function localTime(d: Date): string {
  return lisbon(d).toFormat("HH:mm");
}

/** 'YYYY-MM-DDTHH:mm' em hora de Lisboa. */
export function localDateTime(d: Date): string {
  return lisbon(d).toFormat("yyyy-MM-dd'T'HH:mm");
}

/** Converte 'YYYY-MM-DDTHH:mm' (hora de Lisboa) em instante. Devolve null se a hora não existir (mudança de hora) ou for inválida. */
export function parseLocalDateTime(s: string): Date | null {
  if (!LOCAL_DT_RE.test(s)) return null;
  const dt = DateTime.fromFormat(s, "yyyy-MM-dd'T'HH:mm", { zone: TZ });
  if (!dt.isValid) return null;
  // Se a hora caiu numa lacuna da mudança de hora, o luxon desloca-a; rejeitamos para evitar surpresas.
  if (dt.toFormat("yyyy-MM-dd'T'HH:mm") !== s) return null;
  return dt.toJSDate();
}

export function startOfLocalDay(date: string): Date {
  return DateTime.fromISO(date, { zone: TZ }).startOf("day").toJSDate();
}

/** Início do dia seguinte (hora de Lisboa) — o fim exclusivo do dia. */
export function endOfLocalDay(date: string): Date {
  return DateTime.fromISO(date, { zone: TZ }).plus({ days: 1 }).startOf("day").toJSDate();
}

export function addDays(date: string, n: number): string {
  return DateTime.fromISO(date, { zone: TZ }).plus({ days: n }).toFormat("yyyy-MM-dd");
}

export function isoWeekday(date: string): number {
  return DateTime.fromISO(date, { zone: TZ }).weekday;
}

export function isValidDate(date: string): boolean {
  return DATE_RE.test(date) && DateTime.fromISO(date, { zone: TZ }).isValid;
}

/** Intervalo 'HH:mm:ss' do Postgres → 'HH:mm'. */
export function hhmm(t: string): string {
  return t.slice(0, 5);
}

export function formatEuro(cents: number): string {
  return new Intl.NumberFormat("pt-PT", { style: "currency", currency: "EUR" }).format(cents / 100);
}

const WEEKDAYS_PT = ["", "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"];
export function weekdayName(n: number): string {
  return WEEKDAYS_PT[n] ?? "";
}

/** Texto longo em português, ex.: 'sexta-feira, 23 de outubro de 2026, 09:30'. */
export function formatLongPt(d: Date): string {
  return lisbon(d).setLocale("pt-PT").toFormat("cccc, d 'de' LLLL 'de' yyyy, HH:mm");
}
