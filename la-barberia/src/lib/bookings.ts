import { DB, db, isOverlapError, lockStaff, pgConstraint, pgErrorCode, tx } from "./db";
import { Errors } from "./errors";
import { PublicSlot, loadSlots, mergeSlots } from "./availability";
import { getSettings, listActiveStaff } from "./data";
import { notifyOwner } from "./notify";
import { rateLimit } from "./ratelimit";
import { bookingTokenFromKey, sha256 } from "./tokens";
import { addDays, formatLongPt, localDate } from "./time";
import type { EntryRow, ServiceRow, Settings } from "./types";
import type { PublicBookingInput } from "./validation";

export const MAX_RESCHEDULES = 3;
export const MAX_ACTIVE_BOOKINGS_PER_PHONE = 3;
const MAX_RANGE_DAYS = 62;
const TOKEN_RE = /^[A-Za-z0-9_-]{20,64}$/;

type Now = { now?: Date };

/* ------------------------------------------------------------------ disponibilidade pública */

export async function publicAvailability(
  q: DB,
  p: { serviceId: string; staffId: string; from: string; to: string } & Now,
): Promise<{ days: Record<string, PublicSlot[]>; closed: boolean }> {
  const now = p.now ?? new Date();
  const settings = await getSettings(q);
  if (!settings.onlineBookingOpen) return { days: {}, closed: true };

  const [service] = await q<ServiceRow[]>`select id, duration_min from services where id = ${p.serviceId} and active`;
  if (!service) throw Errors.notFound("Serviço não encontrado.");

  const staff = await listActiveStaff(q);
  const staffIds = p.staffId === "any" ? staff.map((s) => s.id) : staff.filter((s) => s.id === p.staffId).map((s) => s.id);
  if (staffIds.length === 0) throw Errors.notFound("Profissional não encontrado.");

  const today = localDate(now);
  const from = p.from < today ? today : p.from;
  const maxTo = addDays(from, MAX_RANGE_DAYS - 1);
  const to = p.to > maxTo ? maxTo : p.to;
  if (to < from) return { days: {}, closed: false };

  const perStaff = await loadSlots(q, { staffIds, durationMin: service.durationMin, from, to, settings, now });
  return { days: mergeSlots(perStaff), closed: false };
}

/* ------------------------------------------------------------------ criação (cliente) */

async function findByIdempotencyKey(q: DB, key: string) {
  const [row] = await q<EntryRow[]>`select * from calendar_entries where idempotency_key = ${key}`;
  return row;
}

export interface CreatedBooking {
  entry: EntryRow;
  token: string;
  /** true se este pedido repetiu um pedido anterior já concluído. */
  replay: boolean;
}

