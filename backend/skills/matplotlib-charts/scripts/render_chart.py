"""Render bar, pie, or line charts from a UTF-8 JSON configuration."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

# Windows 服务进程的用户目录可能不可写，将字体缓存放到临时目录。
os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(tempfile.gettempdir()) / "xu-agent-matplotlib"),
)

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib import font_manager


SUPPORTED_TYPES = {"bar", "pie", "line"}
DEFAULT_COLORS = [
    "#2563EB",
    "#F97316",
    "#16A34A",
    "#DC2626",
    "#9333EA",
    "#0891B2",
    "#CA8A04",
    "#4F46E5",
]


def _configure_fonts() -> None:
    candidates = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "PingFang SC",
        "WenQuanYi Micro Hei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    installed = {font.name for font in font_manager.fontManager.ttflist}
    matplotlib.rcParams["font.sans-serif"] = [
        font for font in candidates if font in installed
    ] or ["DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must contain numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must contain finite numbers")
    return number


def _labels(config: dict[str, Any]) -> list[str]:
    raw = config.get("labels")
    if not isinstance(raw, list) or not raw:
        raise ValueError("labels must be a non-empty list")
    return [str(item) for item in raw]


def _series(config: dict[str, Any], label_count: int) -> list[dict[str, Any]]:
    raw = config.get("series")
    if not isinstance(raw, list) or not raw:
        raise ValueError("series must be a non-empty list")

    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"series[{index}] must be an object")
        values = item.get("values")
        if not isinstance(values, list) or len(values) != label_count:
            raise ValueError(
                f"series[{index}].values length must equal labels length"
            )
        result.append(
            {
                "name": str(item.get("name") or f"系列 {index + 1}"),
                "values": [
                    _finite_number(value, f"series[{index}].values")
                    for value in values
                ],
            }
        )
    return result


def _colors(config: dict[str, Any]) -> list[str]:
    raw = config.get("colors")
    if raw is None:
        return DEFAULT_COLORS
    if not isinstance(raw, list) or not raw:
        raise ValueError("colors must be a non-empty list")
    return [str(color) for color in raw]


def _render_bar(ax: Any, config: dict[str, Any]) -> None:
    labels = _labels(config)
    series = _series(config, len(labels))
    colors = _colors(config)
    x_positions = list(range(len(labels)))
    group_width = 0.8
    bar_width = group_width / len(series)

    for index, item in enumerate(series):
        offset = (index - (len(series) - 1) / 2) * bar_width
        bars = ax.bar(
            [position + offset for position in x_positions],
            item["values"],
            width=bar_width,
            label=item["name"],
            color=colors[index % len(colors)],
        )
        if config.get("show_values", True):
            ax.bar_label(bars, fmt="%g", padding=3, fontsize=9)

    ax.set_xticks(x_positions, labels)
    if config.get("legend", True) and len(series) > 1:
        ax.legend()


def _render_line(ax: Any, config: dict[str, Any]) -> None:
    labels = _labels(config)
    series = _series(config, len(labels))
    colors = _colors(config)
    x_positions = list(range(len(labels)))

    for index, item in enumerate(series):
        line = ax.plot(
            x_positions,
            item["values"],
            marker="o",
            linewidth=2,
            markersize=5,
            label=item["name"],
            color=colors[index % len(colors)],
        )[0]
        if config.get("show_values", False):
            for x_value, y_value in zip(x_positions, item["values"]):
                ax.annotate(
                    f"{y_value:g}",
                    (x_value, y_value),
                    xytext=(0, 7),
                    textcoords="offset points",
                    ha="center",
                    fontsize=9,
                    color=line.get_color(),
                )

    ax.set_xticks(x_positions, labels)
    if config.get("legend", True):
        ax.legend()


def _render_pie(ax: Any, config: dict[str, Any]) -> None:
    labels = _labels(config)
    raw_values = config.get("values")
    if not isinstance(raw_values, list) or len(raw_values) != len(labels):
        raise ValueError("values length must equal labels length")
    values = [_finite_number(value, "values") for value in raw_values]
    if any(value < 0 for value in values):
        raise ValueError("pie values must not be negative")
    if sum(values) <= 0:
        raise ValueError("pie values must sum to more than zero")

    colors = _colors(config)
    show_values = config.get("show_values", True)
    wedges, _, _ = ax.pie(
        values,
        labels=labels,
        autopct="%1.1f%%" if show_values else None,
        startangle=90,
        counterclock=False,
        colors=[colors[index % len(colors)] for index in range(len(values))],
        wedgeprops={"linewidth": 1, "edgecolor": "white"},
    )
    ax.axis("equal")
    if config.get("legend", False):
        ax.legend(wedges, labels, loc="center left", bbox_to_anchor=(1, 0.5))


def render_chart(config: dict[str, Any], output_path: Path) -> Path:
    chart_type = str(config.get("type") or "").lower()
    if chart_type not in SUPPORTED_TYPES:
        raise ValueError("type must be one of: bar, pie, line")

    width = _finite_number(config.get("width", 10), "width")
    height = _finite_number(config.get("height", 6), "height")
    dpi = int(_finite_number(config.get("dpi", 160), "dpi"))
    if width <= 0 or height <= 0 or dpi <= 0:
        raise ValueError("width, height, and dpi must be greater than zero")

    _configure_fonts()
    fig, ax = plt.subplots(figsize=(width, height))
    try:
        if chart_type == "bar":
            _render_bar(ax, config)
        elif chart_type == "line":
            _render_line(ax, config)
        else:
            _render_pie(ax, config)

        ax.set_title(str(config.get("title") or ""), fontsize=15, pad=14)
        if chart_type != "pie":
            ax.set_xlabel(str(config.get("x_label") or ""))
            ax.set_ylabel(str(config.get("y_label") or ""))
            if config.get("grid", True):
                ax.grid(axis="y", linestyle="--", alpha=0.3)
                ax.set_axisbelow(True)

        fig.tight_layout()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=dpi, bbox_inches="tight")
    finally:
        plt.close(fig)
    return output_path.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a bar, pie, or line chart from JSON."
    )
    parser.add_argument("--input", required=True, help="UTF-8 JSON config path")
    parser.add_argument("--output", required=True, help="Output PNG path")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser()
    with input_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("input JSON root must be an object")

    result = render_chart(config, output_path)
    print(f"Chart written to: {result}")
    print(f"Size: {result.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
