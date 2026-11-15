import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.labels import build_location_sef, build_sef


class SefFormatTests(unittest.TestCase):
    def _part(self):
        return SimpleNamespace(
            code="PV-P-000001",
            model="A-E05-005",
            name="STM32G030F6P6",
            package="TSSOP-20",
            unit="PCS",
            _qty=1,
        )

    def test_part_sef_matches_supvan_text_format(self):
        data = json.loads(build_sef([self._part()]).decode("utf-8-sig"))
        self.assertNotIn("DataBaseMessage", data)
        page = data["Pages"][0]
        self.assertIsNone(page["ColumnIndex"])
        items = {json.loads(x["Json"])["name"]: json.loads(x["Json"]) for x in page["Items"]}
        self.assertEqual(items["part"]["Text"], "A-E05-005\x00")
        self.assertEqual(items["qty"]["Text"], "1\x00")
        for name, item in items.items():
            self.assertEqual(item["DataColumn"], "", name)
            self.assertEqual(item["DataColumnName"], "", name)
            self.assertFalse(item["IsBdExcel"], name)
            if item.get("Text"):
                self.assertTrue(item["Text"].endswith("\x00"), name)
        self.assertEqual(items["qr"]["Content"], "PV-P-000001")
        self.assertEqual(items["bc"]["Content"], "PV-P-000001")

    def test_gap_paper_settings(self):
        part = self._part()
        loc = SimpleNamespace(code="A-02-03", name="测试库位", _path="A-02-03")
        for raw in (build_sef([part]), build_location_sef([loc])):
            data = json.loads(raw.decode("utf-8-sig"))
            self.assertEqual(data["LabelKindName"], "间隙标签")
            models = [data["PageModel"]] + [p["PageModel"] for p in data["Pages"]]
            for container in models:
                model = json.loads(container["ModelJson"])
                self.assertEqual(model["PaperType"], 1)
                self.assertEqual(model["Padding"]["Top"], 1.5)
                self.assertEqual(model["Padding"]["Bottom"], 1.5)
                self.assertEqual(model["DieCutGap"], 2.0)

    def test_location_sef_uses_location_layout(self):
        loc = SimpleNamespace(code="A-02-03", name="测试库位", _path="A-02-03")
        data = json.loads(build_location_sef([loc]).decode("utf-8-sig"))
        page = data["Pages"][0]
        self.assertIsNone(page["ColumnIndex"])
        items = {json.loads(x["Json"])["name"]: json.loads(x["Json"]) for x in page["Items"]}
        self.assertIn("brand", items)
        self.assertIn("bc", items)
        for name in ("qty", "unit", "lotlabel", "lot"):
            self.assertTrue(items[name]["IsEmpty"], name)
        self.assertEqual(items["kind"]["Text"], "库位标签\x00")
        self.assertEqual(items["kind"]["X"], 16)
        self.assertEqual(items["kind"]["Y"], 10)
        self.assertEqual(items["brand"]["Text"], "元件库\x00")
        self.assertEqual(items["brand"]["X"], 294)
        self.assertEqual(items["brand"]["FontFormat"]["HorizontalAlignment"], 2)
        self.assertEqual(items["qr"]["Content"], "PV-L-A-02-03")
        self.assertEqual(items["bc"]["Content"], "PV-L-A-02-03")
        self.assertEqual(items["bc"]["DataColumn"], "")
        self.assertEqual(items["part"]["Text"], "A-02-03\x00")
        self.assertTrue(items["pkg"]["IsEmpty"])


if __name__ == "__main__":
    unittest.main()
