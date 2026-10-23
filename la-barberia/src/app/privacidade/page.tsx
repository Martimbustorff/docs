import type { Metadata } from "next";
import Link from "next/link";
import { db } from "@/lib/db";
import { getSettings } from "@/lib/data";
import { waLink } from "@/lib/site";
import { SiteHeader } from "../site-chrome";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Privacidade" };

const TODO = <strong className="field-error">[A PREENCHER PELO RESPONSÁVEL]</strong>;

export default async function Privacidade() {
  const s = await getSettings(db());
  const missing = !s.legalName || !s.privacyEmail;
  return (
    <>
      <SiteHeader settings={s} open={false} />
      <main id="conteudo" className="wrap narrow stack" style={{ padding: "2rem 1rem 4rem" }}>
        <h1 style={{ fontSize: "2.2rem" }}>Política de privacidade</h1>
        {missing && (
          <div className="error" role="note">
            Atenção (apenas visível enquanto faltarem dados): preencha o nome do responsável, o NIF e o email de privacidade em Definições, no painel, antes de publicar o site.
          </div>
        )}

        <h2>Quem é o responsável</h2>
        <p>
          {s.legalName || TODO}
          {s.legalNif ? `, NIF ${s.legalNif}` : null}, que explora a {s.shopName}, {s.address}. Contacto para questões de privacidade:{" "}
          {s.privacyEmail ? <a href={`mailto:${s.privacyEmail}`}>{s.privacyEmail}</a> : TODO}.
        </p>

        <h2>Que dados tratamos</h2>
        <ul>
          <li>Ao marcar: nome, telemóvel, email (opcional), serviço, profissional, data e hora, estado da marcação (confirmada, cancelada, concluída, falta).</li>
          <li>Notas internas que o barbeiro registe sobre o atendimento, quando necessário.</li>
          <li>Para evitar abusos, um código irreversível derivado do endereço IP (não guardamos o IP em si), apagado ao fim de um dia.</li>
        </ul>

        <h2>Para que usamos e com que fundamento</h2>
        <ul>
          <li>Gerir a sua marcação e contactá-lo se for necessário: execução do pedido que nos faz (art. 6.º, n.º 1, al. b) do RGPD).</li>
          <li>Segurança do serviço e prevenção de marcações abusivas: interesse legítimo (art. 6.º, n.º 1, al. f)).</li>
          <li>Não usamos os seus dados para marketing e não lhe pedimos consentimento de marketing.</li>
        </ul>

        <h2>Durante quanto tempo</h2>
        <p>Guardamos as marcações até {s.retentionMonths} meses depois da data do atendimento. Depois, são apagadas automaticamente.</p>

        <h2>Quem tem acesso</h2>
        <p>
          Apenas a barbearia. Recorremos a prestadores técnicos que tratam dados em nosso nome (alojamento do site, base de dados e, se estiver ativo, envio de emails), obrigados a proteger os dados. Não vendemos nem cedemos dados a terceiros para outros fins.
        </p>

        <h2>Cookies e terceiros</h2>
        <p>
          Este site não usa cookies nem rastreadores para clientes e não carrega fontes, scripts ou mapas de terceiros. A área privada da barbearia usa um cookie técnico de sessão, estritamente necessário. As ligações para WhatsApp, Instagram e mapas abrem serviços externos, com as suas próprias políticas.
        </p>

        <h2>Os seus direitos</h2>
        <p>
          Pode pedir o acesso, a retificação, o apagamento, a limitação ou a oposição ao tratamento dos seus dados, e a portabilidade, através de {s.privacyEmail ? <a href={`mailto:${s.privacyEmail}`}>{s.privacyEmail}</a> : TODO} ou por <a href={waLink(s)}>WhatsApp</a>. Pode também apresentar reclamação à Comissão Nacional de Proteção de Dados (<a href="https://www.cnpd.pt" target="_blank" rel="noopener noreferrer">cnpd.pt</a>).
        </p>

        <p className="muted small">Texto-modelo adequado ao funcionamento desta plataforma; deve ser validado pelo responsável (e, se possível, por apoio jurídico) antes da publicação.</p>
        <p><Link href="/">← Voltar ao início</Link></p>
      </main>
    </>
  );
}
