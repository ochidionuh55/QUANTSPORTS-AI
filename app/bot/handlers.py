"""Bot handlers.

Phase 3 scope: onboarding and orientation only. No analysis, no selections, no
booking codes — those arrive with their phases and are not hinted at here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.bot.formatting import (
    format_admin_dashboard,
    format_best_today,
    format_board,
    format_breakdown,
    format_fixture_list,
    format_highlight,
    format_history_day,
    format_home,
    format_league_profile,
    format_market_menu,
    format_my_quantsport,
    format_performance,
    format_search_results,
    format_service_record,
    format_stored_detail,
    format_summary_line,
    format_team_profile,
    format_track_record,
    format_why,
    format_why_selection,
)
from app.bot.keyboards import acceptance_keyboard, back_to_menu, main_menu
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.terms import (
    AGE_NOTICE,
    CONFIRM_CALLBACK_DATA,
    DECLINE_CALLBACK_DATA,
    DECLINED_MESSAGE,
    ESTIMATES_NOTICE,
    NO_GUARANTEE_NOTICE,
    RESPONSIBLE_USE_NOTICE,
    TERMS_VERSION,
    acceptance_prompt,
    reacceptance_prompt,
)
from app.database.models import (
    HighlightFollow,
    HighlightSelection,
    Team,
    User,
    UserFeedback,
)
from app.providers.base import OddsProvider
from app.providers.live import live_odds_provider
from app.services.activity import ActivityService
from app.services.aliases import AliasReviewService
from app.services.analytics import AnalyticsService
from app.services.boards import TRACK_DESCRIPTIONS, TRACK_LABELS
from app.services.daily_scan import AnalysisRepository
from app.services.highlights import (
    RECONSTRUCTED,
    HighlightService,
    TrackRecord,
)
from app.services.nl_query import parse as nl_parse
from app.services.nl_query import suggestions as nl_suggestions
from app.services.profiles import ProfileService
from app.services.queries import (
    MARKET_FILTERS,
    MARKETS_BY_KEY,
    FixtureQuery,
    FixtureQueryService,
    probability_for,
)
from app.services.selections import SelectionService
from app.services.settlement import PerformanceService
from app.services.user_service import UserService, has_valid_acceptance

logger = get_logger(__name__)

WELCOME = (
    "<b>QUANTSPORT AI</b>\n"
    "Mathematical intelligence for sports markets.\n\n"
    "Analysis features are still in development. Nothing is available to "
    "generate selections yet."
)

SUPPORTED_LEAGUES: dict[str, str] = {
    "E0": "Premier League",
    "E1": "Championship",
    "D1": "Bundesliga",
    "SP1": "La Liga",
    "I1": "Serie A",
    "F1": "Ligue 1",
}

NO_PROVIDER = (
    "Live fixtures are not configured on this deployment yet, so there is "
    "nothing to show for today. Historical analysis is unaffected."
)

HOW_IT_WORKS = (
    "<b>How it works</b>\n\n"
    "QUANTSPORT AI compares statistical estimates of match outcomes against "
    "market prices, and looks for cases where the two disagree by more than "
    "the model's measured error.\n\n"
    "The models are validated against historical data before any output is "
    "shown to users. Until that validation passes, no selections are produced "
    "at all — an unvalidated model produces noise, not edge.\n\n"
    "<b>What you get today</b>\n"
    "Match probabilities, expected goals and goals-market estimates for "
    "fixtures in our covered leagues, plus each side's recent record.\n\n"
    "<b>What you do not get</b>\n"
    "Betting selections. Our models are well calibrated, but backtesting "
    "across nine seasons shows they do not beat bookmaker prices. Publishing "
    "selections on that basis would be dishonest, so we do not.\n\n"
    "<b>Coverage grades</b>\n"
    "🟢 Fully modelled · 🟡 Partially modelled · 🔵 Data only · ⚪ Unsupported"
)


def _menu_text(user: User) -> str:
    """Return the main menu body for a user."""
    return (
        f"<b>Main menu</b>\n\n"
        f"Balance: {user.credits} credits\n"
        f"Plan: {user.plan}\n\n"
        "Choose an option below."
    )


async def handle_start(message: Message, user: User, session: object, settings: Settings) -> None:
    """Begin onboarding, or show the menu to an already-accepted user."""
    if has_valid_acceptance(user):
        await message.answer(
            f"{WELCOME}\n\n{_menu_text(user)}",
            reply_markup=main_menu(settings.features),
        )
        return

    prompt = (
        acceptance_prompt()
        if user.accepted_terms_version is None
        else reacceptance_prompt(user.accepted_terms_version)
    )
    await message.answer(f"{WELCOME}\n\n{prompt}", reply_markup=acceptance_keyboard())
    logger.info("gate.prompted", telegram_id=user.telegram_id)


async def handle_accept(
    callback: CallbackQuery, user: User, session: object, settings: Settings
) -> None:
    """Record acceptance and open the menu."""
    service = UserService(session)  # type: ignore[arg-type]
    await service.record_acceptance(user, version=TERMS_VERSION)

    await callback.answer("Confirmed.")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Thank you. Your confirmation has been recorded.\n\n{_menu_text(user)}",
            reply_markup=main_menu(settings.features),
        )


async def handle_decline(callback: CallbackQuery, user: User) -> None:
    """Acknowledge a declined confirmation without recording acceptance."""
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(DECLINED_MESSAGE)
    logger.info("gate.declined", telegram_id=user.telegram_id)


async def handle_menu(callback: CallbackQuery, user: User, settings: Settings) -> None:
    """Return to the main menu."""
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            _menu_text(user), reply_markup=main_menu(settings.features)
        )


async def handle_account(callback: CallbackQuery, user: User) -> None:
    """Show the account summary."""
    await callback.answer()
    accepted = (
        user.terms_accepted_at.strftime("%d %b %Y") if user.terms_accepted_at else "not recorded"
    )
    body = (
        "<b>My account</b>\n\n"
        f"Balance: {user.credits} credits\n"
        f"Plan: {user.plan}\n"
        f"Terms accepted: {accepted} (version {user.accepted_terms_version})\n\n"
        "Credits cannot be purchased yet."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(body, reply_markup=back_to_menu())


async def handle_how_it_works(callback: CallbackQuery) -> None:
    """Explain the approach without promising outcomes."""
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(HOW_IT_WORKS, reply_markup=back_to_menu())


async def handle_terms_button(callback: CallbackQuery) -> None:
    """Re-display the notices from the menu."""
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(_terms_body(), reply_markup=back_to_menu())


def _terms_body() -> str:
    """Return the standing terms and safety text."""
    return (
        f"<b>Terms and safety</b> (version {TERMS_VERSION})\n\n"
        f"{AGE_NOTICE}\n\n"
        f"{ESTIMATES_NOTICE}\n\n"
        f"{NO_GUARANTEE_NOTICE}\n\n"
        f"{RESPONSIBLE_USE_NOTICE}"
    )


async def handle_terms_command(message: Message) -> None:
    """Show the notices. Reachable before acceptance, by design."""
    await message.answer(_terms_body())


async def handle_help(message: Message, user: User) -> None:
    """List what currently exists. Reachable before acceptance, by design."""
    gated = "" if has_valid_acceptance(user) else "\n\nSend /start to begin."
    await message.answer(
        "<b>Available commands</b>\n\n"
        "/start - begin or open the menu\n"
        "/team - a club's full record, e.g. /team Arsenal\n"
        "/find - search fixtures, e.g. /find today's BTTS analysis\n"
        "/recent - fixtures you recently viewed\n"
        "/saved - fixtures you saved\n"
        "/terms - age, safety and terms notices\n"
        "/help - this message\n\n"
        "Analysis commands will appear here when those features are "
        f"released.{gated}"
    )


async def handle_today(
    callback: CallbackQuery, user: User, session: object, settings: Settings
) -> None:
    """Show every analysed fixture, read from storage.

    Nothing is computed here. The worker precomputes the card, so this is a
    single query no matter how many people open the bot at once.
    """
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    repository = AnalysisRepository(session)  # type: ignore[arg-type]
    records = await repository.upcoming(limit=25)

    if not records:
        provider = _live_provider(settings)
        await callback.message.edit_text(
            NO_PROVIDER
            if provider is None
            else (
                "No analysed fixtures yet.\n\n"
                "The scan runs every few hours and on worker start-up. If this "
                "deployment has just come up, check again in a minute."
            ),
            reply_markup=back_to_menu(),
        )
        return

    computed = await repository.last_computed()
    blocks = [
        "<b>Today's analysis</b>",
        f"{len(records)} fixtures · updated "
        + (f"{computed:%d %b %H:%M} UTC" if computed else "unknown"),
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for record in records:
        blocks.append(format_summary_line(record))
        blocks.append("")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{record.home_name} v {record.away_name}",
                    callback_data=f"analyse:{record.provider_event_id}",
                )
            ]
        )

    blocks.append("🟢 fully modelled · 🟡 partial · 🔵 data only · ⚪ unsupported")
    blocks.append("")
    blocks.append("Tap a fixture for the full breakdown.")
    buttons.append([InlineKeyboardButton(text="Back to menu", callback_data="menu:main")])

    # Telegram caps a message at 4096 characters, so a long card is trimmed
    # rather than silently failing to send.
    text = "\n".join(blocks)
    if len(text) > 3900:
        text = text[:3850].rsplit("\n", 1)[0] + "\n\n… list trimmed; use the buttons below."

    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons[:26])
    )


async def handle_analyse(
    callback: CallbackQuery, user: User, session: object, settings: Settings
) -> None:
    """Show the full analysis of one fixture."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    fixture_id = (callback.data or "").split(":", 1)[-1]
    record = await AnalysisRepository(session).get(fixture_id)  # type: ignore[arg-type]

    if record is None:
        await callback.message.edit_text(
            "That fixture is no longer in the current card. It may have "
            "finished, or the scan has moved on.",
            reply_markup=back_to_menu(),
        )
        return

    await callback.message.edit_text(
        format_stored_detail(record),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="👍 Useful", callback_data=f"fb:up:{fixture_id}"),
                    InlineKeyboardButton(
                        text="👎 Not useful", callback_data=f"fb:down:{fixture_id}"
                    ),
                ],
                [InlineKeyboardButton(text="⚽ Today's analysis", callback_data="menu:today")],
                [InlineKeyboardButton(text="Back to menu", callback_data="menu:main")],
            ]
        ),
    )


