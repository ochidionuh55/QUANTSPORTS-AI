import type { Metadata } from "next";
import { PageHeader } from "@/components/product/PageHeader";
import { Button } from "@/components/ui/Button";

export const metadata: Metadata = {
  title: "Sign in",
  description: "Access QUANTSPORT through Telegram.",
};

export default function LoginPage() {
  return (
    <>
      <PageHeader
        label="Sign in"
        title="Your account lives in Telegram."
        lead="QUANTSPORT runs as a Telegram bot, so there is no separate password to remember. Web sign-in is being built to link the same account."
      />

      <section className="mx-auto max-w-shell px-6 py-20">
        <div className="mx-auto max-w-md rounded-lg border border-line bg-white p-9 shadow-float">
          <h2 className="text-[17px] font-semibold tracking-[-0.02em] text-ink">
            Open QUANTSPORT
          </h2>
          <p className="mt-3 leading-relaxed text-ink-muted">
            Start the bot and your account is created on first use. Everything
            free stays free, and the trial starts when you choose it.
          </p>
          <div className="mt-7">
            <Button href="https://t.me/quantpredictzbot" external>
              Continue with Telegram
            </Button>
          </div>
          <p className="mt-7 border-t border-line pt-6 text-xs leading-relaxed text-ink-faint">
            We ask for nothing beyond the Telegram identifier needed to reply to
            you. No email, no card, no personal details.
          </p>
        </div>
      </section>
    </>
  );
}
