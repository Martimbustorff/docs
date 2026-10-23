import { DateTime } from "luxon";
import type { DB } from "./db";
import type { Settings } from "./types";
import { TZ, addDays, endOfLocalDay, hhmm, isoWeekday, localDate, localTime, startOfLocalDay } from "./time";

export interface Interval {
  start: Date;
  end: Date;
}

export interface DayInput {
  /** Dia local 'YYYY-MM-DD' (Europe/Lisbon). */
  date: string;
  durationMin: number;
  stepMin: number;
  /** Janelas de trabalho do dia em hora local; as pausas são os intervalos entre janelas. */
  hours: { start: string; end: string }[];
  busy: Interval[];
  now: Date;
  minNoticeMin: number;
  /** Último dia local marcável (inclusive). */
  maxDate: string;
}

/**
 * Calcula os inícios possíveis para um dia. Função pura: sem acesso à base de dados.
 * Os passos são contados em tempo decorrido, por isso dias com mudança de hora (23h/25h) ficam corretos.
 */
export function slotsForDay(i: DayInput): Date[] {
  if (i.date > i.maxDate) return [];
  const durMs = i.durationMin * 60_000;
  const stepMs = i.stepMin * 60_000;
  const earliest = i.now.getTime() + i.minNoticeMin * 60_000;
  const seen = new Set<number>();
  const out: Date[] = [];
  for (const h of i.hours) {
    const s = DateTime.fromISO(`${i.date}T${h.start}`, { zone: TZ }).toMillis();
    const e = DateTime.fromISO(`${i.date}T${h.end}`, { zone: TZ }).toMillis();
    for (let t = s; t + durMs <= e; t += stepMs) {
      if (t < earliest || seen.has(t)) continue;
      if (i.busy.some((b) => t < b.end.getTime() && t + durMs > b.start.getTime())) continue;
      seen.add(t);
      out.push(new Date(t));
    }
  }
  return out.sort((a, b) => a.getTime() - b.getTime());
}

export function lastBookableDate(settings: Pick<Settings, "horizonDays">, now: Date): string {
  return addDays(localDate(now), settings.horizonDays);
}

export function eachDate(from: string, to: string): string[] {
  const days: string[] = [];
  for (let d = from; d <= to && days.length < 400; d = addDays(d, 1)) days.push(d);
  return days;
}

/** Carrega horários e reservas e devolve, por profissional e por dia, os inícios disponíveis. */
export async function loadSlots(
  q: DB,
  p: {
    staffIds: string[];
    durationMin: number;
    from: string;
    to: string;
    settings: Pick<Settings, "slotStepMinutes" | "minNoticeMinutes" | "horizonDays">;
    now: Date;
    excludeEntryId?: string;
  },
): Promise<Map<string, Map<string, Date[]>>> {
  const result = new Map<string, Map<string, Date[]>>();
  if (p.staffIds.length === 0) return result;

  const hours = await q<{ staffId: string; weekday: number; startTime: string; endTime: string }[]>`
    select staff_id, weekday, start_time, end_time from working_hours where staff_id in ${q(p.staffIds)}`;
  const busyRows = await q<{ staffId: string; startsAt: Date; endsAt: Date }[]>`
    select staff_id, starts_at, ends_at from calendar_entries
    where staff_id in ${q(p.staffIds)} and status <> 'cancelled'
      and starts_at < ${endOfLocalDay(p.to)} and ends_at > ${startOfLocalDay(p.from)}
      ${p.excludeEntryId ? q`and id <> ${p.excludeEntryId}` : q``}`;

  const maxDate = lastBookableDate(p.settings, p.now);
  const days = eachDate(p.from, p.to);
  for (const staffId of p.staffIds) {
    const perDay = new Map<string, Date[]>();
    const busy = busyRows.filter((b) => b.staffId === staffId).map((b) => ({ start: b.startsAt, end: b.endsAt }));
    for (const date of days) {
      const wd = isoWeekday(date);
      const dayHours = hours
        .filter((h) => h.staffId === staffId && h.weekday === wd)
        .map((h) => ({ start: hhmm(h.startTime), end: hhmm(h.endTime) }));
      const slots = slotsForDay({
        date,
        durationMin: p.durationMin,
        stepMin: p.settings.slotStepMinutes,
        hours: dayHours,
        busy,
        now: p.now,
        minNoticeMin: p.settings.minNoticeMinutes,
        maxDate,
      });
      if (slots.length) perDay.set(date, slots);
    }
    result.set(staffId, perDay);
  }
  return result;
}

export interface PublicSlot {
  startsAt: string;
  label: string;
}

/** Une os horários de vários profissionais (para "sem preferência"), sem expor quem os faz. */
export function mergeSlots(perStaff: Map<string, Map<string, Date[]>>): Record<string, PublicSlot[]> {
  const byDate = new Map<string, Map<number, Date>>();
  for (const perDay of perStaff.values()) {
    for (const [date, slots] of perDay) {
      const m = byDate.get(date) ?? new Map<number, Date>();
      for (const s of slots) m.set(s.getTime(), s);
      byDate.set(date, m);
    }
  }
  const out: Record<string, PublicSlot[]> = {};
  for (const date of [...byDate.keys()].sort()) {
    out[date] = [...byDate.get(date)!.values()]
      .sort((a, b) => a.getTime() - b.getTime())
      .map((d) => ({ startsAt: d.toISOString(), label: localTime(d) }));
  }
  return out;
}
