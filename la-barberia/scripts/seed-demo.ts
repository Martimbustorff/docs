import "./_env";
import postgres from "postgres";

/**
 * Dados DEMONSTRATIVOS (serviços, preços, durações e horários provisórios). Não são dados reais da barbearia.
 * Por segurança, recusa correr numa base de dados remota sem --allow-remote e numa base que já tenha profissionais sem --force.
 */
const url = process.env.DATABASE_URL;
if (!url) {
  console.error("Defina DATABASE_URL.");
  process.exit(1);
}
const args = new Set(process.argv.slice(2));
const host = new URL(url).hostname;
const isLocal = ["localhost", "127.0.0.1", "::1", ""].includes(host) || host.startsWith("/");
if (!isLocal && !args.has("--allow-remote")) {
  console.error(`A base de dados (${host}) não é local. Para inserir dados de demonstração mesmo assim, use --allow-remote.`);
  process.exit(1);
}

async function main() {
const sql = postgres(url!, { max: 1, onnotice: () => {} });
try {
  const [{ n }] = await sql<{ n: number }[]>`select count(*)::int as n from staff`;
  if (n > 0 && !args.has("--force")) {
    console.error("Já existem profissionais nesta base de dados; nada foi inserido. Use --force para inserir mesmo assim.");
    process.exit(1);
  }
  await sql.begin(async (t) => {
    const [carlos] = await t<{ id: string }[]>`insert into staff (name, sort_order) values ('Carlos', 0) returning id`;
    await t`insert into services (name, description, price_cents, duration_min, sort_order, is_demo) values
      ('Corte', 'Corte clássico ou moderno (valor provisório)', 1500, 30, 1, true),
      ('Barba', 'Barba e cuidados masculinos (valor provisório)', 1000, 20, 2, true),
      ('Corte + barba', 'Corte e barba (valor provisório)', 2200, 45, 3, true)`;
    // Horário provisório: terça a sexta 09:30-13:00 e 14:30-19:00; sábado 09:30-17:00.
    for (const wd of [2, 3, 4, 5]) {
      await t`insert into working_hours (staff_id, weekday, start_time, end_time) values
        (${carlos.id}, ${wd}, '09:30', '13:00'), (${carlos.id}, ${wd}, '14:30', '19:00')`;
    }
    await t`insert into working_hours (staff_id, weekday, start_time, end_time) values (${carlos.id}, 6, '09:30', '17:00')`;
    await t`update settings set demo_mode = true where id`;
  });
  console.log("Dados de demonstração inseridos (profissional Carlos, 3 serviços, horário provisório).");
} finally {
  await sql.end({ timeout: 5 });
}
}
main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
