"""Message formatting for the beta.

Kept separate from handlers so the wording of a disclosure is reviewed in one
place rather than scattered across callbacks.

Every analysis carries its coverage grade and an explicit statement of model
status. The rule is simple: never show a number without showing how much it can
be trusted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from app.services.match_analysis import MatchAnalysis

MARKET_ONLY_NOTICE = (
    "📊 <b>Market view, not a QUANTSPORT estimate.</b> We have no matched "
    "history for these clubs, so our models did not run. These are the "
    "bookmakers' own implied probabilities with their margin removed."
)

EXPERIMENTAL_NOTICE = (
    "🧪 <b>Experimental analysis.</b> These are statistical estimates from "
    "models still under evaluation. They are not predictions and not betting "
    "advice."
)

FOOTER = "⚠️ QUANTSPORT provides analytical information only. No outcome is " "guaranteed. 18+."


def _percent(value: Decimal | None) -> str:
    """Format a probability as a percentage, or a dash when absent."""
    return f"{float(value) * 100:.0f}%" if value is not None else "—"


def format_fixture_line(home: str, away: str, kickoff_label: str, competition: str | None) -> str:
    """Format one fixture for a list."""
    league = f" · {competition}" if competition else ""
    return f"{kickoff_label}{league}\n{home} v {away}"


def format_analysis(analysis: MatchAnalysis) -> str:
    """Render a full analysis message."""
    lines = [
        f"<b>{analysis.home_name} v {analysis.away_name}</b>",
        f"{analysis.kickoff:%a %d %b, %H:%M} UTC",
    ]
    if analysis.competition:
        lines.append(analysis.competition)
    lines.append("")
    lines.append(f"<b>Coverage:</b> {analysis.coverage.badge}")

    if not analysis.has_probabilities:
        lines.append("")
        lines.append(analysis.unavailable_reason or "No analysis is available for this fixture.")
        lines.append("")
        lines.append(_history_block(analysis))
        lines.append("")
        lines.append(FOOTER)
        return "\n".join(lines)

    lines.append("")
    lines.append(EXPERIMENTAL_NOTICE)
    lines.append("")
    lines.append("<b>Match probabilities</b>")
    lines.append(f"Home win: {_percent(analysis.probabilities.get('home'))}")
    lines.append(f"Draw: {_percent(analysis.probabilities.get('draw'))}")
    lines.append(f"Away win: {_percent(analysis.probabilities.get('away'))}")

    if analysis.expected_home_goals is not None:
        lines.append("")
        lines.append("<b>Expected goals</b>")
        lines.append(f"{analysis.home_name}: {analysis.expected_home_goals}")
        lines.append(f"{analysis.away_name}: {analysis.expected_away_goals}")

    if analysis.over_under:
        lines.append("")
        lines.append("<b>Goals markets</b>")
        for line in ("1.5", "2.5"):
            over = analysis.over_under.get(f"over_{line}")
            if over is not None:
                lines.append(f"Over {line}: {_percent(over)}")
        if analysis.both_teams_score is not None:
            lines.append(f"Both teams score: {_percent(analysis.both_teams_score)}")

    lines.append("")
    lines.append(_history_block(analysis))

    if analysis.market_odds:
        lines.append("")
        lines.append("<b>Market</b>")
        odds = analysis.market_odds
        lines.append(
            f"Odds: {odds['home']} / {odds['draw']} / {odds['away']} " "(bookmaker average)"
        )

    lines.append("")
    lines.append("<b>Model status</b>")
    lines.append(_model_status(analysis))

    lines.append("")
    lines.append("<b>Market assessment</b>")
    lines.append(analysis.market_assessment)

    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def _model_status(analysis: MatchAnalysis) -> str:
    """Describe which models contributed and their standing."""
    used = ", ".join(analysis.components_used) or "none"
    text = f"Under evaluation. Components used: {used}."
    if analysis.components_dropped:
        text += f"\nExcluded for insufficient data: " f"{', '.join(analysis.components_dropped)}."
    if analysis.market_probabilities:
        text += (
            "\nMarket prices are used as the baseline; our models have not "
            "earned the right to move them yet."
        )
    return text


def _history_block(analysis: MatchAnalysis) -> str:
    """Summarise each side's recent record."""
    rows = ["<b>Recent record (last 3 seasons on file)</b>"]
    for snapshot in (analysis.home, analysis.away):
        if not snapshot.is_resolved:
            rows.append(f"{snapshot.source_name}: no historical record")
            continue
        elo = f", Elo {snapshot.elo:.0f}" if snapshot.elo else ""
        rows.append(
            f"{snapshot.source_name}: {snapshot.matches} matches, "
            f"{snapshot.goals_scored_per_match:.2f} scored / "
            f"{snapshot.goals_conceded_per_match:.2f} conceded per match, "
            f"{snapshot.points_per_match:.2f} pts/match{elo}"
        )
    return "\n".join(rows)


def format_league_status(code: str, name: str, matches: int, teams: int) -> str:
    """Render a league's coverage and research standing."""
    return (
        f"<b>{name} — {code}</b>\n\n"
        f"Historical coverage: {'available' if matches else 'none'}\n"
        f"Matches on file: {matches:,}\n"
        f"Teams on file: {teams}\n\n"
        "Model status: 🧪 Experimental\n"
        "Research status: under evaluation\n\n"
        "Current finding: our models are well calibrated but have not "
        "demonstrated an edge over market prices in backtesting. No value "
        "selections are published."
    )


def _pct(raw: str | None) -> str:
    """Format a stored probability string as a percentage."""
    if raw is None:
        return "—"
    try:
        return f"{float(raw) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


COVERAGE_BADGE = {
    "fully_modelled": "🟢",
    "partially_modelled": "🟡",
    "data_only": "🔵",
    "unsupported": "⚪",
}


# Markets worth naming as a fixture's strongest angle, with the floor each
# must clear. Over 0.5 is true in almost every match, so surfacing it would
# fill the card with a fact rather than a finding.
_ANGLE_FLOORS: dict[tuple[str, str], float] = {
    ("1X2", "Home"): 0.55,
    ("1X2", "Away"): 0.45,
    ("1X2", "Draw"): 0.32,
    ("Double chance", "1X (home or draw)"): 0.72,
    ("Double chance", "X2 (draw or away)"): 0.70,
    ("Goals", "Over 1.5"): 0.78,
    ("Goals", "Over 2.5"): 0.60,
    ("Goals", "Under 2.5"): 0.60,
    ("Goals", "Under 3.5"): 0.78,
    ("Both teams to score", "Yes"): 0.62,
    ("Both teams to score", "No"): 0.60,
}


def strongest_angle(record: object) -> tuple[str, float] | None:
    """Return a fixture's most notable market, or ``None`` if it has none.

    This is not a selection. It is the highest market this fixture reaches
    against a per-market floor, shown so that every analysed match says
    something rather than leaving the reader to compare percentages
    themselves. Selections come from the ranked services and are marked
    differently, because a fixture that no service chose was not chosen.
    """
    markets = getattr(record, "markets", {}) or {}
    best: tuple[str, float] | None = None

    for (market, outcome), floor in _ANGLE_FLOORS.items():
        raw = (markets.get(market) or {}).get(outcome)
        if raw is None:
            continue
        try:
            probability = float(str(raw))
        except (TypeError, ValueError):
            continue
        if probability < floor:
            continue
        # Ranked by how far past its own floor each market sits, so a 63%
        # BTTS is not buried by an 80% over 1.5 that barely cleared its bar.
        margin = probability - floor
        if best is None or margin > best[1]:
            best = (f"{outcome} {probability * 100:.0f}%", margin)

    return best


