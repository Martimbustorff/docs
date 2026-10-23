import { changePassword } from "@/lib/auth";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { passwordChangeInput } from "@/lib/validation";

export const dynamic = "force-dynamic";

export const POST = route(async (req) => {
  const admin = await requireAdminApi(req);
  const { current, next } = await readJson(req, passwordChangeInput);
  await changePassword(db(), admin.id, current, next, admin.token);
  return { ok: true };
});
