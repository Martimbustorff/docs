import type { Metadata } from "next";

export const metadata: Metadata = {
  title: { default: "Painel", template: "%s — Painel La barberia" },
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default function AdminRootLayout({ children }: { children: React.ReactNode }) {
  return <div className="admin-theme" style={{ minHeight: "100vh", background: "var(--bg)", color: "var(--text)" }}>{children}</div>;
}