def format_summary_line(
    record: object,
    picks_by_fixture: dict[str, list[tuple[str, str, float]]] | None = None,
) -> str:
    """Render one fixture for the daily list.

    Deliberately compact: a user scanning a card wants the shape of each match
    at a glance, with the detail one tap away.

    Args:
        record: The stored analysis.
        picks_by_fixture: Services that selected each fixture, keyed by
            provider event id. Supplied by the caller so the whole day needs
            one query rather than one per fixture.
    """
    markets = getattr(record, "markets", {}) or {}
    one_x_two = markets.get("1X2", {})
    goals = markets.get("Goals", {})
    btts = markets.get("Both teams to score", {})
    badge = COVERAGE_BADGE.get(getattr(record, "coverage", ""), "⚪")
    picks_by_fixture = picks_by_fixture or {}

    header = (
        f"{badge} <b>{record.home_name} v {record.away_name}</b>\n"  # type: ignore[attr-defined]
        f"{record.kickoff:%a %H:%M} · {record.competition or 'Unknown league'}"  # type: ignore[attr-defined]
    )
    if not one_x_two:
        reason = getattr(record, "unavailable_reason", None)
        return f"{header}\nNo analysis: {reason or 'insufficient data'}"

    if list(getattr(record, "components_used", []) or []) == ["market"]:
        market_line = (
            f"{header}\n"
            f"Market  {_pct(one_x_two.get('Home'))} / "
            f"{_pct(one_x_two.get('Draw'))} / {_pct(one_x_two.get('Away'))}"
            "\n<i>bookmaker view — our models could not run</i>"
        )
        # A strongest angle is still worth naming here, but it is the market's
        # angle, not ours, and saying so is the difference between informing a
        # reader and relaying a price back to them as if it were analysis.
        angle = strongest_angle(record)
        if angle is not None:
            market_line += f"\n📊 Market's strongest: {angle[0]}"
        return market_line

    parts = [
        header,
        f"1X2  {_pct(one_x_two.get('Home'))} / {_pct(one_x_two.get('Draw'))}"
        f" / {_pct(one_x_two.get('Away'))}",
    ]
    xg_home = getattr(record, "expected_home_goals", None)
    xg_away = getattr(record, "expected_away_goals", None)
    if xg_home is not None:
        parts.append(f"xG   {xg_home} - {xg_away}")
    extras = []
    if goals.get("Over 2.5"):
        extras.append(f"O2.5 {_pct(goals.get('Over 2.5'))}")
    if btts.get("Yes"):
        extras.append(f"BTTS {_pct(btts.get('Yes'))}")
    if extras:
        parts.append("  ".join(extras))

    # The services that selected this fixture, if any were passed in. Shown on
    # the card itself because a user scanning the day should not have to open
    # every fixture to discover which ones our services actually chose.
    picks = (
        picks_by_fixture.get(getattr(record, "provider_event_id", ""), [])
        if picks_by_fixture
        else []
    )
    if picks:
        headline = picks[0]
        extra = f" +{len(picks) - 1} more" if len(picks) > 1 else ""
        parts.append(f"⭐ {headline[0]}: {headline[1]} {headline[2] * 100:.0f}%{extra}")
    else:
        # No service chose this fixture, so nothing here is presented as a
        # selection. Its strongest market is still named, marked differently,
        # so a reader can tell a ranked pick from a plain observation.
        angle = strongest_angle(record)
        if angle is not None:
            parts.append(f"📌 Strongest: {angle[0]}")
    return "\n".join(parts)


def format_stored_detail(record: object) -> str:
    """Render the full analysis of a stored fixture."""
    badge = COVERAGE_BADGE.get(getattr(record, "coverage", ""), "⚪")
    lines = [
        f"<b>{record.home_name} v {record.away_name}</b>",  # type: ignore[attr-defined]
        f"{record.kickoff:%a %d %b, %H:%M} UTC",  # type: ignore[attr-defined]
    ]
    if record.competition:  # type: ignore[attr-defined]
        lines.append(record.competition)  # type: ignore[attr-defined]
    lines.append("")
    lines.append(f"<b>Coverage:</b> {badge} {record.coverage.replace('_', ' ')}")  # type: ignore[attr-defined]

    markets = getattr(record, "markets", {}) or {}
    if not markets:
        lines.append("")
        lines.append(
            getattr(record, "unavailable_reason", None)
            or "No analysis is available for this fixture."
        )
        lines.append("")
        lines.append(_stored_history(record))
        lines.append("")
        lines.append(FOOTER)
        return "\n".join(lines)

    used = getattr(record, "components_used", []) or []
    market_only = list(used) == ["market"]

    lines.append("")
    lines.append(MARKET_ONLY_NOTICE if market_only else EXPERIMENTAL_NOTICE)
    if market_only:
        reason = getattr(record, "unavailable_reason", None)
        if reason:
            lines.append("")
            lines.append(reason)

    for name in ("1X2", "Double chance", "Goals", "Both teams to score"):
        outcomes = markets.get(name)
        if not outcomes:
            continue
        lines.append("")
        lines.append(f"<b>{name}</b>")
        for label, value in outcomes.items():
            lines.append(f"{label}: {_pct(value)}")

    xg_home = getattr(record, "expected_home_goals", None)
    if xg_home is not None:
        lines.append("")
        lines.append("<b>Expected goals</b>")
        lines.append(f"{record.home_name}: {xg_home}")  # type: ignore[attr-defined]
        lines.append(f"{record.away_name}: {record.expected_away_goals}")  # type: ignore[attr-defined]

    lines.append("")
    lines.append(_stored_history(record))

    odds = getattr(record, "market_odds", None)
    if odds:
        lines.append("")
        lines.append("<b>Market</b>")
        lines.append(
            f"Odds: {odds.get('home')} / {odds.get('draw')} / {odds.get('away')} "
            "(bookmaker average)"
        )

    used = ", ".join(used) or "none"
    dropped = ", ".join(getattr(record, "components_dropped", []) or [])
    lines.append("")
    lines.append("<b>Model status</b>")
    lines.append(f"Under evaluation. Components used: {used}.")
    if dropped:
        lines.append(f"Excluded for insufficient data: {dropped}.")
    lines.append(f"Last computed: {record.computed_at:%d %b %H:%M} UTC.")  # type: ignore[attr-defined]

    lines.append("")
    lines.append("<b>Market assessment</b>")
    lines.append(
        "No qualified value opportunity.\n\n"
        "The value engine is disabled. Our models are well calibrated but have "
        "not demonstrated an edge over market prices in backtesting, so we do "
        "not publish betting selections."
        if odds
        else "No market assessment: no odds were available for this fixture."
    )
    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def _stored_history(record: object) -> str:
    """Summarise both sides' records from stored statistics."""
    rows = ["<b>Recent record (last 3 seasons on file)</b>"]
    for stats in (
        getattr(record, "home_stats", {}) or {},
        getattr(record, "away_stats", {}) or {},
    ):
        name = stats.get("name", "?")
        if not stats.get("resolved"):
            rows.append(f"{name}: no historical record")
            continue
        elo = f", Elo {stats['elo']:.0f}" if stats.get("elo") else ""
        rows.append(
            f"{name}: {stats.get('matches', 0)} matches, "
            f"{stats.get('scored_per_match', 0)} scored / "
            f"{stats.get('conceded_per_match', 0)} conceded per match, "
            f"{stats.get('points_per_match', 0)} pts/match{elo}"
        )
    return "\n".join(rows)


def format_performance(
    periods: Mapping[str, object], counts: Mapping[str, int] | None = None
) -> str:
    """Render the model's measured performance over time.

    Accuracy is shown but never leads. Favourites win roughly 60% of matches by
    definition, so a high figure is not evidence of skill; Brier against the
    market is.
    """
    lines = ["<b>Model performance</b>", ""]
    if counts:
        live = counts.get("live", 0)
        historical = counts.get("backfill", 0)
        lines.append(
            f"Live predictions settled: {live:,}. " f"Historical backtest: {historical:,}."
        )
        lines.append(
            "The two are reported separately and never averaged: the backtest "
            "was produced knowing which fixtures and leagues exist, so mixing "
            "them would misrepresent both."
        )
        lines.append("")
    for label, summary in periods.items():
        sample = getattr(summary, "sample", 0)
        if not sample:
            lines.append(f"<b>{label.title()}</b>: no settled predictions yet")
            lines.append("")
            continue

        accuracy = getattr(summary, "favourite_accuracy", None)
        brier = getattr(summary, "brier", None)
        market = getattr(summary, "market_brier", None)
        skill = getattr(summary, "brier_skill", None)

        lines.append(f"<b>{label.title()}</b> — {sample} predictions")
        if accuracy is not None:
            lines.append(f"Favourite won: {accuracy:.0%}")
        if brier is not None:
            row = f"Brier: {brier:.4f}"
            if market is not None:
                row += f" (market {market:.4f})"
            lines.append(row)
        if skill is not None:
            lines.append(f"Skill vs market: {skill:+.2%}")
        for name, key in (
            ("Over 2.5", "over_2_5_accuracy"),
            ("BTTS", "btts_accuracy"),
        ):
            value = getattr(summary, key, None)
            if value is not None:
                lines.append(f"{name} correct: {value:.0%}")
        lines.append("")

    all_time = periods.get("all time")
    if all_time is not None:
        lines.append("<b>Verdict</b>")
        lines.append(getattr(all_time, "verdict", ""))
    lines.append("")
    lines.append(
        "Every published analysis is settled automatically and kept "
        "permanently — good days and bad days alike."
    )
    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_breakdown(title: str, groups: Mapping[str, object]) -> str:
    """Render performance split by league, coverage or model version."""
    lines = [f"<b>{title}</b>", ""]
    if not groups:
        lines.append("No settled predictions yet.")
        return "\n".join(lines)

    for name, summary in groups.items():
        sample = getattr(summary, "sample", 0)
        if not sample:
            continue
        brier = getattr(summary, "brier", None)
        skill = getattr(summary, "brier_skill", None)
        row = f"<b>{name}</b> — {sample} predictions"
        if brier is not None:
            row += f", Brier {brier:.4f}"
        if skill is not None:
            row += f", skill {skill:+.2%}"
        lines.append(row)

    lines.append("")
    lines.append(
        "A sample under a few hundred cannot separate skill from luck, " "whichever way it points."
    )
    return "\n".join(lines)


