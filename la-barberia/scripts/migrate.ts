import "./_env";
import { migrate } from "./migrate-lib";

const url = process.env.DATABASE_URL;
if (!url) {
  console.error("Defina DATABASE_URL (ver .env.example).");
  process.exit(1);
}
migrate(url)
  .then((a) => console.log(a.length ? `Concluído: ${a.length} migração(ões).` : "Nada a fazer."))
  .catch((e) => {
    console.error("Falha na migração:", e instanceof Error ? e.message : e);
    process.exit(1);
  });
