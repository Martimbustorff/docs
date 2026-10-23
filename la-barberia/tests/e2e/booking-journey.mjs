// Verificação ponta a ponta num navegador real (usada no desenvolvimento). Não faz parte de `npm test`.
// Requer: npm i --no-save playwright-core axe-core, um Chromium (CHROME_PATH), servidor em BASE e acesso psql à BD de desenvolvimento.
// Os caminhos/credenciais abaixo são os do ambiente de desenvolvimento: adapte antes de usar.
import { chromium } from "playwright-core";
import { execSync } from "node:child_process";
import fs from "node:fs";

const BASE = "http://localhost:3000";
const OUT = new URL("./shots/", import.meta.url).pathname;
fs.mkdirSync(OUT, { recursive: true });
const exe = execSync("find /opt/pw-browsers -maxdepth 4 -name chrome -type f | head -1").toString().trim();
const axeSrc = fs.readFileSync(new URL("./node_modules/axe-core/axe.min.js", import.meta.url), "utf8");
const psql = (q) => execSync(`psql -h /tmp/pgtest -p 54329 -U postgres -d labarberia_dev -At -c "${q}"`).toString().trim();

import http from "node:http";
const hooks = [];
const hookServer = http.createServer((req, res) => { let b = ""; req.on("data", (c) => (b += c)); req.on("end", () => { hooks.push(JSON.parse(b)); res.end("ok"); }); }).listen(9911);

