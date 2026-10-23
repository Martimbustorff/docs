import { NextResponse } from "next/server";
import { createStaff, listStaff } from "@/lib/admin";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { staffInput } from "@/lib/validation";

export const dynamic = "force-dynamic";

export const GET = route(async (req) => {
  await requireAdminApi(req);
  return { staff: await listStaff(db()) };
});

export const POST = route(async (req) => {
  await requireAdminApi(req);
  return NextResponse.json({ staff: await createStaff(db(), await readJson(req, staffInput)) }, { status: 201 });
});
