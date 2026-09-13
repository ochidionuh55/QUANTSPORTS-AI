"use client";

import { useEffect, useState } from "react";

/**
 * Whether the reader has asked for reduced motion.
 *
 * Checked as a live media query rather than once at mount, because the
 * preference can change while the page is open — on a phone entering low-power
 * mode, for instance.
 *
 * Starts false so the server and the first client render agree; the effect
 * corrects it immediately after hydration, which cannot mismatch because it
 * runs after.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(query.matches);

    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return reduced;
}

/**
 * Whether an element has entered the viewport.
 *
 * Animations that run before a reader can see them are wasted, and worse, a
 * page that animates everything at once on load feels cheap. This lets each
 * visual build itself as it is reached.
 */
export function useInView<T extends Element>(
  ref: React.RefObject<T | null>,
  once = true,
): boolean {
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    // Without IntersectionObserver, show the content rather than hide it.
    if (typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setInView(true);
          if (once) observer.disconnect();
        } else if (!once) {
          setInView(false);
        }
      },
      { threshold: 0.25, rootMargin: "0px 0px -8% 0px" },
    );

    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, once]);

  return inView;
}
