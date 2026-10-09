from fastapi import APIRouter, BackgroundTasks
from fastapi import  HTTPException
from src.models.roomUsage import RoomUsage_Base, RoomUsage_Controller,Confirm_Status_Booking_Base,Confirm_Status_Controller
from src.models.schedule import Schedule_Controller
from src.util.telegram_notifier import send_booking_notification, telegram_is_configured
from datetime import datetime, date
import uuid as uuid_lib
from PIL import Image

router = APIRouter(
    prefix="/room-usage",
    tags=["room-usage"],
    responses={404: {"description": "Not found"}},
)

@router.get("/get_schedule_wait_aprove/")
async def get_schedule_wait_aprove():
    ctrl =RoomUsage_Controller()
    results=ctrl.get_schedule_wait_aprove()
    return results 



@router.get("/set_status_booking/{id}")
async def set_status_booking(id: int,usage_status:int):
    ctrl = RoomUsage_Controller()
    result = ctrl.set_status_booking(id,usage_status)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to set close status")
    return {"message": "Change status set successfully"}



@router.get("/set_status_schedules/{id}")
async def set_status_schedules(id: int,usage_status:int):
    ctrl = RoomUsage_Controller()
    #result = ctrl.set_status_schedules(id,usage_status)
    result = ctrl.set_status_schedules(id,usage_status)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to set close status")
    return {"message": "Change status set successfully"}



@router.get("/set_status_close_booking/{id}")
async def set_status_close_booking(id: int):
    ctrl = RoomUsage_Controller()
    result = ctrl.set_status_booking(id,4)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to set close status")
    return {"message": "Close status set successfully"}


@router.get("/set_status_close_schedules/{id}")
async def set_status_close_schedules(id: int):
    ctrl = RoomUsage_Controller()
    result = ctrl.set_status_chedules(id,4)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to set close status")
    return {"message": "Close status set successfully"}


@router.post("/add_confirm/")
async def add_confirm(confirm_base:Confirm_Status_Booking_Base):
    ctrl=Confirm_Status_Controller()
    result=ctrl.add( confirm_base.dict())
    return result 
    

@router.get("/set_close_status/{room_no}")
async def set_close_status(room_no: str):
    ctrl = RoomUsage_Controller()
    result = ctrl.set_close_status(room_no)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to set close status")
    return {"message": "Close status set successfully"}

@router.get("/set_complete/")
async def set_complete(uuid:str):
    ctrl=RoomUsage_Controller()
    result=ctrl.set_complete(uuid)
    return result 

@router.get("/get_periods")
async def get_periods():
    ctrl =RoomUsage_Controller()
    results=ctrl.get_periods()
    return results 


@router.get("/get_is_confirm_progress/")
async def get_is_confirm_progress(uuid:str):
    ctrl=RoomUsage_Controller()
    return ctrl.get_is_confirm_progress(uuid)

@router.get("/generate_uuid/")
async def generate_uuid(subject_code: str, user_code: str):
    today      = datetime.now().strftime("%Y-%m-%d")
    result     = str(uuid_lib.uuid5(uuid_lib.NAMESPACE_URL, f"{subject_code}{today}{user_code}"))
    return {"uuid": result}

@router.post("/add")
async def add_room_usage(room_usage: RoomUsage_Base, background_tasks: BackgroundTasks):
    db = RoomUsage_Controller()
    booking = room_usage.dict()
    result = db.add(booking)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to add room usage")

    # A conflict response is truthy but no booking was created.
    if isinstance(result, dict) and result.get("error"):
        return result

    try:
        db1=Schedule_Controller()
        db1.migrate_schedule_2_json()
    except Exception:
        pass  # keep the booking even if refreshing the JSON snapshot fails

    # Telegram is an optional side effect. A notification failure must never
    # roll back or delay a successful booking.
    if telegram_is_configured():
        background_tasks.add_task(send_booking_notification, booking)

    return result

@router.put("/update/{id}")
async def update_room_usage(id: int, room_usage: RoomUsage_Base):
    db = RoomUsage_Controller()
    result = db.update(room_usage.dict(), id)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to update room usage")
    try:
        db1=Schedule_Controller()
        db1.migrate_schedule_2_json()
        del db1 
    except Exception:
        pass  # keep the update even if refreshing the JSON snapshot fails
    return {"message": "Room usage updated successfully"}   


@router.delete("/delete/{id}")
async def delete_room_usage(id: int):   
    db = RoomUsage_Controller()
    result = db.delete(id)
    try:
        db1=Schedule_Controller()
        db1.migrate_schedule_2_json()
        del db1
    except Exception:
        pass  # keep the deletion even if refreshing the JSON snapshot fails

    if not result:
        raise HTTPException(status_code=400, detail="Failed to delete room usage")
    return {"message": "Room usage deleted successfully"}


@router.get("/get_by_user_name/{user_name}")
async def get_room_usage_by_user_name(user_name: str):  
    db = RoomUsage_Controller()
    results = db.get_by_user_name(user_name)
    if not results:
        raise HTTPException(status_code=404, detail="No room usage found for the given user name")
    return results

@router.get("/verify_access")   
async def verify_access(user_name: str, pwd: str):
    db = RoomUsage_Controller()
    result = db.verify_access(user_name, pwd)
    return result

@router.get("/get_rooms")
async def get_rooms():
    db=RoomUsage_Controller()
    results=db.get_rooms()
    return results 

@router.get("/change_room/{id}")
async def change_room(id: int, room_no: str, booking_date: date, start_time: str, finish_time: str):
    db = RoomUsage_Controller()
    result = db.is_exist(room_no, booking_date, start_time, finish_time)
    if result["isExist"]:
        return {"status": "exist", "message": "Room is already booked for the specified time slot"}

    existing = db.get_by_id(id)
    if not existing:
        return {"status": "error", "message": "Room usage not found"}

    existing.pop("id", None)
    existing["room_no"]      = room_no
    existing["booking_date"] = booking_date
    existing["start_time"]   = start_time
    existing["finish_time"]  = finish_time

    db.update(existing, id)

    try:
        Schedule_Controller().migrate_schedule_2_json()
    except Exception:
        pass  # keep the change even if refreshing the JSON snapshot fails

    return {"status": "success", "message": "Room changed successfully"}

@router.get("/get_exist_class/{uuid}")
async def get_exist_class(uuid:str):
    db=RoomUsage_Controller()
    return db.get_exist_class(uuid)


@router.get("/get_room_usage_by_admin/")
async def get_room_usage_by_admin(room_no: str = "", booking_date: str = "", keyword: str = None):
    db = RoomUsage_Controller()
    results = db.get_room_usage_by_admin(room_no, booking_date, keyword)
    return results

@router.delete("/delete_by_admin/{id}")
async def delete_room_usage_by_admin(id: int):
    db = RoomUsage_Controller()
    result = db.delete_by_admin(id)
    if not result:
        raise HTTPException(status_code=400, detail="Failed to delete room usage")
    return {"message": "Room usage deleted successfully"}


@router.get("get_current_usage")
async  def get_current_usage():    
    db=RoomUsage_Controller()
    results=db.get_current_usage()
    return results 