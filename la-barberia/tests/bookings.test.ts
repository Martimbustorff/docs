import { afterAll, beforeEach, describe, expect, it } from "vitest";
import { closeDb, db } from "../src/lib/db";
import { ApiError } from "../src/lib/errors";
import {
  MAX_ACTIVE_BOOKINGS_PER_PHONE,
  availabilityForToken,
  cancelByToken,
  createOnlineBooking,
  getBookingView,
  publicAvailability,
  rescheduleByToken,
} from "../src/lib/bookings";
import { createAdminAppointment, createBlocks, deleteBlock, patchAppointment } from "../src/lib/admin";
import { NOW, WED, Seed, bookingInput, ip, resetDb, seed, uniqueKey } from "./helpers";

let s: Seed;
const book = (over: Partial<Record<string, unknown>> = {}, ipHash = ip()) =>
  createOnlineBooking(bookingInput(s, over), { ipHash, now: NOW });
const code = async (p: Promise<unknown>) => {
  try {
    await p;
    return "OK";
  } catch (e) {
    return e instanceof ApiError ? e.code : `ERR:${e instanceof Error ? e.message : e}`;
  }
};
const slotsOn = async (staffId = "any", serviceId = s.corte, date = WED) =>
  (await publicAvailability(db(), { serviceId, staffId, from: date, to: date, now: NOW })).days[date]?.map((x) => x.label) ?? [];

afterAll(closeDb);
beforeEach(async () => {
  await resetDb();
  s = await seed();
});

describe("criar e consultar marcações", () => {
  it("guarda a marcação na base de dados e o link privado mostra-a", async () => {
    const created = await book({ email: "joao@example.com" });
    expect(created.replay).toBe(false);
    const [row] = await db()`select status, source, customer_name, customer_email, price_cents, duration_min from calendar_entries`;
    expect(row).toMatchObject({ status: "confirmed", source: "online", customerName: "João Silva", priceCents: 1500, durationMin: 30 });

    const view = await getBookingView(created.token, { ipHash: ip(), now: NOW });
    expect(view).toMatchObject({ serviceName: "Corte", staffName: "Carlos", status: "confirmed", canModify: true, customerName: "João Silva" });
    expect(view.startLabel).toContain("10:30");
    expect(JSON.stringify(view)).not.toContain("912345678"); // o telemóvel não volta a ser exposto
  });

  it("o token guardado é um hash e o link não contém dados pessoais", async () => {
    const created = await book();
    expect(created.token).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const [row] = await db()`select token_hash from calendar_entries`;
    expect(Buffer.from(row.tokenHash).toString("base64url")).not.toBe(created.token);
    expect(created.token.toLowerCase()).not.toContain("joao");
  });

  it("a vaga desaparece da disponibilidade depois de reservada", async () => {
    expect(await slotsOn(s.carlos)).toContain("10:30");
    await book();
    const after = await slotsOn(s.carlos);
    expect(after).not.toContain("10:30");
    expect(after).not.toContain("10:15"); // 10:15 + 30 min sobrepunha-se
    expect(after).toContain("10:00");
    expect(after).toContain("11:00");
  });

  it("respeita pausa, horário e duração: rejeita horas fora das vagas", async () => {
    // 13:30 locais = pausa de almoço (12:30Z+... 13:30 WEST = 12:30Z)
    expect(await code(book({ startsAt: "2026-10-21T12:30:00.000Z" }))).toBe("SLOT_TAKEN");
    // 20:00 locais, depois do fecho
    expect(await code(book({ startsAt: "2026-10-21T19:00:00.000Z" }))).toBe("SLOT_TAKEN");
    // início 12:45 locais com serviço de 30 min ultrapassaria o fecho da manhã (13:00)
    expect(await code(book({ startsAt: "2026-10-21T11:45:00.000Z" }))).toBe("SLOT_TAKEN");
    // desalinhado do passo de 15 minutos
    expect(await code(book({ startsAt: "2026-10-21T09:37:00.000Z" }))).toBe("SLOT_TAKEN");
    // segunda-feira (dia de folga)
    expect(await code(book({ startsAt: "2026-10-26T09:30:00.000Z" }))).toBe("SLOT_TAKEN");
  });

  it("respeita a antecedência mínima e o horizonte", async () => {
    // agora = 11:00 locais; antecedência 120 min => 12:00 locais já não, 14:30 sim (mesmo dia)
    expect(await code(book({ startsAt: "2026-10-20T10:30:00.000Z" }))).toBe("SLOT_TAKEN"); // 11:30 locais
    expect(await code(book({ startsAt: "2026-10-20T13:30:00.000Z" }))).toBe("OK"); // 14:30 locais
    // 45 dias de horizonte
    expect(await code(book({ startsAt: "2027-01-12T10:00:00.000Z" }))).toBe("SLOT_TAKEN");
  });

  it("não expõe dados pessoais na disponibilidade pública", async () => {
    await book({ name: "Zé Segredo", phone: "+351933333333", email: "ze@example.com" });
    const res = JSON.stringify(await publicAvailability(db(), { serviceId: s.corte, staffId: "any", from: WED, to: "2026-10-31", now: NOW }));
    expect(res).not.toMatch(/Segredo|933333333|ze@example/);
    expect(res).not.toContain(s.carlos);
  });
});

