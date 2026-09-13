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
      <PageHeader
        label="Market Explorer"
        title="Ask the card a question."
        lead="Every modelled fixture, ranked by the market you care about. Choose a market, set a floor, and see what today actually supports."
      />

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        <MarketExplorer
          markets={markets ?? []}
          initialMarket="home"
          initialResults={initial ?? []}
        />
      </section>
    </>
  );
}
