import "./_env";
import { closeDb, db } from "../src/lib/db";
import { runMaintenance } from "../src/lib/maintenance";

async function main() {
try {
  const r = await runMaintenance(db());
  console.log(
    `Limpeza concluída: ${r.entriesDeleted} registo(s) com mais de ${r.retentionMonths} meses apagados; ` +
      `${r.sessionsDeleted} sessão(ões) expirada(s); ${r.rateLimitsDeleted} contador(es) antigos.`,
  );
} finally {
  await closeDb();
}
}
main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
