import type { DB } from "./db";
import { getSettings } from "./data";

/** Limpeza periódica: dados pessoais fora do prazo de conservação, sessões expiradas e contadores antigos. */
export async function runMaintenance(q: DB, now = new Date()) {
  const settings = await getSettings(q);
  const cutoff = new Date(now);
  cutoff.setMonth(cutoff.getMonth() - settings.retentionMonths);

  const purged = await q`
    delete from calendar_entries where ends_at < ${cutoff} returning id`;
  const sessions = await q`delete from admin_sessions where expires_at < ${now} returning 1`;
  const limits = await q`delete from rate_limits where window_start < ${new Date(now.getTime() - 86_400_000)} returning 1`;
  return {
    entriesDeleted: purged.length,
    sessionsDeleted: sessions.length,
    rateLimitsDeleted: limits.length,
    retentionMonths: settings.retentionMonths,
  };
}
