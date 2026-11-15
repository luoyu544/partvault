import os
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    BomItem,
    BomProject,
    Loan,
    Location,
    Part,
    PartSource,
    StockLot,
    StockMove,
    Supplier,
)
from app.services.backup import (
    BackupError,
    create_backup,
    inspect_archive,
    restore_archive,
)
from app.services.bom import BomError, assign_item, clear_item
from app.services.loans import borrow
from app.services.repair import legacy_return_report, repair_legacy_returns
from app.services.stock import adjust_stock, apply_stocktake, stock_in
from app.services.suppliers import (
    SupplierError,
    create_supplier,
    delete_supplier,
    update_supplier,
)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.old_lock = os.environ.get("PARTVAULT_WRITE_LOCK")
        os.environ["PARTVAULT_WRITE_LOCK"] = str(self.tmp_path / "write.lock")
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
        if self.old_lock is None:
            os.environ.pop("PARTVAULT_WRITE_LOCK", None)
        else:
            os.environ["PARTVAULT_WRITE_LOCK"] = self.old_lock
        self.tmp.cleanup()


class StocktakeTests(ServiceTestBase):
    def test_adjust_and_signed_stocktake_moves(self):
        stock_in(self.db, self.part.id, self.loc_a.id, 10, lot_no="L1",
                 date_code="2026-01", unit_price=1.5)
        lot = self.db.query(StockLot).one()
        self.assertAlmostEqual(adjust_stock(self.db, lot.id, 7, note="月度盘点"), -3)
        self.assertAlmostEqual(lot.qty, 7)
        move = self.db.query(StockMove).filter(StockMove.type == "adjust").one()
        self.assertAlmostEqual(move.qty, -3)
        self.assertEqual(move.ref_type, "stocktake")
        self.assertEqual(move.from_location_id, self.loc_a.id)

        self.assertAlmostEqual(adjust_stock(self.db, lot.id, 12, note="复盘"), 5)
        self.assertAlmostEqual(lot.qty, 12)
        plus = self.db.query(StockMove).filter(
            StockMove.type == "adjust", StockMove.qty > 0
        ).one()
        self.assertEqual(plus.to_location_id, self.loc_a.id)

    def test_stocktake_is_atomic_and_rejects_negative(self):
        stock_in(self.db, self.part.id, self.loc_a.id, 5, lot_no="L1")
        lot = self.db.query(StockLot).one()
        with self.assertRaises(ValueError):
            apply_stocktake(self.db, [(lot.id, 8), (999999, 2)])
        self.db.expire_all()
        self.assertAlmostEqual(self.db.get(StockLot, lot.id).qty, 5)
        with self.assertRaises(ValueError):
            apply_stocktake(self.db, [(lot.id, -1)])
        self.assertEqual(self.db.query(StockMove).filter(StockMove.type == "adjust").count(), 0)

    def test_stocktake_no_change(self):
        stock_in(self.db, self.part.id, self.loc_a.id, 5, lot_no="L1")
        lot = self.db.query(StockLot).one()
        result = apply_stocktake(self.db, [(lot.id, 5)])
        self.assertEqual(result["count"], 0)
        self.assertEqual(self.db.query(StockMove).filter(StockMove.type == "adjust").count(), 0)

    def test_concurrent_stock_in_with_file_database(self):
        db_path = self.tmp_path / "concurrent.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        db = Session()
        loc = Location(code="C", name="C")
        part = Part(code="PV-P-900001", model="THREAD-TEST")
        db.add_all([loc, part])
        db.commit()
        loc_id, part_id = loc.id, part.id
        db.close()

        errors = []

        def worker():
            session = Session()
            try:
                stock_in(session, part_id, loc_id, 1, lot_no="T")
            except Exception as exc:  # pragma: no cover - 失败时输出便于定位
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        check = Session()
        try:
            total = sum(float(row.qty or 0) for row in check.query(StockLot).all())
            self.assertAlmostEqual(total, 12)
        finally:
            check.close()
            engine.dispose()


class BomMatchTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.project = BomProject(name="测试项目")
        self.item = BomItem(project_id=1, model="BOM-MODEL", qty=2, designators="R1")
        self.db.add(self.project)
        self.db.commit()
        self.item.project_id = self.project.id
        self.db.add(self.item)
        self.db.commit()

    def test_assign_by_model_code_and_source_code(self):
        assign_item(self.db, self.project.id, self.item.id, part_query="R-1")
        self.assertEqual(self.item.part_id, self.part.id)
        self.assertTrue(self.item.matched)

        clear_item(self.db, self.project.id, self.item.id)
        self.assertIsNone(self.item.part_id)
        self.assertFalse(self.item.matched)

        self.db.add(PartSource(part_id=self.part.id, supplier_part_no="C12345"))
        self.db.commit()
        assign_item(self.db, self.project.id, self.item.id, part_query="C12345")
        self.assertEqual(self.item.part_id, self.part.id)

        clear_item(self.db, self.project.id, self.item.id)
        assign_item(self.db, self.project.id, self.item.id, part_query="PV-P-000001")
        self.assertEqual(self.item.part_id, self.part.id)

    def test_assign_missing_part_is_rejected(self):
        with self.assertRaises(BomError):
            assign_item(self.db, self.project.id, self.item.id, part_query="NO-SUCH")
        self.assertIsNone(self.item.part_id)


