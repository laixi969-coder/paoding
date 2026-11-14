from alembic import context

from paoding.core.config import Settings
from paoding.storage.db import Database
from paoding.storage.tables import Base

config = context.config
url = config.attributes.get("database_url") or Settings().database_url
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    db = Database(Settings(database_url=url, demo_mode=False))
    with db.engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    db.engine.dispose()
