#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Рендер PNG-превью из .drawio (те же координаты, что и в схеме).

Конвенции (бриф §7): matplotlib, figsize W/100 x H/100, dpi 160;
шрифты эмпирически: заголовок 12, контейнеры 7, блоки 6.5-7.

Использование:
    python tools/render_drawio.py "Архитектура мультипоточной системы.drawio"
    python tools/render_drawio.py in.drawio out.png
"""

import html
import sys
import xml.etree.ElementTree as ET

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as matplotlib_pyplot
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

matplotlib_pyplot.rcParams["font.family"] = "DejaVu Sans"

EDGE_COLOR = "#4a5a7a"
TEXT_COLOR = "#1a1a2e"

# drawio fontSize -> размер шрифта PNG (бриф: заголовок 12, контейнеры 7, блоки 6.5-7)
FONT_MAP = {15: 12, 12: 7, 11: 7, 10: 6.5, 9: 6}


def parse_style(style):
    props = {}
    for item in (style or "").split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            props[key] = value
        elif item:
            props[item] = True
    return props


def png_font_size(drawio_size):
    return FONT_MAP.get(int(float(drawio_size)), 7)


def parse_drawio(path):
    tree = ET.parse(path)
    model = tree.getroot().find("diagram").find("mxGraphModel")
    width = int(float(model.get("pageWidth", "1560")))
    height = int(float(model.get("pageHeight", "800")))
    vertices, edges = [], []
    for cell in model.find("root").findall("mxCell"):
        geometry = cell.find("mxGeometry")
        if cell.get("vertex") == "1":
            vertices.append(
                {
                    "id": cell.get("id"),
                    "value": html.unescape(cell.get("value") or ""),
                    "style": parse_style(cell.get("style")),
                    "x": float(geometry.get("x", "0")),
                    "y": float(geometry.get("y", "0")),
                    "w": float(geometry.get("width", "0")),
                    "h": float(geometry.get("height", "0")),
                }
            )
        elif cell.get("edge") == "1":
            points = []
            array = geometry.find("Array") if geometry is not None else None
            if array is not None:
                for point in array.findall("mxPoint"):
                    points.append((float(point.get("x")), float(point.get("y"))))
            edges.append(
                {
                    "id": cell.get("id"),
                    "value": html.unescape(cell.get("value") or ""),
                    "style": parse_style(cell.get("style")),
                    "source": cell.get("source"),
                    "target": cell.get("target"),
                    "points": points,
                }
            )
    return width, height, vertices, edges


def anchor(vertex, fx, fy):
    return (
        vertex["x"] + fx * vertex["w"],
        vertex["y"] + fy * vertex["h"],
    )


def route(start, end, waypoints):
    """Ортогональный маршрут: явные точки или H-V-H / V-H-V."""
    if waypoints:
        return [start] + list(waypoints) + [end]
    (x0, y0), (x1, y1) = start, end
    if abs(x1 - x0) < 1 or abs(y1 - y0) < 1:
        return [start, end]
    if abs(x1 - x0) >= abs(y1 - y0):
        mid_x = (x0 + x1) / 2.0
        return [start, (mid_x, y0), (mid_x, y1), end]
    mid_y = (y0 + y1) / 2.0
    return [start, (x0, mid_y), (x1, mid_y), end]


def longest_segment_mid(points):
    best, best_len = (points[0], points[1]), -1.0
    for a, b in zip(points, points[1:]):
        length = abs(b[0] - a[0]) + abs(b[1] - a[1])
        if length > best_len:
            best, best_len = (a, b), length
    (a, b) = best
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def render(drawio_path, png_path):
    width, height, vertices, edges = parse_drawio(drawio_path)

    # drawio: y вниз; matplotlib: y вверх
    def ty(y):
        return height - y

    fig = matplotlib_pyplot.figure(figsize=(width / 100.0, height / 100.0), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(0, height)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    by_id = {v["id"]: v for v in vertices}

    # вершины
    for v in vertices:
        style = v["style"]
        fs = png_font_size(style.get("fontSize", 11))
        bold = str(style.get("fontStyle", "0")) not in ("0", "None") and "1" in str(
            style.get("fontStyle", "0")
        )
        lines = v["value"].split("<br>") if v["value"] else []
        has_fill = "fillColor" in style
        if has_fill:
            dashed = "dashed" in style
            box = FancyBboxPatch(
                (v["x"], ty(v["y"] + v["h"])),
                v["w"],
                v["h"],
                boxstyle="round,pad=0,rounding_size=6",
                linewidth=1.0,
                linestyle=(0, (4, 3)) if dashed else "solid",
                edgecolor=style.get("strokeColor", "#8fa3c7"),
                facecolor=style.get("fillColor", "#eef2f7"),
                zorder=2,
            )
            ax.add_patch(box)
        if not lines:
            continue
        align = style.get("align", "center")
        ha = {"left": "left", "center": "center"}.get(align, "center")
        tx = v["x"] + 6 if ha == "left" else v["x"] + v["w"] / 2.0
        if has_fill and style.get("verticalAlign") == "top":
            spacing_top = float(style.get("spacingTop", 8))
            line_h = fs * 1.35
            total_h = line_h * len(lines)
            cy = v["y"] + spacing_top + total_h / 2.0
        else:
            cy = v["y"] + v["h"] / 2.0
        ax.text(
            tx,
            ty(cy),
            "\n".join(lines),
            ha=ha,
            va="center",
            fontsize=fs,
            fontweight="bold" if bold else "normal",
            color=style.get("fontColor", TEXT_COLOR),
            zorder=3,
            linespacing=1.35,
        )

    # рёбра
    for e in edges:
        style = e["style"]
        src, dst = by_id.get(e["source"]), by_id.get(e["target"])
        if not src or not dst:
            continue
        exit_fx = float(style.get("exitX", 1))
        exit_fy = float(style.get("exitY", 0.5))
        entry_fx = float(style.get("entryX", 0))
        entry_fy = float(style.get("entryY", 0.5))
        start = anchor(src, exit_fx, exit_fy)
        end = anchor(dst, entry_fx, entry_fy)
        points = route(start, end, e["points"])
        pts = [(x, ty(y)) for x, y in points]
        dashed = "dashed" in style
        linestyle = (0, (4, 3)) if dashed else "solid"
        # все сегменты, кроме последнего
        for a, b in zip(pts, pts[1:-1]):
            ax.plot(
                [a[0], b[0]],
                [a[1], b[1]],
                color=EDGE_COLOR,
                linewidth=1.0,
                linestyle=linestyle,
                zorder=1,
                solid_capstyle="round",
            )
        # последний сегмент — со стрелкой
        arrow = FancyArrowPatch(
            pts[-2],
            pts[-1],
            arrowstyle="-|>",
            mutation_scale=10,
            color=EDGE_COLOR,
            linewidth=1.0,
            linestyle=linestyle,
            zorder=1,
        )
        ax.add_patch(arrow)
        if e["value"]:
            mx, my = longest_segment_mid(points)
            ax.text(
                mx,
                ty(my),
                e["value"],
                ha="center",
                va="center",
                fontsize=6.5,
                color=TEXT_COLOR,
                zorder=4,
                bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor="none"),
            )

    fig.savefig(png_path, dpi=160, facecolor="white")
    matplotlib_pyplot.close(fig)
    print("OK: {} -> {} ({}x{})".format(drawio_path, png_path, width, height))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    drawio = sys.argv[1]
    png = sys.argv[2] if len(sys.argv) > 2 else drawio.rsplit(".", 1)[0] + ".png"
    render(drawio, png)