async def handle_feedback(callback: CallbackQuery, user: User, session: object) -> None:
    """Record a verdict on an analysis.

    Stored rather than logged: logs rotate away, and product feedback is only
    useful if it can be counted later. Append-only, so a user changing their
    mind adds a row instead of overwriting the first.
    """
    parts = (callback.data or "").split(":")
    verdict = parts[1] if len(parts) > 1 else "unknown"
    fixture = parts[2] if len(parts) > 2 else None

    session.add(  # type: ignore[attr-defined]
        UserFeedback(
            user_id=user.id,
            provider_event_id=fixture,
            feature="analysis",
            verdict=verdict,
        )
    )
    logger.info(
        "feedback.recorded",
        telegram_id=user.telegram_id,
        verdict=verdict,
        fixture=fixture,
    )
    await callback.answer("Thank you — that helps us decide what to build next.", show_alert=False)


async def handle_highlights(
    callback: CallbackQuery, user: User, session: object, settings: Settings
) -> None:
    """Show the three daily boards."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    service = HighlightService(session)  # type: ignore[arg-type]
    selections = await service.today()
    counts = {track: 0 for track in TRACK_LABELS}
    for selection in selections:
        counts[selection.track] = counts.get(selection.track, 0) + 1

    records = await AnalysisRepository(session).upcoming(limit=200)  # type: ignore[arg-type]
    coverage: dict[str, int] = {}
    for record in records:
        coverage[record.coverage] = coverage.get(record.coverage, 0) + 1

    rows = [
        [
            InlineKeyboardButton(
                text=f"{TRACK_LABELS[track]} ({counts.get(track, 0)})",
                callback_data=f"board:{track}",
            )
        ]
        for track in TRACK_LABELS
    ]
    rows.append(
        [
            InlineKeyboardButton(text="📋 Track record", callback_data="hl:record"),
            InlineKeyboardButton(text="🗓 History", callback_data="hl:history"),
        ]
    )
    rows.append([InlineKeyboardButton(text="👤 My QUANTSPORT", callback_data="menu:mine")])
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="menu:main")])

    await callback.message.edit_text(
        format_home(coverage, counts),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _back(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    """Build a keyboard, always ending at home."""
    keyboard = [list(row) for row in rows]
    keyboard.append([InlineKeyboardButton(text="🏠 Home", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def handle_best_today(callback: CallbackQuery, session: object) -> None:
    """Show today's published Best of the Day selections."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    service = SelectionService(session)  # type: ignore[arg-type]
    selections = await service.today()
    view = await service.day_view(datetime.now(UTC).date())

    rows: list[list[InlineKeyboardButton]] = []
    for selection in selections[:10]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔎 {selection.service_label}",
                    callback_data=f"sel:{selection.id}:today",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="📚 History", callback_data="hist:days"),
            InlineKeyboardButton(text="📈 Track record", callback_data="sel:record"),
        ]
    )

    await callback.message.edit_text(
        format_best_today(list(selections), view.snapshot), reply_markup=_back(*rows)
    )


