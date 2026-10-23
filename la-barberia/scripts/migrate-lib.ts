import fs from "node:fs";
import path from "node:path";
import postgres from "postgres";

/** Aplica, por ordem, as migrações de db/migrations que ainda não foram aplicadas. Cada uma corre numa transação. */
export async function migrate(url: string, log: (msg: string) => void = console.log): Promise<string[]> {
  const dir = path.resolve(__dirname, "..", "db", "migrations");
  const files = fs.readdirSync(dir).filter((f) => /^\d+_.+\.sql$/.test(f)).sort();
  const sql = postgres(url, { max: 1, onnotice: () => {} });
  const applied: string[] = [];
  try {
    await sql`select pg_advisory_lock(727274)`; // evita duas migrações em simultâneo
    await sql`create table if not exists schema_migrations (name text primary key, applied_at timestamptz not null default now())`;
    const done = new Set((await sql<{ name: string }[]>`select name from schema_migrations`).map((r) => r.name));
    for (const f of files) {
      if (done.has(f)) continue;
      log(`A aplicar ${f} ...`);
      await sql.begin(async (t) => {
        await t.file(path.join(dir, f));
        await t`insert into schema_migrations (name) values (${f})`;
      });
      applied.push(f);
    }
    if (applied.length === 0) log("Base de dados já está atualizada.");
    await sql`select pg_advisory_unlock(727274)`;
  } finally {
    await sql.end({ timeout: 5 });
  }
  return applied;
}
