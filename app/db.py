from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .models import Base, Category, Location, Setting, Supplier

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR = DATA_DIR / "backups"

for d in (DATA_DIR, UPLOAD_DIR, EXPORT_DIR, BACKUP_DIR):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "partvault.db"
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
    finally:
        cur.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.query(Category).count() == 0:
            cats = [
                ("电阻", 1), ("电容", 2), ("电感", 3), ("二极管", 4),
                ("三极管/MOS", 5), ("集成电路", 6), ("连接器", 7),
                ("晶振/振荡器", 8), ("传感器", 9), ("模块/开发板", 10),
                ("紧固件", 11), ("工具/耗材", 12), ("其他", 99),
            ]
            for name, sort in cats:
                db.add(Category(name=name, sort=sort))
        if db.query(Location).count() == 0:
            db.add(Location(code="W1", name="默认仓库", kind="warehouse"))
        if db.query(Supplier).count() == 0:
            db.add_all([
                Supplier(name="立创商城", type="lcsc", homepage="https://www.szlcsc.com"),
                Supplier(name="淘宝", type="taobao", homepage="https://www.taobao.com"),
                Supplier(name="其他", type="other"),
            ])
        defaults = {
            "site_name": "元件库管家",
            "label_brand": "元件库",
            "label_kind": "元件标签",
            "default_unit": "PCS",
            "low_stock_default": "0",
        }
        for k, v in defaults.items():
            if db.get(Setting, k) is None:
                db.add(Setting(key=k, value=v))
        db.commit()
    finally:
        db.close()


def get_setting(db, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
