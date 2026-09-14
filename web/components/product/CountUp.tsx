"use client";

import { useEffect, useRef, useState } from "react";
import { useInView, useReducedMotion } from "@/lib/motion";

/**
 * A figure that counts up as it is reached.
 *
 * Used only for headline numbers. Animating every figure on a page turns
 * measurement into decoration, and this product's numbers are the argument —
 * they should arrive with weight, not sparkle.
 */
export function CountUp({
  value,
  decimals = 0,
  suffix = "",
  duration = 900,
}: {
  value: number;
  decimals?: number;
  suffix?: string;
  duration?: number;
}) {
  const container = useRef<HTMLSpanElement>(null);
  const visible = useInView(container);
  const reducedMotion = useReducedMotion();
  const [shown, setShown] = useState(0);

  useEffect(() => {
    if (reducedMotion || !visible) {
      setShown(reducedMotion ? value : 0);
      return;
    }

    let frame = 0;
    const start = performance.now();

    const step = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      // Eased so the number decelerates into place rather than stopping dead.
      const eased = 1 - (1 - progress) ** 3;
      setShown(value * eased);
      if (progress < 1) frame = requestAnimationFrame(step);
    };

    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value, duration, visible, reducedMotion]);

  return (
    <span ref={container} className="tabular">
      {shown.toFixed(decimals)}
      {suffix}
    </span>
  );
}
