import { listEntries } from "@/lib/admin";
import { db } from "@/lib/db";
import { Errors } from "@/lib/errors";
import { route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { isValidDate } from "@/lib/time";

export const dynamic = "force-dynamic";

export const GET = route(async (req) => {
  await requireAdminApi(req);
  const from = req.nextUrl.searchParams.get("from") ?? "";
  const to = req.nextUrl.searchParams.get("to") ?? from;
  if (!isValidDate(from) || !isValidDate(to) || to < from) throw Errors.validation("Datas inválidas.");
  return { entries: await listEntries(db(), from, to) };
});