describe("concorrência e repetições", () => {
  it("20 pedidos simultâneos para a mesma vaga: só um é aceite", async () => {
    const results = await Promise.all(
      Array.from({ length: 20 }, (_, i) => code(book({ name: `Cliente ${i}`, phone: `+3519100000${String(i).padStart(2, "0")}` }))),
    );
    expect(results.filter((r) => r === "OK")).toHaveLength(1);
    expect(results.filter((r) => r === "SLOT_TAKEN")).toHaveLength(19);
    const [{ n }] = await db()`select count(*)::int as n from calendar_entries where kind = 'appointment'`;
    expect(n).toBe(1);
  });

  it("pedidos simultâneos para vagas que se sobrepõem parcialmente: nunca há sobreposição", async () => {
    const starts = ["09:30", "09:45", "10:00", "10:15", "10:30", "10:45", "11:00"].map((t) => {
      const [h, m] = t.split(":").map(Number);
      return `2026-10-21T${String(h - 1).padStart(2, "0")}:${String(m).padStart(2, "0")}:00.000Z`;
    });
    await Promise.all(starts.map((startsAt, i) => code(book({ startsAt, phone: `+3519200000${i}0` }))));
    const rows = await db()`select starts_at, ends_at from calendar_entries order by starts_at`;
    for (let i = 1; i < rows.length; i++) expect(rows[i].startsAt.getTime()).toBeGreaterThanOrEqual(rows[i - 1].endsAt.getTime());
    expect(rows.length).toBeGreaterThan(0);
  });

  it("a mesma chave de idempotência devolve a mesma marcação e o mesmo link", async () => {
    const key = uniqueKey();
    const a = await book({ idempotencyKey: key });
    const b = await book({ idempotencyKey: key });
    expect(b.replay).toBe(true);
    expect(b.entry.id).toBe(a.entry.id);
    expect(b.token).toBe(a.token);
    const [{ n }] = await db()`select count(*)::int as n from calendar_entries`;
    expect(n).toBe(1);
  });

  it("cliques repetidos em simultâneo com a mesma chave criam uma única marcação", async () => {
    const key = uniqueKey();
    const rs = await Promise.all(Array.from({ length: 8 }, () => book({ idempotencyKey: key })));
    expect(new Set(rs.map((r) => r.entry.id)).size).toBe(1);
    const [{ n }] = await db()`select count(*)::int as n from calendar_entries`;
    expect(n).toBe(1);
  });

  it("reutilizar a chave para outra marcação é rejeitado", async () => {
    const key = uniqueKey();
    await book({ idempotencyKey: key });
    expect(await code(book({ idempotencyKey: key, startsAt: "2026-10-21T10:30:00.000Z" }))).toBe("CONFLICT");
  });

  it("'sem preferência' com dois profissionais distribui e esgota", async () => {
    await resetDb();
    s = await seed({ second: true });
    const r = await Promise.all([0, 1, 2].map((i) => code(book({ staffId: "any", phone: `+3519300000${i}0` }))));
    expect(r.filter((x) => x === "OK")).toHaveLength(2);
    expect(r.filter((x) => x === "SLOT_TAKEN")).toHaveLength(1);
    const rows = await db()`select distinct staff_id from calendar_entries`;
    expect(rows).toHaveLength(2);
    expect(await slotsOn("any")).not.toContain("10:30");
  });
});

