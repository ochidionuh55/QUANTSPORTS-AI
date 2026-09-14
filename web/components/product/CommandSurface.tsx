"use client";

import { useEffect, useRef, useState } from "react";
import type { MarketOption } from "@/lib/api";

/**
 * The Market Explorer's search surface.
 *
 * Section 08 asks for this to feel like QUANTSPORT's search engine rather than
 * a filter bar, so the query leads and the controls follow.
 *
 * Typed language maps onto the same structured filters the controls set, and
 * both stay visible. A search box that hides what it did leaves the reader
 * unable to tell a narrow result from a misunderstood question.
 */

type Parsed = { market?: string; floor?: number; note: string };

const PHRASES: { match: RegExp; market: string }[] = [
  { match: /\bhome\s*(win|wins)?\b/i, market: "home" },
  { match: /\baway\s*(win|wins)?\b/i, market: "away" },
  { match: /\bdraws?\b/i, market: "draw" },
  { match: /\bbtts|both teams?\b/i, market: "btts" },
  { match: /\bover\s*2\.?5\b/i, market: "over_2_5" },
  { match: /\bover\s*1\.?5\b/i, market: "over_1_5" },
  { match: /\bunder\s*2\.?5\b/i, market: "under_2_5" },
  { match: /\bunder\s*3\.?5\b/i, market: "under_3_5" },
];

/**
 * Read a query into filters.
 *
 * Deliberately modest: it recognises markets and a probability floor, and says
 * so. Pretending to understand more than it does would be a lie told through
 * interface design, and the reader finds out the moment results look wrong.
 */
export function parseQuery(text: string, markets: MarketOption[]): Parsed {
  const lower = text.toLowerCase();

  const phrase = PHRASES.find((entry) => entry.match.test(lower));
  const named = markets.find((option) =>
    lower.includes(option.outcome.toLowerCase()),
  );
  const market = phrase?.market ?? named?.key;

  const percent = lower.match(/(\d{2})\s*%/);
  const floor = percent ? Number(percent[1]) / 100 : undefined;

  if (!market && floor === undefined) {
    return {
      note: "Not recognised. Try a market name, for example “draws above 30%”.",
    };
  }

  const label = market
    ? (markets.find((option) => option.key === market)?.label ?? market)
    : "current market";

  return {
    market,
    floor,
    note: `Reading as ${label}${floor ? ` above ${Math.round(floor * 100)}%` : ""}.`,
  };
}

export function CommandSurface({
  markets,
  onQuery,
}: {
  markets: MarketOption[];
  onQuery: (parsed: Parsed) => void;
}) {
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const input = useRef<HTMLInputElement>(null);

  // Section 13 asks for a command palette. The same shortcut focuses the
  // surface here, so the habit carries across the product.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const submit = () => {
    if (!text.trim()) return;
    const parsed = parseQuery(text, markets);
    setNote(parsed.note);
    if (parsed.market || parsed.floor !== undefined) onQuery(parsed);
  };

  return (
    <div>
      <div className="glass flex items-center gap-4 rounded-pill px-5 py-4 sm:px-7 sm:py-5">
        <span aria-hidden className="font-mono text-sm text-emerald">
          /find
        </span>
        <input
          ref={input}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && submit()}
          placeholder="draws above 30%"
          aria-label="Search today's card"
          className="min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-ink-faint sm:text-[17px]"
        />
        <kbd className="hidden shrink-0 rounded-xs border border-line bg-white px-2 py-1 font-mono text-[10px] text-ink-faint sm:block">
          ⌘K
        </kbd>
        <button
          type="button"
          onClick={submit}
          className="shrink-0 rounded-pill bg-ink px-4 py-2 text-sm font-medium text-white transition-all duration-base ease-quant hover:shadow-float"
        >
          Ask
        </button>
      </div>

      <div className="mt-3 min-h-[1.25rem] px-2">
        {note ? (
          <p className="animate-rise font-mono text-[11px] text-ink-muted">
            {note}
          </p>
        ) : (
          <p className="font-mono text-[11px] text-ink-faint">
            Recognises markets and a probability floor. Filters below stay in
            sync.
          </p>
        )}
      </div>
    </div>
  );
}
