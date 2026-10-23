import { z } from "zod";
import { getWorkingHours, replaceWorkingHours } from "@/lib/admin";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { workingHoursInput } from "@/lib/validation";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ id: string }> };

export const GET = route<Ctx>(async (req, { params }) => {
  await requireAdminApi(req);
  return { intervals: await getWorkingHours(db(), z.uuid().parse((await params).id)) };
});

export const PUT = route<Ctx>(async (req, { params }) => {
  await requireAdminApi(req);
  const id = z.uuid().parse((await params).id);
  await replaceWorkingHours(db(), id, await readJson(req, workingHoursInput));
  return { ok: true };
});
