import { z } from "zod";
import { patchAppointment } from "@/lib/admin";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { adminAppointmentPatch } from "@/lib/validation";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ id: string }> };

export const PATCH = route<Ctx>(async (req, { params }) => {
  await requireAdminApi(req);
  const id = z.uuid().parse((await params).id);
  const patch = await readJson(req, adminAppointmentPatch);
  return { entry: await patchAppointment(db(), id, patch) };
});
