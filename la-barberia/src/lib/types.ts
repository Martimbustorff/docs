export interface Settings {
  shopName: string;
  tagline: string;
  address: string;
  whatsappE164: string;
  instagram: string;
  legalName: string;
  legalNif: string;
  privacyEmail: string;
  contactConfirmed: boolean;
  demoMode: boolean;
  onlineBookingOpen: boolean;
  minNoticeMinutes: number;
  horizonDays: number;
  cancelDeadlineHours: number;
  slotStepMinutes: number;
  retentionMonths: number;
}

export interface ServiceRow {
  id: string;
  name: string;
  description: string;
  durationMin: number;
  active: boolean;
  sortOrder: number;
  isDemo: boolean;
}

export interface StaffRow {
  id: string;
  name: string;
  active: boolean;
  sortOrder: number;
}

export interface WorkingHoursRow {
  staffId: string;
  weekday: number;
  startTime: string;
  endTime: string;
}

export interface EntryRow {
  id: string;
  kind: "appointment" | "block";
  staffId: string;
  startsAt: Date;
  endsAt: Date;
  status: "confirmed" | "completed" | "no_show" | "cancelled";
  blockKind: "block" | "vacation" | "holiday" | null;
  blockGroup: string | null;
  reason: string | null;
  serviceId: string | null;
  serviceName: string | null;
  durationMin: number | null;
  customerName: string | null;
  customerPhone: string | null;
  notes: string | null;
  source: "online" | "phone" | "whatsapp" | "walk_in" | null;
  rescheduleCount: number;
  cancelledAt: Date | null;
  cancelledBy: "customer" | "admin" | null;
  createdAt: Date;
  updatedAt: Date;
}
