import { NextResponse } from "next/server";
import { SESSION_COOKIE, logout } from "@/lib/auth";
import { db } from "@/lib/db";
import { route } from "@/lib/http";
import { requireAdminApi, sessionCookieOptions } from "@/lib/session";

export const dynamic = "force-dynamic";

export const POST = route(async (req) => {
  const admin = await requireAdminApi(req);
  await logout(db(), admin.token);
  const res = NextResponse.json({ ok: true });
  res.cookies.set(SESSION_COOKIE, "", sessionCookieOptions(new Date(0)));
  return res;
});