describe("cancelar e remarcar pelo link privado", () => {
  it("cancelar liberta a vaga e é idempotente", async () => {
    const { token } = await book();
    expect(await slotsOn(s.carlos)).not.toContain("10:30");
    const v = await cancelByToken(token, { ipHash: ip(), now: NOW });
    expect(v.status).toBe("cancelled");
    expect(await slotsOn(s.carlos)).toContain("10:30");
    expect((await cancelByToken(token, { ipHash: ip(), now: NOW })).status).toBe("cancelled");
    // a vaga pode ser reservada por outra pessoa
    expect(await code(book({ phone: "+351966666666" }))).toBe("OK");
  });

  it("depois do prazo de cancelamento, o cliente é remetido para o contacto manual", async () => {
    const { token } = await book({ startsAt: "2026-10-20T13:30:00.000Z" }); // 14:30 locais, hoje
    // agora 11:00 -> faltam 3,5 h; prazo configurado = 4 h
    expect(await code(cancelByToken(token, { ipHash: ip(), now: NOW }))).toBe("FORBIDDEN");
    expect(await code(rescheduleByToken(token, "2026-10-21T09:30:00.000Z", { ipHash: ip(), now: NOW }))).toBe("FORBIDDEN");
    const view = await getBookingView(token, { ipHash: ip(), now: NOW });
    expect(view.canModify).toBe(false);
    expect(view.status).toBe("confirmed");
  });

  it("remarcar move a mesma marcação para uma vaga livre", async () => {
    const { token, entry } = await book();
    const v = await rescheduleByToken(token, "2026-10-22T13:30:00.000Z", { ipHash: ip(), now: NOW }); // quinta 14:30
    expect(v.rescheduleCount).toBe(1);
    expect(v.startLabel).toContain("14:30");
    const [{ n }] = await db()`select count(*)::int as n from calendar_entries`;
    expect(n).toBe(1);
    expect(await slotsOn(s.carlos)).toContain("10:30"); // a vaga original ficou livre
    expect(entry.id).toBeTruthy();
  });

  it("remarcar para uma vaga ocupada falha e a reserva original mantém-se", async () => {
    await book({ startsAt: "2026-10-22T09:30:00.000Z", phone: "+351977777777" }); // quinta 10:30 ocupada
    const mine = await book({ phone: "+351988888888" }); // quarta 10:30
    expect(await code(rescheduleByToken(mine.token, "2026-10-22T09:30:00.000Z", { ipHash: ip(), now: NOW }))).toBe("SLOT_TAKEN");
    const v = await getBookingView(mine.token, { ipHash: ip(), now: NOW });
    expect(v.status).toBe("confirmed");
    expect(v.startsAt).toBe("2026-10-21T09:30:00.000Z");
    expect(v.rescheduleCount).toBe(0);
  });

  it("remarcações simultâneas para a mesma vaga: só uma vence, a outra mantém a reserva", async () => {
    const a = await book({ phone: "+351911111111" });
    const b = await book({ startsAt: "2026-10-21T10:30:00.000Z", phone: "+351922222222" });
    const target = "2026-10-23T09:30:00.000Z";
    const r = await Promise.all([a, b].map((x) => code(rescheduleByToken(x.token, target, { ipHash: ip(), now: NOW }))));
    expect(r.sort()).toEqual(["OK", "SLOT_TAKEN"]);
    const [{ n }] = await db()`select count(*)::int as n from calendar_entries where status = 'confirmed'`;
    expect(n).toBe(2);
  });

  it("pode remarcar para uma vaga que se sobrepõe à própria marcação", async () => {
    const { token } = await book();
    const v = await rescheduleByToken(token, "2026-10-21T09:45:00.000Z", { ipHash: ip(), now: NOW }); // +15 min
    expect(v.startLabel).toContain("10:45");
  });

  it("a disponibilidade de remarcação inclui a vaga da própria marcação", async () => {
    const { token } = await book();
    const days = await availabilityForToken(token, { from: WED, to: WED }, { ipHash: ip(), now: NOW });
    expect(days[WED].map((x) => x.label)).toContain("10:30");
  });

  it("limita o número de remarcações online", async () => {
    const { token } = await book();
    const targets = ["2026-10-22T09:30:00.000Z", "2026-10-23T09:30:00.000Z", "2026-10-22T10:30:00.000Z"];
    for (const t of targets) await rescheduleByToken(token, t, { ipHash: ip(), now: NOW });
    expect(await code(rescheduleByToken(token, "2026-10-21T09:30:00.000Z", { ipHash: ip(), now: NOW }))).toBe("FORBIDDEN");
  });

  it("tokens inválidos ou inexistentes dão 404", async () => {
    expect(await code(getBookingView("curto", { ipHash: ip(), now: NOW }))).toBe("NOT_FOUND");
    expect(await code(getBookingView("a".repeat(43), { ipHash: ip(), now: NOW }))).toBe("NOT_FOUND");
  });
});

