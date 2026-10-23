import postgres from "postgres";
import { migrate } from "../scripts/migrate-lib";

/** Recria o esquema numa base de dados de testes descartável e aplica as migrações. */
export default async function setup() {
  const url = process.env.TEST_DATABASE_URL ?? "postgres://postgres@localhost:54329/labarberia_test";
  const dbName = new URL(url).pathname.slice(1);
  if (!/test/i.test(dbName)) {
    throw new Error(`Recuso apagar a base de dados "${dbName}": o nome tem de conter "test".`);
  }
  const sql = postgres(url, { max: 1, onnotice: () => {} });
  await sql`drop schema public cascade`;
  await sql`create schema public`;
  await sql.end({ timeout: 5 });
  await migrate(url, () => {});
}
