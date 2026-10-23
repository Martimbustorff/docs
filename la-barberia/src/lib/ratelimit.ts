import type { DB } from "./db";
import { Errors } from "./errors";

/**
 * Limitador de janela fixa guardado na base de dados (funciona em alojamento sem memória partilhada).
 * Lança 429 quando o limite é excedido.
 */
export async function rateLimit(q: DB, bucket: string, id: string, limit: number, windowSeconds: number): Promise<void> {
  const key = `${bucket}:${id}`;
  const [row] = await q<{ hits: number }[]>`
    insert into rate_limits (key, window_start, hits) values (${key}, now(), 1)
    on conflict (key) do update set
      hits = case when rate_limits.window_start < now() - make_interval(secs => ${windowSeconds}) then 1 else rate_limits.hits + 1 end,
      window_start = case when rate_limits.window_start < now() - make_interval(secs => ${windowSeconds}) then now() else rate_limits.window_start end
    returning hits`;
  if (row.hits > limit) throw Errors.rateLimited();
}
