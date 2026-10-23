import Link from "next/link";

export default function NotFound() {
  return (
    <main id="conteudo" className="wrap narrow" style={{ padding: "4rem 1rem" }}>
      <h1>Página não encontrada</h1>
      <p className="muted">O endereço que abriu não existe ou já não está disponível.</p>
      <Link className="btn" href="/">Voltar ao início</Link>
    </main>
  );
}
