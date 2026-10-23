import { NextResponse } from "next/server";
import { createService, listServices } from "@/lib/admin";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { serviceInput } from "@/lib/validation";

export const dynamic = "force-dynamic";

export const GET = route(async (req) => {
  await requireAdminApi(req);
  return { services: await listServices(db()) };
});

export const POST = route(async (req) => {
  await requireAdminApi(req);
  return NextResponse.json({ service: await createService(db(), await readJson(req, serviceInput)) }, { status: 201 });
});
