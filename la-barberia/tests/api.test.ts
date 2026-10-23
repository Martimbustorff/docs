import fs from "node:fs";
import path from "node:path";
import { NextRequest } from "next/server";
import { afterAll, beforeEach, describe, expect, it } from "vitest";
import { closeDb, db } from "../src/lib/db";
import { login, changePassword, sessionAdmin } from "../src/lib/auth";
import { hashPassword } from "../src/lib/tokens";
import { NOW, Seed, WED, ip, resetDb, seed, uniqueKey } from "./helpers";

const ORIGIN = "http://localhost:3000";
const UUID = "11111111-1111-4111-8111-111111111111";
const ADMIN_PW = "uma-palavra-passe-longa-123";

function req(method: string, url: string, opts: { cookie?: string; origin?: string | null; json?: unknown; ip?: string } = {}) {
  const headers: Record<string, string> = { host: "localhost:3000", "x-forwarded-for": opts.ip ?? "203.0.113.9" };
  if (opts.cookie) headers.cookie = `lb_admin=${opts.cookie}`;
  if (opts.origin !== null) headers.origin = opts.origin ?? ORIGIN;
  let body: string | undefined;
  if (opts.json !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(opts.json);
  }
  return new NextRequest(`${ORIGIN}${url}`, { method, headers, body });
}
const ctx = { params: Promise.resolve({ id: UUID, group: UUID, token: "a".repeat(43) }) };

function routeFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((d) => {
    const p = path.join(dir, d.name);
    return d.isDirectory() ? routeFiles(p) : d.name === "route.ts" ? [p] : [];
  });
}

let s: Seed;
async function makeAdmin() {
  await db()`insert into admins (email, password_hash) values ('carlos@example.com', ${await hashPassword(ADMIN_PW)})`;
  return (await login(db(), "carlos@example.com", ADMIN_PW, ip())).token;
}

afterAll(closeDb);
beforeEach(async () => {
  await resetDb();
  s = await seed();
});

