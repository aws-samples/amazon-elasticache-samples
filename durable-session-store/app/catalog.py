# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A small, static product catalog for the demo storefront.

The catalog is intentionally hardcoded (no database): it exists only to make the
session/cart demo feel like a real store. Prices are integer cents to avoid
floating-point rounding surprises.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    """A single catalog item. ``price_cents`` is an integer to avoid float math.

    ``color`` and ``emoji`` drive a generated SVG thumbnail (see
    :mod:`app.thumbnails`) so the demo needs no binary image assets.
    """

    id: str
    name: str
    price_cents: int
    description: str
    color: str
    emoji: str

    @property
    def price_display(self) -> str:
        """Human-friendly price, e.g. ``$12.50``."""
        return f"${self.price_cents / 100:,.2f}"


# A fixed, in-memory catalog. In a real store this would come from a database or
# product service; here it is static so the sample has no external dependencies.
_PRODUCTS: tuple[Product, ...] = (
    Product("sku-coffee", "Single-Origin Coffee Beans", 1499,
            "250g whole-bean, medium roast with notes of cocoa and citrus.",
            color="#6f4e37", emoji="\N{HOT BEVERAGE}"),
    Product("sku-mug", "Ceramic Pour-Over Mug", 1899,
            "350ml double-walled mug that keeps your coffee warm longer.",
            color="#c0392b", emoji="\N{TEACUP WITHOUT HANDLE}"),
    Product("sku-grinder", "Hand Burr Grinder", 4599,
            "Adjustable conical burr grinder for a consistent, quiet grind.",
            color="#2c3e50", emoji="\N{GEAR}"),
    Product("sku-kettle", "Gooseneck Kettle", 5299,
            "1L stovetop kettle with a precision spout for controlled pours.",
            color="#16a085", emoji="\N{AMPHORA}"),
    Product("sku-filters", "Pour-Over Filters (100)", 799,
            "Pack of 100 unbleached paper filters for cone drippers.",
            color="#f39c12", emoji="\N{PAGE FACING UP}"),
    Product("sku-scale", "Digital Brew Scale", 2599,
            "0.1g precision scale with a built-in brew timer.",
            color="#8e44ad", emoji="\N{STOPWATCH}"),
    Product("sku-dripper", "Ceramic Pour-Over Dripper", 2299,
            "Single-cup cone dripper with a spiral-ribbed interior.",
            color="#2980b9", emoji="\N{HOURGLASS WITH FLOWING SAND}"),
    Product("sku-french-press", "Glass French Press", 3299,
            "800ml borosilicate press with a stainless steel mesh filter.",
            color="#34495e", emoji="\N{AMPHORA}"),
    Product("sku-aeropress", "Air Press Brewer", 3999,
            "Lightweight pressure brewer that makes a clean, rich cup.",
            color="#e67e22", emoji="\N{SYRINGE}"),
    Product("sku-moka", "Stovetop Moka Pot", 2899,
            "6-cup aluminum moka pot for a bold, espresso-style brew.",
            color="#7f8c8d", emoji="\N{HOT BEVERAGE}"),
    Product("sku-espresso-beans", "Espresso Roast Beans", 1599,
            "250g dark roast blended for a syrupy, chocolatey shot.",
            color="#4e342e", emoji="\N{SEEDLING}"),
    Product("sku-decaf", "Swiss-Water Decaf Beans", 1699,
            "250g chemical-free decaf with a smooth, balanced body.",
            color="#5d6d7e", emoji="\N{HERB}"),
    Product("sku-cold-brew", "Cold Brew Mason Kit", 2499,
            "1L mason jar with a reusable fine-mesh steeping filter.",
            color="#1abc9c", emoji="\N{SNOWFLAKE}"),
    Product("sku-milk-frother", "Handheld Milk Frother", 1299,
            "Battery whisk that whips silky microfoam in seconds.",
            color="#9b59b6", emoji="\N{CLOUD WITH TORNADO}"),
    Product("sku-milk-pitcher", "Steaming Milk Pitcher", 1799,
            "350ml stainless pitcher with a tapered latte-art spout.",
            color="#95a5a6", emoji="\N{GLASS OF MILK}"),
    Product("sku-tamper", "Espresso Tamper 58mm", 2199,
            "Flat-base stainless tamper sized for 58mm portafilters.",
            color="#616a6b", emoji="\N{HAMMER}"),
    Product("sku-portafilter", "Bottomless Portafilter", 3499,
            "Naked 58mm portafilter for watching your extraction.",
            color="#283747", emoji="\N{HOCHO}"),
    Product("sku-knock-box", "Espresso Knock Box", 2699,
            "Compact puck bin with a padded knock bar.",
            color="#424949", emoji="\N{WASTEBASKET}"),
    Product("sku-thermometer", "Clip-On Brew Thermometer", 1199,
            "Dial thermometer that clips to kettle or pitcher.",
            color="#c0392b", emoji="\N{THERMOMETER}"),
    Product("sku-timer", "Magnetic Brew Timer", 1399,
            "Minimalist countdown timer that sticks to any surface.",
            color="#d35400", emoji="\N{ALARM CLOCK}"),
    Product("sku-travel-mug", "Insulated Travel Mug", 2399,
            "470ml vacuum tumbler that holds temperature for hours.",
            color="#2471a3", emoji="\N{CUP WITH STRAW}"),
    Product("sku-carafe", "Thermal Serving Carafe", 3799,
            "1L double-wall carafe that keeps a batch brew hot.",
            color="#117864", emoji="\N{AMPHORA}"),
    Product("sku-beans-sampler", "Roaster's Bean Sampler", 2999,
            "Four 100g bags spanning light to dark roast profiles.",
            color="#a04000", emoji="\N{SHOPPING BAGS}"),
    Product("sku-syrup", "Vanilla Bean Syrup", 999,
            "375ml real-vanilla syrup for lattes and cold brew.",
            color="#f1c40f", emoji="\N{HONEY POT}"),
    Product("sku-cocoa", "Dark Drinking Cocoa", 1099,
            "300g single-origin cocoa for a rich mocha.",
            color="#6e2c00", emoji="\N{HOT BEVERAGE}"),
    Product("sku-cleaning-tabs", "Espresso Cleaning Tablets", 899,
            "Pack of 30 backflush tablets for group-head hygiene.",
            color="#148f77", emoji="\N{BAR OF SOAP}"),
    Product("sku-descaler", "Citric Descaler Solution", 849,
            "500ml food-safe descaler for kettles and machines.",
            color="#27ae60", emoji="\N{DROPLET}"),
    Product("sku-brush", "Group-Head Cleaning Brush", 699,
            "Heat-resistant detailing brush with an angled head.",
            color="#7d3c98", emoji="\N{BROOM}"),
    Product("sku-storage", "Vacuum Bean Canister", 2799,
            "1.2L airtight canister with a one-way CO2 valve.",
            color="#1f618d", emoji="\N{CANNED FOOD}"),
    Product("sku-cups-set", "Double-Wall Glass Cups (2)", 2299,
            "Set of two 240ml insulated glasses for espresso drinks.",
            color="#b03a2e", emoji="\N{CLINKING GLASSES}"),
    Product("sku-apron", "Barista Canvas Apron", 3199,
            "Durable waxed-canvas apron with adjustable straps.",
            color="#515a5a", emoji="\N{T-SHIRT}"),
    Product("sku-gift-card", "Digital Gift Card", 5000,
            "A reloadable gift card for the coffee lover in your life.",
            color="#884ea0", emoji="\N{WRAPPED PRESENT}"),
)

_BY_ID: dict[str, Product] = {p.id: p for p in _PRODUCTS}


def all_products() -> tuple[Product, ...]:
    """Return every product in the catalog, in display order."""
    return _PRODUCTS


def get_product(product_id: str) -> Product | None:
    """Look up a single product by id, or ``None`` if it does not exist."""
    return _BY_ID.get(product_id)