LEAN_NOTICE = (
    "🧪 <b>Statistical leans are analysis, not betting advice.</b> They show "
    "the strongest conclusion our models can draw, not that a price is "
    "generous. Our models have not demonstrated an edge over market prices."
)


def format_lean_line(lean: object, index: int | None = None) -> str:
    """Render one statistical lean for a list."""
    badge = COVERAGE_BADGE.get(getattr(lean, "coverage", ""), "⚪")
    prefix = f"<b>{index}.</b> " if index else ""
    kickoff = getattr(lean, "kickoff", None)
    when = f"{kickoff:%a %H:%M}" if kickoff else ""
    return (
        f"{prefix}{badge} <b>{lean.home_name} v {lean.away_name}</b>\n"  # type: ignore[attr-defined]
        f"{when} · {getattr(lean, 'competition', None) or 'Unknown league'}\n"
        f"<b>{lean.market}: {lean.headline}</b>\n"  # type: ignore[attr-defined]
        f"<i>{lean.reason}</i>"  # type: ignore[attr-defined]
    )


def format_highlights(leans: list[object], total_fixtures: int) -> str:
    """Render the strongest leans across today's card."""
    lines = ["<b>⭐ Today's statistical highlights</b>", ""]
    if not leans:
        lines.append("No fixture today produced a conclusion strong enough to highlight.")
        lines.append("")
        lines.append(
            "That is a real answer, not an empty screen: an evenly balanced "
            "card has nothing worth singling out."
        )
        lines.append("")
        lines.append(FOOTER)
        return "\n".join(lines)

    lines.append(
        f"The strongest {len(leans)} of {total_fixtures} analysed fixtures, "
        "ranked by how much our analysis supports them."
    )
    lines.append("")
    for index, lean in enumerate(leans, start=1):
        lines.append(format_lean_line(lean, index))
        lines.append("")

    lines.append(LEAN_NOTICE)
    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_lean_detail(lean: object) -> str:
    """Render the score breakdown behind one lean."""
    lines = [
        f"<b>{lean.home_name} v {lean.away_name}</b>",  # type: ignore[attr-defined]
        f"{getattr(lean, 'competition', None) or 'Unknown league'}",
        "",
        f"<b>Statistical lean:</b> {lean.market} — {lean.headline}",  # type: ignore[attr-defined]
        "",
        f"<i>{lean.reason}</i>",  # type: ignore[attr-defined]
        "",
        "<b>How this was scored</b>",
        f"Overall: {lean.score:.2f} / 1.00",  # type: ignore[attr-defined]
        f"Confidence: {lean.confidence:.2f}",  # type: ignore[attr-defined]
        f"Coverage: {lean.coverage_score:.2f}",  # type: ignore[attr-defined]
        f"Data depth: {lean.data_quality:.2f}",  # type: ignore[attr-defined]
        f"Model agreement: {lean.stability:.2f}",  # type: ignore[attr-defined]
        f"Market agreement: {lean.market_agreement:.2f}",  # type: ignore[attr-defined]
        "",
        "Market agreement scores high, not low. Without a demonstrated edge, "
        "disagreeing sharply with the market is more likely to be our error "
        "than our insight.",
        "",
        LEAN_NOTICE,
        "",
        FOOTER,
    ]
    return "\n".join(lines)


def format_fixture_list(title: str, records: Sequence[object]) -> str:
    """Render a saved or recently-viewed list."""
    lines = [f"<b>{title}</b>", ""]
    for record in records[:12]:
        kickoff = getattr(record, "kickoff", None)
        when = f"{kickoff:%a %d %b, %H:%M}" if kickoff else "time unknown"
        lines.append(
            f"<b>{record.home_name} v {record.away_name}</b>\n"  # type: ignore[attr-defined]
            f"{when} · {getattr(record, 'competition', None) or 'Unknown league'}"
        )
        lines.append("")
    lines.append("Tap a fixture to reopen its analysis.")
    return "\n".join(lines)


PAGE_SIZE = 8
"""Fixtures per page.

Small enough that a page fits a phone screen without scrolling past the
buttons, which is what made the previous truncated list feel broken.
"""


def format_search_results(
    description: str,
    records: Sequence[object],
    market_key: str | None = None,
    page: int = 0,
) -> str:
    """Render one page of search results.

    Previously this listed twelve and said "narrow the search to see the rest",
    which left a user told there were twenty-one fixtures and shown eight with
    no way forward. Every fixture is now reachable by paging.
    """
    from app.services.queries import MARKETS_BY_KEY, probability_for

    definition = MARKETS_BY_KEY.get(market_key) if market_key else None
    total = len(records)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    visible = list(records)[start : start + PAGE_SIZE]

    lines = [
        "<b>Search results</b>",
        f"<i>{description}</i>",
        f"{total} fixture(s)" + (f" · page {page + 1} of {pages}" if pages > 1 else ""),
        "",
    ]

    for record in visible:
        badge = COVERAGE_BADGE.get(getattr(record, "coverage", ""), "⚪")
        kickoff = getattr(record, "kickoff", None)
        when = f"{kickoff:%a %H:%M}" if kickoff else ""
        row = (
            f"{badge} <b>{record.home_name} v {record.away_name}</b>\n"  # type: ignore[attr-defined]
            f"{when} · {getattr(record, 'competition', None) or 'Unknown league'}"
        )
        if definition is not None:
            probability = probability_for(record, definition.key)  # type: ignore[arg-type]
            if probability is not None:
                row += f"\n{definition.label}: {probability * 100:.0f}%"
        lines.append(row)
        lines.append("")

    if pages > 1:
        lines.append(
            f"Showing {start + 1}-{start + len(visible)} of {total}. " "Use the arrows below."
        )
        lines.append("")

    lines.append(EXPERIMENTAL_NOTICE)
    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def _rate(value: float | None) -> str:
    """Format a rate, or note that the sample is too small."""
    return f"{value * 100:.0f}%" if value is not None else "n/a"


def format_team_profile(profile: object) -> str:
    """Render a club's statistical profile.

    The reason to open QUANTSPORT rather than a bookmaker's app: counted
    history, not a price. Every figure here comes from matches that happened.
    """
    if not getattr(profile, "has_data", False):
        return (
            f"<b>{profile.name}</b>\n\n"  # type: ignore[attr-defined]
            "No matches on record for this club in the last three seasons."
        )

    overall = profile.overall  # type: ignore[attr-defined]
    home = profile.home  # type: ignore[attr-defined]
    away = profile.away  # type: ignore[attr-defined]

    lines = [
        f"<b>{profile.name}</b>",  # type: ignore[attr-defined]
        f"{getattr(profile, 'country', None) or ''}".strip(),
        "",
        f"<b>Last 3 seasons</b> — {overall.played} matches",
        f"{overall.won}W {overall.drawn}D {overall.lost}L"
        f" · {overall.points_per_game:.2f} pts/game",
        f"Goals {overall.scored}-{overall.conceded}" f" · {overall.goals_per_game:.2f} per game",
        "",
        "<b>How their matches go</b>",
        f"Over 1.5: {_rate(overall.rate(overall.over_1_5))}"
        f" · Over 2.5: {_rate(overall.rate(overall.over_2_5))}",
        f"Both teams score: {_rate(overall.rate(overall.both_scored))}",
        f"Clean sheets: {_rate(overall.rate(overall.clean_sheets))}"
        f" · Failed to score: {_rate(overall.rate(overall.failed_to_score))}",
        "",
        "<b>Home vs away</b>",
        f"Home: {home.points_per_game:.2f} pts/game"
        f" · {_rate(home.rate(home.over_2_5))} over 2.5"
        if home.played
        else "Home: no matches",
        f"Away: {away.points_per_game:.2f} pts/game"
        f" · {_rate(away.rate(away.over_2_5))} over 2.5"
        if away.played
        else "Away: no matches",
    ]

    advantage = getattr(profile, "home_advantage", None)
    if advantage is not None:
        verdict = (
            "much stronger at home"
            if advantage > 0.5
            else ("stronger at home" if advantage > 0.15 else "travels well")
        )
        lines.append(f"<i>{advantage:+.2f} pts/game at home — {verdict}.</i>")

    form = getattr(profile, "form_string", "")
    if form:
        lines.append("")
        lines.append(f"<b>Recent form</b> (oldest first)  {form}")
        for venue, opponent, scored, conceded, played in profile.recent_scorelines[:5]:  # type: ignore[attr-defined]
            lines.append(f"{played:%d %b} {venue}  v {opponent}  {scored}-{conceded}")

    competitions = getattr(profile, "competitions", [])
    if competitions:
        lines.append("")
        lines.append(f"<i>Competitions: {', '.join(competitions[:4])}</i>")

    lines.append("")
    lines.append("<i>Counted from matches on record — no model, no forecast.</i>")
    return "\n".join(lines)


