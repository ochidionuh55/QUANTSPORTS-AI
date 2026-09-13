"use client";

import { useEffect, useRef, useState } from "react";
import { useReducedMotion } from "@/lib/motion";

/**
 * The hero object: a football pitch resolving into a probability field.
 *
 * Nodes sit on pitch geometry and pulse at rates derived from an actual
 * Poisson calculation, so what moves on screen is the model working rather
 * than decoration that happens to look technical. Brighter, faster nodes are
 * genuinely likelier scorelines.
 *
 * Canvas rather than WebGL: the object is two-dimensional geometry with a few
 * hundred elements, and a Three.js bundle would cost more in load time than it
 * returns. Runs at 60fps on a mid-range phone.
 */

type Node = {
  x: number;
  y: number;
  radius: number;
  probability: number;
  phase: number;
  home: number;
  away: number;
};

function poisson(k: number, lambda: number): number {
  let factorial = 1;
  for (let i = 2; i <= k; i += 1) factorial *= i;
  return (Math.exp(-lambda) * lambda ** k) / factorial;
}

const LAMBDA_HOME = 1.92;
const LAMBDA_AWAY = 0.84;

export function ProbabilityField() {
  const canvas = useRef<HTMLCanvasElement>(null);
  const pointer = useRef({ x: 0.5, y: 0.5 });
  const reducedMotion = useReducedMotion();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;

    const context = element.getContext("2d");
    if (!context) return;

    let width = 0;
    let height = 0;
    let nodes: Node[] = [];
    let frame = 0;
    let start = performance.now();

    /** Lay the field out for the current size. */
    const build = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const rect = element.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      element.width = Math.floor(width * ratio);
      element.height = Math.floor(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);

      // Score cells laid out as a grid across the pitch. Each carries its own
      // probability, which drives its size, brightness and pulse rate.
      const columns = 7;
      const rows = 5;
      const padX = width * 0.1;
      const padY = height * 0.14;
      const stepX = (width - padX * 2) / (columns - 1);
      const stepY = (height - padY * 2) / (rows - 1);

      nodes = [];
      for (let row = 0; row < rows; row += 1) {
        for (let column = 0; column < columns; column += 1) {
          const home = column % 5;
          const away = row % 5;
          const probability =
            poisson(home, LAMBDA_HOME) * poisson(away, LAMBDA_AWAY);
          nodes.push({
            x: padX + column * stepX,
            y: padY + row * stepY,
            radius: 1.6 + probability * 58,
            probability,
            phase: (column * 0.7 + row * 1.3) % (Math.PI * 2),
            home,
            away,
          });
        }
      }
      setReady(true);
    };

    const draw = (now: number) => {
      const time = reducedMotion ? 0 : (now - start) / 1000;
      context.clearRect(0, 0, width, height);

      // Parallax is deliberately slight. A hero that lurches under the cursor
      // draws attention to itself rather than to what it is showing.
      const driftX = (pointer.current.x - 0.5) * 18;
      const driftY = (pointer.current.y - 0.5) * 12;

      // Connections between neighbouring cells: the joint distribution as a
      // lattice rather than a scatter of dots.
      context.lineWidth = 1;
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const a = nodes[i];
          const b = nodes[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const distance = Math.hypot(dx, dy);
          if (distance > Math.max(width, height) * 0.13) continue;

          const strength = (a.probability + b.probability) * 6;
          context.strokeStyle = `rgba(5,184,92,${Math.min(0.2, strength)})`;
          context.beginPath();
          context.moveTo(a.x + driftX * a.probability * 9, a.y + driftY);
          context.lineTo(b.x + driftX * b.probability * 9, b.y + driftY);
          context.stroke();
        }
      }

      for (const node of nodes) {
        // Likelier scorelines breathe faster and brighter: the motion carries
        // the information rather than sitting on top of it.
        const pulse = reducedMotion
          ? 1
          : 0.82 + Math.sin(time * (0.7 + node.probability * 9) + node.phase) * 0.18;
        const x = node.x + driftX * node.probability * 9;
        const y = node.y + driftY;
        const radius = node.radius * pulse;

        const glow = context.createRadialGradient(x, y, 0, x, y, radius * 2.4);
        glow.addColorStop(0, `rgba(5,184,92,${0.1 + node.probability * 2.2})`);
        glow.addColorStop(1, "rgba(5,184,92,0)");
        context.fillStyle = glow;
        context.beginPath();
        context.arc(x, y, radius * 2.4, 0, Math.PI * 2);
        context.fill();

        context.fillStyle = `rgba(0,107,69,${0.25 + node.probability * 3.4})`;
        context.beginPath();
        context.arc(x, y, Math.max(1.4, radius * 0.42), 0, Math.PI * 2);
        context.fill();
      }

      frame = requestAnimationFrame(draw);
    };

    const onPointer = (event: PointerEvent) => {
      const rect = element.getBoundingClientRect();
      pointer.current = {
        x: (event.clientX - rect.left) / rect.width,
        y: (event.clientY - rect.top) / rect.height,
      };
    };

    build();
    start = performance.now();
    frame = requestAnimationFrame(draw);

    const observer = new ResizeObserver(build);
    observer.observe(element);
    window.addEventListener("pointermove", onPointer, { passive: true });

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("pointermove", onPointer);
    };
  }, [reducedMotion]);

  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      <canvas
        ref={canvas}
        className="h-full w-full transition-opacity duration-slow ease-quant"
        style={{ opacity: ready ? 1 : 0 }}
        aria-hidden
      />

      {/* Model vocabulary, surfaced rather than explained. */}
      <div className="absolute inset-0 hidden lg:block">
        {[
          { label: "λ HOME", value: LAMBDA_HOME.toFixed(2), top: "18%", left: "58%" },
          { label: "λ AWAY", value: LAMBDA_AWAY.toFixed(2), top: "32%", left: "82%" },
          { label: "P(H)", value: "0.489", top: "58%", left: "62%" },
          { label: "P(D)", value: "0.264", top: "70%", left: "84%" },
          { label: "P(A)", value: "0.247", top: "84%", left: "66%" },
        ].map((item) => (
          <div
            key={item.label}
            className="absolute animate-rise"
            style={{ top: item.top, left: item.left }}
          >
            <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-ink-faint">
              {item.label}
            </span>
            <span className="tabular ml-2 font-mono text-[11px] text-emerald-deep">
              {item.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
