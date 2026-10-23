import { route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";

export const dynamic = "force-dynamic";

export const GET = route(async (req) => {
  const a = await requireAdminApi(req);
  return { id: a.id, email: a.email };
});
