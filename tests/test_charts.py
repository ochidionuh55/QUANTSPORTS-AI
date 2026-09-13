"""Chart rendering tests.

A chart is a claim made with a picture, which is harder to argue with than a
number and therefore easier to mislead with. These tests keep the drawing
honest: thin samples are refused rather than drawn, and nothing is invented to
make a line look better.
"""

from __future__ import annotations

from app.bot.charts import (
    MIN_SAMPLE_FOR_RATE,
    ServiceBar,
    calibration_chart,
    form_chart,
    plain,
    track_record_chart,
)


class TestTrackRecordChart:
    """Drawing live service performance."""

    def test_renders_a_png(self) -> None:
        chart = track_record_chart(
            [
                ServiceBar("Best Home Win", 0.688, 0.711, 1340),
                ServiceBar("Best Home or BTTS", 0.892, 0.887, 1422),
            ]
        )
        assert chart is not None
        assert chart[:4] == b"\x89PNG"

    def test_thin_samples_are_refused(self) -> None:
        """A confident-looking bar over nine results is a lie told with a
        picture."""
        chart = track_record_chart([ServiceBar("Best Draw", 0.80, 0.55, MIN_SAMPLE_FOR_RATE - 1)])
        assert chart is None

    def test_mixed_samples_drop_only_the_thin_ones(self) -> None:
        chart = track_record_chart(
            [
                ServiceBar("Solid", 0.70, 0.69, 400),
                ServiceBar("Thin", 0.95, 0.60, 3),
            ]
        )
        assert chart is not None

    def test_empty_input_returns_nothing(self) -> None:
        assert track_record_chart([]) is None


class TestCalibrationChart:
    """Predicted against observed."""

    def test_renders_with_enough_points(self) -> None:
        chart = calibration_chart([(0.55, 0.54, 300), (0.65, 0.66, 280), (0.75, 0.73, 240)])
        assert chart is not None
        assert chart[:4] == b"\x89PNG"

    def test_too_few_points_returns_nothing(self) -> None:
        """Three points is a shape, not a calibration curve."""
        assert calibration_chart([(0.55, 0.54, 300), (0.65, 0.66, 280)]) is None

    def test_thin_bands_are_excluded(self) -> None:
        assert calibration_chart([(0.5, 0.5, 5), (0.6, 0.6, 5), (0.7, 0.7, 5)]) is None


class TestFormChart:
    """A club's recent scoring."""

    def test_renders(self) -> None:
        chart = form_chart("Everton", ["W", "L", "W", "D", "L"], [2, 0, 3, 1, 0], [1, 2, 1, 1, 3])
        assert chart is not None
        assert chart[:4] == b"\x89PNG"

    def test_short_history_returns_nothing(self) -> None:
        assert form_chart("Everton", ["W", "L"], [2, 0], [1, 2]) is None


class TestLabels:
    """Chart text must render, not turn into empty boxes."""

    def test_emoji_are_stripped(self) -> None:
        """The chart font carries no emoji glyphs."""
        assert plain("🔥 Best Home or BTTS") == "Best Home or BTTS"
        assert plain("🏆 Best Home Win") == "Best Home Win"

    def test_plain_labels_are_untouched(self) -> None:
        assert plain("Best Over 1.5") == "Best Over 1.5"
