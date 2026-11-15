from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    parent_id = Column(Integer, ForeignKey("categories.id"))
    code = Column(String(40))
    attr_schema = Column(Text)  # JSON: [{"key":"阻值","unit":"Ω"}...]
    sort = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)


class Location(Base):
    __tablename__ = "locations"
    id = Column(Integer, primary_key=True)
    code = Column(String(60), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    parent_id = Column(Integer, ForeignKey("locations.id"))
    kind = Column(String(30), default="cell")  # warehouse/cabinet/shelf/box/cell
    note = Column(Text)
    sort = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    type = Column(String(20), default="other")  # lcsc/taobao/other
    homepage = Column(String(300))
    note = Column(Text)


class Part(Base):
    __tablename__ = "parts"
    id = Column(Integer, primary_key=True)
    code = Column(String(40), unique=True, nullable=False)
    model = Column(String(200), nullable=False, index=True)
    name = Column(String(300))
    brand = Column(String(120))
    package = Column(String(80))
    category_id = Column(Integer, ForeignKey("categories.id"))
    default_location_id = Column(Integer, ForeignKey("locations.id"))
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    supplier_part_no = Column(String(120), index=True)
    datasheet_url = Column(String(500))
    purchase_url = Column(String(500))
    image_path = Column(String(300))
    attributes = Column(Text)
    note = Column(Text)
    status = Column(String(20), default="active")
    min_stock = Column(Float, default=0)
    unit = Column(String(20), default="PCS")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class PartSource(Base):
    __tablename__ = "part_sources"
    id = Column(Integer, primary_key=True)
    part_id = Column(Integer, ForeignKey("parts.id"), index=True, nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    supplier_part_no = Column(String(120))
    url = Column(String(500))
    price = Column(Float)
    currency = Column(String(8), default="CNY")
    moq = Column(Integer)
    last_purchased_at = Column(DateTime)
    note = Column(Text)


class StockLot(Base):
    __tablename__ = "stock_lots"
    id = Column(Integer, primary_key=True)
    part_id = Column(Integer, ForeignKey("parts.id"), index=True, nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), index=True)
    qty = Column(Float, default=0, nullable=False)
    lot_no = Column(String(120))
    date_code = Column(String(60))
    unit_price = Column(Float)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    received_at = Column(DateTime, default=datetime.now)
    note = Column(Text)


class StockMove(Base):
    __tablename__ = "stock_moves"
    id = Column(Integer, primary_key=True)
    part_id = Column(Integer, ForeignKey("parts.id"), index=True, nullable=False)
    lot_id = Column(Integer, ForeignKey("stock_lots.id"))
    type = Column(String(20), nullable=False)  # in/out/adjust/move
    qty = Column(Float, nullable=False)
    from_location_id = Column(Integer, ForeignKey("locations.id"))
    to_location_id = Column(Integer, ForeignKey("locations.id"))
    unit_price = Column(Float)
    ref_type = Column(String(20), default="manual")  # manual/ocr/import/bom/scan
    ref_id = Column(Integer)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class Loan(Base):
    __tablename__ = "loans"
    id = Column(Integer, primary_key=True)
    part_id = Column(Integer, ForeignKey("parts.id"), index=True, nullable=False)
    qty = Column(Float, nullable=False)
    returned_qty = Column(Float, default=0, nullable=False)
    out_location_id = Column(Integer, ForeignKey("locations.id"), index=True)
    return_location_id = Column(Integer, ForeignKey("locations.id"))
    borrower = Column(String(120))
    note = Column(Text)
    status = Column(String(20), default="open", index=True)  # open/returned
    created_at = Column(DateTime, default=datetime.now, index=True)
    returned_at = Column(DateTime)

    @property
    def outstanding_qty(self) -> float:
        return max(0.0, float(self.qty or 0) - float(self.returned_qty or 0))


class OcrJob(Base):
    __tablename__ = "ocr_jobs"
    id = Column(Integer, primary_key=True)
    image_path = Column(String(300))
    engine = Column(String(40), default="rapidocr")
    raw_text = Column(Text)
    parsed_json = Column(Text)
    status = Column(String(20), default="pending")  # pending/confirmed/failed
    part_id = Column(Integer, ForeignKey("parts.id"))
    created_at = Column(DateTime, default=datetime.now)


class Attachment(Base):
    __tablename__ = "attachments"
    id = Column(Integer, primary_key=True)
    part_id = Column(Integer, ForeignKey("parts.id"), index=True)
    kind = Column(String(30), default="screenshot")
    path = Column(String(300), nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(80), primary_key=True)
    value = Column(Text)


class BomProject(Base):
    __tablename__ = "bom_projects"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class BomItem(Base):
    __tablename__ = "bom_items"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("bom_projects.id"), nullable=False)
    part_id = Column(Integer, ForeignKey("parts.id"))
    model = Column(String(200))
    qty = Column(Float, default=0)
    designators = Column(String(300))
    note = Column(Text)
    matched = Column(Boolean, default=False)
