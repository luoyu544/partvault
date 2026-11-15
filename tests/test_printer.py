import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import printer
from app.services import label_layout


def _part(**overrides):
    data = dict(
        code="PV-P-000001",
        model="C1525",
        name="100nF 50V X7R",
        package="0603",
        unit="PCS",
        _qty=4000,
        _loc="A-02-03",
        _price=0.0123,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


class PrinterJobTests(unittest.TestCase):
    def test_part_job_matches_sef_layout(self):
        job = printer.build_part_job(
            [_part()], brand="元件库", show_price=True,
            kind="元件标签", today="2026-10-26",
        )
        label = job["labels"][0]
        self.assertEqual(label["qr"]["data"], "PV-P-000001")
        self.assertEqual(label["qr"]["x"], 32.25)
        self.assertEqual(label["qr"]["y"], 5.0)
        self.assertEqual(label["qr"]["w"], 16.25)
        self.assertEqual(label["qr"]["h"], 16.25)
        self.assertEqual(label["barcode"]["data"], "PV-P-000001")
        self.assertEqual(label["barcode"]["x"], 2.0)
        self.assertEqual(label["barcode"]["y"], 23.25)
        self.assertEqual(label["barcode"]["w"], 46.0)
        self.assertEqual(label["barcode"]["h"], 5.25)

        texts = {t["text"]: t for t in label["texts"]}
        self.assertEqual(texts["元件库"]["y"], 1.25)
        self.assertEqual(texts["元件库"]["size"], 1.62)
        self.assertTrue(texts["元件库"]["bold"])
        self.assertEqual(texts["元件标签"]["x"], 12.5)
        self.assertEqual(texts["C1525"]["y"], 4.25)
        self.assertTrue(texts["C1525"]["bold"])
        self.assertEqual(texts["C1525"]["size"], 4.25)
        self.assertEqual(texts["100nF 50V X7R"]["y"], 12.5)
        self.assertEqual(texts["0603"]["y"], 16.5)
        self.assertEqual(texts["4000"]["y"], 16.5)
        self.assertEqual(texts["PCS"]["y"], 16.75)
        self.assertEqual(texts["LOT"]["y"], 20.0)
        self.assertEqual(texts["2026-10-26 · ¥0.0123"]["y"], 20.0)
        self.assertIn("image", label)
        self.assertTrue(label["image"])

    def test_long_name_shrinks_and_keeps_ellipsis(self):
        name = "这是一个非常非常长的物料名称需要换行显示并且继续变长直到必须截断为止超长文本" * 2
        job = printer.build_part_job([_part(name=name, model="")])
        label = job["labels"][0]
        name_text = [t for t in label["texts"] if t["y"] == 12.5][0]
        self.assertLess(name_text["size"], 1.75)
        self.assertTrue(name_text["text"].endswith("…"))
        self.assertLessEqual(
            label_layout._width_em(name_text["text"], name_text["bold"])
            * name_text["size"],
            28.75,
        )

    def test_no_name_and_price_is_optional(self):
        job = printer.build_part_job([_part(name="", _price=12.5)])
        texts = {t["text"]: t for t in job["labels"][0]["texts"]}
        self.assertNotIn("¥12.5", texts)
        self.assertIn("LOT", texts)
        self.assertTrue(any(t.startswith("2026-") for t in texts))

    def test_price_is_added_to_lot_line(self):
        job = printer.build_part_job([_part(_price=12.5)], show_price=True,
                                     today="2026-10-26")
        texts = {t["text"]: t for t in job["labels"][0]["texts"]}
        self.assertIn("2026-10-26 · ¥12.5", texts)

    def test_barcode_uses_full_code_when_headline_is_clipped(self):
        code = "PV-P-000001-VERY-LONG-CODE-123"
        job = printer.build_part_job([_part(code=code, model=code)])
        label = job["labels"][0]
        self.assertEqual(label["qr"]["data"], code)
        self.assertEqual(label["barcode"]["data"], code)

        clipped = [t for t in label["texts"] if t["text"].endswith("…")]
        self.assertTrue(clipped)
        self.assertNotEqual(clipped[0]["text"], code)

    def test_location_job_qr_and_layout(self):
        loc = SimpleNamespace(code="A-02-03", name="测试库位",
                              _path="A-02 / 抽屉3")
        job = printer.build_location_job([loc], brand="元件库", kind="库位标签")
        label = job["labels"][0]
        self.assertEqual(label["qr"]["data"], "PV-L-A-02-03")
        self.assertEqual(label["barcode"]["data"], "PV-L-A-02-03")

        code_text = [t for t in label["texts"] if t["text"] == "A-02-03"]
        self.assertEqual(len(code_text), 1)
        self.assertEqual(code_text[0]["y"], 4.25)
        self.assertTrue(code_text[0]["bold"])

        by_text = {t["text"]: t for t in label["texts"]}
        self.assertEqual(by_text["测试库位"]["y"], 12.5)
        self.assertEqual(by_text["A-02 / 抽屉3"]["y"], 16.5)
        self.assertEqual(by_text["库位标签"]["y"], 1.25)
        self.assertEqual(by_text["元件库"]["y"], 1.25)
        self.assertEqual(by_text["元件库"]["align"], 2)

    @unittest.skipUnless(
        sys.platform == "win32" and printer.helper_available(),
        "Supvan print helper is only built on Windows",
    )
    def test_helper_accepts_generated_job(self):
        job = printer.build_part_job([_part()], brand="元件库", show_price=True)
        result = printer._run_helper("validate", job, timeout=20)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("totalPages"), 1)

        loc = SimpleNamespace(code="A-02-03", name="测试库位",
                              _path="A-02 / 抽屉3")
        result = printer._run_helper(
            "validate", printer.build_location_job([loc]), timeout=20
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("totalPages"), 1)


if __name__ == "__main__":
    unittest.main()
