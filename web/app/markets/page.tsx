import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { MarketExplorer } from "@/components/product/MarketExplorer";
import { getMarkets, searchFixtures } from "@/lib/api";

export const metadata: Metadata = {
  title: "Market Explorer",
  description:
    "Search any market across 38 competitions, ranked by model probability.",
};

export const revalidate = 300;

export default async function MarketsPage() {
  // The first market is resolved on the server so the page arrives with
  // results rather than a spinner, which is what makes a search-led screen
  // feel like a product rather than a form.
  const [markets, initial] = await Promise.all([
    getMarkets(),
    searchFixtures({ market: "home", minProbability: 0.5 }),
  ]);

  return (
    <>
      <section className="light-field grain relative overflow-hidden border-b border-line">
        <div className="mx-auto max-w-shell px-5 pb-14 pt-32 sm:px-6 sm:pb-16 sm:pt-40">
          <p className="mono-label animate-rise">Market Explorer</p>
          <h1
            className="mt-6 max-w-3xl animate-rise text-hero font-semibold text-ink"
            style={{ animationDelay: "60ms" }}
          >
            Ask the card
            <br />
            <span className="text-emerald">a question.</span>
          </h1>
          <p
            className="mt-8 max-w-xl animate-rise text-lead text-ink-muted"
            style={{ animationDelay: "130ms" }}
          >
            Every modelled fixture, searchable by the market you care about.
          </p>
        </div>
      </section>

      <section className="mx-auto max-w-shell px-5 py-12 sm:px-6 sm:py-16">
        <MarketExplorer
          markets={markets ?? []}
          initialMarket="home"
          initialResults={initial ?? []}
        />
      </section>
    </>
  );
}
