"use client";

import { useEffect, useRef, useState } from "react";
import { useInView, useReducedMotion } from "@/lib/motion";

/**
 * Storyboard 05 — Ask the Data.
 *
 * A demonstration of natural-language discovery, typed out and answered.
 *
 * The examples shown are queries the product genuinely supports. Demonstrating
 * an interaction the system cannot actually perform would be a lie told
 * through interface design, and a reader who tried it would find out
 * immediately.
 */

const QUERIES = [
  {
    text: "draws in France above 30%",
    reply: "Filters the card by competition and market, ranked by probability.",
  },
  {
    text: "strongest home wins today",
    reply: "Ranks every modelled fixture by home-win probability.",
  },
  {
    text: "under 2.5 in Serie A",
    reply: "Returns Italian fixtures where the totals model favours the under.",
  },
];

const TYPE_MS = 55;
const HOLD_MS = 2200;

export function CommandBar() {
  const container = useRef<HTMLDivElement>(null);
  const visible = useInView(container, false);
  const reducedMotion = useReducedMotion();

  const [index, setIndex] = useState(0);
  const [typed, setTyped] = useState("");

  useEffect(() => {
    // With reduced motion, or off screen, show a finished query rather than an
    // empty box. The reader still learns what the feature does.
    if (reducedMotion || !visible) {
      setTyped(QUERIES[index].text);
      return;
    }

    let position = 0;
    setTyped("");

    const timer = window.setInterval(() => {
      position += 1;
      setTyped(QUERIES[index].text.slice(0, position));

      if (position >= QUERIES[index].text.length) {
        window.clearInterval(timer);
        window.setTimeout(
          () => setIndex((value) => (value + 1) % QUERIES.length),
          HOLD_MS,
        );
      }
    }, TYPE_MS);

    return () => window.clearInterval(timer);
  }, [index, reducedMotion, visible]);

  const complete = typed === QUERIES[index].text;

  return (
    <div ref={container}>
      <div className="mx-auto max-w-2xl text-center">
        <p className="mono-label">Ask the Data</p>
        <h2 className="mt-6 text-display font-semibold text-ink">
          Plain language.
          <br />
          Real answers.
        </h2>
        <p className="mt-7 leading-relaxed text-ink-muted">
          Describe what you are looking for. QUANTSPORT turns it into filters
          over the day&rsquo;s modelled fixtures — and tells you honestly when a
          request is outside what it supports.
        </p>
      </div>

      <div className="mx-auto mt-12 max-w-2xl">
        <div className="glass flex items-center gap-4 rounded-pill px-6 py-4">
          <span aria-hidden className="font-mono text-sm text-emerald">
            /find
          </span>
          <span className="min-h-[1.5rem] flex-1 text-left text-[15px] text-ink">
            {typed}
            {!reducedMotion ? (
              <span
                className="ml-0.5 inline-block h-[1.05em] w-px translate-y-[0.15em] bg-emerald"
                style={{
                  animation: complete ? "none" : "glow 1s steps(2) infinite",
                }}
              />
            ) : null}
          </span>
        </div>

        <div
          className="mt-4 rounded-md border border-line bg-white px-6 py-4 transition-opacity duration-slow ease-quant"
          style={{ opacity: complete ? 1 : 0.35 }}
        >
          <p className="text-sm leading-relaxed text-ink-muted">
            {QUERIES[index].reply}
          </p>
        </div>

        <p className="mt-5 text-center text-xs text-ink-faint">
          Available now in the Telegram bot. Coming to Market Explorer on the
          web.
        </p>
      </div>
    </div>
  );
}
