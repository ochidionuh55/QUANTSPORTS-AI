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
from app.research.lab import CONTROL_VERSION, OUTCOMES, Fixture, ResearchLab, label_for

HEADER = "🧪 <b>QUANTSPORT RESEARCH LAB</b>"
DISCLAIMER = "🔒 <b>RESEARCH ONLY — NOT PRODUCTION.</b> Does not affect published selections."
NOT_ADMIN = "That surface is not available."
CONTROL_LABEL = label_for(CONTROL_VERSION)
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


def _probs_line(icon: str, name: str, probs: dict[str, float]) -> str:
    cells = " · ".join(f"{o[0].upper()} {_pct(probs.get(o, 0.0))}" for o in OUTCOMES)
    return f"{icon} <b>{name}</b>   {cells}"


def _card(fx: Fixture) -> str:
    comp = f"  <i>{fx.competition}</i>" if fx.competition else ""
    lines = [f"<b>{fx.home_name} vs {fx.away_name}</b>{comp}"]
    lines.append(_probs_line("🏆", CONTROL_LABEL, fx.control))
    for version, probs in fx.challengers.items():
        d = fx.deltas(version)
        lines.append(_probs_line("🧪", label_for(version), probs))
        lines.append("     Diff  " + " · ".join(f"{o[0].upper()} {_delta(d[o])}" for o in OUTCOMES))
    if fx.settled and fx.home_goals is not None:
        result = f" ({fx.settled_outcome})" if fx.settled_outcome else ""
        lines.append(
            f"     FT {fx.home_goals}-{fx.away_goals}{result} · "
            f"recorded {fx.generated_at:%d %b %H:%M} before KO {fx.kickoff:%H:%M} UTC · settled ✓"
        )
    else:
        lines.append(
            f"     <i>SHADOW — NOT PRODUCTION</i> · "
            f"recorded {fx.generated_at:%H:%M} · KO {fx.kickoff:%d %b %H:%M} UTC"
        )
    return "\n".join(lines)


def _page(title: str, subtitle: str, fixtures: list[Fixture], empty: str) -> str:
    body = [HEADER, f"<b>{title}</b>", f"<i>{subtitle}</i>", ""]
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
            f"(<code>{CONTROL_LABEL}</code>) on real upcoming fixtures. "
            "Every prediction is recorded before kickoff and never edited after.",
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
        _page(
            "Today's Shadow Matches",
            f"{CONTROL_LABEL} vs challenger — recorded before kickoff",
            fixtures,
            "No upcoming fixtures with a stored shadow comparison yet.",
        ),
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
            "Sorted by largest probability gap vs control",
            fixtures,
            "No challenger comparisons stored yet — disagreements appear once a "
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
        _page(
            "Recent Settled Shadow Results",
            "Original pre-kickoff forecasts, shown against the eventual result",
            fixtures,
            "No settled shadow fixtures yet.",
        ),
        _back(),
    )


def _historical_line(metrics: dict[str, object]) -> str | None:
    delta = metrics.get("test_paired_delta")
    ci = metrics.get("test_delta_ci")
    if not isinstance(delta, int | float):
        return None
    line = f"  historical (held-out): paired Brier Δ {float(delta):+.4f}"
    if isinstance(ci, list) and len(ci) == 2:
        line += f" · 95% CI [{float(ci[0]):+.4f}, {float(ci[1]):+.4f}]"
    return line


async def handle_lab_experiments(callback: CallbackQuery, user: User, session: object) -> None:
    if not await _guard(callback, user):
        return
    lab = ResearchLab(session)
    experiments = await lab.experiments()
    counts = await lab.shadow_counts()
    lines = [HEADER, "<b>Experiment Status</b>", ""]
    if not experiments:
        lines.append("No experiments registered yet.")
    else:
        for e in experiments:
            challenger = e.challenger_version or "—"
            block = [
                f"<b>{e.experiment_id}</b> · {e.status}",
                f"  {e.title}",
                f"  control <code>{e.control_version}</code> · "
                f"challenger <code>{challenger}</code>",
            ]
            hist = _historical_line(e.metrics or {})
            if hist:
                block.append(hist)
            c = counts.get(e.challenger_version or "", {})
            if c:
                block.append(
                    f"  shadow: {c.get('fixtures', 0)} fixtures "
                    f"({c.get('settled', 0)} settled)"
                )
            lines.append("\n".join(block))
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
