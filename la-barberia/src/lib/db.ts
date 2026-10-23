import postgres from "postgres";
import { env } from "./env";

export type DB = postgres.Sql<{}>;

const g = globalThis as unknown as { __lbSql?: DB };

/** Ligação partilhada ao Postgres (colunas snake_case são devolvidas em camelCase). */
export function db(): DB {
  if (!g.__lbSql) {
    g.__lbSql = postgres(env().DATABASE_URL, {
      max: Number(process.env.DB_POOL_MAX) || 5,
      prepare: false, // compatível com poolers em modo transação (Supabase, Neon)
      transform: postgres.camel,
      idle_timeout: 20,
      connect_timeout: 15,
      onnotice: () => {},
    });
  }
  return g.__lbSql;
}

export async function closeDb() {
  if (g.__lbSql) {
    await g.__lbSql.end({ timeout: 5 });
    g.__lbSql = undefined;
  }
}

/** Executa fn numa transação; o tipo da transação é alinhado com DB para simplificar as funções. */
export function tx<T>(fn: (q: DB) => Promise<T>): Promise<T> {
  return db().begin((t) => fn(t as unknown as DB)) as Promise<T>;
}

export function pgErrorCode(e: unknown): string | undefined {
  return typeof e === "object" && e !== null && "code" in e ? String((e as { code: unknown }).code) : undefined;
}
export function pgConstraint(e: unknown): string | undefined {
  return typeof e === "object" && e !== null && "constraint_name" in e
    ? String((e as { constraint_name: unknown }).constraint_name)
    : undefined;
}

/**
 * Serializa escritas no calendário por profissional (lock consultivo até ao fim da transação).
 * A restrição de exclusão continua a ser a última barreira, mas sem este lock pedidos simultâneos
 * podem terminar em "deadlock detected". Vários ids são bloqueados por ordem para evitar ciclos.
 */
export async function lockStaff(q: DB, staffIds: string[]): Promise<void> {
  for (const id of [...new Set(staffIds)].sort()) {
    await q`select pg_advisory_xact_lock(hashtextextended(${id}::text, 727))`;
  }
}

/** Erros de concorrência que significam "esse horário foi tomado por outro pedido". */
export function isOverlapError(e: unknown): boolean {
  const c = pgErrorCode(e);
  return c === "23P01" || c === "40P01" || c === "40001";
}
