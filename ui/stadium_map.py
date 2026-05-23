"""2D real-map view of M. Chinnaswamy with live density heatmap overlay.

Uses Plotly's Densitymapbox on OpenStreetMap tiles (no Mapbox token needed).
The abstract 100x100 stadium grid is mapped onto real lat/lon via an affine
transform calibrated against the actual stadium footprint.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import plotly.graph_objects as go

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# M. Chinnaswamy stadium (Bengaluru) — centre of the playing field.
CENTER_LAT = 12.9788
CENTER_LON = 77.5996

# Calibration: the 100×100 abstract grid spans ~250 m of real ground
# (covers the stadium plus a small ring of surrounding road).
METERS_PER_UNIT = 2.5

# Degrees per metre at this latitude.
_DEG_LAT_PER_M = 1.0 / 111_320.0
_DEG_LON_PER_M = 1.0 / (111_320.0 * 0.9737)  # cos(12.97°)


def _xy_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """100x100 grid (low y = north, screen-style) → (lon, lat)."""
    dx_m = (x - 50.0) * METERS_PER_UNIT
    dy_m = (50.0 - y) * METERS_PER_UNIT  # invert: low y → positive latitude
    return (
        CENTER_LON + dx_m * _DEG_LON_PER_M,
        CENTER_LAT + dy_m * _DEG_LAT_PER_M,
    )


def _load_zones() -> dict:
    return json.loads((DATA_DIR / "stadium_zones.json").read_text())


# Soft-falloff offsets in grid units — one point becomes a small cluster so
# Densitymapbox renders smooth blobs rather than pinpricks.
_BLOB_OFFSETS = [
    (0, 0), (1.5, 0), (-1.5, 0), (0, 1.5), (0, -1.5),
    (1.1, 1.1), (-1.1, -1.1), (1.1, -1.1), (-1.1, 1.1),
]


def build_map_figure(
    state: dict,
    title: str = "Live density — M. Chinnaswamy",
    density_overrides: Optional[dict[str, int]] = None,
    height: int = 520,
) -> go.Figure:
    """Build a 2D map heatmap of zone density over the real stadium location."""
    zones_data = _load_zones()

    # Heatmap points: scatter a small cluster around each zone centre, each
    # weighted by current density so high-density zones dominate visually.
    h_lats: list[float] = []
    h_lons: list[float] = []
    h_weights: list[float] = []

    for z_meta in zones_data["zones"]:
        if z_meta.get("type") == "field":
            continue
        zid = z_meta["id"]
        live_zone = state["zones"].get(zid)
        if not live_zone:
            continue
        density = (density_overrides or {}).get(zid, live_zone["density_pct"])
        for dx, dy in _BLOB_OFFSETS:
            lon, lat = _xy_to_lonlat(z_meta["x"] + dx, z_meta["y"] + dy)
            h_lats.append(lat)
            h_lons.append(lon)
            h_weights.append(float(density))

    heat = go.Densitymapbox(
        lat=h_lats,
        lon=h_lons,
        z=h_weights,
        radius=26,
        opacity=0.55,
        colorscale=[
            [0.00, "rgba(74,144,226,0.0)"],
            [0.25, "rgba(74,144,226,0.55)"],
            [0.50, "rgba(126,211,33,0.70)"],
            [0.70, "rgba(245,166,35,0.80)"],
            [0.85, "rgba(233,75,75,0.88)"],
            [1.00, "rgba(155,28,28,0.95)"],
        ],
        zmin=0,
        zmax=100,
        showscale=True,
        colorbar=dict(
            title=dict(text="Density %", font=dict(color="white")),
            tickfont=dict(color="white"),
            outlinewidth=0,
            x=1.0,
        ),
        hoverinfo="skip",
        name="Density",
    )

    # Per-zone summary markers so hover gives the actual zone breakdown.
    z_lats, z_lons, z_text, z_hover = [], [], [], []
    for z_meta in zones_data["zones"]:
        if z_meta.get("type") == "field":
            continue
        zid = z_meta["id"]
        live_zone = state["zones"].get(zid)
        if not live_zone:
            continue
        density = (density_overrides or {}).get(zid, live_zone["density_pct"])
        lon, lat = _xy_to_lonlat(z_meta["x"], z_meta["y"])
        z_lats.append(lat)
        z_lons.append(lon)
        z_text.append(zid.replace("_STAND", "").replace("_", " "))
        z_hover.append(
            f"<b>{z_meta['name']}</b><br>"
            f"Density: {density}%<br>"
            f"Occupancy: {live_zone['occupants']:,}/{live_zone['capacity']:,}<br>"
            f"Gates: {', '.join(z_meta.get('gates', []) or ['—'])}"
        )
    zones_trace = go.Scattermapbox(
        lat=z_lats,
        lon=z_lons,
        mode="text",
        text=z_text,
        textfont=dict(size=10, color="white"),
        hovertext=z_hover,
        hoverinfo="text",
        name="Zones",
        showlegend=False,
    )

    # Gates — open/closed markers on the perimeter.
    g_lats, g_lons, g_colors, g_text, g_hover = [], [], [], [], []
    for g in state["gates"].values():
        lon, lat = _xy_to_lonlat(g["x"], g["y"])
        g_lats.append(lat)
        g_lons.append(lon)
        is_open = g.get("is_open", True)
        g_colors.append("#5BD96B" if is_open else "#E94B4B")
        g_text.append(g["id"])
        g_hover.append(
            f"<b>{g['id']}</b> · {g.get('kind','')}<br>"
            f"{g.get('road','')}<br>"
            f"Throughput: {g.get('throughput_per_min', 0):.0f}/min<br>"
            f"Status: {'OPEN' if is_open else 'CLOSED'}"
        )
    gates_trace = go.Scattermapbox(
        lat=g_lats,
        lon=g_lons,
        mode="markers+text",
        marker=dict(size=12, color=g_colors),
        text=g_text,
        textposition="top right",
        textfont=dict(size=9, color="#FFEFA8"),
        hovertext=g_hover,
        hoverinfo="text",
        name="Gates",
        showlegend=False,
    )

    fig = go.Figure(data=[heat, zones_trace, gates_trace])
    fig.update_layout(
        title=dict(text=title, font=dict(color="#0A0A0A", size=14)),
        mapbox=dict(
            style="open-street-map",
            center=dict(lat=CENTER_LAT, lon=CENTER_LON),
            zoom=16.4,
        ),
        margin=dict(l=0, r=0, t=30, b=0),
        height=height,
        paper_bgcolor="#EFEDE7",
        showlegend=False,
    )
    return fig