def format_head_to_head(record: object) -> str:
    """Render the record between two clubs."""
    if not getattr(record, "has_data", False):
        return (
            f"<b>{record.home_name} v {record.away_name}</b>\n\n"  # type: ignore[attr-defined]
            "These clubs have not met in our records."
        )

    lines = [
        f"<b>{record.home_name} v {record.away_name}</b>",  # type: ignore[attr-defined]
        f"<b>{record.played} meetings on record</b>",  # type: ignore[attr-defined]
        "",
        f"{record.home_name}: {record.home_wins} wins",  # type: ignore[attr-defined]
        f"Draws: {record.draws}",  # type: ignore[attr-defined]
        f"{record.away_name}: {record.away_wins} wins",  # type: ignore[attr-defined]
        f"Goals: {record.home_goals}-{record.away_goals}",  # type: ignore[attr-defined]
    ]

    over = record.rate(record.over_2_5)  # type: ignore[attr-defined]
    btts = record.rate(record.both_scored)  # type: ignore[attr-defined]
    if over is not None:
        lines.append("")
        lines.append(f"Over 2.5 in {_rate(over)} of meetings")
        lines.append(f"Both scored in {_rate(btts)}")

    lines.append("")
    lines.append("<b>Recent meetings</b>")
    for played, venue, first, second, _ in record.meetings[:6]:  # type: ignore[attr-defined]
        lines.append(f"{played:%d %b %Y}  ({venue})  {first}-{second}")

    lines.append("")
    lines.append("<i>Counted from matches on record.</i>")
    return "\n".join(lines)


def format_league_profile(profile: object) -> str:
    """Render a competition's character."""
    if not getattr(profile, "has_data", False):
        return f"<b>{profile.name}</b>\n\nNo matches on record."  # type: ignore[attr-defined]

    lines = [
        f"<b>{profile.name}</b>",  # type: ignore[attr-defined]
        f"{profile.matches:,} matches over the last 3 seasons",  # type: ignore[attr-defined]
        "",
        f"<b>{profile.goals_per_game:.2f} goals per match</b>",  # type: ignore[attr-defined]
        "",
        "<b>How results fall</b>",
        f"Home win: {_rate(profile.rate(profile.home_wins))}",  # type: ignore[attr-defined]
        f"Draw: {_rate(profile.rate(profile.draws))}",  # type: ignore[attr-defined]
        f"Away win: {_rate(profile.rate(profile.away_wins))}",  # type: ignore[attr-defined]
        "",
        "<b>Goals markets</b>",
        f"Over 2.5: {_rate(profile.rate(profile.over_2_5))}",  # type: ignore[attr-defined]
        f"Both teams score: {_rate(profile.rate(profile.both_scored))}",  # type: ignore[attr-defined]
        "",
        "<i>Counted from matches on record — use it to judge whether a "
        "forecast for this league is unusual or ordinary.</i>",
    ]
    return "\n".join(lines)


def format_market_menu(counts: dict[str, int]) -> str:
    """Render the market explorer entry screen."""
    lines = [
        "<b>Markets</b>",
        "",
        "Pick a market to see every fixture ranked by it, rather than one "
        "daily list chosen for you.",
        "",
    ]
    if counts:
        lines.append("<b>On today's card</b>")
        for label, count in counts.items():
            lines.append(f"{label}: {count} fixture(s) above 55%")
        lines.append("")
    return "\n".join(lines)


HIGHLIGHT_METHOD = (
    "<b>What a Daily Highlight is</b>\n"
    "The highest-ranked qualifying analysis of the day, scored on how much "
    "information it carries against the outcome's normal frequency, plus data "
    "coverage, sample depth, agreement between our models, and agreement with "
    "the market.\n\n"
    "It is <b>not</b> the highest probability on the card, <b>not</b> a claim "
    "that the price is generous, and <b>not</b> a tip. If nothing qualifies, "
    "we publish nothing."
)


def format_highlight(selection: object, index: int | None = None) -> str:
    """Render one recorded highlight."""
    prefix = f"<b>{index}.</b> " if index else ""
    badge = COVERAGE_BADGE.get(getattr(selection, "coverage", ""), "⚪")
    status = getattr(selection, "status", "pending")
    mark = {"won": "✅", "lost": "❌", "void": "⚪", "pending": "⏳"}.get(status, "⏳")

    kickoff = getattr(selection, "kickoff", None)
    when = f"{kickoff:%a %d %b, %H:%M}" if kickoff else ""
    line = (
        f"{prefix}{mark} {badge} <b>{selection.home_name} v {selection.away_name}</b>\n"  # type: ignore[attr-defined]
        f"{when} · {getattr(selection, 'competition', None) or 'Unknown league'}\n"
        f"<b>{selection.market}: {selection.outcome} — "  # type: ignore[attr-defined]
        f"{selection.probability * 100:.0f}%</b>"  # type: ignore[attr-defined]
    )

    base = getattr(selection, "base_rate", None)
    if base:
        line += f"  <i>(normally {base * 100:.0f}%)</i>"

    if status in {"won", "lost"}:
        home_goals = getattr(selection, "home_goals", None)
        away_goals = getattr(selection, "away_goals", None)
        if home_goals is not None:
            line += f"\nFinal: {home_goals}-{away_goals} — {status.upper()}"
    return line


def format_daily_highlights(selections: list[object], fixtures_considered: int) -> str:
    """Render today's highlights, or say honestly that there are none."""
    if not selections:
        return (
            "<b>⭐ No Daily Highlight today</b>\n\n"
            "No fixture currently meets the qualification criteria.\n\n"
            "That is a real answer, not an empty screen. We do not create a "
            "highlight to fill the day — a feature that must produce something "
            "eventually produces something worthless.\n\n"
            "Try <b>Explore → Markets</b> to search the card yourself.\n\n"
            f"{HIGHLIGHT_METHOD}\n\n{FOOTER}"
        )

    lines = [
        "<b>⭐ Today's Daily Highlights</b>",
        f"{len(selections)} qualified from {fixtures_considered} analysed fixtures.",
        "",
    ]
    for index, selection in enumerate(selections, start=1):
        lines.append(format_highlight(selection, index))
        lines.append("")

    lines.append(LEAN_NOTICE)
    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_why(selection: object) -> str:
    """Explain what caused a highlight to qualify."""
    factors = getattr(selection, "factors", {}) or {}
    labels = {
        "informational_content": "Information carried",
        "coverage": "Data coverage",
        "data_depth": "Sample depth",
        "model_agreement": "Model agreement",
        "market_agreement": "Market agreement",
    }

    lines = [
        f"<b>Why {selection.home_name} v {selection.away_name}?</b>",  # type: ignore[attr-defined]
        "",
        f"<b>{selection.market}: {selection.outcome} — "  # type: ignore[attr-defined]
        f"{selection.probability * 100:.0f}%</b>",  # type: ignore[attr-defined]
        "",
        getattr(selection, "rationale", "") or "",
        "",
        "<b>Score breakdown</b>",
    ]
    for key, label in labels.items():
        value = factors.get(key)
        if value is None:
            continue
        bar = "█" * max(1, round(float(value) * 10))
        lines.append(f"{label}: {float(value):.2f}  {bar}")

    overall = factors.get("overall")
    if overall is not None:
        lines.append("")
        lines.append(f"<b>Overall: {float(overall):.2f} / 1.00</b>")

    lines.append("")
    lines.append(
        f"Recorded {selection.recorded_at:%d %b %H:%M} UTC, "  # type: ignore[attr-defined]
        f"before kickoff at {selection.kickoff:%H:%M}."  # type: ignore[attr-defined]
    )
    lines.append(f"Model version: {getattr(selection, 'model_version', 'unknown')}")
    lines.append("")
    lines.append(HIGHLIGHT_METHOD)
    return "\n".join(lines)


