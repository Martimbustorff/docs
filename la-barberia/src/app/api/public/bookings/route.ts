import { createOnlineBooking, getBookingView } from "@/lib/bookings";
import { clientIpHash, readJson, route } from "@/lib/http";
import { publicBookingInput } from "@/lib/validation";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export const POST = route(async (req) => {
  const input = await readJson(req, publicBookingInput);
  const ipHash = clientIpHash(req);
  const created = await createOnlineBooking(input, { ipHash });
  const booking = await getBookingView(created.token, { ipHash });
  return NextResponse.json({ token: created.token, booking }, { status: created.replay ? 200 : 201 });
});
