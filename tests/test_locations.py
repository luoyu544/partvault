import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models import Base, Loan, Location, Part, StockLot, StockMove
from app.services.locations import (
    TEMP_LOCATION_CODE,
    LocationError,
    create_location,
    create_locations_batch,
    delete_locations,
    move_parts_from_location,
    parse_location_batch,
    update_location,
)


class LocationBatchParserTests(unittest.TestCase):
    def test_parse_supported_separators_and_default_name(self):
        drafts = parse_location_batch(
            "A-01\n"
            "A-02,第二层\n"
            "A-03\t第三层\n"
            "A-04，第四层\n"
            "A-05|第五层\n"
            "# 注释行\n"
            "\n"
        )
        self.assertEqual([d.code for d in drafts], ["A-01", "A-02", "A-03", "A-04", "A-05"])
        self.assertEqual(drafts[0].name, "A-01")
        self.assertEqual(drafts[1].name, "第二层")
        self.assertEqual(drafts[4].name, "第五层")
        self.assertEqual(drafts[1].line_no, 2)

    def test_parse_rejects_duplicate_codes_without_case_sensitivity(self):
        with self.assertRaises(LocationError) as ctx:
            parse_location_batch("A-01\nA-02\na-01,重复")
        self.assertIn("第 3 行", str(ctx.exception))
        self.assertIn("第 1 行", str(ctx.exception))

    def test_parse_rejects_empty_code(self):
        with self.assertRaises(LocationError):
            parse_location_batch(",只有名称")

    def test_parse_limits_batch_size(self):
        text = "\n".join(f"L-{i:03d}" for i in range(201))
        with self.assertRaises(LocationError):
            parse_location_batch(text)


class LocationBatchCreateTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.parent = Location(code="W1", name="默认仓库", kind="warehouse")
        self.db.add(self.parent)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_create_batch_is_atomic_and_uses_common_parent_and_kind(self):
        created = create_locations_batch(
            self.db, "A-01\nA-02,第二层", parent_id=self.parent.id, kind="box",
        )
        self.assertEqual([loc.code for loc in created], ["A-01", "A-02"])
        self.assertEqual(created[1].name, "第二层")
        self.assertTrue(all(loc.parent_id == self.parent.id for loc in created))
        self.assertTrue(all(loc.kind == "box" for loc in created))
        self.assertEqual(self.db.query(Location).count(), 3)

    def test_duplicate_existing_code_does_not_create_anything(self):
        create_location(self.db, "A-01", parent_id=self.parent.id)
        with self.assertRaises(LocationError):
            create_locations_batch(self.db, "A-02\nA-01\nA-03", parent_id=self.parent.id)
        self.assertEqual(self.db.query(Location).count(), 2)

    def test_create_single_location_defaults_name_to_code(self):
        loc = create_location(self.db, "  B-01  ", parent_id=self.parent.id, kind="cell")
        self.assertEqual(loc.code, "B-01")
        self.assertEqual(loc.name, "B-01")

    def test_invalid_parent_and_kind_are_rejected(self):
        with self.assertRaises(LocationError):
            create_locations_batch(self.db, "A-01", parent_id=9999)
        with self.assertRaises(LocationError):
            create_locations_batch(self.db, "A-01", parent_id=self.parent.id, kind="unknown")
        self.assertEqual(self.db.query(Location).count(), 1)

    def test_temp_code_is_reserved(self):
        with self.assertRaises(LocationError) as ctx:
            create_location(self.db, "temp")
        self.assertIn("保留", str(ctx.exception))
        with self.assertRaises(LocationError):
            create_locations_batch(self.db, "A-01\nTEMP")
        self.assertEqual(self.db.query(Location).count(), 1)


class LocationDeleteAndMoveTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.root = Location(code="W1", name="默认仓库", kind="warehouse")
        self.db.add(self.root)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _location(self, code, parent=None):
        loc = Location(code=code, name=code, kind="cell", parent_id=parent.id if parent else None)
        self.db.add(loc)
        self.db.commit()
        return loc

    def _part(self, code, model, default_location_id=None):
        part = Part(code=code, model=model, default_location_id=default_location_id)
        self.db.add(part)
        self.db.commit()
        return part

    def _lot(self, part, location, qty=1, lot_no="L1", price=1.25):
        lot = StockLot(
            part_id=part.id, location_id=location.id if location else None,
            qty=qty, lot_no=lot_no, date_code="2412", unit_price=price,
        )
        self.db.add(lot)
        self.db.commit()
        return lot

    def test_delete_location_moves_stock_to_temp_and_records_move(self):
        source = self._location("A-01", self.root)
        part = self._part("PV-P-000001", "R1", default_location_id=source.id)
        lot = self._lot(part, source, qty=5)

        result = delete_locations(self.db, [source.id])

        temp = self.db.query(Location).filter(Location.code == TEMP_LOCATION_CODE).one()
        moved_lot = self.db.get(StockLot, lot.id)
        self.assertEqual(result.deleted_codes, ["A-01"])
        self.assertEqual(result.moved_lot_count, 1)
        self.assertEqual(result.moved_part_count, 1)
        self.assertEqual(result.moved_qty, 5)
        self.assertEqual(result.temp_code, TEMP_LOCATION_CODE)
        self.assertIsNone(self.db.get(Location, source.id))
        self.assertEqual(moved_lot.location_id, temp.id)
        self.assertEqual(moved_lot.lot_no, "L1")
        self.assertEqual(moved_lot.unit_price, 1.25)
        self.assertIsNone(self.db.get(Part, part.id).default_location_id)

        move = self.db.query(StockMove).filter(StockMove.type == "move").one()
        self.assertEqual(move.qty, 5)
        self.assertEqual(move.from_location_id, source.id)
        self.assertEqual(move.to_location_id, temp.id)
        self.assertEqual(move.lot_id, lot.id)

    def test_batch_delete_requires_children_and_handles_parent_with_children(self):
        child = self._location("A-01", self.root)
        part = self._part("PV-P-000002", "C1")
        child_lot = self._lot(part, child, qty=2)
        root_lot = self._lot(part, self.root, qty=3, lot_no="ROOT")

        with self.assertRaises(LocationError) as ctx:
            delete_locations(self.db, [self.root.id])
        self.assertIn("未选中的子库位", str(ctx.exception))
        self.assertEqual(self.db.query(Location).count(), 2)
        self.assertIsNone(self.db.query(Location).filter(Location.code == TEMP_LOCATION_CODE).first())
        self.assertEqual(self.db.get(StockLot, child_lot.id).location_id, child.id)

        result = delete_locations(self.db, [self.root.id, child.id])
        temp = self.db.query(Location).filter(Location.code == TEMP_LOCATION_CODE).one()
        self.assertEqual(result.moved_lot_count, 2)
        self.assertEqual(result.moved_part_count, 1)
        self.assertEqual(result.moved_qty, 5)
        self.assertEqual(self.db.query(Location).count(), 1)
        self.assertEqual(self.db.get(StockLot, child_lot.id).location_id, temp.id)
        self.assertEqual(self.db.get(StockLot, root_lot.id).location_id, temp.id)

    def test_temp_location_cannot_be_deleted(self):
        source = self._location("A-01", self.root)
        part = self._part("PV-P-000003", "C2")
        self._lot(part, source, qty=1)
        delete_locations(self.db, [source.id])
        temp = self.db.query(Location).filter(Location.code == TEMP_LOCATION_CODE).one()

        with self.assertRaises(LocationError) as ctx:
            delete_locations(self.db, [temp.id])
        self.assertIn("临时库位", str(ctx.exception))
        self.assertIsNotNone(self.db.get(Location, temp.id))

    def test_delete_location_with_open_loan_is_blocked(self):
        source = self._location("A-01", self.root)
        part = self._part("PV-P-000004", "C3")
        self._lot(part, source, qty=1)
        self.db.add(Loan(part_id=part.id, qty=1, out_location_id=source.id, status="open"))
        self.db.commit()

        with self.assertRaises(LocationError) as ctx:
            delete_locations(self.db, [source.id])
        self.assertIn("未归还借出", str(ctx.exception))
        self.assertIsNotNone(self.db.get(Location, source.id))
        self.assertIsNone(self.db.query(Location).filter(Location.code == TEMP_LOCATION_CODE).first())

    def test_move_only_selected_parts_from_location(self):
        source = self._location("A-01", self.root)
        target = self._location("A-02", self.root)
        part1 = self._part("PV-P-000005", "R2")
        part2 = self._part("PV-P-000006", "R3")
        lot1 = self._lot(part1, source, qty=5, lot_no="L-A")
        lot2 = self._lot(part2, source, qty=3, lot_no="L-B")

        result = move_parts_from_location(
            self.db, from_location_id=source.id, to_location_id=target.id,
            part_ids=[part1.id],
        )

        self.assertEqual(result.part_count, 1)
        self.assertEqual(result.lot_count, 1)
        self.assertEqual(result.qty, 5)
        self.assertEqual(result.to_code, "A-02")
        self.assertEqual(self.db.get(StockLot, lot1.id).location_id, target.id)
        self.assertEqual(self.db.get(StockLot, lot2.id).location_id, source.id)
        move = self.db.query(StockMove).filter(StockMove.type == "move").one()
        self.assertEqual(move.lot_id, lot1.id)
        self.assertEqual(move.from_location_id, source.id)
        self.assertEqual(move.to_location_id, target.id)

    def test_move_rejects_same_location_and_part_without_stock(self):
        source = self._location("A-01", self.root)
        target = self._location("A-02", self.root)
        part = self._part("PV-P-000007", "R4")

        with self.assertRaises(LocationError):
            move_parts_from_location(
                self.db, from_location_id=source.id, to_location_id=source.id,
                part_ids=[part.id],
            )
        with self.assertRaises(LocationError) as ctx:
            move_parts_from_location(
                self.db, from_location_id=source.id, to_location_id=target.id,
                part_ids=[part.id],
            )
        self.assertIn("没有库存", str(ctx.exception))
        self.assertEqual(self.db.query(StockMove).count(), 0)

    def test_update_location_fields_and_default_name(self):
        loc = self._location("A-01", self.root)
        updated = update_location(
            self.db, loc.id, code="  A-09  ", name="", parent_id=None,
            kind="box", note="  新备注  ",
        )
        self.assertEqual(updated.code, "A-09")
        self.assertEqual(updated.name, "A-09")
        self.assertIsNone(updated.parent_id)
        self.assertEqual(updated.kind, "box")
        self.assertEqual(updated.note, "新备注")

    def test_update_location_rejects_duplicate_and_cycle(self):
        parent = self._location("A", self.root)
        child = self._location("A-01", parent)
        other = self._location("B-01", self.root)

        with self.assertRaises(LocationError) as ctx:
            update_location(self.db, other.id, code="a-01", parent_id=self.root.id)
        self.assertIn("已存在", str(ctx.exception))

        with self.assertRaises(LocationError) as ctx:
            update_location(self.db, parent.id, code="A", parent_id=child.id)
        self.assertIn("子库位", str(ctx.exception))

        with self.assertRaises(LocationError):
            update_location(self.db, parent.id, code="A", parent_id=parent.id)
        self.assertEqual(self.db.get(Location, parent.id).parent_id, self.root.id)

    def test_update_temp_keeps_code_and_parent_but_allows_name(self):
        temp = Location(
            code=TEMP_LOCATION_CODE, name="临时库位", kind="cell",
            parent_id=self.root.id,
        )
        self.db.add(temp)
        self.db.commit()

        updated = update_location(
            self.db, temp.id, code="NEW-TEMP", name="暂存区",
            parent_id=None, kind="box", note="保留",
        )
        self.assertEqual(updated.code, TEMP_LOCATION_CODE)
        self.assertEqual(updated.parent_id, self.root.id)
        self.assertEqual(updated.name, "暂存区")
        self.assertEqual(updated.kind, "box")
        self.assertEqual(updated.note, "保留")

        normal = self._location("A-01", self.root)
        with self.assertRaises(LocationError):
            update_location(self.db, normal.id, code="temp", parent_id=None)


if __name__ == "__main__":
    unittest.main()
