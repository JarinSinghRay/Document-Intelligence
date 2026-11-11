from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from .config import settings

settings.ensure_dirs()
_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_engine(settings.database_url, pool_pre_ping=True,
                       connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {})
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor(); cur.execute("PRAGMA journal_mode=WAL"); cur.execute("PRAGMA foreign_keys=ON"); cur.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def init_db():
    from . import models  # noqa
    Base.metadata.create_all(engine)
