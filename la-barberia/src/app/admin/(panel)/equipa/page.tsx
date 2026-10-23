import type { Metadata } from "next";
import { db } from "@/lib/db";
import { getWorkingHours, listStaff } from "@/lib/admin";
import Team from "./Team";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Equipa e horários" };

export default async function Page() {
  const staff = await listStaff(db());
  const withHours = await Promise.all(staff.map(async (s) => ({ ...s, intervals: await getWorkingHours(db(), s.id) })));
  return <Team initial={withHours} />;
}