class SupplierTests(ServiceTestBase):
    def test_create_update_and_delete_unused_supplier(self):
        supplier = create_supplier(self.db, "测试店铺", "taobao", "https://example.com")
        updated = update_supplier(
            self.db, supplier.id, "测试店铺二店", "other", "", "备注",
        )
        self.assertEqual(updated.name, "测试店铺二店")
        self.assertEqual(updated.type, "other")
        deleted = delete_supplier(self.db, supplier.id)
        self.assertEqual(deleted.id, supplier.id)
        self.assertIsNone(self.db.get(Supplier, supplier.id))

    def test_duplicate_name_and_referenced_delete(self):
        first = create_supplier(self.db, "同名供应商")
        with self.assertRaises(SupplierError):
            create_supplier(self.db, "同名供应商")
        self.part.supplier_id = first.id
        self.db.commit()
        with self.assertRaises(SupplierError):
            delete_supplier(self.db, first.id)
        self.assertIsNotNone(self.db.get(Supplier, first.id))


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "partvault.db"
        self.upload_dir = self.root / "uploads"
        self.backup_dir = self.root / "backups"
        self.upload_dir.mkdir()
        self.backup_dir.mkdir()
        self.engine = create_engine(f"sqlite:///{self.db_path}", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        db = self.Session()
        loc = Location(code="A", name="A")
        part = Part(code="PV-P-000001", model="R-1")
        db.add_all([loc, part])
        db.commit()
        db.add(StockLot(part_id=part.id, location_id=loc.id, qty=3, lot_no="L1"))
        db.commit()
        db.close()

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_backup_inspect_and_restore(self):
        (self.upload_dir / "photo.txt").write_text("hello", encoding="utf-8")
        archive = create_backup(self.db_path, self.upload_dir, self.backup_dir)
        info = inspect_archive(archive)
        self.assertEqual(info["counts"]["parts"], 1)
        self.assertEqual(info["counts"]["stock_lots"], 1)
        self.assertTrue(info["has_uploads"])

        db = self.Session()
        db.query(StockLot).delete()
        db.query(Part).delete()
        db.commit()
        db.close()
        (self.upload_dir / "photo.txt").unlink()

        result = restore_archive(
            archive, self.db_path, self.upload_dir, self.backup_dir,
        )
        self.assertTrue((self.backup_dir / result["safety_backup"]).exists())
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM stock_lots").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual((self.upload_dir / "photo.txt").read_text(encoding="utf-8"), "hello")

    def test_reject_missing_database_and_zip_slip(self):
        bad = self.backup_dir / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("other.txt", "x")
        with self.assertRaises(BackupError):
            inspect_archive(bad)

        evil = self.backup_dir / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape.txt", "x")
            zf.writestr("partvault.db", "not a db")
        with self.assertRaises(BackupError):
            inspect_archive(evil)

    def test_restore_without_uploads_keeps_existing_files(self):
        archive = create_backup(self.db_path, self.upload_dir, self.backup_dir)
        (self.upload_dir / "keep.txt").write_text("keep", encoding="utf-8")
        restore_archive(archive, self.db_path, self.upload_dir, self.backup_dir)
        self.assertTrue((self.upload_dir / "keep.txt").exists())


class LegacyReturnRepairTests(ServiceTestBase):
    def _legacy_return(self, qty: float):
        return stock_in(
            self.db, self.part.id, self.loc_b.id, qty,
            note="旧版归还", ref_type="return", ref_id=1,
        )

    def test_repairs_single_source_metadata(self):
        stock_in(self.db, self.part.id, self.loc_a.id, 10, lot_no="L1",
                 date_code="2026-01", unit_price=1.5, supplier_id=None)
        loan = borrow(self.db, self.part.id, self.loc_a.id, 4, borrower="张三")
        returned = stock_in(
            self.db, self.part.id, self.loc_b.id, 2,
            note="旧版归还", ref_type="return", ref_id=loan.id,
        )
        loan.returned_qty = 2
        self.db.commit()
        self.assertTrue(returned.lot_no is None)

        report = legacy_return_report(self.db)
        self.assertEqual(report.repairable_lots, 1)
        result = repair_legacy_returns(self.db)
        self.assertEqual(result["repaired_lots"], 1)
        self.db.refresh(returned)
        self.assertEqual(returned.lot_no, "L1")
        self.assertEqual(returned.date_code, "2026-01")
        self.assertAlmostEqual(returned.unit_price, 1.5)
        move = self.db.query(StockMove).filter(
            StockMove.ref_type == "return", StockMove.lot_id == returned.id
        ).one()
        self.assertAlmostEqual(move.unit_price, 1.5)
        self.assertIn("历史归还批次信息已补全", move.note)

    def test_cross_lot_return_is_not_guessed(self):
        stock_in(self.db, self.part.id, self.loc_a.id, 2, lot_no="L1",
                 date_code="D1", unit_price=1)
        stock_in(self.db, self.part.id, self.loc_a.id, 5, lot_no="L2",
                 date_code="D2", unit_price=2)
        loan = borrow(self.db, self.part.id, self.loc_a.id, 4)
        returned = stock_in(
            self.db, self.part.id, self.loc_b.id, 4,
            note="旧版归还", ref_type="return", ref_id=loan.id,
        )
        loan.returned_qty = 4
        self.db.commit()
        report = legacy_return_report(self.db)
        self.assertEqual(report.ambiguous_lots, 1)
        self.assertEqual(repair_legacy_returns(self.db)["repaired_lots"], 0)
        self.db.refresh(returned)
        self.assertIsNone(returned.lot_no)

    def test_no_source_is_reported(self):
        loan = Loan(part_id=self.part.id, qty=2, returned_qty=2, status="returned")
        self.db.add(loan)
        self.db.commit()
        returned = self._legacy_return(2)
        report = legacy_return_report(self.db)
        self.assertEqual(report.no_source_lots, 1)
        self.assertEqual(repair_legacy_returns(self.db)["repaired_lots"], 0)
        self.db.refresh(returned)
        self.assertIsNone(returned.lot_no)


if __name__ == "__main__":
    unittest.main()