export async function createOnlineBooking(input: PublicBookingInput, ctx: { ipHash: string } & Now): Promise<CreatedBooking> {
  const now = ctx.now ?? new Date();
  const q = db();

  if (input.website && input.website.trim() !== "") throw Errors.validation(); // honeypot
  await rateLimit(q, "book-ip", ctx.ipHash, 10, 3600);

  const token = bookingTokenFromKey(input.idempotencyKey);
  const startsAt = new Date(input.startsAt);

  // Repetição do mesmo pedido (clique duplo, falha de rede): devolve a marcação original.
  const existing = await findByIdempotencyKey(q, input.idempotencyKey);
  if (existing) return replayOrConflict(existing, input, token);

  const settings = await getSettings(q);
  if (!settings.onlineBookingOpen) throw Errors.closed();

  const [service] = await q<ServiceRow[]>`
    select id, name, duration_min from services where id = ${input.serviceId} and active`;
  if (!service) throw Errors.notFound("Serviço não encontrado.");

  const [{ count }] = await q<{ count: number }[]>`
    select count(*)::int as count from calendar_entries
    where kind = 'appointment' and customer_phone = ${input.phone} and status = 'confirmed' and ends_at > ${now}`;
  if (count >= MAX_ACTIVE_BOOKINGS_PER_PHONE) {
    throw Errors.conflict("Este número já tem várias marcações ativas. Cancele uma ou contacte-nos por WhatsApp.");
  }

  const candidates = await orderedCandidates(q, input.staffId, startsAt);
  if (candidates.length === 0) throw Errors.notFound("Profissional não encontrado.");

  for (const staffId of candidates) {
    try {
      const entry = await tx(async (t) => {
        await lockStaff(t, [staffId]);
        await assertSlotAvailable(t, { staffId, startsAt, durationMin: service.durationMin, settings, now });
        const [row] = await t<EntryRow[]>`
          insert into calendar_entries
            (kind, staff_id, starts_at, ends_at, status, service_id, service_name, duration_min,
             customer_name, customer_phone, source, token_hash, idempotency_key)
          values ('appointment', ${staffId}, ${startsAt}, ${new Date(startsAt.getTime() + service.durationMin * 60_000)}, 'confirmed',
                  ${service.id}, ${service.name}, ${service.durationMin},
                  ${input.name}, ${input.phone}, 'online', ${sha256(token)}, ${input.idempotencyKey})
          returning *`;
        return row;
      });
      // Aviso ao Carlos (WhatsApp). Depois de a marcação estar guardada; nunca falha o pedido.
      await notifyOwner("new", entry);
      return { entry, token, replay: false };
    } catch (e) {
      if (isOverlapError(e) || e instanceof SlotUnavailable) continue; // outro profissional, se "sem preferência"
      // Corrida entre pedidos repetidos: o token deriva da chave, por isso qualquer das duas unicidades pode disparar.
      if (
        pgErrorCode(e) === "23505" &&
        ["calendar_entries_idempotency_key_key", "calendar_entries_token_hash_key"].includes(pgConstraint(e) ?? "")
      ) {
        const again = await findByIdempotencyKey(q, input.idempotencyKey);
        if (again) return replayOrConflict(again, input, token);
      }
      throw e;
    }
  }
  // Pode ter perdido a corrida contra o seu próprio pedido repetido (clique duplo): nesse caso a marcação já existe.
  const again = await findByIdempotencyKey(q, input.idempotencyKey);
  if (again) return replayOrConflict(again, input, token);
  throw Errors.slotTaken();
}

function replayOrConflict(existing: EntryRow, input: PublicBookingInput, token: string): CreatedBooking {
  if (existing.serviceId !== input.serviceId || existing.startsAt.getTime() !== new Date(input.startsAt).getTime()) {
    throw Errors.conflict("Esta chave de pedido já foi usada para outra marcação. Recarregue a página e tente novamente.");
  }
  return { entry: existing, token, replay: true };
}

/** Para "sem preferência": profissionais com menos marcações nesse dia primeiro. */
async function orderedCandidates(q: DB, staffId: string, startsAt: Date): Promise<string[]> {
  const staff = await listActiveStaff(q);
  if (staffId !== "any") return staff.some((s) => s.id === staffId) ? [staffId] : [];
  if (staff.length <= 1) return staff.map((s) => s.id);
  const day = localDate(startsAt);
  const counts = await q<{ staffId: string; n: number }[]>`
    select staff_id, count(*)::int as n from calendar_entries
    where status <> 'cancelled' and staff_id in ${q(staff.map((s) => s.id))}
      and starts_at >= ${new Date(startsAt.getTime() - 36 * 3600_000)} and starts_at < ${new Date(startsAt.getTime() + 36 * 3600_000)}
      and to_char(starts_at at time zone 'Europe/Lisbon', 'YYYY-MM-DD') = ${day}
    group by staff_id`;
  const n = new Map(counts.map((c) => [c.staffId, c.n]));
  return [...staff].sort((a, b) => (n.get(a.id) ?? 0) - (n.get(b.id) ?? 0) || a.sortOrder - b.sortOrder).map((s) => s.id);
}

class SlotUnavailable extends Error {}

