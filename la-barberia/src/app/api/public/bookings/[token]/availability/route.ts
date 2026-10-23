import { availabilityForToken } from "@/lib/bookings";
import { Errors } from "@/lib/errors";
import { clientIpHash, route } from "@/lib/http";
import { isValidDate } from "@/lib/time";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ token: string }> };

export const GET = route<Ctx>(async (req, { params }) => {
  const { token } = await params;
  const from = req.nextUrl.searchParams.get("from") ?? "";
  const to = req.nextUrl.searchParams.get("to") ?? "";
  if (!isValidDate(from) || !isValidDate(to)) throw Errors.validation("Datas inválidas.");
  return { days: await availabilityForToken(token, { from, to }, { ipHash: clientIpHash(req) }) };
});
