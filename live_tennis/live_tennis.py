"""InkyPi plugin: live tennis scoreboard powered by the Live Tennis API.

Shows the matches in play right now (players + rankings, per-set games,
in-game points, serving dot, derived break-point marker) and falls back to
the next scheduled fixtures when nothing is live.

API: https://docs.livetennisapi.com (GET /matches?status=live, GET /fixtures,
Authorization: Bearer <key>). The free tier allows 100 requests/day, so this
plugin never calls the API more often than once per REFRESH_MIN_SECONDS
(15 minutes) regardless of how often InkyPi asks it to refresh — in between,
it re-renders from its local cache.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

try:  # running inside InkyPi
    from plugins.base_plugin.base_plugin import BasePlugin
except ImportError:  # running standalone (tests / local rendering)
    class BasePlugin(object):
        def __init__(self, config, **dependencies):
            self.config = config

logger = logging.getLogger(__name__)

API_BASE = "https://api.livetennisapi.com/api/public/v1"
ENV_KEY = "LIVE_TENNIS_API_KEY"

# Free tier is 100 requests/day. One live poll per 15 minutes is ~96/day;
# the fixtures fallback is cached separately for FIXTURES_TTL_SECONDS so an
# all-quiet day stays within the free quota. Do not lower this below 900
# on a free key.
REFRESH_MIN_SECONDS = 15 * 60
FIXTURES_TTL_SECONDS = 6 * 60 * 60

CACHE_FILE = os.path.join(os.path.dirname(__file__), "live_tennis_cache.json")

TOURS = ("atp", "wta", "challenger", "itf", "juniors")

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


# ---------------------------------------------------------------------------
# Pure logic (importable without InkyPi — covered by tests)
# ---------------------------------------------------------------------------

def is_break_point(score):
    """Derive whether the current point is a break point.

    Break point: the receiver wins this point => wins the game. That is:
      - receiver has "AD", or
      - receiver has "40" and the server has less than 40 ("0"/"15"/"30").
    Never in a tiebreak (there are set points, not break points).

    `score` is the Score object from the API: points is
    [p1_points, p2_points] as tennis strings ("0","15","30","40","AD"),
    entries may be null; server is 1, 2 or null; is_tiebreak is a boolean.
    """
    if not score or score.get("is_tiebreak"):
        return False
    server = score.get("server")
    if server not in (1, 2):
        return False
    points = score.get("points") or []
    if len(points) < 2:
        return False
    server_pts = points[server - 1]
    receiver_pts = points[2 - server]
    if server_pts is None or receiver_pts is None:
        return False
    if receiver_pts == "AD":
        return True
    return receiver_pts == "40" and server_pts in ("0", "15", "30")


def match_view(match):
    """Reduce an API Match object to the fields the renderer needs."""
    players = match.get("players") or {}
    p1 = players.get("p1") or {}
    p2 = players.get("p2") or {}
    score = match.get("score") or {}
    games = score.get("games") or []
    points = score.get("points") or []
    server = score.get("server")
    bp = is_break_point(score)
    receiver = (3 - server) if server in (1, 2) else None  # 1->2, 2->1
    return {
        "id": match.get("id"),
        "tournament": match.get("tournament") or "",
        "tour": match.get("tour"),  # filter vocabulary (atp/wta/...) or null
        "round": match.get("round") or "",
        "is_doubles": bool(match.get("is_doubles")),
        "p1": _player_view(p1),
        "p2": _player_view(p2),
        "sets": score.get("sets") or [],
        "games_p1": games[0] if len(games) > 0 else [],
        "games_p2": games[1] if len(games) > 1 else [],
        "points_p1": points[0] if len(points) > 0 else None,
        "points_p2": points[1] if len(points) > 1 else None,
        "server": server if server in (1, 2) else None,
        "is_tiebreak": bool(score.get("is_tiebreak")),
        "break_point": bp,
        "bp_player": receiver if bp else None,  # the receiver holds the BP
    }


def _player_view(p):
    return {
        "name": p.get("name") or "Unknown",
        "ranking": p.get("ranking"),  # may be null
    }


def fixture_view(fx):
    """Reduce an API Fixture object to the fields the renderer needs.

    Note: Fixture.tour is the record's OWN granular vocabulary
    (e.g. "challenger_men"), not the filter enum — treated as opaque and
    only used for display, never parsed.
    """
    return {
        "id": fx.get("id"),
        "p1": fx.get("player1_name") or "TBD",
        "p2": fx.get("player2_name") or "TBD",
        "tournament": fx.get("tournament") or "",
        "round": fx.get("round") or "",
        "start_time": fx.get("start_time"),  # UTC ISO 8601 or null
        "event_date": fx.get("event_date"),  # date or null
    }


def player_filter_hit(names, query):
    """Case-insensitive substring filter. `query` may hold several
    comma-separated terms; any term matching any name is a hit."""
    terms = [t.strip().lower() for t in (query or "").split(",") if t.strip()]
    if not terms:
        return True
    hay = " | ".join(n.lower() for n in names if n)
    return any(t in hay for t in terms)


def filter_matches(views, player_query):
    return [v for v in views
            if player_filter_hit([v["p1"]["name"], v["p2"]["name"]],
                                 player_query)]


def filter_fixtures(views, player_query):
    return [v for v in views
            if player_filter_hit([v["p1"], v["p2"]], player_query)]


def fmt_start(fx, tz=None):
    """Human string for a fixture's start: HH:MM if a time is assigned,
    the bare date if only event_date is known, else 'TBD'."""
    st = fx.get("start_time")
    if st:
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
            if tz is not None:
                dt = dt.astimezone(tz)
            return dt.strftime("%a %H:%M")
        except ValueError:
            pass
    ed = fx.get("event_date")
    if ed:
        try:
            return datetime.strptime(ed, "%Y-%m-%d").strftime("%a %d %b")
        except ValueError:
            return ed
    return "TBD"


# ---------------------------------------------------------------------------
# API access + cache
# ---------------------------------------------------------------------------

def _api_get(path, api_key, params=None):
    import requests
    try:
        resp = requests.get(
            API_BASE + path,
            headers={"Authorization": "Bearer %s" % api_key},
            params=params or {},
            timeout=20,
        )
    except requests.RequestException as e:
        raise RuntimeError("Live Tennis API unreachable: %s" % e)
    if resp.status_code == 401:
        raise RuntimeError("Live Tennis API key rejected (401). Check the "
                           "API Key in the plugin settings.")
    if resp.status_code == 403:
        raise RuntimeError("Live Tennis API: upgrade required (403) — this "
                           "call is above your plan's tier.")
    if resp.status_code == 429:
        raise RuntimeError("Live Tennis API rate limit hit (429). Free tier "
                           "is 30 req/min and 100 req/day; increase the "
                           "refresh interval.")
    if resp.status_code != 200:
        raise RuntimeError("Live Tennis API error %d" % resp.status_code)
    return resp.json()


def _load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except OSError as e:
        logger.warning("Could not write cache: %s", e)


def fetch_live(api_key, tour, now=None):
    """Fetch live matches, enforcing the minimum poll interval through the
    local cache (never hits the API more than once per REFRESH_MIN_SECONDS).
    Returns (live_matches, fetched_at_ts)."""
    now = now if now is not None else time.time()
    cache = _load_cache()
    fingerprint = "tour=%s" % (tour or "all")

    lc = cache.get("live")
    if lc and lc.get("fingerprint") == fingerprint and \
            now - lc.get("fetched_at", 0) < REFRESH_MIN_SECONDS:
        return lc["data"], lc["fetched_at"]

    params = {"status": "live", "limit": 50}
    if tour:
        params["tour"] = tour
    payload = _api_get("/matches", api_key, params)
    live = payload.get("data") or []
    cache["live"] = {"fingerprint": fingerprint, "fetched_at": now,
                     "data": live}
    _save_cache(cache)
    return live, now


def fetch_fixtures(api_key, tour, now=None):
    """Fetch upcoming fixtures, cached for FIXTURES_TTL_SECONDS (the forward
    schedule moves slowly; the long TTL keeps quiet days inside the free
    daily quota)."""
    now = now if now is not None else time.time()
    cache = _load_cache()
    fingerprint = "tour=%s" % (tour or "all")

    fc = cache.get("fixtures")
    if fc and fc.get("fingerprint") == fingerprint and \
            now - fc.get("fetched_at", 0) < FIXTURES_TTL_SECONDS:
        return fc["data"]

    params = {"limit": 50}
    if tour:
        params["tour"] = tour
    payload = _api_get("/fixtures", api_key, params)
    fixtures = payload.get("data") or []
    cache["fixtures"] = {"fingerprint": fingerprint, "fetched_at": now,
                         "data": fixtures}
    _save_cache(cache)
    return fixtures


# ---------------------------------------------------------------------------
# Rendering (pure Pillow — e-ink friendly: pure black on white, no grays)
# ---------------------------------------------------------------------------

def load_font(size, bold=False):
    size = int(size)
    try:  # InkyPi's bundled Jost
        from utils.app_utils import get_font
        f = get_font("Jost", size, "bold" if bold else "normal")
        if f:
            return f
    except Exception:
        pass
    font_dir = os.environ.get("LIVE_TENNIS_FONT_DIR")
    if font_dir:
        name = "Jost-SemiBold.ttf" if bold else "Jost.ttf"
        path = os.path.join(font_dir, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                 if bold else
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _fit_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _text_h(font):
    ascent, descent = font.getmetrics()
    return ascent + descent


def _header(draw, width, title, right_text, font, pad):
    h = _text_h(font) + pad
    draw.rectangle([0, 0, width, h], fill=BLACK)
    draw.text((pad, pad // 2), title, font=font, fill=WHITE)
    if right_text:
        w = draw.textlength(right_text, font=font)
        draw.text((width - w - pad, pad // 2), right_text, font=font,
                  fill=WHITE)
    return h


def _player_label(p):
    label = p["name"]
    if p.get("ranking"):
        label += "  #%d" % p["ranking"]
    return label


def _draw_player_row(draw, y, x0, name_w, cols_x, v, who, fonts, row_h):
    """One player line of a match card: dot, name+rank, per-set games,
    points, BP badge."""
    p = v["p1"] if who == 1 else v["p2"]
    games = v["games_p1"] if who == 1 else v["games_p2"]
    points = v["points_p1"] if who == 1 else v["points_p2"]
    f = fonts["row"]
    fb = fonts["row_bold"]
    cy = y + row_h // 2

    # serving dot
    if v["server"] == who:
        r = max(3, row_h // 7)
        draw.ellipse([x0, cy - r, x0 + 2 * r, cy + r], fill=BLACK)
    name_x = x0 + row_h // 2 + 6
    label = _fit_text(draw, _player_label(p), f, name_w - (name_x - x0))
    draw.text((name_x, cy), label, font=f, fill=BLACK, anchor="lm")

    # per-set games
    set_x, pts_x, bp_x = cols_x
    x = set_x
    col_w = fonts["set_col_w"]
    for g in games:
        draw.text((x + col_w // 2, cy), str(g), font=fb, fill=BLACK,
                  anchor="mm")
        x += col_w
    # current points
    pts = points if points is not None else "–"
    draw.text((pts_x, cy), str(pts), font=fb, fill=BLACK, anchor="mm")
    # BP badge on the receiver's row
    if v["break_point"] and v["bp_player"] == who:
        bw = int(draw.textlength("BP", font=fonts["small"])) + 10
        bh = _text_h(fonts["small"]) + 2
        draw.rounded_rectangle([bp_x, cy - bh // 2, bp_x + bw, cy + bh // 2],
                               radius=3, fill=BLACK)
        draw.text((bp_x + bw // 2, cy), "BP", font=fonts["small"],
                  fill=WHITE, anchor="mm")


def render_live(views, width, height, updated_label=""):
    image = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(image)
    scale = height / 480.0
    fonts = {
        "header": load_font(26 * scale, bold=True),
        "meta": load_font(17 * scale),
        "row": load_font(23 * scale),
        "row_bold": load_font(23 * scale, bold=True),
        "small": load_font(15 * scale, bold=True),
        "set_col_w": int(30 * scale),
    }
    pad = int(10 * scale)
    y = _header(draw, width, "● LIVE TENNIS", updated_label,
                fonts["header"], pad)

    meta_h = _text_h(fonts["meta"]) + int(4 * scale)
    row_h = _text_h(fonts["row"]) + int(8 * scale)
    card_h = meta_h + 2 * row_h + int(12 * scale)
    max_cards = max(1, (height - y - pad) // card_h)

    # column geometry
    max_sets = max((max(len(v["games_p1"]), len(v["games_p2"]))
                    for v in views[:max_cards]), default=3)
    max_sets = max(1, min(max_sets, 5))
    bp_w = int(38 * scale)
    pts_w = int(44 * scale)
    sets_w = fonts["set_col_w"] * max_sets
    bp_x = width - pad - bp_w
    pts_x = bp_x - pts_w // 2 - int(6 * scale)
    set_x = pts_x - pts_w // 2 - sets_w
    name_w = set_x - pad - int(6 * scale)

    y += int(6 * scale)
    for v in views[:max_cards]:
        meta_bits = [b for b in (v["tournament"], v["round"]) if b]
        if v["tour"]:
            meta_bits.append(v["tour"].upper())
        if v["is_tiebreak"]:
            meta_bits.append("TIEBREAK")
        meta = _fit_text(draw, "  ·  ".join(meta_bits), fonts["meta"],
                         width - 2 * pad)
        draw.text((pad, y), meta, font=fonts["meta"], fill=BLACK)
        y += meta_h
        for who in (1, 2):
            _draw_player_row(draw, y, pad, name_w,
                             (set_x, pts_x, bp_x), v, who, fonts, row_h)
            y += row_h
        y += int(6 * scale)
        draw.line([pad, y, width - pad, y], fill=BLACK, width=1)
        y += int(6 * scale)

    if len(views) > max_cards:
        more = "+%d more live" % (len(views) - max_cards)
        draw.text((width - pad, height - pad), more, font=fonts["small"],
                  fill=BLACK, anchor="rs")
    return image


def render_fixtures(views, width, height, tz=None, updated_label=""):
    image = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(image)
    scale = height / 480.0
    fonts = {
        "header": load_font(26 * scale, bold=True),
        "meta": load_font(16 * scale),
        "row": load_font(22 * scale),
        "row_bold": load_font(22 * scale, bold=True),
        "small": load_font(15 * scale, bold=True),
    }
    pad = int(10 * scale)
    y = _header(draw, width, "UP NEXT — TENNIS", updated_label,
                fonts["header"], pad)
    y += int(8 * scale)

    if not views:
        draw.text((width // 2, height // 2), "No live matches or fixtures",
                  font=fonts["row"], fill=BLACK, anchor="mm")
        return image

    row_h = _text_h(fonts["row"]) + int(4 * scale)
    meta_h = _text_h(fonts["meta"]) + int(2 * scale)
    block_h = row_h + meta_h + int(10 * scale)
    time_w = int(120 * scale)
    for fx in views:
        if y + block_h > height - pad:
            break
        when = fmt_start(fx, tz)
        draw.text((pad, y + row_h // 2), when, font=fonts["row_bold"],
                  fill=BLACK, anchor="lm")
        names = "%s  vs  %s" % (fx["p1"], fx["p2"])
        names = _fit_text(draw, names, fonts["row"],
                          width - pad * 2 - time_w)
        draw.text((pad + time_w, y + row_h // 2), names, font=fonts["row"],
                  fill=BLACK, anchor="lm")
        y += row_h
        meta_bits = [b for b in (fx["tournament"], fx["round"]) if b]
        meta = _fit_text(draw, "  ·  ".join(meta_bits), fonts["meta"],
                         width - pad * 2 - time_w)
        draw.text((pad + time_w, y), meta, font=fonts["meta"], fill=BLACK)
        y += meta_h + int(6 * scale)
        draw.line([pad, y, width - pad, y], fill=BLACK, width=1)
        y += int(4 * scale)
    return image


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class LiveTennis(BasePlugin):
    """Live tennis scoreboard for InkyPi."""

    def generate_image(self, settings, device_config):
        api_key = (settings.get("api_key") or "").strip()
        if not api_key:
            try:
                api_key = device_config.load_env_key(ENV_KEY) or ""
            except Exception:
                api_key = ""
        if not api_key:
            raise RuntimeError(
                "Live Tennis API key missing. Get a free key at "
                "https://livetennisapi.com/subscribe/free and paste it in "
                "the plugin settings (or set %s in the .env file)." % ENV_KEY)

        tour = (settings.get("tour") or "").strip().lower()
        if tour not in TOURS:
            tour = ""  # all tours
        player_query = settings.get("player_filter") or ""

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        width, height = dimensions

        tz = None
        try:
            from zoneinfo import ZoneInfo
            tz_name = device_config.get_config("timezone")
            if tz_name:
                tz = ZoneInfo(tz_name)
        except Exception:
            tz = None

        live, fetched_at = fetch_live(api_key, tour)
        stamp = datetime.fromtimestamp(fetched_at, timezone.utc)
        if tz is not None:
            stamp = stamp.astimezone(tz)
        updated = stamp.strftime("%H:%M")
        right = ("%s · %s" % (tour.upper(), updated)) if tour else updated

        views = filter_matches([match_view(m) for m in live], player_query)
        if views:
            return render_live(views, width, height, right)

        # nothing live (or everything filtered out) — show the schedule
        fixtures = fetch_fixtures(api_key, tour)
        fx_views = filter_fixtures([fixture_view(f) for f in fixtures],
                                   player_query)
        return render_fixtures(fx_views, width, height, tz, right)
