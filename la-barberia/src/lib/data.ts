import type { DB } from "./db";
import type { ServiceRow, Settings, StaffRow, WorkingHoursRow } from "./types";

export async function getSettings(q: DB): Promise<Settings> {
  const [s] = await q<Settings[]>`
    select shop_name, tagline, address, whatsapp_e164, instagram, legal_name, legal_nif, privacy_email,
           contact_confirmed, demo_mode, online_booking_open, min_notice_minutes, horizon_days,
           cancel_deadline_hours, slot_step_minutes, retention_months
    from settings where id`;
  return s;
}

export async function listActiveServices(q: DB): Promise<ServiceRow[]> {
  return q<ServiceRow[]>`select id, name, description, price_cents, duration_min, active, sort_order, is_demo
    from services where active order by sort_order, name`;
}

export async function listActiveStaff(q: DB): Promise<StaffRow[]> {
  return q<StaffRow[]>`select id, name, active, sort_order from staff where active order by sort_order, name`;
}

export async function listWorkingHours(q: DB, staffIds?: string[]): Promise<WorkingHoursRow[]> {
  return q<WorkingHoursRow[]>`
    select staff_id, weekday, start_time, end_time from working_hours
    ${staffIds ? q`where staff_id in ${q(staffIds)}` : q``}
    order by weekday, start_time`;
}
