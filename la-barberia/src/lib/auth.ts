import type { DB } from "./db";
import { ApiError, Errors } from "./errors";
import { rateLimit } from "./ratelimit";
import { DUMMY_HASH, hashPassword, hmacShort, randomToken, sha256, verifyPassword } from "./tokens";

export const SESSION_COOKIE = "lb_admin";
export const SESSION_DAYS = 7;
const MAX_FAILS = 5;
const LOCK_MINUTES = 15;

export interface AdminUser {
  id: string;
  email: string;
}

const BAD_LOGIN = () => new ApiError(401, "BAD_LOGIN", "Email ou palavra-passe incorretos, ou conta temporariamente bloqueada.");

export async function login(q: DB, email: string, password: string, ipHash: string, now = new Date()) {
  await rateLimit(q, "login-ip", ipHash, 10, 900);
  await rateLimit(q, "login-email", hmacShort(email), 15, 900);

  const [admin] = await q<{ id: string; email: string; passwordHash: string; failedAttempts: number; lockedUntil: Date | null }[]>`
    select id, email, password_hash, failed_attempts, locked_until from admins where email = ${email}`;

  // Verifica sempre um hash (mesmo sem utilizador) para não revelar se o email existe pelo tempo de resposta.
  const ok = await verifyPassword(password, admin?.passwordHash ?? DUMMY_HASH);
  const locked = !!admin?.lockedUntil && admin.lockedUntil > now;
  if (!admin || locked || !ok) {
    if (admin && !locked) {
      const fails = admin.failedAttempts + 1;
      await q`update admins set failed_attempts = ${fails >= MAX_FAILS ? 0 : fails},
              locked_until = ${fails >= MAX_FAILS ? new Date(now.getTime() + LOCK_MINUTES * 60_000) : null}
              where id = ${admin.id}`;
    }
    throw BAD_LOGIN();
  }

  await q`update admins set failed_attempts = 0, locked_until = null where id = ${admin.id}`;
  const token = randomToken(32);
  const expiresAt = new Date(now.getTime() + SESSION_DAYS * 86_400_000);
  await q`insert into admin_sessions (token_hash, admin_id, expires_at) values (${sha256(token)}, ${admin.id}, ${expiresAt})`;
  await q`delete from admin_sessions where expires_at < ${now}`;
  return { token, expiresAt, admin: { id: admin.id, email: admin.email } as AdminUser };
}

export async function sessionAdmin(q: DB, token: string | undefined, now = new Date()): Promise<AdminUser | null> {
  if (!token || token.length < 20 || token.length > 100) return null;
  const [row] = await q<AdminUser[]>`
    select a.id, a.email from admin_sessions s join admins a on a.id = s.admin_id
    where s.token_hash = ${sha256(token)} and s.expires_at > ${now}`;
  return row ?? null;
}

export async function logout(q: DB, token: string | undefined) {
  if (token) await q`delete from admin_sessions where token_hash = ${sha256(token)}`;
}

export async function changePassword(q: DB, adminId: string, current: string, next: string, keepToken?: string) {
  const [a] = await q<{ passwordHash: string }[]>`select password_hash from admins where id = ${adminId}`;
  if (!a || !(await verifyPassword(current, a.passwordHash))) throw Errors.forbidden("A palavra-passe atual não está correta.");
  await q`update admins set password_hash = ${await hashPassword(next)} where id = ${adminId}`;
  // Termina as outras sessões (por exemplo, um telemóvel perdido).
  await q`delete from admin_sessions where admin_id = ${adminId} ${keepToken ? q`and token_hash <> ${sha256(keepToken)}` : q``}`;
}

export function assertStrongPassword(p: string) {
  if (p.length < 12) throw Errors.validation("A palavra-passe tem de ter pelo menos 12 caracteres.");
}
