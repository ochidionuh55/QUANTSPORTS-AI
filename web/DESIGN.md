# QUANTSPORT AI — design system

Tokens live in `tailwind.config.ts` and `app/globals.css`. Components reference
tokens, never raw hex or arbitrary pixels, so the brand changes in one place
rather than being hunted through markup.

## Colour

| Token | Hex | Use |
|---|---|---|
| `emerald` | `#05B85C` | Primary. Brand, data, affirmative state |
| `emerald-deep` | `#006B45` | Depth, gradient origin |
| `emerald-forest` | `#063D31` | Heavy type on light |
| `emerald-mint` | `#5EF38A` | Highlights on dark |
| `lime` | `#A9FF3F` | Light source. Never a fill |
| `cyan` | `#2CD9C5` | Analytics accent. Sparing |
| `ink` | `#071A17` | Body type |
| `ink-muted` / `ink-faint` | `#5B6B66` / `#8C9A95` | Secondary, tertiary |
| `night-deep` | `#041B17` | Cinematic sections |

Backgrounds use **light fields**, not gradients: three radial sources at low
opacity, with grain over them. A linear three-colour gradient reads as a
template; overlapping radial light reads as atmosphere.

## Type

Geist Sans for everything, Geist Mono for measurements. The contrast is what
creates the quant character — `λH 1.92` in mono beside prose in sans says more
about the product than any adjective.

Display type is `clamp()`-based, tracking `-0.05em`, line-height `0.9`.
Numbers use `.tabular` so they never shift width as they change.

## Motion

One curve — `cubic-bezier(0.22, 1, 0.36, 1)` — across the product. Weighted,
never bouncy. Three durations: `fast` 180ms, `base` 320ms, `slow` 620ms.

Only transforms and opacity animate. `prefers-reduced-motion` is honoured
globally in `globals.css`, not per component.

## Shadows

Tinted green, never black. A neutral shadow over a tinted background reads as
dirt; a hue-consistent one reads as depth. Three layers: `surface`, `float`,
`overlay`, plus `glass` for floating chrome.

## Glass

Used only for chrome that floats over content — navigation, filters, overlays.
Not for ordinary cards. Everything frosted is the same mistake as nothing
frosted.

## What is deliberately absent

**No WebGL yet.** The hero's mathematics is real and interactive without it,
and a Three.js bundle would cost more in LCP than it currently returns. Worth
adding where it materially improves a visualisation — not as the default.

**No GSAP yet.** Scroll choreography earns its weight on long narrative pages.
The hero does not need it.

Both are easy to add once there is a page that justifies them. Shipping them
first would have meant a slower site with the same amount of information on it.

## The rule that governs everything

No probability is computed in the frontend. The score matrix on the homepage is
an illustration of the method, clearly framed as such. Every published figure
comes from the intelligence layer that Telegram also reads, so the two
interfaces cannot disagree about what QUANTSPORT said.

## API fields are not guaranteed

Optional API fields are typed optional and read through a guard, always.

The website and the API deploy independently, so the site may be newer than the
server it is talking to. A type annotation is a claim about data we control,
and a network response is not that — typing a field as required because our own
API returns it turns a rolling deploy into a runtime crash on a live page.

Where a field is missing the component degrades: the lead selection drops its
evidence meters and keeps its headline figures, rather than taking the page
down. Making these optional also let the compiler find three more unguarded
call sites we had not noticed.
