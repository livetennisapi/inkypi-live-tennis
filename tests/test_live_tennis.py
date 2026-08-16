import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "live_tennis"))

from live_tennis import (  # noqa: E402
    is_break_point,
    match_view,
    fixture_view,
    filter_matches,
    filter_fixtures,
    fmt_start,
)

HERE = os.path.dirname(__file__)


def load(name):
    with open(os.path.join(HERE, name)) as f:
        return json.load(f)


def score(points, server=1, tiebreak=False):
    return {"points": points, "server": server, "is_tiebreak": tiebreak}


# ---------------------------------------------------------------------------
# Break-point derivation
# ---------------------------------------------------------------------------

class TestBreakPoint:
    def test_receiver_ad_is_bp(self):
        # p1 serves, p2 (receiver) has AD
        assert is_break_point(score(["40", "AD"], server=1))

    def test_server_ad_is_not_bp(self):
        assert not is_break_point(score(["AD", "40"], server=1))

    def test_receiver_40_vs_server_0(self):
        assert is_break_point(score(["0", "40"], server=1))

    def test_receiver_40_vs_server_15(self):
        assert is_break_point(score(["15", "40"], server=1))

    def test_receiver_40_vs_server_30(self):
        assert is_break_point(score(["30", "40"], server=1))

    def test_deuce_is_not_bp(self):
        assert not is_break_point(score(["40", "40"], server=1))

    def test_server_leading_is_not_bp(self):
        assert not is_break_point(score(["40", "30"], server=1))

    def test_server_2_receiver_is_p1(self):
        assert is_break_point(score(["40", "30"], server=2))
        assert not is_break_point(score(["30", "40"], server=2))

    def test_never_in_tiebreak(self):
        # A tiebreak has set points, not break points — even "6-5 shaped"
        # numeric points must never flag BP.
        assert not is_break_point(score(["6", "5"], server=2, tiebreak=True))
        assert not is_break_point(score(["40", "AD"], server=1,
                                        tiebreak=True))

    def test_null_points_are_not_bp(self):
        # The schema allows null entries in points (observed live on
        # completed matches).
        assert not is_break_point(score([None, None], server=1))
        assert not is_break_point(score(["30", None], server=1))

    def test_no_server_is_not_bp(self):
        assert not is_break_point(score(["0", "40"], server=None))

    def test_empty_or_missing_score(self):
        assert not is_break_point(None)
        assert not is_break_point({})
        assert not is_break_point({"points": [], "server": 1})


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

class TestMatchParsing:
    def setup_method(self):
        self.payload = load("sample_live.json")
        self.views = [match_view(m) for m in self.payload["data"]]

    def test_basic_fields(self):
        v = self.views[0]
        assert v["tournament"] == "Cincinnati Open"
        assert v["tour"] == "atp"
        assert v["p1"]["name"] == "Alcaraz C."
        assert v["p1"]["ranking"] == 2
        assert v["games_p1"] == [6, 4, 3]
        assert v["games_p2"] == [3, 6, 4]
        assert v["points_p1"] == "30"
        assert v["points_p2"] == "40"
        assert v["server"] == 1

    def test_bp_marker_on_receiver(self):
        # match 101: p1 serves at 30-40 -> BP for p2
        v = self.views[0]
        assert v["break_point"] is True
        assert v["bp_player"] == 2
        # match 103: p2 serves, receiver (p1) has AD -> BP for p1
        v = self.views[2]
        assert v["break_point"] is True
        assert v["bp_player"] == 1

    def test_tiebreak_never_bp(self):
        v = self.views[1]
        assert v["is_tiebreak"] is True
        assert v["break_point"] is False
        assert v["bp_player"] is None

    def test_null_ranking_survives(self):
        assert self.views[2]["p2"]["ranking"] is None

    def test_null_score_match(self):
        v = match_view({"id": 1, "players": {}})
        assert v["games_p1"] == []
        assert v["points_p1"] is None
        assert v["server"] is None
        assert v["break_point"] is False
        assert v["p1"]["name"] == "Unknown"

    def test_player_filter(self):
        hits = filter_matches(self.views, "sinner")
        assert [h["id"] for h in hits] == [101]
        hits = filter_matches(self.views, "Swiatek, gadecki")
        assert [h["id"] for h in hits] == [102, 103]
        assert filter_matches(self.views, "") == self.views
        assert filter_matches(self.views, "federer") == []


class TestFixtureParsing:
    def setup_method(self):
        self.payload = load("sample_fixtures.json")
        self.views = [fixture_view(f) for f in self.payload["data"]]

    def test_basic_fields(self):
        v = self.views[0]
        assert v["p1"] == "Alcaraz C."
        assert v["p2"] == "Fritz T."
        assert v["tournament"] == "Winston-Salem Open"
        assert v["start_time"] == "2026-08-17T15:00:00Z"

    def test_fmt_start_with_time(self):
        assert fmt_start(self.views[0]) == "Mon 15:00"

    def test_fmt_start_date_only(self):
        # start_time null is a real state (order of play not yet assigned)
        assert self.views[2]["start_time"] is None
        assert fmt_start(self.views[2]) == "Tue 18 Aug"

    def test_fmt_start_nothing(self):
        assert fmt_start({"start_time": None, "event_date": None}) == "TBD"

    def test_player_filter(self):
        hits = filter_fixtures(self.views, "swiatek")
        assert [h["id"] for h in hits] == [9002]


# ---------------------------------------------------------------------------
# Rendering smoke tests (no network, no InkyPi)
# ---------------------------------------------------------------------------

class TestRendering:
    def test_render_live_returns_image(self):
        from live_tennis import render_live
        views = [match_view(m) for m in load("sample_live.json")["data"]]
        img = render_live(views, 800, 480, "18:45")
        assert img.size == (800, 480)

    def test_render_fixtures_returns_image(self):
        from live_tennis import render_fixtures
        views = [fixture_view(f)
                 for f in load("sample_fixtures.json")["data"]]
        img = render_fixtures(views, 800, 480, None, "18:45")
        assert img.size == (800, 480)

    def test_render_small_display(self):
        # Inky pHAT-sized display must not crash
        from live_tennis import render_live
        views = [match_view(m) for m in load("sample_live.json")["data"]]
        img = render_live(views, 250, 122, "")
        assert img.size == (250, 122)

    def test_render_empty_fixtures(self):
        from live_tennis import render_fixtures
        img = render_fixtures([], 800, 480, None, "")
        assert img.size == (800, 480)
