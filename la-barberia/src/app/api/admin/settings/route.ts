import { updateSettings } from "@/lib/admin";
import { db } from "@/lib/db";
import { getSettings } from "@/lib/data";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { settingsInput } from "@/lib/validation";

export const dynamic = "force-dynamic";

export const GET = route(async (req) => {
  await requireAdminApi(req);
  return { settings: await getSettings(db()) };
});

export const PUT = route(async (req) => {
  await requireAdminApi(req);
  await updateSettings(db(), await readJson(req, settingsInput));
  return { settings: await getSettings(db()) };
});
