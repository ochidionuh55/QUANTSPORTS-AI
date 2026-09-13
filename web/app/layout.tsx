import type { Metadata } from "next";
import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://quantsport.ai"),
  title: {
    default: "QUANTSPORT AI — Football Intelligence, Quantified",
    template: "%s | QUANTSPORT AI",
  },
  description:
    "Mathematical models, historical evidence and transparent probabilities across 38 competitions. Every selection published before kickoff and settled afterwards, wins and losses alike.",
  openGraph: {
    type: "website",
    siteName: "QUANTSPORT AI",
    title: "QUANTSPORT AI — Football Intelligence, Quantified",
    description:
      "Explore football beyond opinion. 113,000 historical matches, 38 competitions, and a published track record that includes the losses.",
  },
  twitter: { card: "summary_large_image" },
  robots: { index: true, follow: true },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col">
        <Nav />
        <main className="flex-1">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
