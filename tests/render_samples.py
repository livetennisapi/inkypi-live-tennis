"""Render the sample payloads to PNG — regenerates the README screenshots.

Usage:
    LIVE_TENNIS_FONT_DIR=/path/to/inkypi/src/static/fonts \
        python tests/render_samples.py

No network access: the API fetch is not involved; the sample JSON files in
this directory stand in for API responses.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "live_tennis"))

from live_tennis import (  # noqa: E402
    match_view, fixture_view, render_live, render_fixtures,
)

OUT = os.path.join(HERE, "..", "screenshots")


def main():
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(HERE, "sample_live.json")) as f:
        live = json.load(f)["data"]
    with open(os.path.join(HERE, "sample_fixtures.json")) as f:
        fixtures = json.load(f)["data"]

    img = render_live([match_view(m) for m in live], 800, 480, "18:45")
    img.save(os.path.join(OUT, "live.png"))
    print("wrote screenshots/live.png")

    img = render_fixtures([fixture_view(x) for x in fixtures], 800, 480,
                          None, "18:45")
    img.save(os.path.join(OUT, "fixtures.png"))
    print("wrote screenshots/fixtures.png")


if __name__ == "__main__":
    main()
