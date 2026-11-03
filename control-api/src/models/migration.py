
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Union
import requests
from pydantic import BaseModel, Field
from src.util.dbcontroller import DBController as DB


class Semester_Base(BaseModel):
    academic_year:str
    semester:str 
    status:int=1
    created_date:datetime=datetime.now()


class ManualSchedule_Base(BaseModel):
    """One room booked at the same hours on every date in a range."""

    room_no: Union[str, List[str]] = Field(
        ...,
        description='Room number, a list of them, or "all" for every room in the rooms table.',
    )
    start_date: date = Field(..., description="First schedule_date, inclusive.")
    finish_date: date = Field(..., description="Last schedule_date, inclusive.")
    start_time: str = Field(..., description="HH:MM, the same on every date.")
    finish_time: str = Field(..., description="HH:MM, the same on every date.")
    coursename: Optional[str] = Field("กำหนดการใช้ห้อง")
    coursecode: Optional[str] = None
    teacher_name: Optional[str] = None
    userCode: Optional[str] = None



class ManualScheduleAllRooms_Base(BaseModel):
    """The same booking as ManualSchedule_Base, for every room get_rooms lists."""

    start_date: date = Field(..., description="First schedule_date, inclusive.")
    finish_date: date = Field(..., description="Last schedule_date, inclusive.")
    start_time: str = Field(..., description="HH:MM, the same on every date.")
    finish_time: str = Field(..., description="HH:MM, the same on every date.")
    search: Optional[str] = Field(
        None,
        description="Only rooms whose room_no contains this; every room when omitted.",
    )
    coursename: Optional[str] = Field("กำหนดการใช้ห้อง")
    coursecode: Optional[str] = None
    teacher_name: Optional[str] = None
    userCode: Optional[str] = None
    weekdays: Optional[List[int]] = Field(
        None,
        description="Only these weekdays, numbered Sunday=1 … Saturday=7. All days when omitted.",
    )


class Semester_Controller():
    __tablename__ = 'staff_access'

    def __init__(self):
        self.__tableName__ = 'semesters' 


    def reset_status(self):
        db=DB()
        sql="UPDATE semesters SET status=0"
        result=db.set_specific_sql(sql, None)
        del db
        return result
    
    def set_status(self,id:int):
        db=DB()
        sql="UPDATE semesters SET status=%s WHERE id=%s "
        params=(1,id)
        result=db.set_specific_sql(sql, params)
        del db
        return result
    
    def get_list_semester(self):
        db=DB()
        sql=f"""SELECT 
            id,
            academic_year,
            semester,status 
        FROM semesters 
        ORDER BY academic_year,semester """
        results=db.get_specific_sql(sql)
        del db
        return results 
     
    
    def add(self, semester):
        self.reset_status()
        db = DB(self.__tableName__)
        result = db.create(semester)
        del db
        return result
    
    def update(self, semester,id: int):
        # Implementation for updating staff access
        db = DB(self.__tableName__)
        result = db.update(semester, id)
        del db
        return result
    
    def delete(self, id: int):
        # Implementation for deleting staff access
        db = DB(self.__tableName__)
        result = db.delete(id)
        del db
        return result 
    

