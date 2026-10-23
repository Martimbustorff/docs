import { z } from "zod";
import { deleteService, updateService } from "@/lib/admin";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { serviceInput } from "@/lib/validation";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ id: string }> };

export const PUT = route<Ctx>(async (req, { params }) => {
  await requireAdminApi(req);
  const id = z.uuid().parse((await params).id);
  return { service: await updateService(db(), id, await readJson(req, serviceInput)) };
});

export const DELETE = route<Ctx>(async (req, { params }) => {
  await requireAdminApi(req);
  return deleteService(db(), z.uuid().parse((await params).id));
});
