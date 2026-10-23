import { rescheduleByToken } from "@/lib/bookings";
import { clientIpHash, readJson, route } from "@/lib/http";
import { rescheduleInput } from "@/lib/validation";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ token: string }> };

export const POST = route<Ctx>(async (req, { params }) => {
  const { token } = await params;
  const { startsAt } = await readJson(req, rescheduleInput);
  return { booking: await rescheduleByToken(token, startsAt, { ipHash: clientIpHash(req) }) };
});
