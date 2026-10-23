import { getBookingView } from "@/lib/bookings";
import { clientIpHash, route } from "@/lib/http";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ token: string }> };

export const GET = route<Ctx>(async (req, { params }) => {
  const { token } = await params;
  return { booking: await getBookingView(token, { ipHash: clientIpHash(req) }) };
});