const results = [];
const check = (name, ok, extra = "") => { results.push({ name, ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name} ${extra}`); };

const browser = await chromium.launch({ executablePath: exe, args: ["--no-sandbox"] });
const consoleErrors = [];

async function audit(page, label) {
  await page.addScriptTag({ content: axeSrc });
  const r = await page.evaluate(async () => await axe.run(document, { runOnly: ["wcag2a", "wcag2aa", "wcag21aa"] }));
  const v = r.violations.map((x) => `${x.id}(${x.nodes.length}): ${x.nodes[0]?.html?.slice(0, 100)}`);
  check(`acessibilidade (axe) ${label}`, r.violations.length === 0, v.join(" | "));
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  check(`sem scroll horizontal ${label}`, !overflow);
}

// ---------- Cliente (telemóvel)
const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, locale: "pt-PT", timezoneId: "America/New_York" /* fuso do dispositivo diferente do de Lisboa */ });
const page = await phone.newPage();
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => consoleErrors.push(String(e)));

await page.goto(BASE + "/");
check("home mostra 'Marcar agora'", await page.getByRole("link", { name: "Marcar agora" }).isVisible());
check("home mostra aviso de demonstração", await page.getByText("Versão de demonstração").isVisible());
check("home NÃO mostra preços", !/€|\d+,\d\d\s*€/.test(await page.content()));
await page.screenshot({ path: OUT + "01-home.png", fullPage: true });
await audit(page, "home");

await page.getByRole("link", { name: "Marcar agora" }).click();
await page.waitForURL("**/marcar");
check("o cliente não escolhe serviço (vai direto às vagas)", await page.getByRole("heading", { name: "Escolha o dia e a hora" }).isVisible());
await page.getByRole("group", { name: "Dias com vagas" }).waitFor();
await page.locator(".day").nth(2).click(); // um dia mais à frente (fora do prazo de cancelamento)
const firstTime = page.locator(".time").first();
const label = (await firstTime.textContent()).trim();
await firstTime.click();
await page.screenshot({ path: OUT + "03-horas.png" });
await audit(page, "escolha de hora");
await page.getByRole("button", { name: "Continuar" }).click();
await page.getByLabel("Nome").fill("Cliente E2E");
await page.getByLabel(/Telemóvel/).fill("912 000 111");
await page.screenshot({ path: OUT + "04-dados.png" });
await page.getByRole("button", { name: "Continuar" }).click();
await page.getByRole("heading", { name: "Reveja e confirmar".replace("confirmar", "confirme") }).waitFor();
await page.screenshot({ path: OUT + "05-rever.png", fullPage: true });
check("revisão mostra a hora de Lisboa escolhida", (await page.locator(".summary").textContent()).includes(label));
await page.getByRole("button", { name: "Confirmar marcação" }).dblclick(); // duplo clique de propósito
await page.waitForURL("**/marcacao/**");
const url = page.url();
const token = url.split("/marcacao/")[1].split("?")[0];
await page.getByText("Confirmada").first().waitFor();
await page.screenshot({ path: OUT + "06-confirmacao.png", fullPage: true });
check("link privado tem token imprevisível sem dados pessoais", /^[A-Za-z0-9_-]{43}$/.test(token) && !url.toLowerCase().includes("cliente"));
check("duplo clique criou UMA marcação", psql("select count(*) from calendar_entries where customer_name='Cliente E2E'") === "1");
check("marcação guardada na base de dados (confirmed, online)", psql("select status||'/'||source from calendar_entries where customer_name='Cliente E2E'") === "confirmed/online");
await new Promise((r) => setTimeout(r, 500));
check("Carlos foi notificado com nome, telemóvel e hora", hooks.some((h) => h.event === "new" && h.text.includes("Cliente E2E") && h.text.includes("+351912000111") && h.text.includes(label)), JSON.stringify(hooks.at(0)?.text));
check("hora guardada = hora de Lisboa mostrada", (await page.locator(".summary").textContent()).includes(label));
check("há botão de calendário .ics", await page.getByRole("link", { name: /calendário/ }).isVisible());
const ics = await (await phone.request.get(`${BASE}/api/public/bookings/${token}/ics`)).text();
check("ICS descarregável", ics.startsWith("BEGIN:VCALENDAR") && ics.includes("SUMMARY:"));
await audit(page, "confirmação");

// ---------- Painel do Carlos (computador)
const desk = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: "pt-PT" });
const admin = await desk.newPage();
admin.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
const anon = await desk.request.get(BASE + "/api/admin/entries?from=2026-10-23");
check("API admin sem sessão = 401", anon.status() === 401);
await admin.goto(BASE + "/admin");
check("/admin sem sessão redireciona para o login", admin.url().endsWith("/admin/login"));
await admin.getByLabel("Email").fill("carlos@example.com");
await admin.getByLabel("Palavra-passe").fill("demo-password-123456");
await admin.getByRole("button", { name: "Entrar" }).click();
await admin.waitForURL(BASE + "/admin");
const day = psql("select to_char(starts_at at time zone 'Europe/Lisbon','YYYY-MM-DD') from calendar_entries where customer_name='Cliente E2E'");
await admin.getByText("Cliente E2E").first().waitFor();
check("agenda abre em 'Próximas marcações' (cronológica)", await admin.getByRole("heading", { name: "Próximas marcações" }).isVisible());
await admin.screenshot({ path: OUT + "07-agenda.png", fullPage: true });
await admin.getByRole("button", { name: "Dia", exact: true }).click();
await admin.locator("#agenda-date").fill(day);
await admin.getByText("Cliente E2E").first().waitFor();
check("a marcação aparece na agenda privada", await admin.getByText("Cliente E2E").first().isVisible());
check("a agenda mostra o telemóvel (só no painel)", await admin.getByText("+351912000111").first().isVisible());
await audit(admin, "agenda");
await admin.getByRole("button", { name: "Semana" }).click();
await admin.getByText("Cliente E2E").first().waitFor();
await admin.screenshot({ path: OUT + "08-semana.png", fullPage: true });

// Carlos cria uma marcação por telefone na mesma hora => tem de dar conflito
await admin.getByRole("button", { name: "Dia" }).click();
await admin.locator("#na-date").fill(day);
const startTime = psql("select to_char(starts_at at time zone 'Europe/Lisbon','HH24:MI') from calendar_entries where customer_name='Cliente E2E'");
await admin.locator("#na-time").fill(startTime);
await admin.locator("#na-name").fill("Outro Cliente");
await admin.locator("#na-phone").fill("913000222");
await admin.getByRole("button", { name: "Criar marcação" }).click();
await admin.locator(".error[role=alert]").first().waitFor();
const alertText = await admin.locator(".error[role=alert]").first().textContent();
check("admin não consegue sobrepor marcações", alertText.includes("sobrepõe"), alertText);

// ---------- Cancelar pelo link privado
await page.reload();
await page.getByRole("button", { name: "Cancelar marcação" }).click();
await page.getByRole("button", { name: "Sim, cancelar" }).click();
await page.getByText("A marcação foi cancelada").waitFor();
await page.screenshot({ path: OUT + "09-cancelada.png", fullPage: true });
await new Promise((r) => setTimeout(r, 500));
check("Carlos foi notificado do cancelamento", hooks.some((h) => h.event === "cancelled"));
check("cancelamento pelo link guardado na BD", psql("select status||'/'||cancelled_by from calendar_entries where customer_name='Cliente E2E'") === "cancelled/customer");
await admin.reload();
await admin.getByRole("button", { name: "Dia", exact: true }).click();
await admin.locator("#agenda-date").fill(day);
await admin.getByLabel("Mostrar canceladas").check();
await admin.getByText("Cancelada").first().waitFor();
check("a agenda reflete o cancelamento", true);

await admin.goto(BASE + "/admin/equipa"); await admin.screenshot({ path: OUT + "10-equipa.png", fullPage: true }); await audit(admin, "equipa");
await admin.goto(BASE + "/admin/servicos"); await audit(admin, "serviços");
await admin.goto(BASE + "/admin/definicoes"); await audit(admin, "definições");
await page.goto(BASE + "/privacidade"); await audit(page, "privacidade");
await page.goto(BASE + "/marcacao/" + "z".repeat(43));
await page.getByText("Marcação não encontrada").first().waitFor();
check("token inexistente mostra 'não encontrada'", true);

check("sem erros na consola do navegador", consoleErrors.filter((e) => !/401|404|409/.test(e)).length === 0, consoleErrors.join(" | "));
await browser.close();
hookServer.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} verificações OK`);
process.exit(failed.length ? 1 : 0);
