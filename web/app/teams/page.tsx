import type { Metadata } from "next";
import { ComingSoon } from "@/components/product/ComingSoon";

export const metadata: Metadata = {
  title: "Team Intelligence",
  description:
    "A club's full counted record — results, goals, home and away splits, form and head-to-head.",
};

export default function TeamsPage() {
  return (
    <ComingSoon
      label="Team Intelligence"
      title="What a club has actually done."
      lead="Counted from 113,000 matches on record. No forecast involved — just what happened, measured."
      building={[
        "Full record: played, won, drawn, lost, goals for and against",
        "Home and away splits, and the home advantage each club carries",
        "Over and under rates at every line, and both-teams-to-score",
        "Recent form and head-to-head where the sample supports it",
      ]}
      available={{ href: "/methodology", label: "How we count" }}
    />
  );
}
