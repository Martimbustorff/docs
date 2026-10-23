import { randomUUID } from "node:crypto";
import type { z } from "zod";
import { DB, isOverlapError, lockStaff, pgErrorCode, tx } from "./db";
import { env } from "./env";
import { Errors } from "./errors";
import { toCsv } from "./csv";
import { addDays, endOfLocalDay, hhmm, localDate, localDateTime, localTime, parseLocalDateTime, startOfLocalDay } from "./time";
import { randomToken, sha256 } from "./tokens";
import type { EntryRow, ServiceRow, Settings, StaffRow, WorkingHoursRow } from "./types";
import type {
  adminAppointmentInput,
  adminAppointmentPatch,
  blockInput,
  serviceInput,
  settingsInput,
  staffInput,
  workingHoursInput,
} from "./validation";

const MAX_ADMIN_RANGE_DAYS = 62;

export interface AdminEntry extends Omit<EntryRow, "startsAt" | "endsAt" | "createdAt" | "updatedAt" | "cancelledAt"> {
  staffName: string;
  startsAt: string;
  endsAt: string;
  /** Hora de Lisboa, 'YYYY-MM-DDTHH:mm' — o painel mostra sempre estes valores. */
  startLocal: string;
  endLocal: string;
}

function serialize(e: EntryRow & { staffName: string }): AdminEntry {
  // Nunca devolver o hash do token nem a chave de idempotência (o `select e.*` traz-os).
  const { createdAt: _c, updatedAt: _u, cancelledAt: _x, tokenHash: _t, idempotencyKey: _i, ...rest } = e as typeof e & { tokenHash?: unknown; idempotencyKey?: unknown };
  return { ...rest, startsAt: e.startsAt.toISOString(), endsAt: e.endsAt.toISOString(), startLocal: localDateTime(e.startsAt), endLocal: localDateTime(e.endsAt) };
}

export async function listEntries(q: DB, from: string, to: string): Promise<AdminEntry[]> {
  const toClamped = to > addDays(from, MAX_ADMIN_RANGE_DAYS - 1) ? addDays(from, MAX_ADMIN_RANGE_DAYS - 1) : to;
  const rows = await q<(EntryRow & { staffName: string })[]>`
    select e.*, s.name as staff_name from calendar_entries e join staff s on s.id = e.staff_id
    where e.starts_at < ${endOfLocalDay(toClamped)} and e.ends_at > ${startOfLocalDay(from)}
    order by e.starts_at, s.sort_order`;
  return rows.map(serialize);
}

function parseStart(s: string): Date {
  const d = parseLocalDateTime(s);
  if (!d) throw Errors.validation("Data/hora inválida (pode não existir na mudança de hora).");
  const y = 366 * 86_400_000;
  if (Math.abs(d.getTime() - Date.now()) > 2 * y) throw Errors.validation("Data fora do intervalo permitido.");
  return d;
}

async function describeConflicts(q: DB, staffIds: string[], start: Date, end: Date): Promise<string> {
  const rows = await q<{ startsAt: Date; customerName: string | null; reason: string | null; kind: string }[]>`
    select starts_at, customer_name, reason, kind from calendar_entries
    where staff_id in ${q(staffIds)} and status <> 'cancelled' and starts_at < ${end} and ends_at > ${start}
    order by starts_at limit 5`;
  if (!rows.length) return "";
  return rows.map((r) => `${localDate(r.startsAt)} ${localTime(r.startsAt)} ${r.kind === "block" ? `(bloqueio${r.reason ? ": " + r.reason : ""})` : r.customerName}`).join("; ");
}

/* ------------------------------------------------------------------ marcações */

