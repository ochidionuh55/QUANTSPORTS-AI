"""Rendered charts for Telegram.

A track record is a claim about many numbers at once, and a wall of percentages
makes a reader's eyes slide off it. The same figures drawn as a chart are read
in a glance and remembered — which matters here, because the record is the
thing we most want people to actually look at.

**Drawn from stored values only.** Nothing is smoothed, interpolated or
extrapolated to make a line prettier. A service with nine settled selections is
drawn with nine points and labelled as thin, because a confident-looking curve
over a tiny sample is a lie told with a picture.

**Dark by default**, matching the Telegram clients most people use, so a chart
does not arrive as a white rectangle in a dark conversation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure

# QUANTSPORT brand palette. White-first, emerald-led.
#
# Deliberately light rather than matching Telegram's dark chat: a chart that
# arrives as a clean white card reads as a published artefact rather than a
# screenshot, and can be reposted anywhere without looking out of place.
BACKGROUND = "#FFFFFF"
SURFACE = "#EFFBF5"
"""Light green surface for fills behind content."""

TEXT = "#07171D"
"""Near-black. Headings and labels."""

MUTED = "#64757D"
ACCENT = "#08B85A"
"""Primary emerald."""

DEEP = "#006B42"
BRIGHT = "#35E56F"
LIME = "#B6FF37"
GOOD = "#08B85A"
BAD = "#C2410C"
"""Warm amber-red rather than a gambling red.

Losses and overconfidence must be legible without the interface looking like a
casino, so the negative colour sits away from the red used on betting sites.
"""

GRID = "#DFEAE5"


def plain(label: str) -> str:
    """Strip emoji from a label.

    The chart font carries no emoji glyphs, so an unstripped label renders as
    a row of empty boxes — worse than no decoration at all.
    """
    return "".join(
        character for character in label if character.isascii() or character.isalnum()
    ).strip()


MIN_SAMPLE_FOR_RATE = 30
"""Settled selections before a rate is drawn rather than labelled thin."""


@dataclass(frozen=True)
class ServiceBar:
    """One service's live performance, ready to draw."""

    label: str
    actual: float
    expected: float
    settled: int


def _figure(width: float, height: float) -> tuple[Figure, Axes]:
    """Create a dark figure sized for a phone screen."""
    figure, axes = plt.subplots(figsize=(width, height), dpi=160)
    figure.patch.set_facecolor(BACKGROUND)
    axes.set_facecolor(BACKGROUND)
    for spine in axes.spines.values():
        spine.set_visible(False)
    axes.tick_params(colors=MUTED, labelsize=8, length=0)
    axes.grid(axis="x", color=GRID, linewidth=0.9)
    axes.set_axisbelow(True)
    return figure, axes


def _brand(figure: Figure) -> None:
    """Sign a chart, so a reposted image still says where it came from."""
    # Below the axis label rather than beside it, so the signature never
    # collides with the chart's own text.
    figure.text(
        0.01,
        -0.07,
        "QUANTSPORT AI · Football Intelligence, Quantified.",
        color=MUTED,
        fontsize=7,
        ha="left",
    )
    figure.text(
        0.99,
        -0.07,
        "Ask the Data.",
        color=ACCENT,
        fontsize=7.5,
        ha="right",
        style="italic",
    )


def _to_png(figure: Figure) -> bytes:
    """Render a figure to PNG bytes."""
    _brand(figure)
    buffer = io.BytesIO()
    figure.savefig(
        buffer,
        format="png",
        facecolor=figure.get_facecolor(),
        bbox_inches="tight",
        pad_inches=0.25,
    )
    plt.close(figure)
    return buffer.getvalue()


