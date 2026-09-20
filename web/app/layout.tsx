import type { Metadata, Viewport } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import "./globals.css";

export const metadata: Metadata = {
  // Social platforms need an absolute image URL, so this must be the domain
  // actually serving the site. Vercel provides its own URL at build time;
  // SITE_URL overrides it once a custom domain is attached. Pointing this at a
  // domain we do not own resolves every preview image to a 404.
  metadataBase: new URL(
    process.env.SITE_URL ??
      (process.env.VERCEL_PROJECT_PRODUCTION_URL
        ? `https://${process.env.VERCEL_PROJECT_PRODUCTION_URL}`
        : "https://quantsports-ai.vercel.app"),
  ),
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
      "Football intelligence powered by mathematical modelling, historical data and a transparent published track record.",
    url: "/",
    images: [
      {
        url: "/og.png",
        width: 1200,
        height: 630,
        alt: "QUANTSPORT AI",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "QUANTSPORT AI — Football Intelligence, Quantified",
    description:
      "Football intelligence powered by mathematical modelling, historical data and a transparent published track record.",
    images: ["/og.png"],
  },
  robots: { index: true, follow: true },
  manifest: "/manifest.webmanifest",
  icons: {
    icon: [
      { url: "/icon-32.png", sizes: "32x32", type: "image/png" },
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
    ],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180" }],
    shortcut: ["/favicon.ico"],
  },
  applicationName: "QUANTSPORT AI",
  appleWebApp: {
    capable: true,
    title: "QUANTSPORT",
    statusBarStyle: "default",
  },
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
