import type { Metadata } from "next";
import Link from "next/link";
import { PageHeader } from "@/components/product/PageHeader";
import { getHistoryDays } from "@/lib/api";
import { formatDay } from "@/lib/format";

export const metadata: Metadata = {
  title: "History",
  description:
    "Every selection QUANTSPORT has published, by date. Timestamped before kickoff, settled from the result, never edited.",
};

export const revalidate = 600;

export default async function HistoryPage() {
  const days = (await getHistoryDays()) ?? [];

  return (
    <>
      <PageHeader
        label="History"
        title="Every day, kept."
        lead="Open any date to see exactly what QUANTSPORT published that morning and how it finished. Read from storage, never recomputed."
      />

      <section className="mx-auto max-w-shell px-5 py-16 sm:px-6 sm:py-20">
        {days.length === 0 ? (
          <div className="rounded-lg border border-line bg-white p-12 text-center">
            <p className="font-medium text-ink">Nothing published yet</p>
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
              Selections appear here the day they are published.
            </p>
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {days.map((day) => (
              <Link
                key={day}
                href={`/history/${day}`}
                className="group flex items-center justify-between gap-4 rounded-md border border-line bg-white px-5 py-4 transition-all duration-base ease-quant hover:-translate-y-0.5 hover:border-line-strong hover:shadow-float"
              >
                <span className="text-[15px] font-medium text-ink">
                  {formatDay(`${day}T12:00:00Z`)}
                </span>
                <span
                  aria-hidden
                  className="text-ink-faint transition-transform duration-base ease-quant group-hover:translate-x-1"
                >
                  →
                </span>
              </Link>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