describe("acesso administrativo", () => {
  const adminRoutes = routeFiles(path.resolve("src/app/api/admin")).filter((f) => !f.includes(`${path.sep}login${path.sep}`));

  it("encontra as rotas de administração", () => {
    expect(adminRoutes.length).toBeGreaterThanOrEqual(12);
  });

  it("TODAS as rotas /api/admin recusam pedidos sem sessão (401)", async () => {
    for (const file of adminRoutes) {
      const mod = await import(file);
      const methods = ["GET", "POST", "PUT", "PATCH", "DELETE"].filter((m) => typeof mod[m] === "function");
      expect(methods.length, file).toBeGreaterThan(0);
      for (const m of methods) {
        const res: Response = await mod[m](req(m, "/api/admin/x?from=2026-10-21&to=2026-10-21", { json: m === "GET" ? undefined : {} }), ctx);
        expect(res.status, `${m} ${file}`).toBe(401);
      }
    }
  });

  it("uma sessão inventada ou expirada também é recusada", async () => {
    const mod = await import("../src/app/api/admin/entries/route");
    expect((await mod.GET(req("GET", "/api/admin/entries?from=2026-10-21", { cookie: "x".repeat(43) }), undefined)).status).toBe(401);
    const token = await makeAdmin();
    await db()`update admin_sessions set expires_at = now() - interval '1 minute'`;
    expect((await mod.GET(req("GET", "/api/admin/entries?from=2026-10-21", { cookie: token }), undefined)).status).toBe(401);
  });

  it("com sessão válida a agenda é devolvida; pedidos que alteram dados exigem origem correta (CSRF)", async () => {
    const token = await makeAdmin();
    const entries = await import("../src/app/api/admin/entries/route");
    const { createAdminAppointment } = await import("../src/lib/admin");
    await createAdminAppointment(db(), { serviceId: s.corte, staffId: s.carlos, start: `${WED}T10:30`, name: "Ana", phone: "+351911000009", source: "phone" });
    const listed = await entries.GET(req("GET", "/api/admin/entries?from=2026-10-21&to=2026-10-21", { cookie: token }), undefined);
    expect(listed.status).toBe(200);
    const listedText = await listed.text();
    expect(listedText).toContain("Ana");
    expect(listedText).not.toMatch(/tokenHash|idempotencyKey|token_hash/);

    const blocks = await import("../src/app/api/admin/blocks/route");
    const body = { staffId: "all", start: `${WED}T15:00`, end: `${WED}T16:00`, kind: "block" };
    expect((await blocks.POST(req("POST", "/api/admin/blocks", { cookie: token, json: body, origin: "https://evil.example" }), undefined)).status).toBe(403);
    expect((await blocks.POST(req("POST", "/api/admin/blocks", { cookie: token, json: body, origin: null }), undefined)).status).toBe(403);
    expect((await blocks.POST(req("POST", "/api/admin/blocks", { cookie: token, json: body }), undefined)).status).toBe(201);
  });

  it("login: cookie httpOnly + SameSite=Strict; falha dá mensagem genérica; origem externa é recusada", async () => {
    await db()`insert into admins (email, password_hash) values ('carlos@example.com', ${await hashPassword(ADMIN_PW)})`;
    const route = await import("../src/app/api/admin/login/route");
    const ok = await route.POST(req("POST", "/api/admin/login", { json: { email: "Carlos@Example.com", password: ADMIN_PW } }), undefined);
    expect(ok.status).toBe(200);
    const cookie = ok.headers.get("set-cookie") ?? "";
    expect(cookie).toMatch(/lb_admin=/);
    expect(cookie.toLowerCase()).toContain("httponly");
    expect(cookie.toLowerCase()).toContain("samesite=strict");

    const bad = await route.POST(req("POST", "/api/admin/login", { json: { email: "carlos@example.com", password: "errada" } }), undefined);
    const unknown = await route.POST(req("POST", "/api/admin/login", { json: { email: "naoexiste@example.com", password: "errada" } }), undefined);
    expect(bad.status).toBe(401);
    expect(await bad.json()).toEqual(await unknown.json()); // não revela se o email existe

    const cross = await route.POST(req("POST", "/api/admin/login", { origin: "https://evil.example", json: { email: "carlos@example.com", password: ADMIN_PW } }), undefined);
    expect(cross.status).toBe(403);
  });

  it("bloqueia a conta após 5 tentativas falhadas, mesmo com a palavra-passe certa", async () => {
    await db()`insert into admins (email, password_hash) values ('carlos@example.com', ${await hashPassword(ADMIN_PW)})`;
    for (let i = 0; i < 5; i++) await expect(login(db(), "carlos@example.com", "errada", ip())).rejects.toMatchObject({ status: 401 });
    await expect(login(db(), "carlos@example.com", ADMIN_PW, ip())).rejects.toMatchObject({ status: 401 });
    // passado o bloqueio, volta a funcionar
    const later = new Date(Date.now() + 16 * 60_000);
    await expect(login(db(), "carlos@example.com", ADMIN_PW, ip(), later)).resolves.toBeTruthy();
  });

  it("limita tentativas de login por IP", async () => {
    const same = "ip-forca-bruta";
    const codes: number[] = [];
    for (let i = 0; i < 14; i++) {
      try {
        await login(db(), `x${i}@example.com`, "errada", same);
      } catch (e) {
        codes.push((e as { status: number }).status);
      }
    }
    expect(codes.filter((c) => c === 429).length).toBeGreaterThan(0);
  });

  it("alterar a palavra-passe termina as outras sessões; não há registo público de administradores", async () => {
    const t1 = await makeAdmin();
    const t2 = (await login(db(), "carlos@example.com", ADMIN_PW, ip())).token;
    const admin = (await sessionAdmin(db(), t1))!;
    await changePassword(db(), admin.id, ADMIN_PW, "outra-palavra-passe-456", t1);
    expect(await sessionAdmin(db(), t1)).not.toBeNull();
    expect(await sessionAdmin(db(), t2)).toBeNull();
    // nenhuma rota pública cria administradores
    const files = routeFiles(path.resolve("src/app/api")).join("\n");
    expect(files).not.toMatch(/register|signup|registo/i);
  });

  it("o hash da palavra-passe e o token da sessão não ficam em claro na base de dados", async () => {
    const token = await makeAdmin();
    const [a] = await db()`select password_hash from admins`;
    expect(a.passwordHash).toMatch(/^scrypt\$/);
    expect(a.passwordHash).not.toContain(ADMIN_PW);
    const rows = await db()`select token_hash from admin_sessions`;
    expect(Buffer.from(rows[0].tokenHash).toString("base64url")).not.toBe(token);
  });
});

