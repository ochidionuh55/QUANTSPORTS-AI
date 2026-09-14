import type { MetadataRoute } from "next";

const BASE = "https://quantsports-ai.vercel.app";

/**
 * The sitemap.
 *
 * Change frequencies reflect how the pages actually behave: the card turns
 * over daily, the record grows as selections settle, the methodology rarely
 * moves. Declaring everything as "daily" trains crawlers to ignore the hint.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();

  return [
    { url: BASE, lastModified: now, changeFrequency: "daily", priority: 1 },
    { url: `${BASE}/today`, lastModified: now, changeFrequency: "hourly", priority: 0.9 },
    { url: `${BASE}/markets`, lastModified: now, changeFrequency: "hourly", priority: 0.8 },
    { url: `${BASE}/track-record`, lastModified: now, changeFrequency: "daily", priority: 0.8 },
    { url: `${BASE}/teams`, lastModified: now, changeFrequency: "daily", priority: 0.7 },
    { url: `${BASE}/competitions`, lastModified: now, changeFrequency: "weekly", priority: 0.6 },
    { url: `${BASE}/methodology`, lastModified: now, changeFrequency: "monthly", priority: 0.7 },
    { url: `${BASE}/pricing`, lastModified: now, changeFrequency: "monthly", priority: 0.6 },
  ];
}
