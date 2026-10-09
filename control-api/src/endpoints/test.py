import socket
import time
from datetime import datetime

from fastapi import APIRouter, Response, status

from src.endpoints.send_to_device import MQTT_BROKER_HOST, MQTT_BROKER_PORT
from src.util.dbcontroller import DBController

router = APIRouter()


def check_database() -> dict:
    """Open a MySQL connection, confirm it answers a query, and hand it back."""
    started = time.perf_counter()
    conn = None
    try:
        db = DBController()
        conn = db.get_conn()
        if conn is None or not conn.is_connected():
            return {"ok": False, "detail": "Could not connect to MySQL"}

        cursor = conn.cursor()
        cursor.execute("SELECT DATABASE()")
        database = cursor.fetchone()[0]
        cursor.close()
        return {
            "ok": True,
            "server_version": conn.get_server_info(),
            "database": database,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
    finally:
        # DBController opens a connection in __init__ and has no close(), so it
        # has to be released here. A monitor polling this route would otherwise
        # work through max_connections one check at a time.
        if conn is not None and conn.is_connected():
            conn.close()


def check_broker() -> dict:
    """
    Confirm the MQTT broker is accepting connections.

    A plain TCP connect, not an MQTT handshake: it answers "is the broker
    reachable from here" in milliseconds, which is what a health check needs,
    without a client, a client id or a subscription.
    """
    started = time.perf_counter()
    target = f"{MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}"
    try:
        with socket.create_connection((MQTT_BROKER_HOST, MQTT_BROKER_PORT), timeout=3):
            return {
                "ok": True,
                "broker": target,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
    except OSError as exc:
        return {"ok": False, "broker": target, "detail": str(exc)}


@router.get("/ping")
def ping():
    """
    Liveness. Answers without touching the database or the broker.

    Use this to tell "the API is down" from "the API is up but something it
    depends on is not" — /health cannot make that distinction on its own.
    """
    return {
        "status": "ok",
        "service": "control-api",
        "time": datetime.now().isoformat(),
    }


@router.get("/health")
def health(response: Response):
    """
    Readiness: the API plus everything it needs to actually serve requests.

    Returns 503 when any check fails, so a monitor can act on the status code
    instead of having to parse the body.
    """
    checks = {"database": check_database(), "mqtt": check_broker()}
    healthy = all(check["ok"] for check in checks.values())

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if healthy else "degraded",
        "service": "control-api",
        "time": datetime.now().isoformat(),
        "checks": checks,
    }


@router.get("/connection")
def test_connection(response: Response):
    """Database check on its own, for when /health reports it as the failure."""
    result = check_database()
    if not result["ok"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if result["ok"] else "fail",
        "connected": result["ok"],
        **{k: v for k, v in result.items() if k != "ok"},
    }
