import type { Config } from "tailwindcss";

/**
 * QUANTSPORT AI design tokens.
 *
 * White-first and emerald-led. The palette is deliberately narrow: a product
 * whose credibility rests on restraint should not have twelve accent colours.
 */
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        emerald: {
          DEFAULT: "#08B85A",
          deep: "#006B42",
          bright: "#35E56F",
        },
        lime: { DEFAULT: "#B6FF37" },
        ink: { DEFAULT: "#07171D", deep: "#052A22" },
        surface: { DEFAULT: "#F8FCFA", green: "#EFFBF5" },
        muted: { DEFAULT: "#64757D" },
        line: { DEFAULT: "#DFEAE5" },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
      },
      borderRadius: {
        card: "22px",
        pill: "999px",
      },
      boxShadow: {
        card: "0 1px 2px rgba(7,23,29,0.04), 0 8px 24px rgba(7,23,29,0.04)",
        lift: "0 2px 4px rgba(7,23,29,0.05), 0 16px 40px rgba(7,23,29,0.07)",
      },
      letterSpacing: {
        tight: "-0.02em",
      },
    },
  },
  plugins: [],
};

export default config;
