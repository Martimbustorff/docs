import type { Metadata } from "next";
import { localDate } from "@/lib/time";
import Account from "./Account";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Conta e exportação" };

export default function Page() {
  return <Account today={localDate(new Date())} />;
}