/** Confirma, no servidor, que o início pedido é uma vaga real (horário, pausas, antecedência, horizonte, reservas). */
async function assertSlotAvailable(
  q: DB,
  p: { staffId: string; startsAt: Date; durationMin: number; settings: Settings; now: Date; excludeEntryId?: string },
) {
  const date = localDate(p.startsAt);
  const map = await loadSlots(q, {
    staffIds: [p.staffId],
    durationMin: p.durationMin,
    from: date,
    to: date,
    settings: p.settings,
    now: p.now,
    excludeEntryId: p.excludeEntryId,
  });
  const ok = map.get(p.staffId)?.get(date)?.some((d) => d.getTime() === p.startsAt.getTime());
  if (!ok) throw new SlotUnavailable();
}

/* ------------------------------------------------------------------ link privado */

export interface PublicBookingView {
  status: EntryRow["status"];
  serviceName: string;
  durationMin: number;
  staffName: string;
  startsAt: string;
  endsAt: string;
  startLabel: string;
  customerName: string;
  canModify: boolean;
  /** Instante até ao qual é possível cancelar/remarcar online. */
  modifyUntil: string;
  rescheduleCount: number;
  maxReschedules: number;
  shop: { name: string; address: string; whatsappE164: string; cancelDeadlineHours: number };
}

type EntryWithStaff = EntryRow & { staffName: string };

async function loadByToken(q: DB, token: string, lock = false): Promise<EntryWithStaff> {
  if (!TOKEN_RE.test(token)) throw Errors.notFound("Marcação não encontrada.");
  const [row] = await q<EntryWithStaff[]>`
    select e.*, s.name as staff_name from calendar_entries e join staff s on s.id = e.staff_id
    where e.token_hash = ${sha256(token)} and e.kind = 'appointment'
    ${lock ? q`for update of e` : q``}`;
  if (!row) throw Errors.notFound("Marcação não encontrada.");
  return row;
}

function modifyUntil(entry: EntryRow, settings: Settings): Date {
  return new Date(entry.startsAt.getTime() - settings.cancelDeadlineHours * 3600_000);
}

function canModify(entry: EntryRow, settings: Settings, now: Date): boolean {
  return entry.status === "confirmed" && now.getTime() <= modifyUntil(entry, settings).getTime();
}

function toView(entry: EntryWithStaff, settings: Settings, now: Date): PublicBookingView {
  return {
    status: entry.status,
    serviceName: entry.serviceName ?? "",
    durationMin: entry.durationMin ?? 0,
    staffName: entry.staffName,
    startsAt: entry.startsAt.toISOString(),
    endsAt: entry.endsAt.toISOString(),
    startLabel: formatLongPt(entry.startsAt),
    customerName: entry.customerName ?? "",
    canModify: canModify(entry, settings, now),
    modifyUntil: modifyUntil(entry, settings).toISOString(),
    rescheduleCount: entry.rescheduleCount,
    maxReschedules: MAX_RESCHEDULES,
    shop: {
      name: settings.shopName,
      address: settings.address,
      whatsappE164: settings.whatsappE164,
      cancelDeadlineHours: settings.cancelDeadlineHours,
    },
  };
}

export async function getBookingView(token: string, ctx: { ipHash: string } & Now): Promise<PublicBookingView> {
  const q = db();
  await rateLimit(q, "token-ip", ctx.ipHash, 60, 600); // dificulta adivinhar tokens
  const entry = await loadByToken(q, token);
  return toView(entry, await getSettings(q), ctx.now ?? new Date());
}

export async function getBookingForCalendar(token: string, ctx: { ipHash: string } & Now) {
  const q = db();
  await rateLimit(q, "token-ip", ctx.ipHash, 60, 600);
  const entry = await loadByToken(q, token);
  return { entry, settings: await getSettings(q) };
}