async def handle_selection_detail(callback: CallbackQuery, session: object) -> None:
    """Show the evidence behind one published selection."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 3 or not parts[1].isdigit():
        return
    origin = parts[2]

    service = SelectionService(session)  # type: ignore[arg-type]
    selection = await service.selection(int(parts[1]))
    if selection is None:
        await callback.message.edit_text(
            "That selection is no longer available.", reply_markup=back_to_menu()
        )
        return

    # Back returns where the user came from, not the root menu.
    back = (
        [InlineKeyboardButton(text="⬅️ Back to today", callback_data="menu:best")]
        if origin == "today"
        else [InlineKeyboardButton(text="⬅️ Back to that day", callback_data=f"hist:day:{origin}")]
    )

    await callback.message.edit_text(format_why_selection(selection), reply_markup=_back(back))


async def handle_history_days(callback: CallbackQuery, session: object) -> None:
    """List the dates with published selections."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    days = await SelectionService(session).available_days(limit=30)  # type: ignore[arg-type]
    if not days:
        await callback.message.edit_text(
            "<b>📚 HISTORY</b>\n\nNothing has been published yet. Selections "
            "appear here the day after they are made.",
            reply_markup=_back(),
        )
        return

    rows = [
        [
            InlineKeyboardButton(
                text=f"{day:%a %d %b %Y}", callback_data=f"hist:day:{day.isoformat()}"
            )
        ]
        for day in days[:14]
    ]
    await callback.message.edit_text(
        "<b>📚 HISTORY</b>\n\nEvery selection below was published before "
        "kickoff and has not been edited since. Open any date to see exactly "
        "what QUANTSPORT said and what happened.",
        reply_markup=_back(*rows),
    )


