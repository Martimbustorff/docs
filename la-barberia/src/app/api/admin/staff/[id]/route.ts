import { z } from "zod";
import { updateStaff } from "@/lib/admin";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { staffInput } from "@/lib/validation";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ id: string }> };

export const PUT = route<Ctx>(async (req, { params }) => {
  await requireAdminApi(req);
  const id = z.uuid().parse((await params).id);
  return { staff: await updateStaff(db(), id, await readJson(req, staffInput)) };
});
