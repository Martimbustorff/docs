import { NextResponse } from "next/server";
import { createBlocks } from "@/lib/admin";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { blockInput } from "@/lib/validation";

export const dynamic = "force-dynamic";

export const POST = route(async (req) => {
  await requireAdminApi(req);
  const input = await readJson(req, blockInput);
  return NextResponse.json(await createBlocks(db(), input), { status: 201 });
});