async def handle_history_day(callback: CallbackQuery, session: object) -> None:
    """Show one historical day, read from storage."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    raw = (callback.data or "").split(":", 2)[-1]
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return

    service = SelectionService(session)  # type: ignore[arg-type]
    view = await service.day_view(day)
    days = await service.available_days(limit=60)

    rows: list[list[InlineKeyboardButton]] = []
    for selection in view.selections[:10]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔎 {selection.service_label}",
                    callback_data=f"sel:{selection.id}:{day.isoformat()}",
                )
            ]
        )

    # Day-to-day navigation across whatever exists, not a fixed window.
    ordered = sorted(days)
    if day in ordered:
        index = ordered.index(day)
        navigation: list[InlineKeyboardButton] = []
        if index > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="◀️ Previous",
                    callback_data=f"hist:day:{ordered[index - 1].isoformat()}",
                )
            )
        if index < len(ordered) - 1:
            navigation.append(
                InlineKeyboardButton(
                    text="Next ▶️",
                    callback_data=f"hist:day:{ordered[index + 1].isoformat()}",
                )
            )
        if navigation:
            rows.append(navigation)

    rows.append([InlineKeyboardButton(text="⬅️ All dates", callback_data="hist:days")])
    await callback.message.edit_text(format_history_day(view), reply_markup=_back(*rows))


async def handle_service_record(callback: CallbackQuery, session: object) -> None:
    """Show live performance for every published service."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    records = await SelectionService(session).track_record()  # type: ignore[arg-type]
    await callback.message.edit_text(
        format_service_record(list(records)),
        reply_markup=_back(
            [
                InlineKeyboardButton(text="⬅️ Back to today", callback_data="menu:best"),
                InlineKeyboardButton(text="📚 History", callback_data="hist:days"),
            ]
        ),
    )


async def handle_board(callback: CallbackQuery, session: object) -> None:
    """Show one of the three daily boards."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    track = (callback.data or "").split(":", 1)[-1]
    if track not in TRACK_LABELS:
        return

    service = HighlightService(session)  # type: ignore[arg-type]
    selections = await service.today(track=track)

    rows: list[list[InlineKeyboardButton]] = []
    for selection in selections:
        rows.append(
            [
                InlineKeyboardButton(text="⭐ Follow", callback_data=f"follow:{selection.id}"),
                InlineKeyboardButton(text="❓ Why", callback_data=f"why:{selection.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Back to boards", callback_data="menu:highlights")])
    rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="menu:main")])

    await callback.message.edit_text(
        format_board(
            track,
            TRACK_LABELS[track],
            TRACK_DESCRIPTIONS[track],
            list(selections),
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def handle_follow(callback: CallbackQuery, user: User, session: object) -> None:
    """Follow a selection so its result is tracked personally."""
    raw = (callback.data or "").split(":", 1)[-1]
    if not raw.isdigit():
        return

    highlight_id = int(raw)
    existing = await session.execute(  # type: ignore[attr-defined]
        select(HighlightFollow).where(
            HighlightFollow.user_id == user.id,
            HighlightFollow.highlight_id == highlight_id,
        )
    )
    found = existing.scalar_one_or_none()

    if found is not None:
        found.active = not found.active
        await callback.answer("Following again." if found.active else "Unfollowed.")
        return

    session.add(  # type: ignore[attr-defined]
        HighlightFollow(
            user_id=user.id,
            highlight_id=highlight_id,
            followed_at=datetime.now(UTC),
            active=True,
        )
    )
    await callback.answer("Following. See 👤 My QUANTSPORT for your record.")


async def handle_my_quantsport(callback: CallbackQuery, user: User, session: object) -> None:
    """Show the user's personal record, separate from the global one."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    follows = await session.execute(  # type: ignore[attr-defined]
        select(HighlightSelection)
        .join(
            HighlightFollow,
            HighlightFollow.highlight_id == HighlightSelection.id,
        )
        .where(HighlightFollow.user_id == user.id, HighlightFollow.active.is_(True))
        .order_by(HighlightSelection.kickoff.desc())
    )
    selections = list(follows.scalars().all())

    record = TrackRecord(label="Your record")
    probabilities: list[float] = []
    for selection in selections:
        if selection.status == "won":
            record.won += 1
        elif selection.status == "lost":
            record.lost += 1
        elif selection.status == "void":
            record.void += 1
        else:
            record.pending += 1
            continue
        probabilities.append(selection.probability)
    record.total = len(selections)
    if probabilities:
        record.average_probability = sum(probabilities) / len(probabilities)

    saved = await ActivityService(session).saved(user.id)  # type: ignore[arg-type]

    await callback.message.edit_text(
        format_my_quantsport(record, list(selections), len(saved)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 QUANTSPORT record", callback_data="hl:record")],
                [InlineKeyboardButton(text="🏠 Home", callback_data="menu:main")],
            ]
        ),
    )


async def handle_why(callback: CallbackQuery, session: object) -> None:
    """Explain what caused a highlight to qualify."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    raw = (callback.data or "").split(":", 1)[-1]
    if not raw.isdigit():
        return

    selection = await session.get(HighlightSelection, int(raw))  # type: ignore[attr-defined]
    if selection is None:
        await callback.message.edit_text(
            "That selection is no longer available.", reply_markup=back_to_menu()
        )
        return

    await callback.message.edit_text(
        format_why(selection),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Back to highlights", callback_data="menu:highlights"
                    )
                ],
                [InlineKeyboardButton(text="Back to menu", callback_data="menu:main")],
            ]
        ),
    )


async def handle_track_record(callback: CallbackQuery, session: object) -> None:
    """Show the published track record with sample sizes."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    service = HighlightService(session)  # type: ignore[arg-type]
    live = [
        await service.track_record("Last 7 days", days=7),
        await service.track_record("Last 30 days", days=30),
        await service.track_record("All time", days=None),
    ]
    settled = sum(record.settled for record in live)

    if settled:
        note = "Live selections, published before kickoff."
        records = live
    else:
        note = (
            "No live selections have settled yet, so this shows the "
            "<b>reconstructed</b> record: what our method would have chosen on "
            "past fixtures, scored the same way. Honest, but never published — "
            "so it is evidence about the method, not about the live product."
        )
        records = [
            await service.track_record("Last 30 days", days=30, source=RECONSTRUCTED),
            await service.track_record("Last 90 days", days=90, source=RECONSTRUCTED),
            await service.track_record("All time", days=None, source=RECONSTRUCTED),
        ]

    await callback.message.edit_text(
        format_track_record(list(records), note),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Back to highlights", callback_data="menu:highlights"
                    )
                ],
                [InlineKeyboardButton(text="Back to menu", callback_data="menu:main")],
            ]
        ),
    )


