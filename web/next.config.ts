import path from "node:path";
import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,

  // This app lives inside a repository that also holds the Python backend, so
  // Next can find lockfiles above it and guess the wrong workspace root.
  // Stating the root explicitly removes the ambiguity and the warning with it.
  turbopack: {
    root: path.join(__dirname),
  },

  // The website must never recompute a probability. Everything comes from the
  // intelligence layer that Telegram also reads, so the two interfaces cannot
  // disagree about what QUANTSPORT said.
  env: {
    QUANTSPORT_API: process.env.QUANTSPORT_API ?? "http://localhost:8000",
  },
};

export default config;
