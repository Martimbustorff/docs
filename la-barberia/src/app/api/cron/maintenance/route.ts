import { timingSafeEqual } from "node:crypto";
import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import { env } from "@/lib/env";
import { route } from "@/lib/http";
import { Errors } from "@/lib/errors";
import { runMaintenance } from "@/lib/maintenance";

export const dynamic = "force-dynamic";

function authorized(header: string | null): boolean {
  const secret = env().CRON_SECRET;
  if (!secret || !header) return false;
  const a = Buffer.from(header);
  const b = Buffer.from(`Bearer ${secret}`);
  return a.length === b.length && timingSafeEqual(a, b);
}

/** Chamada periódica (ex.: GitHub Actions ou cron-job.org) com "Authorization: Bearer <CRON_SECRET>". */
const handler = route(async (req) => {
  if (!authorized(req.headers.get("authorization"))) throw Errors.unauthorized();
  return NextResponse.json({ ok: true, ...(await runMaintenance(db())) });
});
export const GET = handler;
export const POST = handler;
