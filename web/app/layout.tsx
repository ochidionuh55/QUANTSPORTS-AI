import type { Metadata, Viewport } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
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
    "Mathematical models, verified football data and transparent probabilities — built to reveal the structure behind the game.",
  openGraph: {
    type: "website",
    siteName: "QUANTSPORT AI",
    title: "QUANTSPORT AI — Football Intelligence, Quantified",
    description:
      "Football is full of opinions. We start with probabilities. 113,000 matches, 38 competitions, and a record that includes the losses.",
  },
  twitter: { card: "summary_large_image" },
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  themeColor: "#FFFFFF",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      // Declares the smooth scrolling set in globals.css, so Next can suspend
      // it during route transitions rather than animating navigation itself.
      data-scroll-behavior="smooth"
      className={`${GeistSans.variable} ${GeistMono.variable}`}
    >
      <body className="flex min-h-screen flex-col">
        <Nav />
        <main className="flex-1">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
