import datetime
import logging
import os

from peewee import (
    CharField,
    DateTimeField,
    Model,
    SqliteDatabase,
    TextField,
)

from config.paths import APPDATA_DIR, DB_PATH, LOGS_DIR


db = SqliteDatabase(DB_PATH, pragmas={
    "journal_mode": "wal",
    "foreign_keys": 1,
    "busy_timeout": 30000,
})


def get_db():
    return db


def safe_connect():
    os.makedirs(APPDATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    if db.is_closed():
        try:
            db.connect()
        except Exception as exc:
            logging.error("Failed to connect database: %s", exc)
            return False
    return True


class BaseModel(Model):
    class Meta:
        database = db


class Account(BaseModel):
    channel_id = CharField(unique=True, index=True, null=True)
    channel_name = CharField(null=True)
    name = CharField(null=True)
    email = CharField(null=True)
    avatar = CharField(null=True)
    auth_method = CharField(null=True)
    credentials_json = TextField(null=True)
    cookies_json = TextField(null=True)
    token_path = CharField(max_length=500, null=True)
    session_path = TextField(null=True)
    status = CharField(default="Active")
    last_sync = DateTimeField(null=True)
    last_used = DateTimeField(default=datetime.datetime.now)
    created_at = DateTimeField(default=datetime.datetime.now)


def initialize_database():
    if safe_connect():
        try:
            db.create_tables([Account], safe=True)
            logging.info("Database initialized: %s", DB_PATH)
        finally:
            if not db.is_closed():
                db.close()


init_db = initialize_database
