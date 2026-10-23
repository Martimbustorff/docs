import type { Metadata } from "next";
import { db } from "@/lib/db";
import { listServices } from "@/lib/admin";
import Services from "./Services";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Serviços" };

export default async function Page() {
  return <Services initial={await listServices(db())} />;
}