describe("pausas, bloqueios e marcações do Carlos", () => {
  it("um bloqueio remove vagas e impede reservas", async () => {
    await createBlocks(db(), { staffId: s.carlos, start: `${WED}T10:00`, end: `${WED}T12:00`, kind: "block", reason: "Médico" });
    const t = await slotsOn(s.carlos);
    expect(t).toContain("09:30");
    expect(t).not.toContain("10:00");
    expect(t).not.toContain("11:45");
    expect(t).toContain("12:00");
    expect(await code(book({ startsAt: "2026-10-21T09:30:00.000Z" }))).toBe("SLOT_TAKEN");
  });

  it("apagar o bloqueio devolve as vagas", async () => {
    const { group } = await createBlocks(db(), { staffId: "all", start: `${WED}T09:00`, end: `${WED}T20:00`, kind: "holiday", reason: "Feriado" });
    expect(await slotsOn(s.carlos)).toEqual([]);
    await deleteBlock(db(), group);
    expect((await slotsOn(s.carlos)).length).toBeGreaterThan(10);
  });

  it("não deixa criar um bloqueio por cima de uma marcação existente", async () => {
    await book();
    expect(await code(createBlocks(db(), { staffId: s.carlos, start: `${WED}T10:00`, end: `${WED}T12:00`, kind: "block" }))).toBe("CONFLICT");
    const [{ n }] = await db()`select count(*)::int as n from calendar_entries where kind = 'block'`;
    expect(n).toBe(0);
  });

  it("marcações criadas pelo Carlos usam o mesmo calendário e bloqueiam o cliente", async () => {
    const { entry, link } = await createAdminAppointment(db(), {
      serviceId: s.corte, staffId: s.carlos, start: `${WED}T10:30`, name: "Cliente Telefone", phone: "+351955555555", source: "phone",
    });
    expect(link).toMatch(/\/marcacao\/[A-Za-z0-9_-]{43}$/);
    expect(entry.source).toBe("phone");
    expect(await code(book())).toBe("SLOT_TAKEN");
    expect(await code(createAdminAppointment(db(), { serviceId: s.corte, staffId: s.carlos, start: `${WED}T10:45`, name: "Outro", phone: "+351955555556", source: "phone" }))).toBe("CONFLICT");
  });

  it("o Carlos pode concluir, marcar falta, cancelar e remarcar; a remarcação falhada mantém a original", async () => {
    const a = (await createAdminAppointment(db(), { serviceId: s.corte, staffId: s.carlos, start: `${WED}T10:30`, name: "A", phone: "+351911000001", source: "walk_in" })).entry;
    const b = (await createAdminAppointment(db(), { serviceId: s.corte, staffId: s.carlos, start: `${WED}T11:30`, name: "B", phone: "+351911000002", source: "walk_in" })).entry;
    expect(await code(patchAppointment(db(), b.id, { start: `${WED}T10:45` }))).toBe("CONFLICT");
    const [kept] = await db()`select starts_at from calendar_entries where id = ${b.id}`;
    expect(kept.startsAt).toEqual(b.startsAt);

    expect((await patchAppointment(db(), a.id, { status: "completed" })).status).toBe("completed");
    expect((await patchAppointment(db(), b.id, { status: "no_show" })).status).toBe("no_show");
    const moved = await patchAppointment(db(), a.id, { start: `${WED}T15:00`, status: "confirmed" });
    expect(moved.startLocal).toBe(`${WED}T15:00`);
    const cancelled = await patchAppointment(db(), a.id, { status: "cancelled" });
    expect(cancelled.status).toBe("cancelled");
    expect(cancelled.cancelledBy).toBe("admin");
  });
});