def format_track_record(records: list[object], source_note: str = "") -> str:
    """Render highlight performance with sample sizes throughout."""
    lines = ["<b>📋 QUANTSPORT track record</b>", ""]
    if source_note:
        lines.append(source_note)
        lines.append("")

    any_settled = False
    for record in records:
        settled = getattr(record, "settled", 0)
        if not settled:
            lines.append(f"<b>{record.label}</b>: no settled selections yet")  # type: ignore[attr-defined]
            continue

        any_settled = True
        rate = getattr(record, "strike_rate", None) or 0.0
        expected = getattr(record, "expected_rate", None)
        lines.append(
            f"<b>{record.label}</b>: {record.won}/{settled} ({rate:.1%})"  # type: ignore[attr-defined]
        )
        if expected:
            gap = rate - expected
            lines.append(f"   forecasts implied {expected:.1%} — {gap:+.1%} against expectation")
        pending = getattr(record, "pending", 0)
        if pending:
            lines.append(f"   {pending} still pending")

    if any_settled:
        last = records[-1]
        by_market = getattr(last, "by_market", {}) or {}
        if by_market:
            lines.append("")
            lines.append("<b>By market</b>")
            for market, (won, played) in sorted(by_market.items(), key=lambda item: -item[1][1]):
                lines.append(f"{market}: {won}/{played} ({won / played:.1%})")

        lines.append("")
        lines.append(
            "<i>Winning at roughly the rate the forecasts implied is "
            "calibration, not edge. Beating it consistently over hundreds of "
            "selections would be evidence of something more — we do not claim "
            "that.</i>"
        )

    lines.append("")
    lines.append("Every selection is timestamped before kickoff and never edited.")
    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_home(counts: dict[str, int], boards: dict[str, int]) -> str:
    """Render the home screen with live counts."""
    total = sum(counts.values())
    lines = [
        "<b>QUANTSPORT AI</b>",
        "<i>Football Intelligence, Quantified. · Ask the Data.</i>",
        "",
    ]
    if total:
        lines.append(f"<b>{total} fixtures analysed</b>")
        parts = []
        for grade, badge in (
            ("fully_modelled", "🟢"),
            ("partially_modelled", "🟡"),
            ("data_only", "🔵"),
            ("unsupported", "⚪"),
        ):
            count = counts.get(grade, 0)
            if count:
                parts.append(f"{badge} {count}")
        lines.append(" · ".join(parts))
    else:
        lines.append("No fixtures analysed yet — the scan runs every few hours.")

    lines.append("")
    if any(boards.values()):
        lines.append("<b>Today's boards</b>")
        for track, count in boards.items():
            label = TRACK_LABEL.get(track, track)
            lines.append(
                f"{label}: {count} selection(s)" if count else f"{label}: nothing qualified"
            )
    else:
        lines.append(
            "<b>No selections qualified today.</b> That is a real answer — we "
            "never manufacture one to fill the day."
        )

    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


TRACK_LABEL = {
    "banker": "🎯 Banker Board",
    "sharp": "🔥 Sharp Board",
    "pattern": "📊 Pattern Board",
}


def format_board(track: str, label: str, description: str, selections: list[object]) -> str:
    """Render one board."""
    lines = [f"<b>{label}</b>", f"<i>{description}</i>", ""]

    if not selections:
        lines.append(
            "Nothing qualified for this board today.\n\n"
            "We publish nothing rather than lowering the bar — a feature that "
            "must produce something eventually produces something worthless."
        )
        lines.append("")
        lines.append(FOOTER)
        return "\n".join(lines)

    for index, selection in enumerate(selections, start=1):
        lines.append(format_highlight(selection, index))
        lines.append("")

    if track == "banker":
        lines.append(
            "<i>Most likely is not best value. A 90% outcome priced at 90% is "
            "worth nothing to back.</i>"
        )
    elif track == "pattern":
        lines.append(
            "<i>Counted from the historical record. No model, no forecast — "
            "you can verify every figure by counting.</i>"
        )
    else:
        lines.append(LEAN_NOTICE)

    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_my_quantsport(record: object, followed: list[object], saved_count: int) -> str:
    """Render a user's personal dashboard.

    Separate from the global record: what one person chose to follow is not
    what QUANTSPORT published, and merging them would let selective following
    flatter the platform.
    """
    lines = ["<b>👤 My QUANTSPORT</b>", ""]

    settled = getattr(record, "settled", 0)
    if settled:
        rate = getattr(record, "strike_rate", None) or 0.0
        expected = getattr(record, "expected_rate", None)
        lines.append("<b>Selections you followed</b>")
        lines.append(f"{record.won}/{settled} ({rate:.1%})")  # type: ignore[attr-defined]
        if expected:
            lines.append(f"Forecasts implied {expected:.1%}")
        pending = getattr(record, "pending", 0)
        if pending:
            lines.append(f"{pending} still pending")
    else:
        lines.append(
            "You have not followed any selections yet. Tap ⭐ Follow on a "
            "board selection and its result will be tracked here."
        )

    if followed:
        lines.append("")
        lines.append("<b>Recent</b>")
        for selection in followed[:6]:
            lines.append(format_highlight(selection))
            lines.append("")

    lines.append("")
    lines.append(f"Saved fixtures: {saved_count}")
    lines.append("")
    lines.append(
        "<i>Your record, not ours. QUANTSPORT's published track record is " "under Performance.</i>"
    )
    return "\n".join(lines)


def format_admin_dashboard(snapshot: object) -> str:
    """Render the operator dashboard.

    Retention leads, not total users. Totals only ever rise and so always look
    like growth; the share of last week's users who came back is what says
    whether the product is worth opening twice.
    """
    lines = ["<b>📊 QUANTSPORT operations</b>", ""]

    returning = getattr(snapshot, "returning_rate", None)
    if returning is not None:
        lines.append(f"<b>Weekly retention: {returning:.0%}</b>")
        lines.append("<i>of last week's users who came back. The number that matters.</i>")
    else:
        lines.append("<b>Weekly retention:</b> too few users last week to measure.")
    lines.append("")

    lines.append("<b>Users</b>")
    lines.append(f"Total: {snapshot.total_users:,}")  # type: ignore[attr-defined]
    rate = getattr(snapshot, "acceptance_rate", None)
    if rate is not None:
        lines.append(f"Passed age gate: {snapshot.accepted_terms:,} ({rate:.0%})")  # type: ignore[attr-defined]
    lines.append(
        f"Active — today {snapshot.active_today} · "  # type: ignore[attr-defined]
        f"week {snapshot.active_week} · month {snapshot.active_month}"  # type: ignore[attr-defined]
    )
    lines.append(
        f"New — today {snapshot.new_today} · week {snapshot.new_week}"  # type: ignore[attr-defined]
    )
    lines.append("")

    lines.append("<b>Engagement</b>")
    lines.append(
        f"Fixture views — today {snapshot.views_today} · "  # type: ignore[attr-defined]
        f"week {snapshot.views_week} · all {snapshot.views_all:,}"  # type: ignore[attr-defined]
    )
    depth = getattr(snapshot, "views_per_active_user", None)
    if depth is not None:
        lines.append(f"Views per active user this week: {depth:.1f}")
    lines.append(
        f"Saved: {snapshot.saved_fixtures} · Following: {snapshot.follows}"  # type: ignore[attr-defined]
    )

    total_feedback = getattr(snapshot, "feedback_total", 0)
    if total_feedback:
        satisfaction = getattr(snapshot, "satisfaction", None)
        line = f"Feedback: 👍 {snapshot.feedback_up} 👎 {snapshot.feedback_down}"  # type: ignore[attr-defined]
        if satisfaction is not None:
            line += f" ({satisfaction:.0%} positive)"
        else:
            line += " (too few to rate)"
        lines.append(line)
    lines.append("")

    lines.append("<b>Content</b>")
    lines.append(f"Upcoming fixtures: {snapshot.fixtures_upcoming}")  # type: ignore[attr-defined]
    coverage = getattr(snapshot, "coverage", {}) or {}
    if coverage:
        parts = []
        for grade, badge in (
            ("fully_modelled", "🟢"),
            ("partially_modelled", "🟡"),
            ("data_only", "🔵"),
            ("unsupported", "⚪"),
        ):
            if coverage.get(grade):
                parts.append(f"{badge} {coverage[grade]}")
        lines.append(" · ".join(parts))
    lines.append(f"Highlights today: {snapshot.highlights_today}")  # type: ignore[attr-defined]

    last_scan = getattr(snapshot, "last_scan", None)
    if last_scan:
        lines.append(f"Last scan: {last_scan:%d %b %H:%M} UTC")
    lines.append("")

    daily = getattr(snapshot, "daily_active", []) or []
    if any(count for _, count in daily):
        lines.append("<b>Daily active (14 days)</b>")
        peak = max(count for _, count in daily) or 1
        for day, count in daily[-7:]:
            bar = "█" * max(0, round(count / peak * 12))
            lines.append(f"{day:%d %b}  {bar} {count}")
        lines.append("")

    competitions = getattr(snapshot, "top_competitions", []) or []
    if competitions:
        lines.append("<b>Most viewed competitions this week</b>")
        for name, count in competitions[:5]:
            lines.append(f"{name}: {count}")

    return "\n".join(lines)


RESULT_BADGE = {"won": "✅ WON", "lost": "❌ LOST", "void": "⚪ VOID", "pending": "⏳ PENDING"}

METHOD_NOTE = (
    "<i>Best means the highest-ranked qualifying selection under our published "
    "methodology — probability, model agreement, sample size and coverage — "
    "not simply the highest percentage.</i>"
)

EVIDENCE_NOTE = (
    "⚠️ Statistical analysis only. Our models are calibrated but have not been "
    "shown to beat bookmaker prices. No outcome is guaranteed. 18+."
)


