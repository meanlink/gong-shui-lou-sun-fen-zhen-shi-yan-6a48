from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models

# 撤回能力新增列：对已存在的库做幂等补齐，新库由 create_all 直接建出
_ADDED_COLUMNS = {
    'datasets': [
        ('revoked', "BOOLEAN NOT NULL DEFAULT FALSE"),
        ('revoke_reason', "VARCHAR(500) NOT NULL DEFAULT ''"),
        ('revoked_by', "VARCHAR(150) NOT NULL DEFAULT ''"),
        ('revoked_at', "DOUBLE PRECISION"),
    ],
    'jobs': [
        ('revoked', "BOOLEAN NOT NULL DEFAULT FALSE"),
        ('revoke_reason', "VARCHAR(500) NOT NULL DEFAULT ''"),
        ('revoked_at', "DOUBLE PRECISION"),
    ],
    'reports': [
        ('auto_paused', "BOOLEAN NOT NULL DEFAULT FALSE"),
    ],
    'deliveries': [
        ('invalidated', "BOOLEAN NOT NULL DEFAULT FALSE"),
        ('invalidate_reason', "VARCHAR(500) NOT NULL DEFAULT ''"),
        ('invalidated_at', "DOUBLE PRECISION"),
    ],
}

def migrate():
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    is_sqlite = engine.dialect.name == 'sqlite'
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing:
                continue
            present = {col['name'] for col in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in present:
                    continue
                if is_sqlite:
                    ddl = 'REAL' if ddl.startswith('DOUBLE') else ddl.replace('BOOLEAN', 'BOOLEAN').replace('VARCHAR(500)', 'VARCHAR(500)').replace("DEFAULT FALSE", "DEFAULT 0").replace("DEFAULT ''", "DEFAULT ''")
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))

if __name__=='__main__':migrate()
