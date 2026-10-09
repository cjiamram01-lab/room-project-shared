from fastapi import APIRouter
from src.endpoints import user
from src.endpoints.send_to_device import router as send_to_device_router
from src.endpoints.schedule import router as schedule_router
from src.endpoints.roomCancel import router as roomcancle_router
from src.endpoints.roomUsage import router as room_usage_router
from src.endpoints.schedule_replace import  router as schedule_replace_router
from src.endpoints.roomCancel import router as room_cancel_router
from src.endpoints.test import router as test_router
from src.endpoints.room import router as room_router    
from src.endpoints.roomBinding import router as room_binding_router
from src.endpoints.send_mqtt import router as send_mqtt_router 
from src.endpoints.staff_access import router as staff_access_router
from src.endpoints.mqtt_stream import router as mqtt_stream_router
from src.endpoints.useraccess import router as user_access_router
from src.endpoints.migration    import router as migration_router
from src.endpoints.dashboard_endpoint import router as dashboard_router
from src.endpoints.application  import router as application_router
from src.endpoints.accessory  import router as accessory_router 
from src.endpoints.schedule_AP import router as schedule_AP_router
from src.endpoints.counter_log import router as counter_log_router

#from src.endpoints.send_mqtt import



router = APIRouter()
router.include_router(schedule_AP_router)
router.include_router(counter_log_router)
router.include_router(accessory_router)
router.include_router(application_router)
router.include_router(migration_router)
router.include_router(mqtt_stream_router)
router.include_router(user_access_router)
router.include_router(staff_access_router)
router.include_router(send_mqtt_router)
router.include_router(user.router)
router.include_router(send_to_device_router)
router.include_router(schedule_router)
router.include_router(room_usage_router)
router.include_router(schedule_replace_router)
router.include_router(room_cancel_router)
router.include_router(room_binding_router)
router.include_router(room_router)
router.include_router(dashboard_router)
router.include_router(test_router, prefix="/test", tags=["test"])

