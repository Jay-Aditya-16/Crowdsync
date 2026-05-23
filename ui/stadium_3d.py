"""Plotly 3D digital twin of Chinnaswamy stadium.

Each stand is a wedge-shaped Mesh3d cuboid whose height = current density
percentage. Gates appear as ground-plane markers (green=open, red=closed).
Pitch is a small inner rectangle.

This is the "live digital twin" feature: feed it any state dict from
agents.whatif_simulator and you get a frame of the model.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import plotly.graph_objects as go

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_zones() -> dict:
    return json.loads((DATA_DIR / "stadium_zones.json").read_text())


def _polar_to_xy(angle_deg: float, radius: float, cx: float, cy: float) -> tuple[float, float]:
    """Compass-style: 0° = north (positive y), clockwise."""
    a = math.radians(90 - angle_deg)  # convert compass to math
    return cx + radius * math.cos(a), cy + radius * math.sin(a)


def _color_for_density(d: int) -> str:
    if d >= 95: return "#9B1C1C"
    if d >= 85: return "#E94B4B"
    if d >= 70: return "#F5A623"
    if d >= 50: return "#7ED321"
    return "#4A90E2"


def _wedge_mesh(angle_deg: float, span_deg: float, r_inner: float, r_outer: float,
                cx: float, cy: float, height: float, color: str,
                name: str, hovertext: str, segments: int = 6) -> go.Mesh3d:
    """Build a wedge-shaped 3D bar by triangulating two arcs + flat top/bottom."""
    half = span_deg / 2
    angles = [angle_deg - half + i * (span_deg / segments) for i in range(segments + 1)]

    # Bottom + top vertices, inner arc then outer arc.
    xs, ys, zs = [], [], []
    # Inner-bottom
    for a in angles:
        x, y = _polar_to_xy(a, r_inner, cx, cy)
        xs.append(x); ys.append(y); zs.append(0)
    # Outer-bottom
    for a in angles:
        x, y = _polar_to_xy(a, r_outer, cx, cy)
        xs.append(x); ys.append(y); zs.append(0)
    # Inner-top
    for a in angles:
        x, y = _polar_to_xy(a, r_inner, cx, cy)
        xs.append(x); ys.append(y); zs.append(height)
    # Outer-top
    for a in angles:
        x, y = _polar_to_xy(a, r_outer, cx, cy)
        xs.append(x); ys.append(y); zs.append(height)

    n = segments + 1
    IB = 0           # inner-bottom block start
    OB = n           # outer-bottom
    IT = 2 * n       # inner-top
    OT = 3 * n       # outer-top

    i_idx, j_idx, k_idx = [], [], []

    def quad(a, b, c, d):
        i_idx.extend([a, a]); j_idx.extend([b, c]); k_idx.extend([c, d])

    # Bottom (inner -> outer)
    for s in range(segments):
        quad(IB + s, IB + s + 1, OB + s + 1, OB + s)
    # Top
    for s in range(segments):
        quad(IT + s, OT + s, OT + s + 1, IT + s + 1)
    # Inner wall
    for s in range(segments):
        quad(IB + s, IT + s, IT + s + 1, IB + s + 1)
    # Outer wall
    for s in range(segments):
        quad(OB + s, OB + s + 1, OT + s + 1, OT + s)
    # End caps (start + end of wedge)
    quad(IB, OB, OT, IT)                                 # start cap
    quad(IB + segments, IT + segments, OT + segments, OB + segments)  # end cap

    return go.Mesh3d(
        x=xs, y=ys, z=zs,
        i=i_idx, j=j_idx, k=k_idx,
        color=color,
        opacity=0.92,
        flatshading=True,
        name=name,
        hovertext=hovertext,
        hoverinfo="text",
        showscale=False,
    )


def _ground_plane(geometry: dict) -> go.Mesh3d:
    bb = geometry.get("boundary_box", {"min_x": 0, "max_x": 100, "min_y": 0, "max_y": 100})
    x0, x1 = bb["min_x"], bb["max_x"]
    y0, y1 = bb["min_y"], bb["max_y"]
    return go.Mesh3d(
        x=[x0, x1, x1, x0],
        y=[y0, y0, y1, y1],
        z=[0, 0, 0, 0],
        i=[0, 0],
        j=[1, 2],
        k=[2, 3],
        color="#264D2C",
        opacity=0.5,
        flatshading=True,
        hoverinfo="skip",
        showscale=False,
    )


def _pitch(geometry: dict) -> go.Mesh3d:
    cx = geometry.get("center", {}).get("x", 50)
    cy = geometry.get("center", {}).get("y", 50)
    s = 4  # half-size
    return go.Mesh3d(
        x=[cx - s, cx + s, cx + s, cx - s],
        y=[cy - s, cy - s, cy + s, cy + s],
        z=[0.1, 0.1, 0.1, 0.1],
        i=[0, 0], j=[1, 2], k=[2, 3],
        color="#D4B886",
        opacity=1.0,
        flatshading=True,
        hoverinfo="skip",
        showscale=False,
    )


def _gates_trace(gates: list[dict]) -> go.Scatter3d:
    xs, ys, zs, colors, labels = [], [], [], [], []
    for g in gates:
        xs.append(g["x"])
        ys.append(g["y"])
        zs.append(0.5)
        colors.append("#5BD96B" if g.get("is_open", True) else "#E94B4B")
        labels.append(
            f"<b>{g['id']}</b> ({g.get('kind','')})<br>"
            f"{g.get('road','')}<br>"
            f"Throughput: {g.get('throughput_per_min', 0):.0f}/min<br>"
            f"Status: {'OPEN' if g.get('is_open', True) else 'CLOSED'}"
        )
    return go.Scatter3d(
        x=xs, y=ys, z=zs,
        mode="markers+text",
        marker=dict(size=7, color=colors, symbol="diamond", line=dict(color="white", width=1)),
        text=[g["id"] for g in gates],
        textposition="top center",
        textfont=dict(size=8, color="white"),
        hovertext=labels,
        hoverinfo="text",
        name="Gates",
    )


def _landmarks_trace(landmarks: list[dict]) -> go.Scatter3d:
    xs = [l["x"] for l in landmarks]
    ys = [l["y"] for l in landmarks]
    zs = [0.2] * len(landmarks)
    return go.Scatter3d(
        x=xs, y=ys, z=zs,
        mode="text",
        text=[l["name"] for l in landmarks],
        textposition="middle center",
        textfont=dict(size=9, color="#CCCCCC"),
        hoverinfo="skip",
        showlegend=False,
    )


def build_3d_figure(
    state: dict,
    title: str = "Digital Twin — Live",
    density_overrides: Optional[dict[str, int]] = None,
    height: int = 520,
) -> go.Figure:
    """Build a 3D digital-twin figure.

    state: a baseline_state-shape dict (from agents.whatif_simulator).
           Must contain `zones` (dict of zone dicts with density_pct) and `gates`.
    density_overrides: optional {zone_id: density_pct} to override state values
                       (used by What-If to render scenario state without mutating).
    """
    zones_data = _load_zones()
    geometry = zones_data.get("geometry", {})
    cx = geometry.get("center", {}).get("x", 50)
    cy = geometry.get("center", {}).get("y", 50)

    traces: list = []
    traces.append(_ground_plane(geometry))
    traces.append(_pitch(geometry))

    for zid, z in state["zones"].items():
        density = (density_overrides or {}).get(zid, z["density_pct"])
        height_3d = max(2, density * 0.35)  # scale density to visible height
        color = _color_for_density(density)
        hover = (
            f"<b>{z['name']}</b><br>"
            f"Density: {density}%<br>"
            f"Occupancy: {z['occupants']:,}/{z['capacity']:,}<br>"
            f"Gates: {', '.join(z.get('gates', []))}"
        )
        wedge = _wedge_mesh(
            angle_deg=z["angle_deg"],
            span_deg=z["span_deg"],
            r_inner=z["r_inner"],
            r_outer=z["r_outer"],
            cx=cx, cy=cy,
            height=height_3d,
            color=color,
            name=z["name"],
            hovertext=hover,
        )
        traces.append(wedge)

    traces.append(_gates_trace(list(state["gates"].values())))
    traces.append(_landmarks_trace(zones_data.get("landmarks", [])))

    fig = go.Figure(data=traces)
    fig.update_layout(
        title=dict(text=title, font=dict(color="#0A0A0A", size=14)),
        scene=dict(
            xaxis=dict(visible=False, range=[0, 100]),
            yaxis=dict(visible=False, range=[0, 100]),
            zaxis=dict(visible=False, range=[0, 50]),
            aspectmode="manual",
            aspectratio=dict(x=1, y=1, z=0.4),
            camera=dict(eye=dict(x=1.4, y=-1.4, z=1.1)),
            bgcolor="#EFEDE7",
        ),
        paper_bgcolor="#EFEDE7",
        margin=dict(l=0, r=0, t=30, b=0),
        height=height,
        showlegend=False,
    )
    return fig


def state_with_perturbation_applied(perturbation: Optional[dict]) -> dict:
    """Helper for UI: return baseline state with perturbation pre-applied."""
    from agents.whatif_simulator import apply_perturbation, current_baseline_state
    state = current_baseline_state()
    if perturbation:
        state = apply_perturbation(state, perturbation)
    return state
