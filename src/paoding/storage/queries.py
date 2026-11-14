from sqlalchemy import cast, exists, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def insert_for(session, model):
    return (pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert)(model)


def has_tag(session, column, tag):
    if session.bind.dialect.name == "postgresql":
        return cast(column, JSONB).contains([tag])
    each = func.json_each(column).table_valued("value")
    return exists(select(1).select_from(each).where(each.c.value == tag))
