import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models import Base, Location, Part, PartSource, StockLot, StockMove
from app.services.importer import commit_rows
from app.services.loans import borrow, return_loans
from app.services.stock import stock_in


class InventoryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.loc_a = Location(code="A", name="A")
        self.loc_b = Location(code="B", name="B")
        self.part = Part(code="PV-P-000001", model="R-1", name="电阻")
        self.db.add_all([self.loc_a, self.loc_b, self.part])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_stock_in_rejects_missing_location_without_writing(self):
        with self.assertRaises(ValueError):
            stock_in(self.db, self.part.id, 999999, 2, lot_no="BAD")
        self.assertEqual(self.db.query(StockLot).count(), 0)
        self.assertEqual(self.db.query(StockMove).count(), 0)

    def test_stock_in_rejects_negative_price(self):
        with self.assertRaises(ValueError):
            stock_in(self.db, self.part.id, self.loc_a.id, 2, unit_price=-1)
        self.assertEqual(self.db.query(StockLot).count(), 0)

    def test_stock_in_keeps_date_code_and_price(self):
        stock_in(
            self.db, self.part.id, self.loc_a.id, 2,
            lot_no="L1", date_code="2026-01", unit_price=1.5,
        )
        lot = self.db.query(StockLot).one()
        self.assertEqual(lot.lot_no, "L1")
        self.assertEqual(lot.date_code, "2026-01")
        self.assertAlmostEqual(lot.unit_price, 1.5)
        self.assertAlmostEqual(lot.qty, 2)

    def test_return_restores_original_lot_metadata(self):
        stock_in(
            self.db, self.part.id, self.loc_a.id, 10,
            lot_no="L1", date_code="2026-01", unit_price=1.5,
        )
        loan = borrow(self.db, self.part.id, self.loc_a.id, 4, borrower="张三")
        self.assertEqual(loan.outstanding_qty, 4)
        return_loans(self.db, self.part.id, self.loc_b.id, 2, note="先还一半")
        self.assertEqual(loan.returned_qty, 2)
        returned = self.db.query(StockLot).filter(
            StockLot.part_id == self.part.id, StockLot.location_id == self.loc_b.id
        ).one()
        self.assertEqual(returned.lot_no, "L1")
        self.assertEqual(returned.date_code, "2026-01")
        self.assertAlmostEqual(returned.unit_price, 1.5)
        self.assertAlmostEqual(returned.qty, 2)

        return_loans(self.db, self.part.id, self.loc_b.id, 2, note="还完")
        self.db.refresh(loan)
        self.assertEqual(loan.status, "returned")
        self.assertAlmostEqual(returned.qty, 4)
        self.assertAlmostEqual(
            sum(row.qty for row in self.db.query(StockLot.qty).filter(
                StockLot.part_id == self.part.id
            ).all()),
            10,
        )

    def test_return_spans_multiple_borrowed_lots(self):
        stock_in(
            self.db, self.part.id, self.loc_a.id, 2,
            lot_no="L1", date_code="D1", unit_price=1,
        )
        stock_in(
            self.db, self.part.id, self.loc_a.id, 5,
            lot_no="L2", date_code="D2", unit_price=2,
        )
        borrow(self.db, self.part.id, self.loc_a.id, 4, borrower="李四")
        return_loans(self.db, self.part.id, self.loc_b.id, 4)
        rows = {
            lot.lot_no: lot
            for lot in self.db.query(StockLot).filter(
                StockLot.part_id == self.part.id, StockLot.location_id == self.loc_b.id
            ).all()
        }
        self.assertEqual(set(rows), {"L1", "L2"})
        self.assertAlmostEqual(rows["L1"].qty, 2)
        self.assertAlmostEqual(rows["L2"].qty, 2)
        self.assertEqual(rows["L1"].date_code, "D1")
        self.assertEqual(rows["L2"].date_code, "D2")
        self.assertAlmostEqual(rows["L1"].unit_price, 1)
        self.assertAlmostEqual(rows["L2"].unit_price, 2)

    def test_import_matches_part_source_instead_of_creating_duplicate(self):
        self.part.supplier_part_no = "TB-001"
        self.db.add(PartSource(part_id=self.part.id, supplier_part_no="C900001"))
        self.db.commit()
        rows = [[
            "C900001", "ALT-MODEL", "ALT NAME", "品牌", "0603",
            "2", "1.2", "L9", "2026-02", "A",
        ]]
        mapping = {
            "supplier_part_no": 0, "model": 1, "name": 2, "brand": 3,
            "package": 4, "qty": 5, "price": 6, "lot_no": 7,
            "date_code": 8, "location_code": 9,
        }
        stats = commit_rows(self.db, rows, mapping, self.loc_a.id)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["stocked"], 1)
        self.assertEqual(self.db.query(Part).count(), 1)
        lot = self.db.query(StockLot).one()
        self.assertEqual(lot.part_id, self.part.id)
        self.assertEqual(lot.lot_no, "L9")
        self.assertEqual(lot.date_code, "2026-02")
        self.assertAlmostEqual(lot.qty, 2)

    def test_import_warns_when_location_code_is_missing(self):
        rows = [["NEW-1", "NEW-1", "新型号", "", "0603", "1", "", "", "", "NOPE"]]
        mapping = {
            "supplier_part_no": 0, "model": 1, "name": 2, "brand": 3,
            "package": 4, "qty": 5, "price": 6, "lot_no": 7,
            "date_code": 8, "location_code": 9,
        }
        stats = commit_rows(self.db, rows, mapping, self.loc_a.id)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(len(stats["warnings"]), 1)
        self.assertIn("NOPE", stats["warnings"][0])
        lot = self.db.query(StockLot).one()
        self.assertEqual(lot.location_id, self.loc_a.id)


if __name__ == "__main__":
    unittest.main()
