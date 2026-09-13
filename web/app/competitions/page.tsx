import type { Metadata } from "next";
import { ComingSoon } from "@/components/product/ComingSoon";

export const metadata: Metadata = {
  title: "Competitions",
  description: "How each of the 38 competitions on record actually behaves.",
};

export default function CompetitionsPage() {
  return (
    <ComingSoon
      label="Competitions"
      title="Every league has a character."
      lead="Goals per game, home advantage, draw frequency and scoring patterns, measured per competition rather than assumed."
      building={[
        "Goals per game and total distribution by competition",
        "Home, draw and away frequencies measured from the record",
        "Over 2.5 and both-teams-to-score rates per league",
        "How each competition has shifted across seasons",
      ]}
      available={{ href: "/today", label: "See today's card" }}
    />
  );
}
