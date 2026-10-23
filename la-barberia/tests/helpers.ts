import { db } from "../src/lib/db";

/** "Agora" fixo nos testes: terça-feira, 20/10/2026, 11:00 em Lisboa (WEST, UTC+1). */
export const NOW = new Date("2026-10-20T10:00:00Z");
/** Quarta-feira 21/10/2026 — dia de trabalho no horário de teste. */
export const WED = "2026-10-21";

let n = 0;
export const uniqueKey = () => `k${Date.now().toString(36)}${(n++).toString(36)}`.padEnd(24, "x");
export const ip = () => `ip-${n++}`;

export async function resetDb() {
  const q = db();
  await q`truncate calendar_entries, working_hours, services, staff, admin_sessions, admins, rate_limits restart identity cascade`;
  await q`delete from settings`;
  await q`insert into settings default values`;
  // Os testes de disponibilidade usam passos de 15 minutos; o valor por omissão do produto (30) tem teste próprio.
  await q`update settings set slot_step_minutes = 15`;
}

export interface Seed {
  carlos: string;
  ana?: string;
  corte: string;
  barba: string;
  combo: string;
}

/** Terça a sexta 09:30-13:00 e 14:30-19:00; sábado 09:30-17:00. Opcionalmente um segundo profissional. */
export async function seed(opts: { second?: boolean } = {}): Promise<Seed> {
  const q = db();
  const [carlos] = await q<{ id: string }[]>`insert into staff (name, sort_order) values ('Carlos', 0) returning id`;
  const out: Seed = { carlos: carlos.id } as Seed;
  const staffIds = [carlos.id];
  if (opts.second) {
    const [ana] = await q<{ id: string }[]>`insert into staff (name, sort_order) values ('Ana', 1) returning id`;
    out.ana = ana.id;
    staffIds.push(ana.id);
  }
  for (const s of staffIds) {
    for (const wd of [2, 3, 4, 5]) {
      await q`insert into working_hours (staff_id, weekday, start_time, end_time) values (${s}, ${wd}, '09:30', '13:00'), (${s}, ${wd}, '14:30', '19:00')`;
    }
    await q`insert into working_hours (staff_id, weekday, start_time, end_time) values (${s}, 6, '09:30', '17:00')`;
  }
  const svc = async (name: string, dur: number) =>
    (await q<{ id: string }[]>`insert into services (name, duration_min) values (${name}, ${dur}) returning id`)[0].id;
  out.corte = await svc("Corte", 30);
  out.barba = await svc("Barba", 20); // durações diferentes servem para testar o cálculo de vagas
  out.combo = await svc("Corte + barba", 45);
  return out;
}

export function bookingInput(s: Seed, over: Partial<Record<string, unknown>> = {}) {
  return {
    serviceId: s.corte,
    staffId: s.carlos,
    startsAt: "2026-10-21T09:30:00.000Z", // 10:30 em Lisboa
    name: "João Silva",
    phone: "+351912345678",
    idempotencyKey: uniqueKey(),
    ...over,
  } as import("../src/lib/validation").PublicBookingInput;
}