export async function createAdminAppointment(q: DB, input: z.infer<typeof adminAppointmentInput>) {
  const start = parseStart(input.start);
  const [service] = await q<ServiceRow[]>`select id, name, duration_min from services where id = ${input.serviceId}`;
  if (!service) throw Errors.notFound("Serviço não encontrado.");
  const [staff] = await q<StaffRow[]>`select id from staff where id = ${input.staffId} and active`;
  if (!staff) throw Errors.notFound("Profissional não encontrado.");
  const end = new Date(start.getTime() + service.durationMin * 60_000);
  const token = randomToken(32);
  try {
    const entry = await tx(async (t) => {
      await lockStaff(t, [input.staffId]);
      const [row] = await t<EntryRow[]>`
        insert into calendar_entries
          (kind, staff_id, starts_at, ends_at, status, service_id, service_name, duration_min,
           customer_name, customer_phone, notes, source, token_hash)
        values ('appointment', ${input.staffId}, ${start}, ${end}, 'confirmed', ${service.id}, ${service.name},
                ${service.durationMin}, ${input.name}, ${input.phone}, ${input.notes ?? null}, ${input.source}, ${sha256(token)})
        returning *`;
      return row;
    });
    return { entry, link: `${env().PUBLIC_BASE_URL}/marcacao/${token}` };
  } catch (e) {
    if (isOverlapError(e)) {
      throw Errors.conflict(`Esse horário sobrepõe-se a outra marcação ou bloqueio: ${await describeConflicts(q, [input.staffId], start, end)}`);
    }
    throw e;
  }
}

export async function patchAppointment(q: DB, id: string, patch: z.infer<typeof adminAppointmentPatch>, now = new Date()) {
  const [current] = await q<{ staffId: string }[]>`select staff_id from calendar_entries where id = ${id} and kind = 'appointment'`;
  if (!current) throw Errors.notFound("Marcação não encontrada.");
  try {
    return await tx(async (t) => {
      await lockStaff(t, [current.staffId, patch.staffId ?? current.staffId]);
      const [row] = await t<(EntryRow & { staffName: string })[]>`
        select e.*, s.name as staff_name from calendar_entries e join staff s on s.id = e.staff_id
        where e.id = ${id} and e.kind = 'appointment' for update of e`;
      if (!row) throw Errors.notFound("Marcação não encontrada.");

      const status = patch.status ?? row.status;
      const staffId = patch.staffId ?? row.staffId;
      const start = patch.start ? parseStart(patch.start) : row.startsAt;
      const end = patch.start ? new Date(start.getTime() + (row.durationMin ?? 30) * 60_000) : row.endsAt;
      const cancelling = status === "cancelled" && row.status !== "cancelled";
      const reviving = status !== "cancelled" && row.status === "cancelled";
      const rescheduled = !!patch.start && start.getTime() !== row.startsAt.getTime();

      const [updated] = await t<EntryRow[]>`
        update calendar_entries set
          status = ${status}, staff_id = ${staffId}, starts_at = ${start}, ends_at = ${end},
          notes = ${patch.notes ?? row.notes},
          cancelled_at = ${cancelling ? now : reviving ? null : row.cancelledAt},
          cancelled_by = ${cancelling ? "admin" : reviving ? null : row.cancelledBy},
          reschedule_count = reschedule_count + ${rescheduled ? 1 : 0},
          updated_at = now()
        where id = ${id} returning *`;
      const [staff] = await t<{ name: string }[]>`select name from staff where id = ${staffId}`;
      return serialize({ ...updated, staffName: staff?.name ?? row.staffName });
    });
  } catch (e) {
    if (isOverlapError(e)) {
      throw Errors.conflict("Esse horário sobrepõe-se a outra marcação ou bloqueio. A marcação original foi mantida.");
    }
    if (pgErrorCode(e) === "23503") throw Errors.validation("Profissional inválido.");
    throw e;
  }
}

/* ------------------------------------------------------------------ bloqueios (férias, feriados, pausas pontuais) */

