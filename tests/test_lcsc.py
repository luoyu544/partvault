import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models import Base, Location, Part, PartSource, Supplier
from app.services import lcsc


def raw_item(**overrides):
    data = {
        "id": 18398,
        "code": "C17710",
        "name": "厚膜电阻 470Ω ±1% 125mW",
        "model": "0805W8F4700T5E",
        "brandName": "UNI-ROYAL(厚声)",
        "standard": "0805",
        "catalogName": "贴片电阻",
        "stockNumber": "13800",
        "priceList": [
            {"startNumber": 100, "price": 0.0293},
            {"startNumber": 1000, "price": 0.0232},
        ],
        "pdfFileUrl": "/upload/public/pdf/source/test.pdf",
        "image": "https://example.test/a.jpg<$>https://example.test/b.jpg",
        "device_info": {
            "uuid": "dev-uuid",
            "description": "类型:厚膜电阻;阻值:470Ω;精度:±1%;",
            "attributes": {
                "Manufacturer": "UNI-ROYAL(厚声)",
                "Manufacturer Part": "0805W8F4700T5E",
                "Supplier Footprint": "0805",
                "Value": "470Ω",
                "Tolerance": "±1%",
                "Power(Watts)": "125mW",
                "Voltage-Supply(Max)": "150V",
                "Symbol": "skip-me",
            },
        },
    }
    data.update(overrides)
    return data


class NormalizeTests(unittest.TestCase):
    def test_domestic_item(self):
        item = lcsc._normalize_item(raw_item())
        self.assertEqual(item["code"], "C17710")
        self.assertEqual(item["mpn"], "0805W8F4700T5E")
        self.assertEqual(item["package"], "0805")
        self.assertEqual(item["stock"], 13800)
        self.assertEqual(item["price_min"], 0.0232)
        self.assertEqual(item["product_url"], "https://item.szlcsc.com/18398.html")
        self.assertEqual(item["image_url"], "https://example.test/a.jpg")
        self.assertTrue(item["datasheet_url"].endswith("/upload/public/pdf/source/test.pdf"))
        self.assertEqual(item["attributes"]["值"], "470Ω")
        self.assertEqual(item["attributes"]["精度"], "±1%")
        self.assertNotIn("Symbol", item["attributes"])
        self.assertIn("值: 470Ω", item["attributes_text"])

    def test_build_query(self):
        self.assertEqual(lcsc.build_query("贴片电容", "C1525", "0603"), "C1525")
        self.assertEqual(lcsc.build_query("贴片电容 100nF", "", "0603"), "贴片电容 100nF 0603")
        self.assertEqual(lcsc.build_query("", "C17710", "0805"), "C17710")
        self.assertEqual(lcsc.build_query("", "", "", "TI"), "TI")


class ExtractCodeTests(unittest.TestCase):
    def test_extract_from_barcode_wrappers(self):
        self.assertEqual(lcsc.extract_code("C1525"), "C1525")
        self.assertEqual(lcsc.extract_code(" c17710 "), "C17710")
        self.assertEqual(lcsc.extract_code("LCSC:C1525"), "C1525")
        self.assertEqual(lcsc.extract_code("https://example.test/item?code=C1525&qty=100"), "C1525")
        self.assertEqual(lcsc.extract_code('{"code":"C1525","qty":100}'), "C1525")

    def test_does_not_treat_other_codes_as_lcsc(self):
        self.assertEqual(lcsc.extract_code("PV-P-000001"), "")
        self.assertEqual(lcsc.extract_code("PV-L-A-01"), "")
        self.assertEqual(lcsc.extract_code("C152"), "")
        self.assertEqual(lcsc.extract_code("ABC1525"), "")


class RankTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            lcsc._normalize_item(raw_item()),
            lcsc._normalize_item(raw_item(
                id=2, code="C99999", model="OTHER-470", name="厚膜电阻 470Ω ±5% 125mW",
                standard="0603", stockNumber="0",
            )),
            lcsc._normalize_item(raw_item(
                id=3, code="C88888", model="CAP-100N", name="贴片电容 100nF ±10% 25V",
                standard="0603", stockNumber="100",
            )),
        ]

    def test_exact_code_first_and_recommended(self):
        ranked = lcsc.rank_results("C17710", self.items)
        self.assertEqual(ranked[0]["code"], "C17710")
        self.assertTrue(ranked[0]["is_exact_code"])
        self.assertTrue(ranked[0]["recommended"])

    def test_name_search_never_recommended_without_exact(self):
        ranked = lcsc.rank_results("100nF 50V X7R 0603", self.items)
        self.assertTrue(all(not x["is_exact_code"] for x in ranked))
        self.assertTrue(all(not x["is_exact_mpn"] for x in ranked))
        self.assertTrue(all(not x["recommended"] for x in ranked))

    def test_package_mismatch_warns(self):
        ranked = lcsc.rank_results("470Ω", self.items, package="0603")
        target = next(x for x in ranked if x["code"] == "C17710")
        self.assertFalse(target["package_ok"])
        self.assertTrue(any("封装不一致" in w for w in target["warnings"]))

    def test_missing_key_parameter_warns(self):
        ranked = lcsc.rank_results("100nF 50V", self.items)
        target = next(x for x in ranked if x["code"] == "C88888")
        self.assertTrue(any("关键参数" in w for w in target["warnings"]))

    def test_exact_mpn_recommended(self):
        ranked = lcsc.rank_results("0805W8F4700T5E", self.items)
        self.assertTrue(ranked[0]["is_exact_mpn"])
        self.assertTrue(ranked[0]["recommended"])


