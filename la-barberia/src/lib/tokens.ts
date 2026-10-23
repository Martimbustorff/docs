import { createHash, createHmac, randomBytes, scrypt as scryptCb, timingSafeEqual } from "node:crypto";
import { promisify } from "node:util";
import { env } from "./env";

const scrypt = promisify(scryptCb) as (pw: string, salt: Buffer, keylen: number, opts: object) => Promise<Buffer>;

export function sha256(input: string): Buffer {
  return createHash("sha256").update(input).digest();
}

export function randomToken(bytes = 32): string {
  return randomBytes(bytes).toString("base64url");
}

/**
 * Token do link privado derivado da chave de idempotência do pedido.
 * Assim, se a resposta se perder e o cliente repetir o pedido com a mesma chave, recebe o mesmo link
 * (sem criar uma segunda marcação). Sem o APP_SECRET não é possível adivinhar o token.
 */
export function bookingTokenFromKey(idempotencyKey: string): string {
  return createHmac("sha256", env().APP_SECRET).update(`booking-token:${idempotencyKey}`).digest("base64url");
}

/** Hash (HMAC) usado para limites de pedidos; nunca guardamos IPs em claro. */
export function hmacShort(value: string): string {
  return createHmac("sha256", env().APP_SECRET).update(`rl:${value}`).digest("base64url").slice(0, 22);
}

const N = 16384;
const R = 8;
const P = 1;

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  const key = await scrypt(password, salt, 64, { N, r: R, p: P });
  return `scrypt$${N}$${R}$${P}$${salt.toString("base64")}$${key.toString("base64")}`;
}

export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const parts = stored.split("$");
  if (parts.length !== 6 || parts[0] !== "scrypt") return false;
  const [, n, r, p, saltB64, keyB64] = parts;
  const expected = Buffer.from(keyB64, "base64");
  const key = await scrypt(password, Buffer.from(saltB64, "base64"), expected.length, { N: Number(n), r: Number(r), p: Number(p) });
  return key.length === expected.length && timingSafeEqual(key, expected);
}

/** Hash falso para igualar o tempo de resposta quando o email não existe. */
export const DUMMY_HASH = "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==";
