import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "matplotlib-charts"
    / "scripts"
    / "render_chart.py"
)
SPEC = importlib.util.spec_from_file_location("matplotlib_charts_render", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MatplotlibChartsSkillTest(unittest.TestCase):
    def test_render_supported_chart_types(self):
        configs = {
            "bar": {
                "type": "bar",
                "title": "季度销售额",
                "labels": ["第一季度", "第二季度", "第三季度"],
                "series": [{"name": "销售额", "values": [12, 18, 15]}],
            },
            "pie": {
                "type": "pie",
                "title": "市场份额",
                "labels": ["产品 A", "产品 B", "其他"],
                "values": [45, 35, 20],
            },
            "line": {
                "type": "line",
                "title": "访问趋势",
                "labels": ["周一", "周二", "周三"],
                "series": [{"name": "访问量", "values": [100, 135, 128]}],
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            for chart_type, config in configs.items():
                with self.subTest(chart_type=chart_type):
                    output = Path(temp_dir) / f"{chart_type}.png"
                    result = MODULE.render_chart(config, output)
                    self.assertEqual(result, output.resolve())
                    self.assertGreater(output.stat().st_size, 1_000)
                    with Image.open(output) as image:
                        self.assertEqual(image.format, "PNG")
                        self.assertGreaterEqual(image.width, 1_000)
                        self.assertGreaterEqual(image.height, 600)

    def test_rejects_mismatched_series_length(self):
        config = {
            "type": "bar",
            "labels": ["A", "B"],
            "series": [{"name": "数据", "values": [1]}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "length must equal"):
                MODULE.render_chart(config, Path(temp_dir) / "invalid.png")


if __name__ == "__main__":
    unittest.main()