def format_best_today(selections: list[object], snapshot: object | None) -> str:
    """Render the Best of Today screen."""
    lines = ["<b>🔥 BEST OF TODAY</b>", ""]

    if snapshot is not None:
        available = getattr(snapshot, "fixtures_available", 0)
        modelled = getattr(snapshot, "fixtures_modelled", 0)
        lines.append(f"<i>{modelled} of {available} fixtures modelled today.</i>")
        lines.append("")

    if not selections:
        lines.append(
            "<b>No qualifying selection today.</b>\n\n"
            "Nothing on today's card cleared the bar for any service. That is a "
            "real answer — we never publish a selection to fill a slot."
        )
        lines.append("")
        lines.append(EVIDENCE_NOTE)
        return "\n".join(lines)

    for selection in selections:
        lines.append(format_selection_card(selection))
        lines.append("")

    lines.append(METHOD_NOTE)
    lines.append("")
    lines.append(EVIDENCE_NOTE)
    return "\n".join(lines)


def format_selection_card(selection: object, show_service: bool = True) -> str:
    """Render one published selection, with its result when the match is over.

    The scoreline is the point of a historical card. A reader looking at
    yesterday wants to see what we said and what happened, side by side —
    without it they have to go and check the result themselves, which defeats
    the purpose of keeping a record at all.
    """
    badge = COVERAGE_BADGE.get(getattr(selection, "coverage", ""), "⚪")
    status = getattr(selection, "status", "pending")
    kickoff = getattr(selection, "kickoff", None)
    when = f"{kickoff:%H:%M}" if kickoff else ""

    home_goals = getattr(selection, "home_goals", None)
    away_goals = getattr(selection, "away_goals", None)
    finished = home_goals is not None and away_goals is not None

    head = f"<b>{selection.service_label}</b>\n" if show_service else ""  # type: ignore[attr-defined]

    # With a result, the scoreline sits between the teams so the match reads
    # the way a result always does.
    if finished:
        fixture = (
            f"<b>{selection.home_name} {home_goals}-{away_goals} "  # type: ignore[attr-defined]
            f"{selection.away_name}</b>"  # type: ignore[attr-defined]
        )
    else:
        fixture = f"<b>{selection.home_name} v {selection.away_name}</b>"  # type: ignore[attr-defined]

    card = (
        f"{head}"
        f"{badge} {fixture}\n"
        f"{when} · {getattr(selection, 'competition', None) or 'Unknown league'}\n"
        f"<b>{selection.outcome} — {selection.probability * 100:.0f}%</b>"  # type: ignore[attr-defined]
    )

    if status in {"won", "lost", "void"}:
        card += f"  {RESULT_BADGE.get(status, status)}"
    elif finished:
        # The match is over but settlement has not caught up. Saying so is
        # better than showing PENDING beside a final score, which reads as the
        # product not knowing something the reader can plainly see.
        card += "\n<i>awaiting settlement</i>"
    else:
        card += "  ⏳"

    return card


def format_why_selection(selection: object) -> str:
    """Explain a selection from its stored evidence only."""
    labels = {
        "probability": "Model probability",
        "margin": "Margin over threshold",
        "agreement": "Model agreement",
        "evidence": "Sample depth",
        "coverage": "Data coverage",
    }
    factors = getattr(selection, "factors", {}) or {}

    lines = [
        "<b>🔎 WHY THIS PICK</b>",
        "",
        f"<b>{selection.home_name} v {selection.away_name}</b>",  # type: ignore[attr-defined]
        f"{getattr(selection, 'competition', None) or 'Unknown league'}",
        "",
        f"<b>{selection.market}: {selection.outcome}</b>",  # type: ignore[attr-defined]
        f"Model probability: <b>{selection.probability * 100:.1f}%</b>",  # type: ignore[attr-defined]
        f"Coverage: {COVERAGE_BADGE.get(getattr(selection, 'coverage', ''), '⚪')} "
        f"{getattr(selection, 'coverage', '').replace('_', ' ')}",
        "",
    ]

    scored = [(labels[k], v) for k, v in factors.items() if k in labels]
    if scored:
        lines.append("<b>Ranking breakdown</b>")
        for label, value in scored:
            bar = "█" * max(1, round(float(value) * 10))
            lines.append(f"{label}: {float(value):.2f}  {bar}")
        lines.append("")

    components = getattr(selection, "components_used", []) or []
    if components:
        lines.append("<b>Models that ran</b>")
        lines.append(", ".join(str(c) for c in components))
        lines.append("")

    sample = getattr(selection, "sample_size", 0)
    if sample:
        lines.append(f"History behind the thinner side: {sample} matches")

    published = getattr(selection, "published_at", None)
    kickoff = getattr(selection, "kickoff", None)
    if published and kickoff:
        lines.append(f"Published {published:%d %b %H:%M} UTC, before kickoff at {kickoff:%H:%M}.")
    lines.append(f"Model version: {getattr(selection, 'model_version', 'unknown')}")

    status = getattr(selection, "status", "pending")
    if status != "pending":
        lines.append("")
        lines.append(f"Result: <b>{RESULT_BADGE.get(status, status)}</b>")

    lines.append("")
    lines.append(METHOD_NOTE)
    return "\n".join(lines)


TELEGRAM_LIMIT = 4096
"""Telegram's hard message limit, in characters.

Exceeding it is rejected outright rather than truncated, so anything rendering
an unbounded list has to bound it here.
"""

SAFE_LIMIT = 3800
"""What we render to.

Below the hard limit with room for a closing note, so a message that grows
slightly between rendering and sending cannot tip over.
"""


def fit(text: str, note: str = "") -> str:
    """Trim a message to something Telegram will accept.

    A last line of defence rather than the plan: screens should bound their own
    content. But a rejected message shows the user an error and tells them
    nothing, whereas a trimmed one still answers the question — so nothing is
    ever sent that could be refused outright.
    """
    if len(text) <= SAFE_LIMIT:
        return text

    tail = f"\n\n<i>{note}</i>" if note else ""
    budget = SAFE_LIMIT - len(tail)
    trimmed = text[:budget]

    # Cut at a line break so a tag or word is never left half-written.
    cut = trimmed.rfind("\n")
    if cut > budget * 0.6:
        trimmed = trimmed[:cut]
    return trimmed + tail


def format_history_day(view: object, limit: int = 12) -> str:
    """Render one historical day exactly as it was published.

    Led by the per-service tally, because that is the day's story. The
    selections beneath are the evidence for it.
    """
    day = getattr(view, "day", None)
    selections = list(getattr(view, "selections", []) or [])

    lines = [
        f"<b>📅 QUANTSPORT — {day:%A %d %B %Y}</b>" if day else "<b>📅 History</b>",
        "",
    ]

    if not selections:
        lines.append(
            "Nothing was published on this date. Either no fixture cleared a "
            "service threshold, or the day predates this record."
        )
        lines.append("")
        lines.append(EVIDENCE_NOTE)
        return "\n".join(lines)

    # Per-service tallies. A day summarised as one number hides that a service
    # can have a poor day while the card as a whole looks fine.
    tallies: dict[str, list[int]] = {}
    for selection in selections:
        label = getattr(selection, "service_label", "Unknown")
        tally = tallies.setdefault(label, [0, 0, 0])
        status = getattr(selection, "status", "pending")
        if status == "won":
            tally[0] += 1
            tally[1] += 1
        elif status == "lost":
            tally[1] += 1
        else:
            tally[2] += 1

    settled = sum(tally[1] for tally in tallies.values())
    won = sum(tally[0] for tally in tallies.values())

    if settled:
        lines.append(f"<b>{won}/{settled} settled selections won</b>")
        lines.append("")

    for label, (service_won, played, pending) in sorted(
        tallies.items(), key=lambda item: (-item[1][1], item[0])
    ):
        if played:
            lines.append(f"{label} — <b>{service_won}/{played}</b>")
        else:
            lines.append(f"{label} — {pending} awaiting results")
    lines.append("")

    for selection in selections[:limit]:
        lines.append(format_selection_card(selection))
        lines.append("")

    remaining = len(selections) - limit
    if remaining > 0:
        lines.append(
            f"<i>{remaining} more selection(s) that day. The full record is on " "the website.</i>"
        )
        lines.append("")

    lines.append("<i>Published before kickoff and never edited since.</i>")
    lines.append("")
    lines.append(EVIDENCE_NOTE)
    return "\n".join(lines)