async def handle_highlight_history(callback: CallbackQuery, session: object) -> None:
    """Show recent highlight selections and how they finished."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    service = HighlightService(session)  # type: ignore[arg-type]
    selections = await service.history(days=7)
    source_note = ""
    if not selections:
        selections = await service.history(days=14, source=RECONSTRUCTED)
        source_note = (
            "<i>Reconstructed from historical forecasts — these were never "
            "published live.</i>\n\n"
        )

    if not selections:
        await callback.message.edit_text(
            "No highlight history yet.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ Back to highlights",
                            callback_data="menu:highlights",
                        )
                    ]
                ]
            ),
        )
        return

    lines = ["<b>🗓 Recent highlights</b>", "", source_note]
    current_day = None
    for selection in selections[:20]:
        if selection.selection_date != current_day:
            current_day = selection.selection_date
            lines.append(f"<b>{current_day:%A %d %b}</b>")
        lines.append(format_highlight(selection))
        lines.append("")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Track record", callback_data="hl:record")],
                [
                    InlineKeyboardButton(
                        text="⬅️ Back to highlights", callback_data="menu:highlights"
                    )
                ],
            ]
        ),
    )


async def handle_save(callback: CallbackQuery, user: User, session: object) -> None:
    """Toggle whether a fixture is saved."""
    fixture_id = (callback.data or "").split(":", 1)[-1]
    activity = ActivityService(session)  # type: ignore[arg-type]

    if await activity.is_saved(user.id, fixture_id):
        await activity.unsave(user.id, fixture_id)
        await callback.answer("Removed from saved.")
        return

    record = await AnalysisRepository(session).get(fixture_id)  # type: ignore[arg-type]
    if record is None:
        await callback.answer("That fixture is no longer available.")
        return

    await activity.save(user.id, record)
    await callback.answer("Saved. See /saved.")


async def handle_history(message: Message, user: User, session: object) -> None:
    """List fixtures the user recently opened."""
    views = await ActivityService(session).recent(user.id)  # type: ignore[arg-type]
    if not views:
        await message.answer(
            "No history yet. Open a fixture from Today's analysis and it will " "appear here."
        )
        return
    await message.answer(
        format_fixture_list("Recently viewed", views),
        reply_markup=_fixture_buttons(views),
    )


async def handle_saved(message: Message, user: User, session: object) -> None:
    """List fixtures the user saved."""
    saved = await ActivityService(session).saved(user.id)  # type: ignore[arg-type]
    if not saved:
        await message.answer(
            "Nothing saved yet. Open a fixture and tap the save button to keep " "it here."
        )
        return
    await message.answer(
        format_fixture_list("Saved fixtures", saved),
        reply_markup=_fixture_buttons(saved),
    )


async def handle_find(message: Message, user: User, session: object) -> None:
    """Search fixtures using a natural-language request.

    The text becomes a fixed set of filters over already-computed analyses. It
    cannot reach a model parameter or the value-detection flag, so no phrasing
    can request output the system is not permitted to produce.
    """
    request = (message.text or "").removeprefix("/find").strip()
    if not request:
        examples = "\n".join(f"  /find {s}" for s in nl_suggestions())
        await message.answer(
            "<b>Search fixtures</b>\n\nTell me what you are looking for:\n\n" f"{examples}"
        )
        return

    query = nl_parse(request)
    records = await FixtureQueryService(session).search(query)  # type: ignore[arg-type]

    if not records:
        await message.answer(
            f"Nothing matched: <i>{query.describe()}</i>\n\n"
            "Try a wider request, or /find on its own for examples."
        )
        return

    await message.answer(
        format_search_results(query.describe(), records, query.market),
        reply_markup=_fixture_buttons(records),
    )


def _fixture_buttons(records: Sequence[object]) -> InlineKeyboardMarkup:
    """Build tappable buttons for a list of fixtures."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{r.home_name} v {r.away_name}",  # type: ignore[attr-defined]
                callback_data=f"analyse:{r.provider_event_id}",  # type: ignore[attr-defined]
            )
        ]
        for r in records[:12]
    ]
    rows.append([InlineKeyboardButton(text="Back to menu", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def handle_explore(callback: CallbackQuery) -> None:
    """The hub for everything that is not today's card."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    await callback.message.edit_text(
        "<b>Explore</b>\n\n"
        "QUANTSPORT holds 113,000 matches across 38 competitions. Ask it a "
        "question rather than reading one list.\n\n"
        "<b>Markets</b> — every fixture ranked by the market you care about\n"
        "<b>Teams</b> — a club's full record, home and away splits, form\n"
        "<b>Competitions</b> — how a league actually behaves\n"
        "<b>Search</b> — plain language, e.g. <code>/find draws in France</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📈 Markets", callback_data="markets:menu"),
                    InlineKeyboardButton(text="👕 Teams", callback_data="explore:teams"),
                ],
                [
                    InlineKeyboardButton(text="🏆 Competitions", callback_data="explore:leagues"),
                    InlineKeyboardButton(text="🔎 Search", callback_data="explore:search"),
                ],
                [InlineKeyboardButton(text="Back to menu", callback_data="menu:main")],
            ]
        ),
    )


async def handle_markets_menu(callback: CallbackQuery, session: object) -> None:
    """List every market a user can browse by."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    service = FixtureQueryService(session)  # type: ignore[arg-type]
    counts: dict[str, int] = {}
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []

    for definition in MARKET_FILTERS:
        found = await service.search(
            FixtureQuery(market=definition.key, min_probability=0.55, limit=99)
        )
        if found:
            counts[definition.label] = len(found)
        pair.append(
            InlineKeyboardButton(
                text=f"{definition.label} ({len(found)})",
                callback_data=f"market:{definition.key}",
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="Back", callback_data="menu:explore")])

    await callback.message.edit_text(
        format_market_menu(counts),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def handle_market_browse(callback: CallbackQuery, session: object) -> None:
    """Show every fixture ranked by one market."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    key = (callback.data or "").split(":", 1)[-1]
    definition = MARKETS_BY_KEY.get(key)
    if definition is None:
        await callback.message.edit_text("Unknown market.", reply_markup=back_to_menu())
        return

    query = FixtureQuery(market=key, min_probability=0.50, limit=20)
    records = await FixtureQueryService(session).search(query)  # type: ignore[arg-type]
    records.sort(key=lambda r: probability_for(r, key) or 0.0, reverse=True)

    if not records:
        await callback.message.edit_text(
            f"No fixture on the current card reaches 50% for "
            f"{definition.label}.\n\nThat is a real answer: today's matches "
            "do not favour it.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Back to markets", callback_data="markets:menu")]
                ]
            ),
        )
        return

    await callback.message.edit_text(
        format_search_results(f"{definition.label}, strongest first", records, key),
        reply_markup=_fixture_buttons(records),
    )


async def handle_explore_teams(callback: CallbackQuery) -> None:
    """Prompt for a club name."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    await callback.message.edit_text(
        "<b>Teams</b>\n\n"
        "Send <code>/team Arsenal</code> for a club's full record: results, "
        "goals, home and away splits, over/under and BTTS rates, recent form "
        "and head-to-head.\n\n"
        "All counted from matches on record — no forecast involved.",
        reply_markup=back_to_menu(),
    )


async def handle_explore_search(callback: CallbackQuery) -> None:
    """Show search examples."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    examples = "\n".join(f"<code>/find {s}</code>" for s in nl_suggestions())
    await callback.message.edit_text(
        f"<b>Search</b>\n\nAsk in plain language:\n\n{examples}",
        reply_markup=back_to_menu(),
    )


async def handle_explore_leagues(callback: CallbackQuery, session: object) -> None:
    """List competitions with data, tappable for a profile."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    competitions = await ProfileService(session).competitions_with_data()  # type: ignore[arg-type]
    if not competitions:
        await callback.message.edit_text(
            "No historical data ingested yet.", reply_markup=back_to_menu()
        )
        return

    rows = [
        [InlineKeyboardButton(text=f"{name} ({count:,})", callback_data=f"league:{name[:50]}")]
        for name, count in competitions[:20]
    ]
    rows.append([InlineKeyboardButton(text="Back", callback_data="menu:explore")])

    await callback.message.edit_text(
        "<b>Competitions</b>\n\n"
        f"{len(competitions)} leagues on record. Tap one to see how it "
        "actually behaves — goals per match, how often it draws, how often "
        "both teams score.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def handle_league_profile(callback: CallbackQuery, session: object) -> None:
    """Show one competition's character."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    name = (callback.data or "").split(":", 1)[-1]
    profile = await ProfileService(session).league_profile(name)  # type: ignore[arg-type]
    await callback.message.edit_text(
        format_league_profile(profile),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Back to competitions", callback_data="explore:leagues"
                    )
                ],
                [InlineKeyboardButton(text="Back to menu", callback_data="menu:main")],
            ]
        ),
    )


async def handle_team(message: Message, session: object) -> None:
    """Show a club's statistical profile."""
    query = (message.text or "").removeprefix("/team").strip()
    if not query:
        await message.answer(
            "Usage: <code>/team Arsenal</code>\n\n"
            "Returns results, goals, home and away splits, over/under and "
            "BTTS rates, and recent form — all counted from matches on record."
        )
        return

    service = ProfileService(session)  # type: ignore[arg-type]
    matches = await service.find_teams(query)

    if not matches:
        await message.answer(
            f"No club matching '{query}' in our records. Try a shorter name, "
            "or the spelling used in your league."
        )
        return

    if len(matches) > 1:
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{team.canonical_name}" + (f" ({team.country})" if team.country else ""),
                    callback_data=f"team:{team.id}",
                )
            ]
            for team in matches[:8]
        ]
        await message.answer(
            f"<b>{len(matches)} clubs match '{query}'</b>\n\nWhich one?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return

    profile = await service.team_profile(matches[0].id)
    await message.answer(format_team_profile(profile))


async def handle_team_button(callback: CallbackQuery, session: object) -> None:
    """Show a club chosen from a disambiguation list."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    team_id = (callback.data or "").split(":", 1)[-1]
    if not team_id.isdigit():
        return

    profile = await ProfileService(session).team_profile(int(team_id))  # type: ignore[arg-type]
    if profile is None:
        await callback.message.edit_text(
            "That club is no longer on record.", reply_markup=back_to_menu()
        )
        return

    await callback.message.edit_text(format_team_profile(profile), reply_markup=back_to_menu())


async def handle_performance(callback: CallbackQuery, session: object) -> None:
    """Show measured performance over time."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    service = PerformanceService(session)  # type: ignore[arg-type]
    counts = await service.counts_by_source()
    periods = await service.periods(source="live")
    await callback.message.edit_text(
        format_performance(periods, counts),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="By league", callback_data="perf:league"),
                    InlineKeyboardButton(text="By coverage", callback_data="perf:coverage"),
                ],
                [InlineKeyboardButton(text="Back to menu", callback_data="menu:main")],
            ]
        ),
    )


