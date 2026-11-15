import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import ocr


def analyze(lines, codes=None):
    return ocr._analyze(lines, codes or [], [])


class QuantityRuleTests(unittest.TestCase):
    def test_purchase_spec_times_one(self):
        lines = [
            "拍一件=10个",
            "商家编码：PM003859",
            "单排母双排母2.54mm排针母座排",
            "1x8P2.54MM直插单排母黄色（10个）",
            "￥3.03",
            "x1",
        ]
        out = analyze(lines)
        self.assertEqual(out["fields"]["qty"], "10")
        self.assertEqual(out["qty_detail"]["spec_qty"], 10)
        self.assertEqual(out["qty_detail"]["purchase_qty"], 1)
        self.assertEqual(out["fields"]["supplier_part_no"], "PM003859")
        self.assertNotEqual(out["fields"]["model"], "PM003859")
        self.assertIn("1x8P", out["fields"]["package"])
        self.assertIn("2.54mm", out["fields"]["package"])

    def test_purchase_spec_times_two(self):
        lines = ["1x8P 2.54MM直插单排母（10个）", "￥3.03", "x2"]
        out = analyze(lines)
        self.assertEqual(out["qty_detail"]["spec_qty"], 10)
        self.assertEqual(out["qty_detail"]["purchase_qty"], 2)
        self.assertEqual(out["fields"]["qty"], "20")
        self.assertIn("10", out["qty_detail"]["formula"])

    def test_mobile_inductor(self):
        lines = [
            "0420/0520/0630/0650/1040/1265/1770/0850一体成型贴片功率电感",
            "10uH[印100];0850[8*8*5]（5个）",
            "￥3.60",
            "X1",
        ]
        out = analyze(lines)
        self.assertEqual(out["fields"]["qty"], "5")
        self.assertEqual(out["qty_detail"]["spec_qty"], 5)
        self.assertEqual(out["fields"]["package"], "0850")

    def test_spec_rolls_not_sheets(self):
        lines = ["三防50*30*1250张*2卷横版单排[（强粘）]", "￥3.20", "x1"]
        out = analyze(lines)
        self.assertEqual(out["fields"]["qty"], "2")
        self.assertEqual(out["qty_detail"]["spec_qty"], 2)

    def test_screw_spec(self):
        lines = ["M3*4-100个", "￥1.80", "x1"]
        out = analyze(lines)
        self.assertEqual(out["fields"]["qty"], "100")

    def test_label_qty_is_final(self):
        lines = [
            "5001-A-C11-003",
            "QTY:5个",
            "C55145034 BD",
            "插拔式接线端子",
            "1x2P3.81mm排数：2PIN",
            "莱联",
        ]
        out = analyze(lines, ["X242008196171"])
        self.assertEqual(out["source"], "label")
        self.assertEqual(out["fields"]["qty"], "5")
        self.assertEqual(out["qty_detail"]["purchase_qty"], 1)
        self.assertEqual(out["fields"]["supplier_part_no"], "C55145034")
        self.assertEqual(out["fields"]["model"], "5001-A-C11-003")
        self.assertIn("1x2P", out["fields"]["package"])
        self.assertIn("3.81mm", out["fields"]["package"])
        self.assertEqual(out["fields"]["brand"], "莱联")

    def test_label_barcode_payload(self):
        lines = [
            "ST（意法半导体）",
            "STM32G030F6P6",
            "TSSOP-20",
            "C724040静",
            "4243-A-E05-005",
            "QTY:1个",
        ]
        code = "{on:20260824,pc:C724040,pm:STM32G030F6P6,qty:1,pc:X242008196171}"
        out = analyze(lines, [code])
        self.assertEqual(out["source"], "label")
        self.assertEqual(out["fields"]["qty"], "1")
        self.assertEqual(out["fields"]["supplier_part_no"], "C724040")
        self.assertEqual(out["fields"]["model"], "STM32G030F6P6")
        self.assertEqual(out["fields"]["package"], "TSSOP-20")
        self.assertEqual(out["fields"]["brand"], "ST")

    def test_qty_without_spec(self):
        lines = ["贴片电阻 0603 10KΩ", "￥0.10", "x2"]
        out = analyze(lines)
        self.assertEqual(out["fields"]["qty"], "2")
        self.assertIsNone(out["qty_detail"]["spec_qty"])


if __name__ == "__main__":
    unittest.main()
