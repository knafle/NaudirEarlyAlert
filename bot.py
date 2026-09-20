"""Main entry point for SMN Weather Alert Telegram Bot Daemon (NaudirEarlyAlert).

Orchestrates concurrent execution of:
1. Telegram Bot (python-telegram-bot v20+ polling)
2. Background state-machine worker (WeatherAlertWorker)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from smn_client import SMNClient
from subscribers import SubscribersDB
from telegram_bot import TelegramAlertBot
from worker import WeatherAlertWorker

# Ensure UTF-8 stdout for terminal resilience
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("smn_daemon")


def load_config() -> dict:
    """Load configuration from environment variables and .env file."""
    for _env_name in (".env", "env"):
        _p = Path(__file__).resolve().parent / _env_name
        if _p.is_file():
            load_dotenv(dotenv_path=_p)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        logger.error(
            "CRITICAL: TELEGRAM_BOT_TOKEN is not set in environment or .env file! "
            "Please configure your bot token before starting."
        )
        sys.exit(1)

    try:
        lat = float(os.getenv("ALERT_LAT", "-34.3100"))
        lon = float(os.getenv("ALERT_LON", "-58.7391"))
    except ValueError as err:
        logger.error("Invalid latitude/longitude in environment: %s", err)
        sys.exit(1)

    zone = os.getenv("ALERT_ZONE", "Escobar")
    db_path = os.getenv("DB_PATH", "subscribers.db")
    sat_hours = float(os.getenv("SAT_POLL_INTERVAL_HOURS", "12.0"))
    acp_minutes = float(os.getenv("ACP_POLL_INTERVAL_MINUTES", "3.0"))
    force_acp = os.getenv("FORCE_ACP_POLL", "false").lower() in ("true", "1", "yes")

    return {
        "token": token,
        "lat": lat,
        "lon": lon,
        "zone": zone,
        "db_path": db_path,
        "sat_hours": sat_hours,
        "acp_minutes": acp_minutes,
        "force_acp": force_acp,
    }


async def main() -> None:
    """Initialize and run bot polling and worker daemon concurrently."""
    config = load_config()

    logger.info("Initializing NaudirEarlyAlert Daemon...")
    logger.info("Target: Lat=%s, Lon=%s | Admin Zone=%s", config["lat"], config["lon"], config["zone"])
    logger.info("Database path: %s", Path(config["db_path"]).resolve())

    # Initialize components
    db = SubscribersDB(db_path=config["db_path"])
    smn_client = SMNClient()

    worker = WeatherAlertWorker(
        smn_client=smn_client,
        target_lat=config["lat"],
        target_lon=config["lon"],
        target_zone=config["zone"],
        sat_poll_interval_hours=config["sat_hours"],
        acp_poll_interval_minutes=config["acp_minutes"],
        force_acp_poll=config["force_acp"],
        subscribers_db=db,
    )

    telegram_bot = TelegramAlertBot(
        token=config["token"],
        subscribers_db=db,
        location_name="El Naudir",
        worker_ref=worker,
    )

    # Wire worker callback to Telegram alert broadcaster
    worker.broadcast_callback = telegram_bot.broadcast_alert

    # Setup shutdown event
    stop_event = asyncio.Event()

    def handle_signal(sig, frame):
        logger.info("Received termination signal %s. Shutting down gracefully...", sig)
        stop_event.set()

    # Register OS signals
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
    else:
        # Windows compatibility
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

    # Run Telegram bot and Worker concurrently
    app = telegram_bot.application
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot polling started successfully.")

    worker_task = asyncio.create_task(worker.start(), name="WeatherAlertWorker")
    logger.info("WeatherAlertWorker background task started.")

    try:
        # Wait until stop event is triggered
        await stop_event.wait()
    finally:
        logger.info("Initiating graceful shutdown...")
        worker.stop()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        if app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("NaudirEarlyAlert Daemon stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process exited.")
