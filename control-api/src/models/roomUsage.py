
from calendar import weekday
from datetime import datetime, date
from multiprocessing.connection import Client
from shlex import quote
# import uuid
from pydantic import BaseModel
from src.util.dbcontroller import DBController as DB

class Confirm_Status_Booking_Base(BaseModel):
    booking_id:int 
    schedule_type:str #schedule/booking
    created_date:datetime=datetime.now() 
    
class Confirm_Status_Controller():
    __tablename__ = 'confirm_status_booking'

    def __init__(self):
        self.__tableName__ = 'confirm_status_booking'
        
    def add(self, confirm_status):
        db = DB(self.__tableName__)
        result = db.create(confirm_status)
        del db
        return result


class RoomUsage_Base(BaseModel):
    user_name: str
    room_no: str
    subject_code: str
    objective:str 
    weekday: int
    start_time: str
    finish_time: str
    year_no: str 
    semester: str
    uuid:str 
    created_date: datetime=datetime.now()
    usage_status:int=1
    booking_date: date = date.today()


class RoomUsage_Controller():
    __tablename__ = 'room_usages'

    def __init__(self):
        self.__tableName__ = 'room_usages'
        
    
    def set_status_booking(self, id:int,usage_status:int):
        db = DB()
        sql = """UPDATE room_usages 
                 SET usage_status = %s 
                 WHERE usage_status < %s AND id=%s"""
        result = db.set_specific_sql(sql, (usage_status,usage_status,id))
        del db
        return result
    
    def set_status_schedules(self, id:int,usage_status:int):
        db = DB()
        sql = """UPDATE schedules 
                SET usage_status = %s 
                WHERE usage_status < %s AND id=%s"""
        result = db.set_specific_sql(sql, (usage_status,usage_status,id))
        del db
        return result
    def get_current_usage(self):
        db=DB()
        sql=f"""SELECT 
                V.roomcode, 
                V.scheduleDate,
                V.startTime,
                V.finishTime,
                I.ip_address
        FROM (
            SELECT roomcode,
                schedule_date AS scheduleDate,
                startTime,
                finishTime
            FROM schedules
            WHERE schedule_date = CURDATE()
            AND CURTIME() >= STR_TO_DATE(startTime,  '%H:%i')
            AND CURTIME() <  STR_TO_DATE(finishTime, '%H:%i')

            UNION ALL

            SELECT room_no    AS roomcode,
                booking_date AS scheduleDate,
                start_time AS startTime,
                finish_time AS finishTime
            FROM room_usages
            WHERE booking_date = CURDATE()
            AND CURTIME() >= CAST(start_time  AS TIME)
            AND CURTIME() <  CAST(finish_time AS TIME)
        ) AS V
        INNER JOIN ip_binding I ON V.roomcode = I.room_no;"""
        results=db.get_specific_sql(sql)
        del db
        return results 
    
    def get_schedule_wait_aprove(self):
        db=DB()
        sql=f"""SELECT 
                id,
                room_no,
                `subject_code`,
                `objective`,
                `user_name`,
                booking_date,
                `start_time`,
                finish_time 
        FROM room_usages WHERE 
        `objective`<>'MIS Schedule' 
        AND `usage_status`=1
        """
        results=db.get_specific_sql(sql)
        del db 
        return results                 
                

    def set_status_close_booking(self, id:int):
        db = DB()
        sql = """UPDATE room_usages 
                 SET usage_status = 3 
                 WHERE usage_status < 3 AND id=%s"""
        result = db.set_specific_sql(sql, (id,))
        del db
        return result
    
    def set_status_close_schedules(self, id:int):
        db = DB()
        sql = """UPDATE schedules 
                SET usage_status = 3 
                WHERE usage_status < 3 AND id=%s"""
        result = db.set_specific_sql(sql, (id,))
        del db
        return result
    
    
    
    
    def set_close_status(self, room_no: str):
        db = DB()
        today = datetime.now().strftime("%Y-%m-%d")
        sql = """UPDATE room_usages 
                 SET usage_status = 4 
                 WHERE room_no = %s AND DATE(created_date) = %s AND usage_status < 4"""
        result = db.set_specific_sql(sql, (room_no, today))
        del db
        return result

    def check_usage_room_flag(self):
        #request to check usage room flag by service check usage subscription service
        from datetime import datetime, timedelta
        db = DB()
        sql = "SELECT finishTime FROM periods"
        results = db.get_specific_sql(sql)
        if not results:
            return []
        now = datetime.now()
        matched = []
        for row in results:
            finish_time = datetime.strptime(str(row['finishTime']), '%H:%M:%S').replace(
                year=now.year, month=now.month, day=now.day
            )
            in_range = (finish_time - timedelta(minutes=5)) <= now <= (finish_time + timedelta(minutes=5))
            if in_range:
                finish_str = finish_time.strftime('%H:%M:%S')
                usages = db.get_specific_sql(
                    """SELECT * FROM room_usages
                       WHERE finish_time = %s
                         AND DATE(created_date) = CURDATE()
                         AND usage_status = 1""",
                    None, (finish_str,)
                )
                if usages:
                    db.set_specific_sql(
                        """UPDATE room_usages
                           SET usage_status = 2
                           WHERE finish_time = %s
                             AND DATE(created_date) = CURDATE()
                             AND usage_status = 1""",
                        (finish_str,)
                    )
                matched.extend(usages)
        del db
        return matched
    
    def check_schedule_overlap(self, room_no:str,booking_date:datetime, start_time:str, finish_time:str):
        # Overlap condition: existing.start < req.finish AND existing.finish > req.start
        # Covers all cases:
        #   [existing |----| ]           inside request  → conflict
        #   [request  |----| ]           inside existing → conflict
        #   [existing |--[overlap]--| ]  partial left    → conflict
        #   [      [overlap]--| existing]  partial right → conflict
        #   Adjacent slots (finish == start) are NOT a conflict (strict < / >)
        db = DB()

        booking_date = booking_date.strftime('%Y-%m-%d')

        sql = """SELECT id, start_time, finish_time FROM room_usages
                 WHERE room_no = %s
                 AND DATE(booking_date) = %s
                 AND start_time  < %s
                 AND finish_time > %s"""
        results = db.get_specific_sql(sql, None, (room_no, booking_date,finish_time, start_time))
        del db
        return {"isConflict": len(results) > 0, "conflicts": results}

    def add(self, room_usage):
        overlap = self.check_schedule_overlap(
            room_usage.get('room_no'),
            room_usage.get('booking_date'),
            room_usage.get('start_time'),
            room_usage.get('finish_time'),
        )
        if overlap['isConflict']:
            return {"error": "Time slot conflict", "conflicts": overlap['conflicts']}
        db = DB(self.__tableName__)
        result = db.create(room_usage)
        del db
        return result
    def get_by_id(self, id):
        db = DB(self.__tableName__)
        sql = """SELECT 
                        id, 
                        user_name, 
                        room_no, 
                        subject_code, 
                        objective,
                        weekday, 
                        start_time, 
                        finish_time, 
                        year_no, 
                        semester, 
                        uuid,
                        usage_status,
                        booking_date
                FROM room_usages
                WHERE id = %s"""
        results = db.get_specific_sql(sql, None, (id,))
        del db
        return results[0] if results else None
    
    def get_periods(self):
        db=DB()
        sql="SELECT period,startTime,finishTime,labelSTime,labelFTime FROM periods ORDER BY period"
        results=db.get_specific_sql(sql)
        del db 
        return results 


    def update(self, room_usage, id):
        db = DB(self.__tableName__)
        result = db.update(room_usage, id)
        del db
        return result
    
    def delete(self, id):
        db = DB(self.__tableName__)
        result = db.delete(id)
        del db
        return result
    
    def is_exist(self,room_no:str,booking_date:datetime,start_time:str,finish_time:str):
        db = DB(self.__tableName__)
        sql = """SELECT id FROM room_usages
                 WHERE room_no = %s AND DATE(booking_date) = %s AND start_time = %s AND finish_time = %s"""
        results = db.get_specific_sql(sql, None, (room_no, booking_date.strftime('%Y-%m-%d'), start_time, finish_time))
        del db
        return {"isExist": len(results) > 0}
    
    def get_by_id(self,id):
        db = DB(self.__tableName__)
        sql = """SELECT 
                        id, 
                        user_name, 
                        room_no, 
                        subject_code, 
                        objective,
                        weekday, 
                        start_time, 
                        finish_time, 
                        year_no, 
                        semester, 
                        uuid,
                        usage_status,
                        booking_date
                FROM room_usages
                WHERE id = %s"""
        results = db.get_specific_sql(sql, None, (id,))
        del db
        return results[0] if results else None
    
    

    def set_complete(self,uuid:str):
        db=DB()
        today=datetime.now().strftime("%Y-%m-%d")
        current_time=datetime.now().strftime("%H:%M")
        sql=f"""UPDATE room_usages 
        SET usage_status=3 
        WHERE uuid=%s AND 
        DATE(created_date)=%s AND
        finish_time>%s  """ 
        result=db.set_specific_sql(sql,(uuid,today,current_time))
        del db  
        return result 
    
    
    
    def get_is_confirm_progress(self, uuid: str, start_time: str = None, finish_time: str = None):
        db=DB()
        if start_time and finish_time:
            sql = """SELECT id FROM room_usages
                     WHERE uuid = %s AND start_time = %s AND finish_time = %s AND usage_status = 1"""
            params = (uuid, start_time, finish_time)
        else:
            sql = """SELECT id FROM room_usages WHERE uuid = %s AND usage_status = 1"""
            params = (uuid,)
        results=db.get_specific_sql(sql,None,params)
        del db 
        if len(results)>0:
            return {"is_confirm_progress":len(results)>0,"usage_status":3}
        else:
            return {"is_confirm_progress":False}
        
    
    def get_by_user_name(self, user_name):
        db = DB(self.__tableName__)
        sql = """SELECT 
                        id, 
                        user_name, 
                        room_no, 
                        subject_code, 
                        weekday, 
                        start_time, 
                        finish_time, 
                        year_no, 
                        semester, 
                        uuid,
                        usage_status,
                        booking_date
                FROM room_usages
                WHERE user_name = %s"""
        results = db.get_specific_sql(sql, None, (user_name,))
        del db
        return results
    
    def get_rooms(self):
        db=DB()
        sql="SELECT room_no FROM rooms "
        results=db.get_specific_sql(sql)
        del db 
        return results 
    
    def verify_access(self,user_name,pwd):
        db = DB()
        sql = """SELECT user_name
                 FROM useraccess 
                 WHERE user_name = %s AND pwd = %s"""
        results = db.get_specific_sql(sql, None, (user_name,pwd))
        del db
        return {"access":len(results)>0}
    
    def get_room_usage_by_admin(self, room_no: str = "", booking_date: str = "",keyword: str = None):
        db = DB()
        sql = f"""SELECT
        id,user_name,subject_code,`booking_date`,start_time,finish_time
        FROM room_usages
        WHERE 1=1"""
        params = ()
        if room_no:
            sql += " AND room_no = %s"
            params += (room_no,)
        if booking_date:
            sql += " AND DATE(booking_date) = %s"
            params += (booking_date,)
        if keyword:
            sql += " AND (user_name LIKE %s OR subject_code LIKE %s)"
            params += (f"%{keyword}%", f"%{keyword}%")
        results = db.get_specific_sql(sql, None, params)
        del db
        return results 
    
    def delete_by_admin(self, id: int):
        db = DB()
        sql = """DELETE FROM room_usages WHERE id = %s"""
        result = db.set_specific_sql(sql, (id,))
        del db
        return result
    
    def get_exist_by_time(self,uuid:str ,room_no: str, start_time: str, finish_time: str):
        db = DB()
        today=datetime.now().strftime("%Y-%m-%d")
        sql = """SELECT id FROM cancel_rooms
                 WHERE uuid=%s
                 UNION
                 SELECT id FROM room_usages
                 WHERE room_no = %s AND DATE(created_date)=%s AND start_time = %s AND finish_time = %s"""
        results = db.get_specific_sql(sql, None, (uuid,room_no,today, start_time, finish_time))
        del db
        return {"isExist": len(results) > 0}

    def get_exist_class(self, uuid: str):
        db = DB()
        sql = """SELECT id FROM cancel_rooms WHERE uuid = %s
                 UNION
                 SELECT id FROM room_usages  WHERE uuid = %s"""
        results = db.get_specific_sql(sql, None, (uuid, uuid))
        del db
        return {"isExist": len(results) > 0}

