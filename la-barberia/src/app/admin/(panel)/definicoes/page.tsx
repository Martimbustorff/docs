import type { Metadata } from "next";
import { db } from "@/lib/db";
import { getSettings } from "@/lib/data";
import SettingsForm from "./SettingsForm";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Definições" };

export default async function Page() {
  return <SettingsForm initial={await getSettings(db())} />;
}
