"""Regenerate the OpenLIFU header/host PNG icons from Lucide SVG sources.

Run this whenever we want to refresh the header icon set (database, login,
save, exit, and their state-badge variants). Writes into
``OpenLIFU/Resources/Icons/``.

Icons are sourced from https://github.com/lucide-icons/lucide (ISC licence,
compatible with SlicerOpenLIFU's AGPL). We render each source SVG to a
96x96 PNG with alpha at a mid-grey ``#606060`` so it reads on both Slicer's
light and dark themes; state badges are composited in the bottom-right corner
at ``#303030`` for visual weight against the base glyph.

Requirements (into whatever venv you run this from):

    pip install resvg-py pillow requests

Usage (from repo root):

    python scripts/generate_header_icons.py

Related issue: OpenwaterHealth/SlicerOpenLIFU#595
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Iterable

import requests
import resvg_py
from PIL import Image

# Path setup ---------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = REPO_ROOT / "OpenLIFU" / "Resources" / "Icons"

# Icons are rendered as pure white (``#ffffff``) silhouettes with alpha.
# The consuming code (``OpenLIFULib.module_layout``) tints them to per-state
# colours at load time so a single PNG serves every visual state (dim /
# active / success / warning / danger) and both Slicer's light and dark
# themes without shipping a variant per state.
BASE_COLOR = "#ffffff"
BADGE_COLOR = "#ffffff"

# Render size (Qt will downscale to the button's setIconSize call). 96px is
# 4x the 24px source, plenty of headroom for HiDPI.
BASE_SIZE = 96
# Badge occupies the bottom-right quadrant of the base glyph. Sized so that
# after Qt scales the composite down to the button's ~20px icon size the
# badge is still legible (~11px). Was 44 originally; the crown badge came
# out barely readable at that scale.
BADGE_SIZE = 52

LUCIDE_RAW = "https://raw.githubusercontent.com/lucide-icons/lucide/main/icons/{name}.svg"


def fetch_svg(lucide_name: str, color: str) -> str:
    """Fetch a Lucide SVG and swap ``currentColor`` for an explicit hex."""
    url = LUCIDE_RAW.format(name=lucide_name)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    text = resp.text
    if "currentColor" not in text:
        raise RuntimeError(
            f"Unexpected SVG shape for {lucide_name}: no currentColor to swap"
        )
    return text.replace("currentColor", color)


def render_svg_to_png(svg_text: str, output_size: int) -> Image.Image:
    """Rasterise SVG text to an RGBA Pillow image sized ``output_size`` square."""
    png_bytes = resvg_py.svg_to_bytes(
        svg_string=svg_text,
        width=output_size,
        height=output_size,
    )
    # resvg_py returns a list[int] of PNG bytes; normalise to real bytes.
    if isinstance(png_bytes, list):
        png_bytes = bytes(png_bytes)
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def load_lucide(name: str, size: int, color: str) -> Image.Image:
    return render_svg_to_png(fetch_svg(name, color), size)


def composite_badge(base: Image.Image, badge: Image.Image) -> Image.Image:
    """Paste ``badge`` into the bottom-right of a copy of ``base``."""
    out = base.copy()
    x = out.width - badge.width
    y = out.height - badge.height
    out.alpha_composite(badge, dest=(x, y))
    return out


def save_png(img: Image.Image, filename: str) -> None:
    dest = ICONS_DIR / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG", optimize=True)
    print(f"wrote {dest.relative_to(REPO_ROOT)}")


def build_all() -> None:
    # Base glyphs (rendered white; ``module_layout`` tints at load time).
    folder = load_lucide("folder", BASE_SIZE, BASE_COLOR)
    user = load_lucide("user-round", BASE_SIZE, BASE_COLOR)
    save_glyph = load_lucide("save", BASE_SIZE, BASE_COLOR)
    exit_glyph = load_lucide("log-out", BASE_SIZE, BASE_COLOR)
    # Header device button: Lucide ``plug`` stands in for the LIFU USB
    # hardware. Replaces the earlier bespoke transducer illustration so we
    # can tint the icon per connection state (green / yellow / red / pink)
    # just like the other header buttons.
    plug = load_lucide("plug", BASE_SIZE, BASE_COLOR)

    # Badge glyphs. The admin badge uses ``shield`` (evokes the Windows
    # UAC shield -- a widely recognised admin/elevation signal) rather
    # than ``crown`` which was too visually dense to read at 20px.
    cloud = load_lucide("cloud", BADGE_SIZE, BADGE_COLOR)
    cloud_off = load_lucide("cloud-off", BADGE_SIZE, BADGE_COLOR)
    shield = load_lucide("shield", BADGE_SIZE, BADGE_COLOR)

    save_png(folder, "db.png")
    save_png(composite_badge(folder, cloud), "db-cloud.png")
    save_png(composite_badge(folder, cloud_off), "db-cloud-broken.png")
    save_png(user, "user.png")
    save_png(composite_badge(user, shield), "user-admin.png")
    save_png(save_glyph, "save.png")
    save_png(exit_glyph, "exit.png")
    save_png(plug, "device.png")


def main(argv: Iterable[str]) -> int:
    try:
        build_all()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