export async function createBlocks(q: DB, input: z.infer<typeof blockInput>) {
  const start = parseStart(input.start);
  const end = parseStart(input.end);
  if (end <= start) throw Errors.validation("O fim do bloqueio tem de ser posterior ao início.");
  const staffIds =
    input.staffId === "all" ? (await q<{ id: string }[]>`select id from staff where active`).map((s) => s.id) : [input.staffId];
  if (staffIds.length === 0) throw Errors.validation("Não há profissionais ativos.");
  const group = randomUUID();
  try {
    await tx(async (t) => {
      await lockStaff(t, staffIds);
      for (const staffId of staffIds) {
        await t`insert into calendar_entries (kind, staff_id, starts_at, ends_at, status, block_kind, block_group, reason)
                values ('block', ${staffId}, ${start}, ${end}, 'confirmed', ${input.kind}, ${group}, ${input.reason ?? null})`;
      }
    });
  } catch (e) {
    if (isOverlapError(e)) {
      throw Errors.conflict(
        `Já existem marcações ou bloqueios nesse período: ${await describeConflicts(q, staffIds, start, end)}. Cancele ou remarque-as primeiro.`,
      );
    }
    throw e;
  }
  return { group };
}

export async function deleteBlock(q: DB, group: string) {
  const rows = await q`delete from calendar_entries where block_group = ${group} and kind = 'block' returning id`;
  if (!rows.length) throw Errors.notFound("Bloqueio não encontrado.");
}

/* ------------------------------------------------------------------ serviços */

export async function listServices(q: DB): Promise<ServiceRow[]> {
  return q<ServiceRow[]>`select id, name, description, duration_min, active, sort_order, is_demo from services order by sort_order, name`;
}

export async function createService(q: DB, i: z.infer<typeof serviceInput>) {
  const [row] = await q<ServiceRow[]>`
    insert into services (name, description, duration_min, active, sort_order)
    values (${i.name}, ${i.description}, ${i.durationMin}, ${i.active}, ${i.sortOrder})
    returning id, name, description, duration_min, active, sort_order, is_demo`;
  return row;
}

export async function updateService(q: DB, id: string, i: z.infer<typeof serviceInput>) {
  // Ao editar um serviço de demonstração, passa a ser um dado real do Carlos.
  const [row] = await q<ServiceRow[]>`
    update services set name = ${i.name}, description = ${i.description},
      duration_min = ${i.durationMin}, active = ${i.active}, sort_order = ${i.sortOrder}, is_demo = false
    where id = ${id}
    returning id, name, description, duration_min, active, sort_order, is_demo`;
  if (!row) throw Errors.notFound();
  return row;
}

export async function deleteService(q: DB, id: string): Promise<{ deleted: boolean; deactivated: boolean }> {
  const [used] = await q<{ n: number }[]>`select count(*)::int as n from calendar_entries where service_id = ${id}`;
  if (used.n > 0) {
    await q`update services set active = false where id = ${id}`;
    return { deleted: false, deactivated: true };
  }
  const rows = await q`delete from services where id = ${id} returning id`;
  if (!rows.length) throw Errors.notFound();
  return { deleted: true, deactivated: false };
}

/* ------------------------------------------------------------------ profissionais e horários */

export async function listStaff(q: DB): Promise<StaffRow[]> {
  return q<StaffRow[]>`select id, name, active, sort_order from staff order by sort_order, name`;
}

export async function createStaff(q: DB, i: z.infer<typeof staffInput>) {
  const [row] = await q<StaffRow[]>`insert into staff (name, active, sort_order) values (${i.name}, ${i.active}, ${i.sortOrder}) returning id, name, active, sort_order`;
  return row;
}

export async function updateStaff(q: DB, id: string, i: z.infer<typeof staffInput>) {
  if (!i.active) {
    const [f] = await q<{ n: number }[]>`
      select count(*)::int as n from calendar_entries where staff_id = ${id} and status = 'confirmed' and kind = 'appointment' and ends_at > now()`;
    if (f.n > 0) throw Errors.conflict(`Este profissional tem ${f.n} marcação(ões) futura(s). Cancele ou remarque-as antes de o desativar.`);
  }
  const [row] = await q<StaffRow[]>`update staff set name = ${i.name}, active = ${i.active}, sort_order = ${i.sortOrder} where id = ${id} returning id, name, active, sort_order`;
  if (!row) throw Errors.notFound();
  return row;
}

