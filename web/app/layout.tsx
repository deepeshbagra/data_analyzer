import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Document Intelligence",
  description: "Extraction, reconciliation and findings for business documents",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-white text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-100">
        {children}
      </body>
    </html>
  );
}
