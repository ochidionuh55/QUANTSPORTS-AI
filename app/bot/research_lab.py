"""Admin-only Telegram Research Lab.

A backstage surface for watching challenger models shadow the frozen production
champion on real upcoming fixtures. It is gated to ``user.is_admin`` and is
never added to the customer menu, so published QUANTSPORT selections and every
customer-facing surface are untouched.

The bot computes nothing here: every number is read from the research store via
:class:`app.research.lab.ResearchLab`. A fixture shows only the models that
legitimately stored a pre-kickoff prediction for it — challengers appear the
moment their shadow rows exist, and nothing is invented to fill a screen.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.database.models import User
from app.research.lab import CONTROL_LABEL, OUTCOMES, Fixture, ResearchLab

HEADER = "🧪 <b>QUANTSPORT RESEARCH LAB</b>"
DISCLAIMER = "<i>RESEARCH ONLY — does not affect published QUANTSPORT selections.</i>"
NOT_ADMIN = "That surface is not available."
MAX_CARDS = 8


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Today's Shadow Matches", callback_data="lab:today")],
            [InlineKeyboardButton(text="⚔️ Biggest Disagreements", callback_data="lab:disagree")],
            [InlineKeyboardButton(text="🧪 Experiment Status", callback_data="lab:experiments")],
            [InlineKeyboardButton(text="✅ Recent Settled Shadow", callback_data="lab:settled")],
        ]
    )


def _back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="« Research Lab", callback_data="lab:menu")]]
    )


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _delta(value: float) -> str:
    return f"{round(value * 100):+d}pp"


def _short(version: str) -> str:
    return version.replace("model-only-", "").upper()


def _card(fx: Fixture) -> str:
    lines = [f"<b>{fx.home_name} vs {fx.away_name}</b>"]
    c = fx.control
    lines.append(
        f"🏆 {CONTROL_LABEL}   "
        + " · ".join(f"{o[0].upper()} {_pct(c.get(o, 0.0))}" for o in OUTCOMES)
    )
    for version, probs in fx.challengers.items():
        d = fx.deltas(version)
        lines.append(
            f"🧪 {_short(version)}   "
            + " · ".join(f"{o[0].upper()} {_pct(probs.get(o, 0.0))}" for o in OUTCOMES)
        )
        lines.append("     Δ " + " · ".join(f"{o[0].upper()} {_delta(d[o])}" for o in OUTCOMES))
    if not fx.has_challenger:
        lines.append("     <i>no challenger prediction stored yet</i>")
    if fx.settled and fx.home_goals is not None:
        lines.append(f"     FT {fx.home_goals}-{fx.away_goals} · settled ✓")
    else:
        lines.append(f"     <i>SHADOW — NOT PRODUCTION</i> · KO {fx.kickoff:%H:%M} UTC")
    return "\n".join(lines)


def _page(title: str, fixtures: list[Fixture], empty: str) -> str:
    body = [HEADER, f"<b>{title}</b>", ""]
    if not fixtures:
        body.append(empty)
    else:
        shown = fixtures[:MAX_CARDS]
        body.append("\n\n".join(_card(f) for f in shown))
        if len(fixtures) > len(shown):
            body.append(f"\n… {len(fixtures) - len(shown)} more")
    body += ["", DISCLAIMER]
    return "\n".join(body)


async def _show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Edit the current message, tolerating an unchanged-content edit."""
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def _menu_text() -> str:
    return "\n".join(
        [
            HEADER,
            "",
            "Watch active challengers shadow the frozen champion "
            f"(<code>{CONTROL_LABEL}</code>) on real upcoming fixtures.",
            "",
            DISCLAIMER,
        ]
    )


async def handle_lab(message: Message, user: User, session: object) -> None:
    """``/lab`` — open the Research Lab. Admin only."""
    if not user.is_admin:
        await message.answer(NOT_ADMIN)
        return
    await message.answer(_menu_text(), reply_markup=_menu(), disable_web_page_preview=True)


async def _guard(callback: CallbackQuery, user: User) -> bool:
    if not user.is_admin:
        await callback.answer("Research Lab is admin-only.", show_alert=True)
        return False
    return True


async def handle_lab_menu(callback: CallbackQuery, user: User, session: object) -> None:
    if not await _guard(callback, user):
        return
    await _show(callback, _menu_text(), _menu())


async def handle_lab_today(callback: CallbackQuery, user: User, session: object) -> None:
    if not await _guard(callback, user):
        return
    fixtures = await ResearchLab(session).today()
    await _show(
        callback,
        _page("Today's Shadow Matches", fixtures, "No upcoming fixtures with a stored shadow yet."),
        _back(),
    )


async def handle_lab_disagree(callback: CallbackQuery, user: User, session: object) -> None:
    if not await _guard(callback, user):
        return
    fixtures = await ResearchLab(session).disagreements()
    await _show(
        callback,
        _page(
            "Biggest Model Disagreements",
            fixtures,
            "No challenger predictions stored yet — disagreements appear once a "
            "challenger is producing shadow forecasts.",
        ),
        _back(),
    )


async def handle_lab_settled(callback: CallbackQuery, user: User, session: object) -> None:
    if not await _guard(callback, user):
        return
    fixtures = await ResearchLab(session).recent_settled()
    await _show(
        callback,
        _page("Recent Settled Shadow Results", fixtures, "No settled shadow fixtures yet."),
        _back(),
    )


async def handle_lab_experiments(callback: CallbackQuery, user: User, session: object) -> None:
    if not await _guard(callback, user):
        return
    experiments = await ResearchLab(session).experiments()
    lines = [HEADER, "<b>Experiment Status</b>", ""]
    if not experiments:
        lines.append("No experiments registered yet.")
    else:
        for e in experiments:
            challenger = e.challenger_version or "—"
            lines.append(
                f"<b>{e.experiment_id}</b> · {e.status}\n"
                f"  {e.title}\n"
                f"  control <code>{e.control_version}</code> · challenger <code>{challenger}</code>"
            )
    lines += ["", DISCLAIMER]
    await _show(callback, "\n".join(lines), _back())


def register_research_lab(router: Router) -> None:
    """Attach the Research Lab command and callbacks to the bot router."""
    router.message.register(handle_lab, Command("lab"))
    router.callback_query.register(handle_lab_menu, F.data == "lab:menu")
    router.callback_query.register(handle_lab_today, F.data == "lab:today")
    router.callback_query.register(handle_lab_disagree, F.data == "lab:disagree")
    router.callback_query.register(handle_lab_settled, F.data == "lab:settled")
    router.callback_query.register(handle_lab_experiments, F.data == "lab:experiments")
