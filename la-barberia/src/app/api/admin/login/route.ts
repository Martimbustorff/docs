import { NextResponse } from "next/server";
import { SESSION_COOKIE, login } from "@/lib/auth";
import { db } from "@/lib/db";
import { clientIpHash, readJson, route } from "@/lib/http";
import { assertSameOrigin, sessionCookieOptions } from "@/lib/session";
import { loginInput } from "@/lib/validation";

export const dynamic = "force-dynamic";

export const POST = route(async (req) => {
  assertSameOrigin(req);
  const { email, password } = await readJson(req, loginInput);
  const { token, expiresAt, admin } = await login(db(), email, password, clientIpHash(req));
  const res = NextResponse.json({ ok: true, email: admin.email });
  res.cookies.set(SESSION_COOKIE, token, sessionCookieOptions(expiresAt));
  return res;
});
