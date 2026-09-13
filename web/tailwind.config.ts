import type { Config } from "tailwindcss";

/**
 * QUANTSPORT AI design system.
 *
 * Every value the interface uses is named here. Components reference tokens,
 * never raw hex or arbitrary pixel values, so the brand can be adjusted in one
 * place rather than hunted through markup.
 *
 * The palette is luminous rather than flat: emerald carries the brand, mint
 * and lime provide light, and a restrained cyan gives the analytics layer a
 * technical character without turning the product into a dashboard.
 */
const config: Config = {
  darkMode: "class",
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        white: "#FFFFFF",
        warm: "#FBFDFB",
        surface: { DEFAULT: "#F4FAF7", mint: "#DFFFF0" },
        emerald: {
          DEFAULT: "#05B85C",
          deep: "#006B45",
          forest: "#063D31",
          mint: "#5EF38A",
        },
        lime: { DEFAULT: "#A9FF3F" },
        cyan: { DEFAULT: "#2CD9C5" },
        ink: { DEFAULT: "#071A17", muted: "#5B6B66", faint: "#8C9A95" },
        line: { DEFAULT: "#E3EFE9", strong: "#CBDED6" },
        night: { DEFAULT: "#041512", card: "#071F1A", deep: "#041B17" },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      fontSize: {
        // Display scale. Tight tracking and sub-unity line height are what
        // make large type read as editorial rather than merely big.
        hero: ["clamp(3.25rem, 9vw, 8.5rem)", { lineHeight: "0.9", letterSpacing: "-0.05em" }],
        display: ["clamp(2.25rem, 5vw, 4rem)", { lineHeight: "1.02", letterSpacing: "-0.035em" }],
        title: ["clamp(1.5rem, 2.6vw, 2.25rem)", { lineHeight: "1.12", letterSpacing: "-0.025em" }],
        lead: ["clamp(1.05rem, 1.4vw, 1.3rem)", { lineHeight: "1.55", letterSpacing: "-0.01em" }],
        mono: ["0.72rem", { lineHeight: "1.2", letterSpacing: "0.14em" }],
      },
      borderRadius: {
        xs: "8px",
        sm: "12px",
        md: "18px",
        lg: "24px",
        xl: "32px",
        "2xl": "48px",
        pill: "999px",
      },
      boxShadow: {
        // Shadows are tinted green rather than black. A neutral shadow on a
        // tinted background reads as dirt; a hue-consistent one reads as depth.
        surface: "0 1px 2px rgba(0,50,35,.04)",
        float: "0 1px 2px rgba(0,50,35,.04), 0 16px 40px rgba(0,50,35,.08)",
        overlay: "0 2px 6px rgba(0,50,35,.06), 0 32px 80px rgba(0,50,35,.12)",
        glass:
          "inset 0 1px 0 rgba(255,255,255,.8), 0 12px 40px rgba(0,70,45,.08)",
      },
      transitionTimingFunction: {
        // One curve for the whole product. Weighted, never bouncy.
        quant: "cubic-bezier(0.22, 1, 0.36, 1)",
      },
      transitionDuration: {
        fast: "180ms",
        base: "320ms",
        slow: "620ms",
      },
      keyframes: {
        rise: {
          from: { opacity: "0", transform: "translateY(14px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        glow: {
          "0%, 100%": { opacity: "0.55" },
          "50%": { opacity: "0.9" },
        },
      },
      animation: {
        rise: "rise 620ms cubic-bezier(0.22,1,0.36,1) both",
        glow: "glow 5s ease-in-out infinite",
      },
      maxWidth: { shell: "1440px", prose: "68ch" },
    },
  },
  plugins: [],
};

export default config;