def track_record_chart(bars: list[ServiceBar]) -> bytes | None:
    """Draw each service's actual rate against what it predicted.

    The comparison is the point. A service winning 78% means nothing alone; a
    service winning 78% while predicting 85% is overconfident, and the gap is
    what a reader should take away. Drawing the prediction as a marker on the
    same row makes that gap impossible to miss.
    """
    usable = [bar for bar in bars if bar.settled >= MIN_SAMPLE_FOR_RATE]
    if not usable:
        return None

    usable.sort(key=lambda bar: bar.actual)
    height = max(2.4, 0.42 * len(usable) + 1.2)
    figure, axes = _figure(6.2, height)

    positions = range(len(usable))
    for position, bar in zip(positions, usable, strict=True):
        gap = bar.actual - bar.expected
        # Five points, matching the threshold the track record uses to call a
        # service overconfident. A tighter bar would paint ordinary variance as
        # failure and make every chart look alarming.
        colour = GOOD if gap >= -0.05 else BAD
        axes.barh(position, bar.actual * 100, color=colour, height=0.55, alpha=0.9)
        axes.plot(
            bar.expected * 100,
            position,
            marker="|",
            markersize=14,
            markeredgewidth=2.2,
            color=TEXT,
        )
        # Placed past whichever of the bar and the marker sits furthest right,
        # so the label never lands on top of the prediction line.
        label_x = max(bar.actual, bar.expected) * 100 + 2.5
        axes.text(
            label_x,
            position,
            f"{bar.actual:.0%}  ({bar.settled})",
            va="center",
            color=TEXT,
            fontsize=8,
        )

    axes.set_yticks(list(positions))
    axes.set_yticklabels([plain(bar.label) for bar in usable], color=TEXT, fontsize=8.5)
    axes.set_xlim(0, 122)
    # Ticks stop at 100 because a win rate cannot exceed it. The extra width is
    # only there to hold the labels.
    axes.set_xticks([0, 20, 40, 60, 80, 100])
    axes.set_xlabel("actual win rate %", color=MUTED, fontsize=8)
    axes.set_title(
        "Live record - bar is actual, line is what we predicted",
        color=TEXT,
        fontsize=10,
        pad=12,
        loc="left",
    )
    return _to_png(figure)


def calibration_chart(
    points: list[tuple[float, float, int]], title: str = "Calibration"
) -> bytes | None:
    """Plot predicted probability against observed frequency.

    The diagonal is perfect calibration. Points below it are outcomes we claimed
    more often than they happened, which is the dangerous direction — a user
    following those loses money while being told they are winning.
    """
    usable = [(p, o, n) for p, o, n in points if n >= 20]
    if len(usable) < 3:
        return None

    figure, axes = _figure(4.8, 4.2)
    axes.grid(axis="y", color=GRID, linewidth=0.7)

    axes.plot([0, 100], [0, 100], color=DEEP, linewidth=1, linestyle="--", alpha=0.6)
    sizes = [min(260, 30 + n * 0.6) for _, _, n in usable]
    axes.scatter(
        [p * 100 for p, _, _ in usable],
        [o * 100 for _, o, _ in usable],
        s=sizes,
        color=ACCENT,
        alpha=0.85,
        edgecolors=BACKGROUND,
        linewidths=1.2,
    )

    axes.set_xlim(0, 100)
    axes.set_ylim(0, 100)
    axes.set_xlabel("we predicted %", color=MUTED, fontsize=8)
    axes.set_ylabel("actually happened %", color=MUTED, fontsize=8)
    axes.set_title(title, color=TEXT, fontsize=10, pad=12, loc="left")
    axes.text(
        4,
        92,
        "on the line = honest\nbelow = overconfident",
        color=MUTED,
        fontsize=7.5,
        va="top",
    )
    return _to_png(figure)


def form_chart(
    label: str, results: list[str], goals_for: list[int], goals_against: list[int]
) -> bytes | None:
    """Draw a club's recent scoring, most recent last.

    Counted results only. No trend line is fitted: a line through ten matches
    invites a reader to extrapolate from noise, which is precisely what the
    rest of the product exists to avoid.
    """
    if len(goals_for) < 4:
        return None

    figure, axes = _figure(5.6, 2.8)
    axes.grid(axis="y", color=GRID, linewidth=0.7)
    axes.grid(axis="x", visible=False)

    positions = range(len(goals_for))
    colours = [GOOD if result == "W" else (MUTED if result == "D" else BAD) for result in results]
    axes.bar(positions, goals_for, color=colours, width=0.55, alpha=0.92)
    axes.plot(
        list(positions),
        goals_against,
        color=ACCENT,
        linewidth=1.6,
        marker="o",
        markersize=3.5,
        label="conceded",
    )

    axes.set_xticks(list(positions))
    axes.set_xticklabels(results, color=TEXT, fontsize=8)
    axes.set_ylabel("goals", color=MUTED, fontsize=8)
    axes.set_title(
        f"{plain(label)} - last {len(goals_for)} matches",
        color=TEXT,
        fontsize=10,
        pad=10,
        loc="left",
    )
    legend = axes.legend(frameon=False, fontsize=7.5, loc="upper right", labelcolor=MUTED)
    legend.get_frame().set_alpha(0)
    return _to_png(figure)