class Migration_Controller():


    def remove_all_schedules(self):
        db=DB("schedules")
        result=db.delete_all()
        del db
        return result

    
    
    # schedules.weekday is stored Sunday=1 … Saturday=7, which is what the MIS
    # feed sends. isoweekday() is Monday=1 … Sunday=7, so Sunday wraps round to
    # 1 and every other day shifts up by one — the same mapping
    # get_schedule_migrate applies when it matches a date against the feed.
    @staticmethod
    def weekday_of(day) -> int:
        if isinstance(day, str):
            day = datetime.strptime(day[:10], "%Y-%m-%d").date()
        elif isinstance(day, datetime):
            day = day.date()
        iso = day.isoweekday()
        return 1 if iso == 7 else iso + 1

    def list_rooms(self, search: Optional[str] = None) -> list:
        """
        The rooms a manual schedule can be written against.

        The same table and ordering get_room_numbers validates against, so a
        room offered by the picker is always one set_manual_schedule accepts.

        search is optional. None, an empty string and whitespace all mean the
        same thing — every room — because a cleared search box sends an empty
        string, not an absent parameter, and that has to list everything
        rather than match nothing.
        """
        needle = (search or "").strip()

        db = DB()
        sql = """SELECT
            room_no,
            room_type,
            floor_no,
            building,
            computer_no,
            seat_no
        FROM rooms"""
        params = None
        if needle:
            # Matched in SQL rather than in Python so a large rooms table does
            # not have to come across the wire to be filtered.
            sql += " WHERE room_no LIKE %s"
            params = (f"%{needle}%",)
        sql += " ORDER BY room_no"
        results = db.get_specific_sql(sql, None, params)
        del db
        return results or []
    
    def _date_range(self, start_date, finish_date, step_days: int = 1) -> List[date]:
        """
        Every date from start_date to finish_date, both ends included.

        Returns the dates, not the count: callers walk this once per room, so
        a generator would be empty by the second room. The span is the gap
        between the two dates, which is one less than the number of dates,
        hence the + 1.
        """
        start = self._to_datetime(start_date).date()
        finish = self._to_datetime(finish_date).date()
        if step_days < 1:
            raise ValueError("step_days must be >= 1")
        if start > finish:
            raise ValueError(f"start_date ({start}) is after finish_date ({finish})")
        span = (finish - start).days
        return [start + timedelta(days=i) for i in range(0, span + 1, step_days)]
      
    
    
    @staticmethod
    def _to_datetime(value) -> datetime:
        """
        Normalise str | date | datetime into a datetime.

        Raises ValueError, not HTTPException: this is the model layer, and
        the route turns it into a 422 where it has the request to blame.
        """
        if isinstance(value, datetime):
            return value
        # datetime is a subclass of date, so this has to come second.
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        try:
            # Sliced, so an ISO timestamp works as well as a plain date.
            return datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            raise ValueError(f"Invalid date: {value!r}. Expected YYYY-MM-DD.")

    # Kept here rather than in the endpoint so the schedules columns and the
    # order of the values stay in one place; the route checks the request and
    # hands over clean arguments.
    SCHEDULE_COLUMNS = [
        "roomcode", "weekday", "timeperiodfrom", "timeperiodto", "coursecode", "coursename",
        "revisioncode", "periodfrom", "periodto", "week_type", "week_no", "schedule_date",
        "teacher_name", "startTime", "finishTime", "yearNo", "semester", "userCode"
    ]

    def _teaching_day_hours(self):
        """
        The working day, from the first period's start to the last one's end.

        What a booking means when the caller names dates but no hours. Both
        come back None when the periods table is empty, and the caller then
        has to be told rather than guessed at.
        """
        db = DB()
        rows = db.get_specific_sql(
            "SELECT period, startTime, finishTime FROM periods ORDER BY period") or []
        del db
        if not rows:
            return None, None
        return str(rows[0]["startTime"])[:5], str(rows[-1]["finishTime"])[:5]

    def set_manaul_schedule(self, start_date: date, finish_date: date,
                            start_time: Optional[str] = None,
                            finish_time: Optional[str] = None,
                            search: Optional[str] = None,
                            coursename: Optional[str] = "กำหนดการใช้ห้อง",
                            coursecode: Optional[str] = None,
                            teacher_name: Optional[str] = None,
                            userCode: Optional[str] = None):
        """
        Book every room get_rooms lists, on every date in the range.

        Two nested loops: the rooms, and inside each of them every date from
        start_date to finish_date. Rows for all of them go in as one insert
        rather than one per room or per date — a term for every room is
        thousands of rows, and a connection apiece is what makes that slow.

        Called with dates alone, it books the whole teaching day. Returns what
        was written, and the caller decides what to do about it.
        """
        rooms = self.list_rooms(search)
        if not rooms:
            return {"affected_rows": 0, "rooms": [], "dates": [],
                    "detail": "No rooms to schedule."}

        # _date_range normalises both ends and refuses a reversed range, so
        # the rules about dates live in one place instead of being repeated
        # here. It raises; this layer reports rather than throws.
        try:
            calendar_days = self._date_range(start_date, finish_date)
        except ValueError as error:
            return {"affected_rows": 0, "rooms": [room["room_no"] for room in rooms],
                    "dates": [], "detail": str(error)}

        if not start_time or not finish_time:
            start_time, finish_time = self._teaching_day_hours()
            if not start_time:
                return {"affected_rows": 0, "rooms": [room["room_no"] for room in rooms],
                        "dates": [], "detail": "No periods are defined, so there is no "
                                               "default day; pass start_time and finish_time."}

        period_from, period_to = self.get_periods_for_times(start_time, finish_time)

        # Imported here, not at module scope: Schedule_Controller is only
        # needed for the week lookup, and a top-level import would tie every
        # importer of this module to it.
        from src.models.schedule import Schedule_Controller

        schedule_db = Schedule_Controller()
        current = schedule_db.get_current()
        if current is None:
            return {"affected_rows": 0, "rooms": [], "dates": [],
                    "detail": "No current semester is set."}
        year_no, semester = current["year_no"], current["semester"]

        # The dates are the same for every room, and so is each one's week
        # number, so both are worked out once instead of once per room.
        dates = []
        for day in calendar_days:
            date_str = day.strftime("%Y-%m-%d")
            # Read back off the string that goes into schedule_date, so the
            # weekday column can never describe a different day from the one
            # stored beside it.
            weekday = self.weekday_of(date_str)
            week_info = schedule_db.get_week_type_no_by_date(
                year_no, semester, date_str) or {}
            dates.append((date_str, weekday, week_info))

        records = []
        for item in rooms:
            room_no = item["room_no"]
            for date_str, weekday, week_info in dates:
                records.append((
                    room_no,
                    weekday,
                    period_from,
                    period_to,
                    coursecode,
                    coursename,
                    None,
                    period_from,
                    period_to,
                    week_info.get("week_type"),
                    week_info.get("week_no"),
                    date_str,
                    teacher_name,
                    start_time,
                    finish_time,
                    year_no,
                    semester,
                    userCode,
                ))

        result = self.add_manual_schedules(self.SCHEDULE_COLUMNS, records) or {}
        return {
            "affected_rows": result.get("affected_rows", 0),
            "rows_built": len(records),
            "rooms": [room["room_no"] for room in rooms],
            "dates": [entry[0] for entry in dates],
            "startTime": start_time,
            "finishTime": finish_time,
            "periodfrom": period_from,
            "periodto": period_to,
            "yearNo": year_no,
            "semester": semester,
        }

    def get_room_numbers(self, room_no) -> list:
        """
        Resolve the request's room_no against the rooms table.

        Returns the rooms in the order the table gives them, and never
        invents one: a room that is not in rooms would produce schedule rows
        nothing else in the system can join to.
        """
        db = DB()
        known = [row["room_no"] for row in (db.get_specific_sql(
            "SELECT room_no FROM rooms ORDER BY room_no") or [])]
        del db

        if isinstance(room_no, str):
            wanted = [room_no]
        else:
            wanted = list(room_no or [])

        cleaned = [str(item).strip() for item in wanted if str(item).strip()]
        if len(cleaned) == 1 and cleaned[0].lower() in ("all", "*"):
            return known, []

        valid = [item for item in known if item in cleaned]
        unknown = [item for item in cleaned if item not in known]
        return valid, unknown

    def get_periods_for_times(self, start_time: str, finish_time: str):
        """
        The first and last teaching period the given hours cover.

        Manual bookings do not have to line up with the period grid, so these
        can both come back None; the schedule row is still valid without
        them, it just will not show up in anything that groups by period.
        """
        db = DB()
        rows = db.get_specific_sql(
            "SELECT period, startTime, finishTime FROM periods ORDER BY period") or []
        del db

        def as_minutes(value) -> Optional[int]:
            text = str(value)[:5]
            try:
                parsed = datetime.strptime(text, "%H:%M")
            except ValueError:
                return None
            return parsed.hour * 60 + parsed.minute

        begins, ends = as_minutes(start_time), as_minutes(finish_time)
        if begins is None or ends is None:
            return None, None

        covered = [
            row["period"] for row in rows
            if (as_minutes(row["startTime"]) is not None
                and as_minutes(row["finishTime"]) is not None
                # Any overlap counts, so a booking that starts mid-period
                # still reports the period it started in.
                and as_minutes(row["startTime"]) < ends
                and as_minutes(row["finishTime"]) > begins)
        ]
        if not covered:
            return None, None
        return covered[0], covered[-1]

    def add_manual_schedules(self, columns, records):
        """Insert the generated rows, reporting a database refusal as itself."""
        db = DB("schedules")
        try:
            return db.create_multiple_records(columns, records)
        finally:
            del db

    def get_schedule_datasets(self, roomCode, startDate, finishDate,current_schedule):
        year_no = current_schedule['year_no']
        semester_no = current_schedule['semester']

        start = datetime.strptime(startDate, "%Y-%m-%d").date() if isinstance(startDate, str) else startDate
        finish = datetime.strptime(finishDate, "%Y-%m-%d").date() if isinstance(finishDate, str) else finishDate

        datasets = []
        current_date = start
        while current_date <= finish:
            date_str = current_date.strftime("%Y-%m-%d")
            schedule = self.get_schedule_migrate(year_no, semester_no, roomCode, date_str)
            if schedule is not None:
                datasets.append({
                    "date": date_str,
                    "schedule": schedule
                })
            current_date += timedelta(days=1)

        return datasets
    
    def get_schedule_migrate(self,yearNo,semester,roomCode,scheduleDate=datetime.now):
        url = f"https://cos.nrru.ac.th/mis_api/getRoom.php?yearNo={yearNo}&semester={semester}"
        try:
            response = requests.get(url, verify=False, timeout=10)
            response.raise_for_status()
            data = response.json()  # returns a list, not a dict
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")
            return None
        except ValueError:
            print("Invalid JSON response")
            return None

        week_type_info = self.get_week_type_no(yearNo, semester)
        week_type = week_type_info.get('week_type') if week_type_info else None
        week_no = week_type_info.get('week_no') if week_type_info else None

        if scheduleDate is None:
            booking_date = datetime.now().strftime("%Y-%m-%d")
        elif isinstance(scheduleDate, str):
            booking_date = scheduleDate
        else:
            booking_date = scheduleDate.strftime("%Y-%m-%d")

        if scheduleDate is None:
            week_day = datetime.now().isoweekday()
        elif isinstance(scheduleDate, str):
            week_day = datetime.strptime(scheduleDate, "%Y-%m-%d").isoweekday()
        else:
            week_day = scheduleDate.isoweekday()  # date object

        if week_day==7:
            week_day=1
        else:
            week_day+=1
        keep = {'(คู่)', '**', ''} if week_type == 'E' else {'(คี่)', '**', ''}

        result = [
            item for item in data
            if item['roomcode'][:8] == roomCode
            and item['roomcode'][8:] in keep
            and (week_day is None or int(item['weekday']) == int(week_day))
        ]

        periods_map = self._get_periods_map()

        for item in result:
            item["week_type"]=week_type
            item["week_no"]=week_no
            item["schedule_date"]=booking_date
            item["roomcode"]=item['roomcode'][0:8]
            teacher = (item.get("teacher") or [{}])[0]
            item["teacher_name"]=teacher.get("prefixname","")+" "+teacher.get("officername","")+" "+teacher.get("officersurname","")
            item["user_name"]=teacher.get("officerlogin","")
            item.pop("teacher", None)
            from_period = periods_map.get(int(item['timeperiodfrom']), {})
            to_period   = periods_map.get(int(item['timeperiodto']), {})
            item['startTime']  = str(from_period.get('startTime',  ''))[:5]
            item['finishTime'] = str(to_period.get('finishTime', ''))[:5]
            item["yearNo"]=yearNo
            item["semester"]=semester
            item["userCode"]=teacher.get("officerlogin","")

            
        return result

    #*****************************************************************
