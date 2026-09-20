"""Standalone CLI testing script for NaudirEarlyAlert.

Injects mock ACP radar storm polygons over El Naudir (-34.3100, -58.7391)
to verify coordinate containment, message formatting, and live Telegram dispatch.

Usage:
    # Dry run (no Telegram messages sent, tests logic and formatting):
    python test_alert.py --dry-run

    # Send test alert to a specific chat ID:
    python test_alert.py --chat-id 123456789

    # Send test alert to all subscribed users in subscribers.db:
    python test_alert.py

    # Test rejection of a storm polygon outside the target zone:
    python test_alert.py --outside --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode

from subscribers import SubscribersDB
from telegram_bot import format_acp_message
from worker import is_point_in_polygon, parse_polygon_coords

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def build_mock_acp_payload(
    target_lat: float,
    target_lon: float,
    enclosing: bool = True,
) -> dict:
    """Build a synthetic ACP alert payload.

    If enclosing=True, creates a polygon around target_lat, target_lon.
    If enclosing=False, creates a polygon hundreds of kilometers away.
    """
    now = datetime.datetime.now()
    hour_str = now.strftime("%H:%M")
    date_str = now.strftime("%d/%m/%Y")

    if enclosing:
        # Construct a polygon box around (-34.3100, -58.7391)
        d = 0.08  # ~8-9 km radius box
        poly_str = (
            f"[{target_lat - d:.4f},{target_lon - d:.4f}],"
            f"[{target_lat - d:.4f},{target_lon + d:.4f}],"
            f"[{target_lat + d:.4f},{target_lon + d:.4f}],"
            f"[{target_lat + d:.4f},{target_lon - d:.4f}],"
            f"[{target_lat - d:.4f},{target_lon - d:.4f}]"
        )
        title = "Tormentas fuertes con ráfagas y ocasional caída de granizo"
    else:
        # Construct polygon far away (e.g., south of Buenos Aires province)
        poly_str = "[-38.00,-57.60],[-38.00,-57.40],[-38.20,-57.40],[-38.20,-57.60],[-38.00,-57.60]"
        title = "Tormentas aisladas en zona costera sur"

    return {
        "id": "mock-test-alert-001",
        "date": date_str,
        "hour": hour_str,
        "title": title,
        "description": "Aviso a muy corto plazo emitido con fines de prueba de simulación.",
        "zones": {"0": "Escobar", "1": "Campana", "2": "Tigre"},
        "polygon": poly_str,
        "gmp_general": "https://ws.smn.gob.ar/alerts/radar/radar_animado.gif",
    }


async def run_test(args: argparse.Namespace) -> None:
    """Run testing workflow."""
    load_dotenv()

    target_lat = args.lat
    target_lon = args.lon
    location_name = args.location_name
    enclosing = not args.outside

    print("\n" + "=" * 60)
    print("🌩️  SMN Weather Alert Mock Dispatcher & Verifier")
    print("=" * 60)
    print(f"Target location:   {location_name} (Lat: {target_lat}, Lon: {target_lon})")
    print(f"Test scenario:     {'ENCLOSING target' if enclosing else 'OUTSIDE target'}")
    print(f"Execution mode:    {'DRY-RUN (Simulated)' if args.dry_run else 'LIVE DISPATCH'}")
    print("=" * 60 + "\n")

    # 1. Build mock ACP
    mock_item = build_mock_acp_payload(target_lat, target_lon, enclosing=enclosing)
    print(f"📋 Generated Mock ACP Event ID: {mock_item['id']}")
    print(f"   Title:   {mock_item['title']}")
    print(f"   Time:    {mock_item['hour']} ({mock_item['date']})")
    print(f"   Polygon: {mock_item['polygon']}\n")

    # 2. Parse coordinates and test containment
    coords = parse_polygon_coords(mock_item["polygon"])
    inside = is_point_in_polygon(coords, target_lat, target_lon)

    print("📐 Geospatial Containment Check:")
    print(f"   Parsed vertices count: {len(coords)}")
    print(f"   First vertex: (lon={coords[0][0]}, lat={coords[0][1]})")
    print(f"   Target inside polygon? -> {'✅ YES' if inside else '❌ NO'}")

    if enclosing and not inside:
        print("❌ ERROR: Polygon was expected to enclose coordinates but test check failed!")
        sys.exit(1)
    elif not enclosing and inside:
        print("❌ ERROR: Polygon was expected to NOT enclose coordinates but check returned True!")
        sys.exit(1)

    # 3. Format message
    message_text = format_acp_message(mock_item, location_name=location_name)
    print("\n📱 Formatted Telegram Message Preview:\n")
    print("-" * 50)
    print(message_text)
    print("-" * 50 + "\n")

    # If dry-run, stop here
    if args.dry_run:
        print("✅ Dry-run validation completed successfully. No Telegram messages sent.")
        return

    # 4. Live dispatch
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: TELEGRAM_BOT_TOKEN is not configured in environment or .env file.")
        sys.exit(1)

    bot = Bot(token=token)

    # Determine recipient chat IDs
    recipients = []
    if args.chat_id:
        recipients.append(args.chat_id)
        print(f"🎯 Target recipient: Chat ID {args.chat_id}")
    else:
        db = SubscribersDB(db_path=args.db_path)
        recipients = await db.get_all_subscribers()
        print(f"👥 Target recipients: All {len(recipients)} subscribers in {args.db_path}")

    if not recipients:
        print("⚠️ No recipients found! Use --chat-id <your_id> or register via /start on Telegram first.")
        return

    print(f"\n🚀 Sending live test alert to {len(recipients)} recipient(s)...")
    success_count = 0
    for chat_id in recipients:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode=ParseMode.HTML,
            )
            print(f"  ✅ Sent successfully to chat_id {chat_id}")
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception as err:
            print(f"  ❌ Failed to send to chat_id {chat_id}: {err}")

    print(f"\n🎉 Test dispatch finished: {success_count}/{len(recipients)} messages delivered.\n")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Inject mock ACP radar alerts and test Telegram dispatch for NaudirEarlyAlert."
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        help="Specific Telegram chat ID to receive the test message.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the entire flow without sending any Telegram messages.",
    )
    parser.add_argument(
        "--outside",
        action="store_true",
        help="Inject a storm polygon OUTSIDE target coordinates to verify rejection.",
    )
    parser.add_argument(
        "--lat",
        type=float,
        default=float(os.getenv("ALERT_LAT", "-34.3100")),
        help="Target latitude (default: -34.3100)",
    )
    parser.add_argument(
        "--lon",
        type=float,
        default=float(os.getenv("ALERT_LON", "-58.7391")),
        help="Target longitude (default: -58.7391)",
    )
    parser.add_argument(
        "--location-name",
        type=str,
        default="El Naudir",
        help="Location name display (default: 'El Naudir')",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=os.getenv("DB_PATH", "subscribers.db"),
        help="Path to SQLite database (default: subscribers.db)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    asyncio.run(run_test(cli_args))