export async function cancelByToken(token: string, ctx: { ipHash: string } & Now): Promise<PublicBookingView> {
  const now = ctx.now ?? new Date();
  const q = db();
  await rateLimit(q, "token-ip", ctx.ipHash, 60, 600);
  const settings = await getSettings(q);
  const result = await tx(async (t) => {
    const entry = await loadByToken(t, token, true);
    if (entry.status === "cancelled") return { view: toView(entry, settings, now), changed: null }; // idempotente
    if (entry.status !== "confirmed") throw Errors.conflict("Esta marcação já não pode ser cancelada.");
    if (!canModify(entry, settings, now)) {
      throw Errors.forbidden(
        `O prazo para cancelar online terminou (${settings.cancelDeadlineHours} h antes). Contacte-nos por WhatsApp.`,
      );
    }
    const [updated] = await t<EntryRow[]>`
      update calendar_entries set status = 'cancelled', cancelled_at = ${now}, cancelled_by = 'customer', updated_at = now()
      where id = ${entry.id} returning *`;
    return { view: toView({ ...updated, staffName: entry.staffName }, settings, now), changed: updated };
  });
  if (result.changed) await notifyOwner("cancelled", result.changed);
  return result.view;
}

export async function availabilityForToken(
  token: string,
  range: { from: string; to: string },
  ctx: { ipHash: string } & Now,
): Promise<Record<string, PublicSlot[]>> {
  const now = ctx.now ?? new Date();
  const q = db();
  await rateLimit(q, "token-ip", ctx.ipHash, 60, 600);
  const entry = await loadByToken(q, token);
  const settings = await getSettings(q);
  if (!canModify(entry, settings, now)) return {};
  const today = localDate(now);
  const from = range.from < today ? today : range.from;
  const to = range.to > addDays(from, MAX_RANGE_DAYS - 1) ? addDays(from, MAX_RANGE_DAYS - 1) : range.to;
  if (to < from) return {};
  const map = await loadSlots(q, {
    staffIds: [entry.staffId],
    durationMin: entry.durationMin ?? 30,
    from,
    to,
    settings,
    now,
    excludeEntryId: entry.id,
  });
  return mergeSlots(map);
}

/** Remarcação atómica: atualiza a mesma linha numa transação. Se o novo horário falhar, a reserva original mantém-se. */
export async function rescheduleByToken(token: string, startsAtIso: string, ctx: { ipHash: string } & Now): Promise<PublicBookingView> {
  const now = ctx.now ?? new Date();
  const q = db();
  await rateLimit(q, "token-ip", ctx.ipHash, 60, 600);
  const settings = await getSettings(q);
  const startsAt = new Date(startsAtIso);
  const staffId = (await loadByToken(q, token)).staffId;
  try {
    const result = await tx(async (t) => {
      await lockStaff(t, [staffId]);
      const entry = await loadByToken(t, token, true);
      if (entry.status !== "confirmed") throw Errors.conflict("Esta marcação já não pode ser remarcada.");
      if (!canModify(entry, settings, now)) {
        throw Errors.forbidden(
          `O prazo para remarcar online terminou (${settings.cancelDeadlineHours} h antes). Contacte-nos por WhatsApp.`,
        );
      }
      if (entry.rescheduleCount >= MAX_RESCHEDULES) {
        throw Errors.forbidden("Já atingiu o número máximo de remarcações online. Contacte-nos por WhatsApp.");
      }
      const duration = entry.durationMin ?? 30;
      await assertSlotAvailable(t, { staffId: entry.staffId, startsAt, durationMin: duration, settings, now, excludeEntryId: entry.id });
      const [updated] = await t<EntryRow[]>`
        update calendar_entries
        set starts_at = ${startsAt}, ends_at = ${new Date(startsAt.getTime() + duration * 60_000)},
            reschedule_count = reschedule_count + 1, updated_at = now()
        where id = ${entry.id} returning *`;
      return { view: toView({ ...updated, staffName: entry.staffName }, settings, now), updated, previousStartsAt: entry.startsAt };
    });
    await notifyOwner("rescheduled", { ...result.updated, previousStartsAt: result.previousStartsAt });
    return result.view;
  } catch (e) {
    if (e instanceof SlotUnavailable || isOverlapError(e)) throw Errors.slotTaken();
    throw e;
  }
}

export { SlotUnavailable };