async def handle_performance_breakdown(callback: CallbackQuery, session: object) -> None:
    """Show performance split by league or coverage grade."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    service = PerformanceService(session)  # type: ignore[arg-type]
    which = (callback.data or "").split(":")[-1]
    if which == "backfill":
        title = "Historical backtest by league"
        groups = await service.by_competition(source="backfill")
    elif which == "coverage":
        title, groups = "Performance by coverage", await service.by_coverage()
    else:
        title, groups = "Performance by league", await service.by_competition()

    await callback.message.edit_text(
        format_breakdown(title, groups),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Back to performance", callback_data="menu:performance"
                    )
                ],
                [InlineKeyboardButton(text="Back to menu", callback_data="menu:main")],
            ]
        ),
    )


async def handle_stats(message: Message, user: User, session: object) -> None:
    """Admin: operator dashboard."""
    if not user.is_admin:
        await message.answer("That command is not available.")
        return

    snapshot = await AnalyticsService(session).snapshot()  # type: ignore[arg-type]
    await message.answer(format_admin_dashboard(snapshot))


async def handle_review(message: Message, user: User, session: object) -> None:
    """Admin: list team names awaiting a mapping decision."""
    if not user.is_admin:
        await message.answer("That command is not available.")
        return

    service = AliasReviewService(session)  # type: ignore[arg-type]
    pending = await service.pending()
    if not pending:
        await message.answer("Nothing awaiting review.")
        return

    lines = ["<b>Aliases awaiting review</b>", ""]
    for alias in pending:
        team = await session.get(Team, alias.team_id)  # type: ignore[attr-defined]
        confidence = (
            f"{float(alias.match_confidence):.2f}" if alias.match_confidence is not None else "n/a"
        )
        lines.append(
            f"#{alias.id} <code>{alias.alias}</code> → "
            f"{team.canonical_name if team else '?'} (confidence {confidence})"
        )
    lines.append("")
    lines.append("Approve with /link &lt;feed name&gt; = &lt;canonical name&gt;")
    lines.append("Reject with /rejectalias &lt;id&gt;")
    await message.answer("\n".join(lines))


async def handle_link(message: Message, user: User, session: object) -> None:
    """Admin: map a feed name to a canonical team.

    Deliberately explicit rather than a fuzzy shortcut: an administrator naming
    both sides of the mapping is the human judgement the resolver refused to
    make on its own.
    """
    if not user.is_admin:
        await message.answer("That command is not available.")
        return

    payload = (message.text or "").removeprefix("/link").strip()
    if "=" not in payload:
        await message.answer(
            "Usage: /link Stoke City = Stoke\n\n"
            "Left side is the live feed's spelling, right side is the name in "
            "our historical data."
        )
        return

    feed_name, canonical = (part.strip() for part in payload.split("=", 1))
    service = AliasReviewService(session)  # type: ignore[arg-type]
    matches = await service.search_teams(canonical)
    exact = [t for t in matches if t.canonical_name.lower() == canonical.lower()]

    if not exact:
        options = ", ".join(t.canonical_name for t in matches[:5]) or "none"
        await message.answer(f"No canonical team named '{canonical}'.\n\nDid you mean: {options}")
        return

    alias = await service.link(feed_name, exact[0].id)
    await message.answer(
        f"Linked <code>{alias.alias}</code> → {exact[0].canonical_name}.\n"
        "It will resolve on the next scan."
    )


async def handle_reject_alias(message: Message, user: User, session: object) -> None:
    """Admin: reject a queued alias."""
    if not user.is_admin:
        await message.answer("That command is not available.")
        return

    payload = (message.text or "").removeprefix("/rejectalias").strip()
    if not payload.isdigit():
        await message.answer("Usage: /rejectalias 42")
        return

    try:
        await AliasReviewService(session).reject(int(payload))  # type: ignore[arg-type]
    except LookupError:
        await message.answer(f"No alias with id {payload}.")
        return
    await message.answer(f"Alias #{payload} rejected.")


async def handle_leagues(callback: CallbackQuery, session: object, settings: Settings) -> None:
    """Show coverage and research status per league."""
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    from sqlalchemy import func, select

    from app.database.models import Competition, HistoricalMatch

    rows = ["<b>Supported leagues</b>", ""]
    for code, name in SUPPORTED_LEAGUES.items():
        result = await session.execute(  # type: ignore[attr-defined]
            select(func.count())
            .select_from(HistoricalMatch)
            .join(Competition, HistoricalMatch.competition_id == Competition.id)
            .where(Competition.canonical_name == name)
        )
        count = int(result.scalar_one())
        badge = "🟢" if count > 1000 else ("🟡" if count else "⚪")
        rows.append(f"{badge} {name} ({code}) — {count:,} matches on file")

    rows.append("")
    rows.append(
        "All leagues are 🧪 experimental. Backtesting across nine seasons "
        "found our models well calibrated but not better than market prices, "
        "so no value selections are published for any league."
    )
    await callback.message.edit_text("\n".join(rows), reply_markup=back_to_menu())


def _live_provider(settings: Settings) -> OddsProvider | None:
    """Return the shared live odds provider, or ``None`` if unconfigured."""
    return live_odds_provider()


async def handle_unknown(message: Message, settings: Settings, user: User) -> None:
    """Catch anything unrecognised rather than leaving the user with silence."""
    await message.answer(
        "I did not understand that. Use the menu below or send /help.",
        reply_markup=main_menu(settings.features),
    )


def build_router() -> Router:
    """Build a fresh router with every Phase 3 handler registered.

    A factory rather than a module-level singleton: aiogram refuses to attach
    one router to two dispatchers, so a shared instance would make a second
    dispatcher — webhook mode, or a test — impossible to construct.

    Registration order is significant. ``handle_unknown`` matches any message
    and must therefore be registered last, or it would swallow every command.
    """
    router = Router(name="phase3")

    router.message.register(handle_start, CommandStart())
    router.message.register(handle_terms_command, Command("terms"))
    router.message.register(handle_help, Command("help"))

    router.callback_query.register(handle_accept, F.data == CONFIRM_CALLBACK_DATA)
    router.callback_query.register(handle_decline, F.data == DECLINE_CALLBACK_DATA)
    router.callback_query.register(handle_menu, F.data == "menu:main")
    router.callback_query.register(handle_account, F.data == "menu:account")
    router.callback_query.register(handle_how_it_works, F.data == "menu:how_it_works")
    router.callback_query.register(handle_terms_button, F.data == "menu:terms")
    router.callback_query.register(handle_today, F.data == "menu:today")
    router.callback_query.register(handle_leagues, F.data == "menu:leagues")
    router.callback_query.register(handle_analyse, F.data.startswith("analyse:"))
    router.callback_query.register(handle_feedback, F.data.startswith("fb:"))
    router.callback_query.register(handle_highlights, F.data == "menu:highlights")
    router.callback_query.register(handle_explore, F.data == "menu:explore")
    router.callback_query.register(handle_why, F.data.startswith("why:"))
    router.callback_query.register(handle_board, F.data.startswith("board:"))
    router.callback_query.register(handle_best_today, F.data == "menu:best")
    router.callback_query.register(handle_service_record, F.data.in_({"sel:record", "menu:record"}))
    router.callback_query.register(handle_history_days, F.data.in_({"hist:days", "menu:history"}))

    router.callback_query.register(handle_history_day, F.data.startswith("hist:day:"))
    router.callback_query.register(handle_selection_detail, F.data.startswith("sel:"))
    router.callback_query.register(handle_follow, F.data.startswith("follow:"))
    router.callback_query.register(handle_my_quantsport, F.data == "menu:mine")
    router.callback_query.register(handle_track_record, F.data == "hl:record")
    router.callback_query.register(handle_highlight_history, F.data == "hl:history")
    router.callback_query.register(
        handle_markets_menu, F.data.in_({"markets:menu", "menu:markets"})
    )
    router.callback_query.register(handle_market_browse, F.data.startswith("market:"))
    router.callback_query.register(
        handle_explore_teams, F.data.in_({"explore:teams", "menu:teams"})
    )
    router.callback_query.register(handle_explore_search, F.data == "explore:search")
    router.callback_query.register(
        handle_explore_leagues, F.data.in_({"explore:leagues", "menu:competitions"})
    )
    router.callback_query.register(handle_league_profile, F.data.startswith("league:"))
    router.callback_query.register(handle_team_button, F.data.startswith("team:"))
    router.message.register(handle_team, Command("team"))
    router.callback_query.register(handle_performance, F.data == "menu:performance")
    router.callback_query.register(handle_performance_breakdown, F.data.startswith("perf:"))

    router.message.register(handle_history, Command("history"))
    router.message.register(handle_history, Command("recent"))
    router.message.register(handle_saved, Command("saved"))
    router.message.register(handle_find, Command("find"))
    router.callback_query.register(handle_save, F.data.startswith("save:"))
    router.message.register(handle_stats, Command("stats"))
    router.message.register(handle_review, Command("review"))
    router.message.register(handle_link, Command("link"))
    router.message.register(handle_reject_alias, Command("rejectalias"))

    # Catch-all last.
    router.message.register(handle_unknown)

    return router