describe("proteção contra abuso", () => {
  it("limita o número de marcações ativas por telemóvel", async () => {
    for (let i = 0; i < MAX_ACTIVE_BOOKINGS_PER_PHONE; i++) {
      expect(await code(book({ startsAt: `2026-10-2${1 + i}T09:30:00.000Z` }))).toBe("OK");
    }
    expect(await code(book({ startsAt: "2026-10-24T09:30:00.000Z" }))).toBe("CONFLICT");
  });

  it("limita pedidos por IP (hash)", async () => {
    const same = "ip-abusivo";
    const rs: string[] = [];
    for (let i = 0; i < 12; i++) rs.push(await code(book({ startsAt: `2026-10-22T0${Math.min(9, 9)}:30:00.000Z`, phone: `+3519400000${String(i).padStart(2, "0")}` }, same)));
    expect(rs.filter((r) => r === "RATE_LIMITED").length).toBeGreaterThanOrEqual(2);
  });

  it("rejeita o campo isco preenchido (robôs)", async () => {
    expect(await code(book({ website: "http://spam.example" }))).toBe("VALIDATION");
    const [{ n }] = await db()`select count(*)::int as n from calendar_entries`;
    expect(n).toBe(0);
  });

  it("marcações online fechadas nas definições são recusadas", async () => {
    await db()`update settings set online_booking_open = false`;
    expect(await code(book())).toBe("BOOKING_CLOSED");
    const a = await publicAvailability(db(), { serviceId: s.corte, staffId: "any", from: WED, to: WED, now: NOW });
    expect(a.closed).toBe(true);
  });

  it("a base de dados rejeita sobreposições mesmo que a aplicação falhe (restrição de exclusão)", async () => {
    const q = db();
    await q`insert into calendar_entries (kind, staff_id, starts_at, ends_at, block_kind) values ('block', ${s.carlos}, '2026-10-21T09:00:00Z', '2026-10-21T10:00:00Z', 'block')`;
    await expect(
      q`insert into calendar_entries (kind, staff_id, starts_at, ends_at, block_kind) values ('block', ${s.carlos}, '2026-10-21T09:59:00Z', '2026-10-21T10:30:00Z', 'block')`,
    ).rejects.toMatchObject({ code: "23P01" });
    // encostado (fim == início) é permitido
    await q`insert into calendar_entries (kind, staff_id, starts_at, ends_at, block_kind) values ('block', ${s.carlos}, '2026-10-21T10:00:00Z', '2026-10-21T10:30:00Z', 'block')`;
  });
});
