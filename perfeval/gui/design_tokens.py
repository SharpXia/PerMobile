from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrimitiveTokens:
    """Raw visual values. Components must consume semantic tokens instead."""

    neutral_0: str = "#ffffff"
    neutral_25: str = "#fbfdff"
    neutral_50: str = "#f4f7fb"
    neutral_100: str = "#e9eff5"
    neutral_200: str = "#d6e0e9"
    neutral_300: str = "#b6c5d3"
    neutral_500: str = "#6b7c8f"
    neutral_700: str = "#35485c"
    neutral_800: str = "#1f3042"
    neutral_900: str = "#102235"
    neutral_950: str = "#081521"

    teal_50: str = "#e9fbf7"
    teal_100: str = "#cff5ed"
    teal_300: str = "#70dfcb"
    teal_400: str = "#2ac9b2"
    teal_500: str = "#0eaa96"
    teal_600: str = "#087f75"
    teal_700: str = "#075d57"

    green_600: str = "#24866c"
    amber_600: str = "#b67218"
    red_600: str = "#c34c57"


PRIMITIVES = PrimitiveTokens()


@dataclass(frozen=True)
class SemanticTokens:
    """Purpose-based aliases used by palettes and QSS components."""

    background: str
    surface: str
    surface_alt: str
    surface_strong: str
    text: str
    text_muted: str
    border: str
    border_strong: str
    primary: str
    primary_hover: str
    primary_active: str
    on_primary: str
    selection: str
    selection_text: str
    success: str
    success_bg: str
    warning: str
    warning_bg: str
    error: str
    error_bg: str
    nav_bg: str
    nav_surface: str
    nav_text: str
    nav_muted: str
    nav_selected: str
    focus: str
    code_bg: str
    code_text: str
    is_dark: bool


LIGHT_THEME = SemanticTokens(
    background="#f4f7fb",
    surface="#ffffff",
    surface_alt="#f8fafc",
    surface_strong="#e7edf4",
    text="#17212b",
    text_muted="#64748b",
    border="#d8e1ea",
    border_strong="#b9c7d5",
    primary="#087f75",
    primary_hover="#066b63",
    primary_active="#05564f",
    on_primary="#ffffff",
    selection="#d9f4ef",
    selection_text="#075d57",
    success="#21856b",
    success_bg="#e7f7f1",
    warning="#b26b17",
    warning_bg="#fff4df",
    error="#c84552",
    error_bg="#fcebed",
    nav_bg="#0d1b2a",
    nav_surface="#15283a",
    nav_text="#eef8f7",
    nav_muted="#9ab0bd",
    nav_selected="#1e3e4c",
    focus="#0eaa96",
    code_bg="#0e1a27",
    code_text="#e5f4f2",
    is_dark=False,
)


DARK_THEME = SemanticTokens(
    background="#0b1420",
    surface="#142333",
    surface_alt="#101d2b",
    surface_strong="#203347",
    text="#eef6f7",
    text_muted="#a6b6c4",
    border="#30465a",
    border_strong="#49637a",
    primary="#31c8b2",
    primary_hover="#2ab39f",
    primary_active="#1e9a89",
    on_primary="#ffffff",
    selection="#1d514b",
    selection_text="#c9fff5",
    success="#52d6a3",
    success_bg="#173a35",
    warning="#f5c56a",
    warning_bg="#3d301d",
    error="#ff7b82",
    error_bg="#42252c",
    nav_bg="#07121d",
    nav_surface="#122436",
    nav_text="#eef8f7",
    nav_muted="#92aabb",
    nav_selected="#1b4651",
    focus="#31c8b2",
    code_bg="#08131f",
    code_text="#eef8f7",
    is_dark=True,
)


@dataclass(frozen=True)
class ComponentTokens:
    """Shared component measurements derived from a 4 px spacing grid."""

    space_1: int = 4
    space_2: int = 8
    space_3: int = 12
    space_4: int = 16
    space_5: int = 20
    space_6: int = 24
    space_8: int = 32
    radius_sm: int = 8
    radius_md: int = 12
    radius_lg: int = 18
    control_height: int = 38
    button_height: int = 38
    primary_height: int = 44
    nav_width: int = 224


COMPONENTS = ComponentTokens()
