import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "La barberia — Cascais", template: "%s — La barberia" },
  description: "Barbearia em Cascais desde 2014. Cortes clássicos e modernos, barba e cuidados masculinos. Marque online.",
  robots: { index: true, follow: true },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#0e0d0b" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-PT">
      <body>
        <a className="skip" href="#conteudo">Saltar para o conteúdo</a>
        {children}
      </body>
    </html>
  );
}