class SearchTests(unittest.TestCase):
    def setUp(self):
        lcsc.clear_cache()

    def tearDown(self):
        lcsc.clear_cache()

    def test_search_mocked_and_cached(self):
        with patch.object(lcsc, "_fetch_products", return_value=([raw_item()], 1)) as mocked:
            first = lcsc.search("C17710")
            second = lcsc.search("C17710")
        self.assertEqual(first["results"][0]["code"], "C17710")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(mocked.call_count, 1)

    def test_code_query_filters_fuzzy_codes(self):
        fuzzy = [
            raw_item(id=10, code="C17717710", model="FUZZY-A"),
            raw_item(id=11, code="C177104", model="FUZZY-B"),
            raw_item(),
        ]
        with patch.object(lcsc, "_fetch_products", return_value=(fuzzy, 3)):
            data = lcsc.search("C17710")
        self.assertEqual([x["code"] for x in data["results"]], ["C17710"])
        self.assertTrue(data["results"][0]["is_exact_code"])

    def test_lookup_exact(self):
        with patch.object(lcsc, "_fetch_products", return_value=([raw_item()], 1)):
            item = lcsc.lookup("C17710")
        self.assertEqual(item["code"], "C17710")
        self.assertTrue(item["is_exact_code"])

    def test_lookup_missing_raises(self):
        with patch.object(lcsc, "_fetch_products", return_value=([], 0)):
            with self.assertRaises(lcsc.LcscError):
                lcsc.lookup("C17710")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.taobao = Supplier(name="淘宝", type="taobao")
        self.db.add(self.taobao)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_blank_mode_keeps_existing_model_and_primary_code(self):
        part = Part(code="PV-P-000001", model="TAOBAO-470", name="", supplier_id=self.taobao.id,
                    supplier_part_no="TB-001", attributes="备注: 拆机")
        self.db.add(part)
        self.db.flush()
        candidate = lcsc._normalize_item(raw_item())
        result = lcsc.apply_candidate(self.db, part, candidate, mode="blank")
        self.db.commit()
        self.assertEqual(part.model, "TAOBAO-470")
        self.assertEqual(part.supplier_part_no, "TB-001")
        self.assertEqual(part.supplier_id, self.taobao.id)
        self.assertEqual(part.package, "0805")
        self.assertIn("值: 470Ω", part.attributes)
        self.assertIn("备注: 拆机", part.attributes)
        self.assertIn("立创来源", result["changed"])
        source = self.db.query(PartSource).filter_by(part_id=part.id).one()
        self.assertEqual(source.supplier_part_no, "C17710")
        self.assertEqual(source.currency, "CNY")
        self.assertIn("立创参考价", source.note)

    def test_overwrite_mode_updates_but_keeps_taobao_primary_code(self):
        part = Part(code="PV-P-000002", model="TAOBAO-470", supplier_id=self.taobao.id,
                    supplier_part_no="TB-002")
        self.db.add(part)
        self.db.flush()
        candidate = lcsc._normalize_item(raw_item())
        lcsc.apply_candidate(self.db, part, candidate, mode="overwrite")
        self.db.commit()
        self.assertEqual(part.model, "0805W8F4700T5E")
        self.assertEqual(part.supplier_id, self.taobao.id)
        self.assertEqual(part.supplier_part_no, "TB-002")
        self.assertEqual(part.attributes, candidate["attributes_text"])

    def test_empty_supplier_gets_lcsc_primary(self):
        part = Part(code="PV-P-000003", model="")
        self.db.add(part)
        self.db.flush()
        candidate = lcsc._normalize_item(raw_item())
        lcsc.apply_candidate(self.db, part, candidate, mode="blank")
        self.db.commit()
        lcsc_supplier = self.db.query(Supplier).filter_by(type="lcsc").one()
        self.assertEqual(part.supplier_id, lcsc_supplier.id)
        self.assertEqual(part.supplier_part_no, "C17710")
        self.assertEqual(part.model, "0805W8F4700T5E")

    def test_create_part_from_candidate_for_scan_intake(self):
        loc = Location(code="A-01", name="A-01")
        self.db.add(loc)
        self.db.commit()
        candidate = lcsc._normalize_item(raw_item())
        part = lcsc.create_part_from_candidate(
            self.db, candidate, location_id=loc.id, note="扫码测试"
        )
        self.db.commit()
        self.assertRegex(part.code, r"^PV-P-\d{6}$")
        self.assertEqual(part.model, "0805W8F4700T5E")
        self.assertEqual(part.name, "厚膜电阻 470Ω ±1% 125mW")
        self.assertEqual(part.package, "0805")
        self.assertEqual(part.supplier_part_no, "C17710")
        self.assertEqual(part.default_location_id, loc.id)
        self.assertEqual(part.note, "扫码测试")
        self.assertIn("值: 470Ω", part.attributes)
        source = self.db.query(PartSource).filter_by(part_id=part.id).one()
        self.assertEqual(source.supplier_part_no, "C17710")
        self.assertIn("立创参考价", source.note)
        self.assertEqual(part.min_stock, 0)
        self.assertEqual(part.unit, "PCS")


if __name__ == "__main__":
    unittest.main()
