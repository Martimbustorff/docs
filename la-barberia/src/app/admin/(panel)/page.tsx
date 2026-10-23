import { db } from "@/lib/db";
import { listActiveServices, listActiveStaff } from "@/lib/data";
import { localDate, localDateTime } from "@/lib/time";
import Agenda from "./Agenda";

export const dynamic = "force-dynamic";

export default async function AgendaPage() {
  const [services, staff] = await Promise.all([listActiveServices(db()), listActiveStaff(db())]);
  return (
    <Agenda
      today={localDate(new Date())}
      nowLocal={localDateTime(new Date())}
      staff={staff.map((s) => ({ id: s.id, name: s.name }))}
      services={services.map((s) => ({ id: s.id, name: s.name, durationMin: s.durationMin }))}
    />
  );
}
