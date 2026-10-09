from fastapi import APIRouter, Query
from fastapi import  HTTPException
from src.models.counter_log import  CounterLog_Base,CounterLog_Controller as counter_coltroller
from src.util.dbcontroller import DBController as DB
from datetime import date, datetime, timedelta
from fastapi import HTTPException



router = APIRouter(
    prefix="/counter_controller",
    tags=["counter_controller"],
    responses={404: {"description": "Not found"}},
)



@router.post("/add/")
async def add_counter_log(counter_log: CounterLog_Base):
    """Log one person count. log_date, log_time and created_date default to now."""
    db = counter_coltroller()
    result = db.add(counter_log.model_dump())
    # DBController.create always hands back a dict, so truthiness says nothing
    # about whether the INSERT ran — the outcome is in Flag.
    if not result.get("Flag"):
        raise HTTPException(
            status_code=400,
            detail=result.get("err", "Failed to add counter log"),
        )
    return {"message": "Counter log added successfully", "id": result.get("Id")}
