"""Subscription and entitlement tests.

Two rules matter most here. Access must expire on time without depending on a
background job, and the evidence — history and the track record — must stay
free, because a record a prospective customer cannot inspect is only a claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.subscriptions import (
    FREE_FEATURES,
    PAID_FEATURES,
    TRIAL_DAYS,
    Access,
    Feature,
    Tier,
    describe,
    extend,
    grant,
    resolve,
    start_trial,
)

NOW = datetime(2026, 3, 15, 12, tzinfo=UTC)


class TestEvidenceStaysFree:
    """The record must never sit behind the paywall."""

    def test_history_is_free(self) -> None:
        assert Feature.HISTORY in FREE_FEATURES

    def test_track_record_is_free(self) -> None:
        """Hiding the losses behind a paywall would make the honesty
        performative."""
        assert Feature.TRACK_RECORD in FREE_FEATURES

    def test_todays_analysis_is_free(self) -> None:
        assert Feature.TODAYS_ANALYSIS in FREE_FEATURES

    def test_free_user_reaches_every_free_feature(self) -> None:
        access = Access(Tier.FREE)
        for feature in FREE_FEATURES:
            assert access.allows(feature)

    def test_free_user_reaches_no_paid_feature(self) -> None:
        access = Access(Tier.FREE)
        for feature in PAID_FEATURES:
            assert not access.allows(feature)

    def test_expired_user_keeps_the_evidence(self) -> None:
        """Someone whose subscription lapsed can still check what we
        published."""
        access = Access(Tier.EXPIRED)
        assert access.allows(Feature.HISTORY)
        assert access.allows(Feature.TRACK_RECORD)
        assert not access.allows(Feature.BEST_OF_TODAY)

    def test_every_feature_is_classified(self) -> None:
        """A feature added later must be locked by default, not open by
        oversight."""
        assert set(Feature) == FREE_FEATURES | PAID_FEATURES
        assert not FREE_FEATURES & PAID_FEATURES


class TestTrial:
    """Seven days, once."""

    def test_trial_lasts_seven_days(self) -> None:
        access = start_trial(now=NOW)
        assert access.tier is Tier.TRIAL
        assert access.expires_at == NOW + timedelta(days=TRIAL_DAYS)

    def test_trial_opens_paid_features(self) -> None:
        access = start_trial(now=NOW)
        for feature in PAID_FEATURES:
            assert access.allows(feature)

    def test_days_left_counts_down(self) -> None:
        access = start_trial(now=NOW)
        assert access.days_left(NOW + timedelta(days=5)) == 2


class TestExpiry:
    """Access ends on time, without a job needing to run."""

    def test_lapsed_trial_resolves_to_expired(self) -> None:
        """Computing expiry rather than storing it means a failed background
        job cannot leave a subscription working indefinitely."""
        access = resolve("trial", NOW - timedelta(hours=1), now=NOW)
        assert access.tier is Tier.EXPIRED
        assert not access.active

    def test_lapsed_subscription_resolves_to_expired(self) -> None:
        access = resolve("pro", NOW - timedelta(days=1), now=NOW)
        assert access.tier is Tier.EXPIRED

    def test_live_subscription_stays_active(self) -> None:
        access = resolve("pro", NOW + timedelta(days=10), now=NOW)
        assert access.tier is Tier.PRO
        assert access.active

    def test_expiry_exactly_now_is_expired(self) -> None:
        assert resolve("pro", NOW, now=NOW).tier is Tier.EXPIRED

    def test_naive_timestamps_are_handled(self) -> None:
        """A stored timestamp without a timezone must not crash the check."""
        access = resolve("pro", datetime(2026, 3, 20, 12), now=NOW)
        assert access.tier is Tier.PRO

    def test_unknown_tier_falls_back_to_free(self) -> None:
        """An unrecognised value must never open paid features."""
        access = resolve("platinum-elite", None, now=NOW)
        assert access.tier is Tier.FREE
        assert not access.allows(Feature.BEST_OF_TODAY)


class TestGranting:
    """Manual grants, and renewals."""

    def test_grant_opens_access(self) -> None:
        access = grant(30, now=NOW)
        assert access.tier is Tier.PRO
        assert access.days_left(NOW) == 30

    def test_early_renewal_does_not_shorten(self) -> None:
        """Renewing with time left must add to it, not replace it."""
        existing = Access(Tier.PRO, expires_at=NOW + timedelta(days=20))
        renewed = extend(existing, 30, now=NOW)

        assert renewed.expires_at == NOW + timedelta(days=50)

    def test_renewing_after_expiry_starts_from_now(self) -> None:
        lapsed = Access(Tier.EXPIRED, expires_at=NOW - timedelta(days=10))
        renewed = extend(lapsed, 30, now=NOW)

        assert renewed.expires_at == NOW + timedelta(days=30)


class TestDescription:
    """What a user is told about their plan."""

    @pytest.mark.parametrize(
        ("access", "expected"),
        [
            (Access(Tier.FREE), "Free"),
            (Access(Tier.EXPIRED), "Expired"),
        ],
    )
    def test_plain_states(self, access: Access, expected: str) -> None:
        assert describe(access) == expected

    def test_trial_shows_days_remaining(self) -> None:
        access = start_trial(now=NOW)
        assert "day(s) remaining" in describe(access, now=NOW)

    def test_pro_shows_days_remaining(self) -> None:
        access = grant(30, now=NOW)
        assert "30 day(s)" in describe(access, now=NOW)