export async function getWorkingHours(q: DB, staffId: string): Promise<{ weekday: number; start: string; end: string }[]> {
  const rows = await q<WorkingHoursRow[]>`select staff_id, weekday, start_time, end_time from working_hours where staff_id = ${staffId} order by weekday, start_time`;
  return rows.map((r) => ({ weekday: r.weekday, start: hhmm(r.startTime), end: hhmm(r.endTime) }));
}

export async function replaceWorkingHours(q: DB, staffId: string, i: z.infer<typeof workingHoursInput>) {
  const [s] = await q`select id from staff where id = ${staffId}`;
  if (!s) throw Errors.notFound("Profissional não encontrado.");
  try {
    await tx(async (t) => {
      await t`delete from working_hours where staff_id = ${staffId}`;
      for (const w of i.intervals) {
        await t`insert into working_hours (staff_id, weekday, start_time, end_time) values (${staffId}, ${w.weekday}, ${w.start}, ${w.end})`;
      }
    });
  } catch (e) {
    if (pgErrorCode(e) === "23P01") throw Errors.validation("Há intervalos sobrepostos no mesmo dia.");
    throw e;
  }
}

/* ------------------------------------------------------------------ definições */

export async function updateSettings(q: DB, i: z.infer<typeof settingsInput>): Promise<void> {
  await q`update settings set
    shop_name = ${i.shopName}, tagline = ${i.tagline}, address = ${i.address}, whatsapp_e164 = ${i.whatsappE164},
    instagram = ${i.instagram}, legal_name = ${i.legalName}, legal_nif = ${i.legalNif}, privacy_email = ${i.privacyEmail},
    contact_confirmed = ${i.contactConfirmed}, demo_mode = ${i.demoMode}, online_booking_open = ${i.onlineBookingOpen},
    min_notice_minutes = ${i.minNoticeMinutes}, horizon_days = ${i.horizonDays}, cancel_deadline_hours = ${i.cancelDeadlineHours},
    slot_step_minutes = ${i.slotStepMinutes}, retention_months = ${i.retentionMonths}, updated_at = now()
    where id`;
}

export type { Settings };

/* ------------------------------------------------------------------ exportação */

const STATUS_PT: Record<string, string> = { confirmed: "confirmada", completed: "concluída", no_show: "falta", cancelled: "cancelada" };
const SOURCE_PT: Record<string, string> = { online: "online", phone: "telefone", whatsapp: "WhatsApp", walk_in: "presencial" };

export async function exportCsv(q: DB, from: string, to: string): Promise<string> {
  const toClamped = to > addDays(from, 366) ? addDays(from, 366) : to;
  const rows = await q<(EntryRow & { staffName: string })[]>`
    select e.*, s.name as staff_name from calendar_entries e join staff s on s.id = e.staff_id
    where e.kind = 'appointment' and e.starts_at >= ${startOfLocalDay(from)} and e.starts_at < ${endOfLocalDay(toClamped)}
    order by e.starts_at`;
  return toCsv(
    ["data", "inicio", "fim", "profissional", "servico", "estado", "origem", "nome", "telemovel", "notas", "criada_em_utc"],
    rows.map((r) => [
      localDate(r.startsAt),
      localTime(r.startsAt),
      localTime(r.endsAt),
      r.staffName,
      r.serviceName,
      STATUS_PT[r.status] ?? r.status,
      SOURCE_PT[r.source ?? ""] ?? r.source,
      r.customerName,
      r.customerPhone,
      r.notes,
      r.createdAt.toISOString(),
    ]),
  );
}
