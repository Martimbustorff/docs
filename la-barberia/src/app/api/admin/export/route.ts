import { exportCsv } from "@/lib/admin";
import { db } from "@/lib/db";
import { Errors } from "@/lib/errors";
import { route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { isValidDate } from "@/lib/time";

export const dynamic = "force-dynamic";

export const GET = route(async (req) => {
  await requireAdminApi(req);
  const from = req.nextUrl.searchParams.get("from") ?? "";
  const to = req.nextUrl.searchParams.get("to") ?? "";
  if (!isValidDate(from) || !isValidDate(to) || to < from) throw Errors.validation("Datas inválidas.");
  return new Response(await exportCsv(db(), from, to), {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="marcacoes_${from}_${to}.csv"`,
    },
  });
});