def format_service_record(records: list[object]) -> str:
    """Render live performance per service."""
    lines = [
        "<b>📈 TRACK RECORD</b>",
        "",
        "<i>Live selections, published before kickoff. Rates are shown only "
        "once a service has enough settled selections to mean something.</i>",
        "",
    ]

    any_settled = False
    for record in records:
        settled = getattr(record, "settled", 0)
        label = getattr(record, "label", "")
        status = " · under observation" if getattr(record, "status", "") == "observed" else ""

        if not settled:
            pending = getattr(record, "pending", 0)
            lines.append(f"{label}{status}\n  no settled selections ({pending} pending)")
            continue

        any_settled = True
        if not getattr(record, "meaningful", False):
            lines.append(
                f"{label}{status}\n  {record.won}/{settled} — too few to rate yet"  # type: ignore[attr-defined]
            )
            continue

        actual = getattr(record, "actual_rate", None) or 0.0
        expected = getattr(record, "expected_rate", None) or 0.0
        gap = getattr(record, "gap", None) or 0.0
        lines.append(
            f"{label}{status}\n  <b>{record.won}/{settled} ({actual:.1%})</b> · "  # type: ignore[attr-defined]
            f"said {expected:.1%} · gap {gap:+.1%}"
        )

    if any_settled:
        lines.append("")
        lines.append(
            "<i>A gap near zero means the service knows itself. Winning at the "
            "rate it predicts is calibration, not profit — at market prices "
            "that still loses money.</i>"
        )

    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_service_menu(counts: dict[str, int], modelled: int, available: int) -> str:
    """Render the Best of Today service chooser."""
    lines = ["<b>🔥 BEST OF TODAY</b>", ""]
    if available:
        lines.append(f"<i>{modelled} of {available} fixtures modelled today.</i>")
        lines.append("")

    total = sum(counts.values())
    if not total:
        lines.append(
            "<b>No qualifying selection today.</b>\n\n"
            "Nothing on today's card cleared the bar for any service. That is a "
            "real answer — we never publish a selection to fill a slot."
        )
        lines.append("")
        lines.append(EVIDENCE_NOTE)
        return "\n".join(lines)

    lines.append(
        f"<b>{total} selections across {len(counts)} services.</b>\n\n"
        "Pick the market you care about. Each one opens its own ranked list, "
        "strongest first."
    )
    lines.append("")
    lines.append(METHOD_NOTE)
    lines.append("")
    lines.append(EVIDENCE_NOTE)
    return "\n".join(lines)


def format_service_list(label: str, selections: list[object], day_label: str = "today") -> str:
    """Render one service's ranked selections."""
    lines = [f"<b>{label}</b>", ""]

    if not selections:
        lines.append(
            f"No fixture cleared this service's threshold {day_label}.\n\n"
            "Nothing is published to fill the slot."
        )
        lines.append("")
        lines.append(EVIDENCE_NOTE)
        return "\n".join(lines)

    lines.append(f"<i>{len(selections)} qualifying, strongest first.</i>")
    lines.append("")

    for index, selection in enumerate(selections, start=1):
        badge = COVERAGE_BADGE.get(getattr(selection, "coverage", ""), "⚪")
        kickoff = getattr(selection, "kickoff", None)
        when = f"{kickoff:%H:%M}" if kickoff else ""
        status = getattr(selection, "status", "pending")

        entry = (
            f"<b>{index}.</b> {badge} <b>{selection.home_name} v "  # type: ignore[attr-defined]
            f"{selection.away_name}</b>\n"  # type: ignore[attr-defined]
            f"{when} · {getattr(selection, 'competition', None) or 'Unknown league'}\n"
            f"<b>{selection.outcome} — {selection.probability * 100:.0f}%</b>"  # type: ignore[attr-defined]
        )
        if status != "pending":
            home_goals = getattr(selection, "home_goals", None)
            score = (
                f" ({home_goals}-{getattr(selection, 'away_goals', '')})"
                if home_goals is not None
                else ""
            )
            entry += f"\n{RESULT_BADGE.get(status, status)}{score}"
        lines.append(entry)
        lines.append("")

    lines.append(
        "<i>Ranked by our methodology, so strength falls as you go down the "
        "list. The tenth is genuinely weaker than the first.</i>"
    )
    lines.append("")
    lines.append(EVIDENCE_NOTE)
    return "\n".join(lines)


def format_fixture_picks(picks: list[tuple[str, str, float]]) -> str:
    """Render the services that selected a fixture.

    Shown inside the fixture card so a user reading an analysis can see which
    of our services reached a conclusion about it, rather than having to work
    backwards from the market lists.
    """
    if not picks:
        return (
            "\n\n<b>Recommended picks</b>\n"
            "No service selected this fixture today. Its numbers did not clear "
            "any service threshold."
        )

    lines = ["", "", "<b>Recommended picks</b>"]
    for label, outcome, probability in picks:
        lines.append(f"{label}: <b>{outcome} — {probability * 100:.0f}%</b>")
    lines.append(
        "<i>Published before kickoff and tracked. Open Best of Today to see "
        "where each sits in its ranking.</i>"
    )
    return "\n".join(lines)


SPORT_PICKER = (
    "<b>QUANTSPORT AI</b>\n"
    "<i>Football Intelligence, Quantified.</i>\n\n"
    "Choose a sport to begin.\n\n"
    "⚽ <b>Football</b>\n"
    "113,000 matches · 38 competitions · 18 daily services\n"
    "Live now.\n\n"
    "🏀 <b>Basketball</b>\n"
    "19,000 NBA games · model built and validated\n"
    "Opens with the season in late October.\n\n"
    "<i>Every number we publish is timestamped before kickoff and settled "
    "afterwards, wins and losses alike.</i>"
)

BASKETBALL_STATUS = (
    "<b>🏀 BASKETBALL — not yet live</b>\n\n"
    "The engine is built and tested. What is missing is the season: the NBA "
    "does not start until late October, so there would be nothing to show you "
    "today.\n\n"
    "<b>What exists</b>\n"
    "• 19,164 NBA games ingested, 2011 to 2026\n"
    "• A scoring model built on margin and total, fitted from real residuals\n"
    "• Validation against genuine closing moneyline, spread and total prices\n\n"
    "<b>What we found</b>\n"
    "On seasons up to 2022 the model cleared the betting break-even on spreads "
    "and totals. Tested on 2022 to 2026 — seasons it had never seen — that "
    "advantage disappeared, and its accuracy degraded in every one of those "
    "four years.\n\n"
    "So basketball will ship the way football did: calibrated probabilities, "
    "published and tracked, with no claim of an edge we cannot demonstrate.\n\n"
    "<i>We would rather tell you this than sell you the first result.</i>"
)

HOW_IT_WORKS = (
    "<b>📖 HOW TO USE QUANTSPORT</b>\n\n"
    "<b>1. Start with the market you care about</b>\n"
    "Open <b>🔥 Best of today</b> and pick a service — home wins, over 1.5, "
    "home or BTTS. Each opens its own list, ranked strongest first. The tenth "
    "entry is genuinely weaker than the first; that ordering is information, "
    "not decoration.\n\n"
    "<b>2. Read the percentage as a rate, not a promise</b>\n"
    "A 70% selection loses three times in ten. That is not the model failing — "
    "it is the model being right. A run of losses only means something when it "
    "is far worse than the rate over a large number of selections.\n\n"
    "<b>3. Check the evidence before you trust a number</b>\n"
    "Every selection has <b>🔎 Why this pick</b>: the ranking breakdown, which "
    "models ran, how much history sits behind it, and when it was published. "
    "If the sample is thin, the number deserves less weight.\n\n"
    "<b>4. Use the coverage grade</b>\n"
    "🟢 every model ran · 🟡 some were dropped for thin data · "
    "🔵 bookmaker view only · ⚪ not enough to say anything. "
    "Treat 🔵 as information about the market, not about our models.\n\n"
    "<b>5. Judge us on the record, not on today</b>\n"
    "<b>📈 Track record</b> shows every service's actual rate against what it "
    "predicted. The gap is the number that matters. Near zero means the "
    "service knows itself. Consistently negative means it promises more than "
    "it delivers, and we withhold services that do.\n\n"
    "<b>6. Verify the history yourself</b>\n"
    "<b>📚 History</b> shows exactly what was published on any past date, "
    "including the losses. Nothing is edited after kickoff.\n\n"
    "<b>What QUANTSPORT is not</b>\n"
    "It is not a tipping service and it does not claim to beat bookmakers. We "
    "have tested that directly, more than once, and our models do not. What "
    "they do is produce honest probabilities from 113,000 matches and show you "
    "the working.\n\n"
    "<b>Use it to inform your own judgement.</b> Anyone promising guaranteed "
    "winners is either mistaken or lying, and the maths does not care which.\n\n"
    "⚠️ 18+. Analytical information only. No outcome is guaranteed."
)


