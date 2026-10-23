import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import type { NextRequest } from "next/server";
import { db } from "./db";
import { env } from "./env";
import { Errors } from "./errors";
import { SESSION_COOKIE, sessionAdmin, type AdminUser } from "./auth";

export async function currentAdmin(): Promise<AdminUser | null> {
  const jar = await cookies();
  return sessionAdmin(db(), jar.get(SESSION_COOKIE)?.value);
}

/** Para páginas do painel: redireciona para o login se não houver sessão válida. */
export async function requireAdminPage(): Promise<AdminUser> {
  const admin = await currentAdmin();
  if (!admin) redirect("/admin/login");
  return admin;
}

/** Para a API do painel: valida a sessão e, em pedidos que alteram dados, a origem (proteção CSRF). */
export async function requireAdminApi(req: NextRequest): Promise<AdminUser & { token: string }> {
  if (!["GET", "HEAD", "OPTIONS"].includes(req.method)) assertSameOrigin(req);
  const token = req.cookies.get(SESSION_COOKIE)?.value;
  const admin = await sessionAdmin(db(), token);
  if (!admin || !token) throw Errors.unauthorized();
  return { ...admin, token };
}

export function assertSameOrigin(req: NextRequest) {
  const origin = req.headers.get("origin");
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host");
  let ok = false;
  try {
    ok = !!origin && !!host && new URL(origin).host === host;
  } catch {
    ok = false;
  }
  if (!ok) throw Errors.forbidden("Origem do pedido inválida.");
}

export function sessionCookieOptions(expires: Date) {
  return {
    httpOnly: true,
    secure: env().IS_PROD,
    sameSite: "strict" as const,
    path: "/",
    expires,
  };
}

export async function requestHostIsHttps(): Promise<boolean> {
  const h = await headers();
  return (h.get("x-forwarded-proto") ?? "https") === "https";
}
