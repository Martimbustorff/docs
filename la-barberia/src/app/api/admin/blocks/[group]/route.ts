import { z } from "zod";
import { deleteBlock } from "@/lib/admin";
import { db } from "@/lib/db";
import { route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ group: string }> };

export const DELETE = route<Ctx>(async (req, { params }) => {
  await requireAdminApi(req);
  await deleteBlock(db(), z.uuid().parse((await params).group));
  return { ok: true };
});