def format_basketball_status(competitions: list[object]) -> str:
    """Render every basketball competition and where it stands.

    Listing the unready leagues is deliberate. Showing only the one that works
    would imply the others were never considered; showing all of them with an
    honest status says what exists, what is coming, and what is blocking it.
    """
    lines = [
        "<b>🏀 BASKETBALL</b>",
        "",
        "<i>The model is built and tested. Each competition is added only once "
        "its own numbers have been measured.</i>",
        "",
    ]

    live = [c for c in competitions if getattr(c, "publishable", False)]
    pending = [c for c in competitions if not getattr(c, "publishable", False)]

    if live:
        lines.append("<b>Live</b>")
        for entry in live:
            lines.append(
                f"✅ <b>{entry.name}</b> · {entry.country}\n"  # type: ignore[attr-defined]
                f"   {entry.status_line}\n"  # type: ignore[attr-defined]
                f"   Season: {entry.season_months}"  # type: ignore[attr-defined]
            )
        lines.append("")

    if pending:
        lines.append("<b>Not yet published</b>")
        for entry in pending:
            lines.append(
                f"⏳ <b>{entry.name}</b> · {entry.country}\n"  # type: ignore[attr-defined]
                f"   {entry.status_line}"  # type: ignore[attr-defined]
            )
        lines.append("")

    lines.append(
        "<b>Why not just switch them on?</b>\n"
        "An NBA game totals around 225 points; a WNBA or EuroLeague game around "
        "160. The model's structure carries across competitions but its numbers "
        "do not, so borrowing one league's settings for another would produce "
        "figures that look confident and are wrong.\n\n"
        "Each competition needs its own history, its own fitted parameters and "
        "its own measured calibration before it publishes anything."
    )
    lines.append("")
    lines.append(
        "<i>On the NBA: fitted on 2011-2022, tested on 2022-2026. Calibrated, "
        "with no demonstrated edge over closing prices. We will not claim one "
        "we cannot show.</i>"
    )
    return "\n".join(lines)


def format_basketball_today(
    fixtures: list[object], error: str | None, competitions: list[object]
) -> str:
    """Render today's basketball, separating what we see from what we can say.

    A fixture being visible and a fixture being modellable are different
    things, and the screen keeps them apart. Showing a game we cannot model
    with a probability attached would be the single most dishonest thing this
    product could do.
    """
    lines = ["<b>🏀 BASKETBALL TODAY</b>", ""]

    if error:
        lines.append(f"<b>No fixtures available.</b>\n{error}")
        lines.append("")
        lines.append(
            "<i>A basketball feed covers the leagues playing now — the WNBA, "
            "FIBA tournaments, the Australian NBL. It shows you the games. It "
            "does not, on its own, let us put a probability on them: that "
            "needs each league's own measured history.</i>"
        )
        lines.append("")
        lines.append(format_basketball_status(competitions))
        return "\n".join(lines)

    if not fixtures:
        lines.append("No basketball fixtures found for today in the competitions we " "recognise.")
        lines.append("")
        lines.append(format_basketball_status(competitions))
        return "\n".join(lines)

    modellable = [f for f in fixtures if getattr(f, "modellable", False)]
    watch_only = [f for f in fixtures if not getattr(f, "modellable", False)]

    lines.append(f"<i>{len(fixtures)} fixture(s) today · {len(modellable)} we can model.</i>")
    lines.append("")

    if modellable:
        lines.append("<b>Analysed</b>")
        for fixture in modellable[:12]:
            lines.append(_basketball_line(fixture, "🟢"))
        lines.append("")

    if watch_only:
        lines.append("<b>Listed only — no model for these competitions yet</b>")
        for fixture in watch_only[:15]:
            lines.append(_basketball_line(fixture, "⚪"))
        lines.append("")
        lines.append(
            "<i>These fixtures are real and so is the gap: their competitions "
            "have no measured parameters, so we show the game and say nothing "
            "about it. Borrowing the NBA's numbers would put confident "
            "figures on leagues that score sixty points fewer per game.</i>"
        )
        lines.append("")

    lines.append(EVIDENCE_NOTE)
    return "\n".join(lines)


def _basketball_line(fixture: object, badge: str) -> str:
    """Render one basketball fixture."""
    tip_off = getattr(fixture, "tip_off", None)
    when = f"{tip_off:%H:%M}" if tip_off else ""
    return (
        f"{badge} <b>{fixture.home_name} v {fixture.away_name}</b>\n"  # type: ignore[attr-defined]
        f"   {when} · {getattr(fixture, 'competition_name', 'Unknown')}"
    )


PAYWALL = (
    "<b>🔒 QUANTSPORT Pro</b>\n\n"
    "{feature} is part of Pro.\n\n"
    "<b>What Pro includes</b>\n"
    "🔥 Best of today — 18 services, ten ranked fixtures each\n"
    "📊 Market explorer — search any market across 38 competitions\n"
    "👥 Team intelligence — full club records from 113,000 matches\n"
    "⭐ Follow selections and track your own record\n\n"
    "<b>Always free</b>\n"
    "⚽ Today's analysis · 📚 History · 📈 Track record\n\n"
    "<i>The evidence stays open. You should be able to check what we published "
    "and how it turned out — including the losses — before deciding whether "
    "the rest is worth paying for.</i>"
)

TRIAL_OFFER = (
    "\n\n<b>7 days free</b>\nNo card, no commitment. Long enough to see a full "
    "weekend and watch selections settle."
)

TRIAL_USED = (
    "\n\n<i>Your trial has been used. Subscription options are coming shortly — "
    "we are completing payment setup.</i>"
)


def format_paywall(feature_label: str, trial_available: bool) -> str:
    """Render the paywall for a gated feature."""
    body = PAYWALL.format(feature=feature_label)
    return body + (TRIAL_OFFER if trial_available else TRIAL_USED)


def format_account(access: object, description: str) -> str:
    """Render a user's subscription state."""
    lines = ["<b>👤 YOUR ACCOUNT</b>", "", f"<b>Plan:</b> {description}", ""]

    days = getattr(access, "days_left", lambda: None)()
    if days is not None and getattr(access, "active", False):
        lines.append(f"Access continues for {days} more day(s).")
        lines.append("")

    if getattr(access, "active", False):
        lines.append("<b>You have</b>")
        lines.append(
            "🔥 Best of today · 📊 Market explorer · 👥 Team intelligence · " "⭐ Following"
        )
    else:
        lines.append("<b>You have</b>")
        lines.append("⚽ Today's analysis · 📚 History · 📈 Track record")
        lines.append("")
        lines.append(
            "<i>History and the track record stay open on every plan. Our "
            "record, including losses, is not something you should have to pay "
            "to inspect.</i>"
        )

    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


DIVERGENCE_NOTICE = (
    "<b>What this is — and is not</b>\n"
    "These are fixtures where our mathematics reaches a different conclusion "
    "from the bookmaker. That is all it is.\n\n"
    "It is <b>not</b> a value signal. We have tested whether our models beat "
    "closing prices — across nine seasons of football and eleven of basketball "
    "— and they do not. Where we have disagreed with the price before, the "
    "price was right.\n\n"
    "We show these because a real disagreement is worth seeing, and because "
    "the record will tell us over time whether they carry information. Today "
    "they are a curiosity with evidence attached."
)


def format_divergences(items: list[object]) -> str:
    """Render model-market disagreements.

    The market's own number sits beside ours on every line. A divergence shown
    without the price it diverges from invites a reader to assume we think we
    are right.
    """
    lines = ["<b>⚡ MODEL–MARKET DIVERGENCE</b>", ""]  # noqa: RUF001

    if not items:
        lines.append(
            "Our models and the market agree on today's card, within the "
            "threshold we consider meaningful.\n\n"
            "That is the usual state of things. Bookmakers are good at this, "
            "and a day with no material disagreement is not a day the product "
            "failed."
        )
        lines.append("")
        lines.append(EVIDENCE_NOTE)
        return "\n".join(lines)

    lines.append(
        f"<i>{len(items)} fixture(s) where we differ from the price by eight " "points or more.</i>"
    )
    lines.append("")

    for index, item in enumerate(items, start=1):
        badge = COVERAGE_BADGE.get(getattr(item, "coverage", ""), "⚪")
        kickoff = getattr(item, "kickoff", None)
        when = f"{kickoff:%H:%M}" if kickoff else ""

        lines.append(
            f"<b>{index}.</b> {badge} <b>{item.home_name} v {item.away_name}</b>\n"  # type: ignore[attr-defined]
            f"{when} · {getattr(item, 'competition', None) or 'Unknown league'}\n"
            f"<b>{item.outcome}</b> — "  # type: ignore[attr-defined]
            f"we say {item.model_probability * 100:.0f}% "  # type: ignore[attr-defined]
            f"({item.implied_odds_model:.2f}), "  # type: ignore[attr-defined]
            f"market says {item.market_probability * 100:.0f}% "  # type: ignore[attr-defined]
            f"({item.implied_odds_market:.2f})\n"  # type: ignore[attr-defined]
            f"<i>{item.gap * 100:+.0f} points · {item.sample} matches behind "  # type: ignore[attr-defined]
            "the thinner side</i>"
        )
        lines.append("")

    lines.append(DIVERGENCE_NOTICE)
    lines.append("")
    lines.append(EVIDENCE_NOTE)
    return "\n".join(lines)
