# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate themed SVG product thumbnails at request time.

The storefront shows a thumbnail per product without shipping any binary image
assets or making external network calls: each thumbnail is a small SVG rendered
from the product's ``color`` and ``emoji``. This keeps the sample fully
self-contained and offline-friendly.
"""
from __future__ import annotations

from markupsafe import escape

from .catalog import Product


def _darken(hex_color: str, factor: float = 0.65) -> str:
    """Return a darker shade of ``hex_color`` for a subtle gradient."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    r, g, b = (max(0, int(c * factor)) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def render_thumbnail_svg(product: Product) -> str:
    """Return an SVG document (as text) for a product's thumbnail.

    The product name is escaped before interpolation; ``color`` and ``emoji``
    come from the trusted static catalog, not user input.
    """
    top = escape(product.color)
    bottom = escape(_darken(product.color))
    emoji = escape(product.emoji)
    name = escape(product.name)
    grad_id = f"g-{escape(product.id)}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" \
viewBox="0 0 400 300" role="img" aria-label="{name}">
  <defs>
    <linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{top}"/>
      <stop offset="1" stop-color="{bottom}"/>
    </linearGradient>
  </defs>
  <rect width="400" height="300" fill="url(#{grad_id})"/>
  <circle cx="200" cy="130" r="78" fill="rgba(255,255,255,0.14)"/>
  <text x="200" y="130" font-size="92" text-anchor="middle" \
dominant-baseline="central">{emoji}</text>
  <text x="200" y="250" font-size="22" fill="rgba(255,255,255,0.95)" \
font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif" \
text-anchor="middle">{name}</text>
</svg>"""
