
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from src.util.dbcontroller import DBController as DB


class CounterLog_Base(BaseModel):
    ip_address: str
    personal_count: int
    # default_factory, not datetime.now(): a bare call is evaluated once when
    # this module is imported, so every row logged afterwards would carry the
    # API's start-up timestamp instead of its own.
    log_date: datetime = Field(default_factory=datetime.now)
    log_time: str = Field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    created_date: datetime = Field(default_factory=datetime.now)


class CounterLog_Controller():
    __tablename__ = 'counter_log'

    def __init__(self):
        self.__tableName__ = 'counter_log'

    def add(self, counter_log):
        db = DB(self.__tableName__)
        result = db.create(counter_log)
        del db
        return result

    def update(self, counter_log, id):
        db = DB(self.__tableName__)
        result = db.update(counter_log, id)
        del db
        return result

    def delete(self, id: int):
        db = DB()
        sql = """DELETE FROM counter_log WHERE id = %s"""
        result = db.set_specific_sql(sql, (id,))
        del db
        return result

    def get_by_id(self, id: int):
        db = DB()
        sql = """SELECT
                id,
                ip_address,
                log_date,
                log_time,
                personal_count,
                created_date
        FROM counter_log WHERE id = %s"""
        results = db.get_specific_sql(sql, None, (id,))
        del db
        return results[0] if results else None

    def get_by_ip(self, ip_address: str, log_date: Optional[str] = None, limit: int = 100):
        """Logs for one camera, newest first, optionally narrowed to one day."""
        db = DB()
        sql = """SELECT
                id,
                ip_address,
                log_date,
                log_time,
                personal_count,
                created_date
        FROM counter_log WHERE ip_address = %s"""
        params = (ip_address,)

        if log_date:
            sql += " AND DATE(log_date) = %s"
            params += (log_date,)

        # LIMIT is interpolated, not bound: mysql-connector sends bound values
        # as strings and MySQL rejects a quoted LIMIT. int() keeps it safe.
        sql += f" ORDER BY log_date DESC, id DESC LIMIT {int(limit)}"
        results = db.get_specific_sql(sql, None, params)
        del db
        return results

    def get_latest_by_ip(self, ip_address: str):
        """Most recent count logged for one camera, or None if it has never reported."""
        db = DB()
        sql = """SELECT
                id,
                ip_address,
                log_date,
                log_time,
                personal_count,
                created_date
        FROM counter_log
        WHERE ip_address = %s
        ORDER BY log_date DESC, id DESC
        LIMIT 1"""
        results = db.get_specific_sql(sql, None, (ip_address,))
        del db
        return results[0] if results else None

    def get_by_date_range(self, start_date: str, finish_date: str, ip_address: Optional[str] = None):
        """Logs between two dates inclusive, oldest first, for charting."""
        db = DB()
        sql = """SELECT
                id,
                ip_address,
                log_date,
                log_time,
                personal_count,
                created_date
        FROM counter_log
        WHERE DATE(log_date) BETWEEN %s AND %s"""
        params = (start_date, finish_date)

        if ip_address:
            sql += " AND ip_address = %s"
            params += (ip_address,)

        sql += " ORDER BY log_date, id"
        results = db.get_specific_sql(sql, None, params)
        del db
        return results

    def get_max_count_by_date(self, log_date: str):
        """Peak count each camera reached on one day — the occupancy figure that matters."""
        db = DB()
        sql = """SELECT
                ip_address,
                MAX(personal_count) AS max_count,
                COUNT(*) AS log_entries
        FROM counter_log
        WHERE DATE(log_date) = %s
        GROUP BY ip_address
        ORDER BY ip_address"""
        results = db.get_specific_sql(sql, None, (log_date,))
        del db
        return results
