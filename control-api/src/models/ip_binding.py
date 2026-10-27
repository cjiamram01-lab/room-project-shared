
from pydantic import BaseModel
from src.util.dbcontroller import DBController as DB


class IpBinding_Base(BaseModel):
    room_no: str
    ip_address: str


class IpBinding_Controller():
    __tablename__ = 'ip_binding'

    def __init__(self):
        self.__tableName__ = 'ip_binding'

    def add(self, ip_binding):
        db = DB(self.__tableName__)
        result = db.create(ip_binding)
        del db
        return result

    def update(self, ip_binding, id):
        db = DB(self.__tableName__)
        result = db.update(ip_binding, id)
        del db
        return result

    def get_all(self):
        db = DB()
        sql = """SELECT room_no, ip_address FROM ip_binding ORDER BY room_no, ip_address"""
        results = db.get_specific_sql(sql)
        del db
        return results

    def get_by_room(self, room_no: str):
        """Every camera bound to a room — a room can have more than one."""
        db = DB()
        sql = """SELECT room_no, ip_address FROM ip_binding
                 WHERE room_no = %s ORDER BY ip_address"""
        results = db.get_specific_sql(sql, None, (room_no,))
        del db
        return results

    def get_by_ip(self, ip_address: str):
        """The binding for one camera, or None if that IP is not a known camera."""
        db = DB()
        sql = """SELECT room_no, ip_address FROM ip_binding WHERE ip_address = %s"""
        results = db.get_specific_sql(sql, None, (ip_address,))
        del db
        return results[0] if results else None