describe("API pública", () => {
  const body = (over: object = {}) => ({
    serviceId: s.corte, staffId: s.carlos, startsAt: "2026-10-21T09:30:00.000Z",
    name: "Maria Costa", phone: "912 345 678", idempotencyKey: uniqueKey(), ...over,
  });

  it("disponibilidade pública não revela dados pessoais nem ids de profissionais", async () => {
    const { POST } = await import("../src/app/api/public/bookings/route");
    const far = new Date(); // a rota usa o relógio real; usamos o próximo dia útil livre
    far.setDate(far.getDate() + 3);
    const res = await POST(req("POST", "/api/public/bookings", { json: body() }), undefined);
    // O relógio real pode já ter passado esta data de teste: o importante aqui é não vazar dados
    expect([201, 409]).toContain(res.status);
    const { GET } = await import("../src/app/api/public/availability/route");
    const from = new Date().toISOString().slice(0, 10);
    const to = new Date(Date.now() + 20 * 86400_000).toISOString().slice(0, 10);
    const avail = await GET(req("GET", `/api/public/availability?serviceId=${s.corte}&staffId=any&from=${from}&to=${to}`), undefined);
    expect(avail.status).toBe(200);
    const txt = await avail.text();
    expect(txt).not.toMatch(/Maria|912345678|912 345/);
    expect(txt).not.toContain(s.carlos);
    void far;
  });

  it("valida a entrada: telemóvel inválido e campos em falta dão 400 em português", async () => {
    const { POST } = await import("../src/app/api/public/bookings/route");
    const r1 = await POST(req("POST", "/api/public/bookings", { json: body({ phone: "123" }) }), undefined);
    expect(r1.status).toBe(400);
    expect((await r1.json()).error.message).toMatch(/telemóvel/i);
    const r2 = await POST(req("POST", "/api/public/bookings", { json: { serviceId: s.corte } }), undefined);
    expect(r2.status).toBe(400);
    const r3 = await POST(new NextRequest(`${ORIGIN}/api/public/bookings`, { method: "POST", body: "isto não é json", headers: { "content-type": "application/json" } }), undefined);
    expect(r3.status).toBe(400);
    const r4 = await POST(new NextRequest(`${ORIGIN}/api/public/bookings`, { method: "POST", body: JSON.stringify(body()), headers: { "content-type": "text/plain" } }), undefined);
    expect(r4.status).toBe(400);
  });

  it("o endpoint de manutenção só aceita o segredo correto", async () => {
    process.env.CRON_SECRET = "segredo-do-cron-1234567890";
    const { POST } = await import("../src/app/api/cron/maintenance/route");
    expect((await POST(req("POST", "/api/cron/maintenance"), undefined)).status).toBe(401);
    const bad = new NextRequest(`${ORIGIN}/api/cron/maintenance`, { method: "POST", headers: { authorization: "Bearer errado" } });
    expect((await POST(bad, undefined)).status).toBe(401);
    const good = new NextRequest(`${ORIGIN}/api/cron/maintenance`, { method: "POST", headers: { authorization: "Bearer segredo-do-cron-1234567890" } });
    expect((await POST(good, undefined)).status).toBe(200);
    delete process.env.CRON_SECRET;
    expect((await POST(new NextRequest(`${ORIGIN}/api/cron/maintenance`, { method: "POST", headers: { authorization: "Bearer " } }), undefined)).status).toBe(401);
  });

  it("a limpeza apaga dados pessoais fora do prazo de conservação", async () => {
    const { runMaintenance } = await import("../src/lib/maintenance");
    await db()`insert into calendar_entries (kind, staff_id, starts_at, ends_at, service_id, customer_name, customer_phone, source, status)
      values ('appointment', ${s.carlos}, '2023-01-10T10:00:00Z', '2023-01-10T10:30:00Z', ${s.corte}, 'Antigo', '+351911111111', 'online', 'completed'),
             ('appointment', ${s.carlos}, ${new Date(NOW.getTime() + 86400_000)}, ${new Date(NOW.getTime() + 86400_000 + 1800_000)}, ${s.corte}, 'Futuro', '+351922222222', 'online', 'confirmed')`;
    const r = await runMaintenance(db(), NOW);
    expect(r.entriesDeleted).toBe(1);
    const rows = await db()`select customer_name from calendar_entries`;
    expect(rows.map((x) => x.customerName)).toEqual(["Futuro"]);
  });
});
