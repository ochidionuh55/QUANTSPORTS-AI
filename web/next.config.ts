import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // The website must never recompute a probability. Everything comes from the
  // intelligence layer that Telegram also reads, so the two interfaces cannot
  // disagree about what QUANTSPORT said.
  env: {
    QUANTSPORT_API: process.env.QUANTSPORT_API ?? "http://localhost:8000",
  },
};

export default config;
