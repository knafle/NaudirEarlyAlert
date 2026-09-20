"""Two-tier background state machine daemon for SMN weather alerts.

Tier 1 (SAT): Monitors regional alerts for Partido de Escobar every 12 hours.
Tier 2 (ACP): When SAT is ACTIVE, monitors short-term radar warnings every 3 minutes.
Performs geospatial containment checks using Shapely and manages deduplication.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from shapely.geometry import Point, Polygon
from shapely.validation import make_valid

from smn_client import SMNClient

logger = logging.getLogger(__name__)


def parse_polygon_coords(raw: Any) -> List[Tuple[float, float]]:
    """Parse raw polygon data from SMN ACP feed into (longitude, latitude) tuples.

    Handles:
    - String representations: "[-34.12,-58.50],[-34.40,-58.90]" or "[[...]]"
    - List of coordinates: [[-34.12, -58.50], ...]
    - Auto-detects [lat, lon] vs [lon, lat] ordering based on Argentine coordinate ranges.
    """
    pairs: List[Tuple[float, float]] = []

    if isinstance(raw, str):
        # Match coordinate pairs like [-34.12, -58.50]
        matches = re.findall(
            r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]", raw
        )
        for m1, m2 in matches:
            try:
                pairs.append((float(m1), float(m2)))
            except ValueError:
                continue
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    pairs.append((float(item[0]), float(item[1])))
                except (ValueError, TypeError):
                    continue

    if not pairs:
        return []

    # Argentine coordinate heuristics:
    # Latitude ranges roughly from -20 to -55
    # Longitude ranges roughly from -53 to -73
    # If the first value is between -20 and -55 and second is between -53 and -75, it is [lat, lon].
    v0, v1 = pairs[0]
    is_lat_first = (abs(v0) < abs(v1)) if (v0 < 0 and v1 < 0) else True

    coords: List[Tuple[float, float]] = []
    for c0, c1 in pairs:
        if is_lat_first:
            lat, lon = c0, c1
        else:
            lon, lat = c0, c1
        # Store as standard GIS (x=longitude, y=latitude)
        coords.append((lon, lat))

    return coords


def is_point_in_polygon(coords: List[Tuple[float, float]], lat: float, lon: float) -> bool:
    """Check if the given (lat, lon) point falls inside the polygon coordinates."""
    if len(coords) < 3:
        return False
    try:
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = make_valid(poly)
        point = Point(lon, lat)
        return bool(poly.contains(point))
    except Exception as err:
        logger.warning("Error evaluating polygon containment: %s", err)
        return False


def generate_event_id(item: Dict[str, Any]) -> str:
    """Generate a unique deduplication fingerprint for an ACP alert event."""
    if "id" in item and item["id"]:
        return str(item["id"])
    if "_id" in item and item["_id"]:
        return str(item["_id"])

    date_str = str(item.get("date", "")).strip()
    hour_str = str(item.get("hour", "")).strip()
    title_str = str(item.get("title", "")).strip()
    poly_snippet = str(item.get("polygon", ""))[:120].strip()

    raw_key = f"{date_str}::{hour_str}::{title_str}::{poly_snippet}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]


class WeatherAlertWorker:
    """Two-tier background state-machine daemon."""

    def __init__(
        self,
        smn_client: SMNClient,
        target_lat: float = -34.3100,
        target_lon: float = -58.7391,
        target_zone: str = "Escobar",
        sat_poll_interval_hours: float = 12.0,
        acp_poll_interval_minutes: float = 3.0,
        force_acp_poll: bool = False,
        broadcast_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self.client = smn_client
        self.target_lat = target_lat
        self.target_lon = target_lon
        self.target_zone = target_zone.strip().lower()
        self.sat_interval_seconds = sat_poll_interval_hours * 3600.0
        self.acp_interval_seconds = acp_poll_interval_minutes * 60.0
        self.force_acp_poll = force_acp_poll
        self.broadcast_callback = broadcast_callback

        # State tracking
        self.sat_active: bool = False
        self.active_sat_alert: Optional[Dict[str, Any]] = None
        self.seen_acp_ids: Set[str] = set()
        self._sat_event = asyncio.Event()
        self._running = False

    def check_sat_matches(self, alerts: List[Dict[str, Any]]) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Check if any active SAT alert matches the target zone with above-normal severity."""
        for alert in alerts:
            # Check zones mapping or list
            zones_data = alert.get("zones", {})
            zone_texts: List[str] = []
            if isinstance(zones_data, dict):
                zone_texts = [str(v) for v in zones_data.values()]
            elif isinstance(zones_data, list):
                zone_texts = [str(v) for v in zones_data]
            elif isinstance(zones_data, str):
                zone_texts = [zones_data]

            # Also check title and description
            all_text = " ".join(zone_texts) + " " + str(alert.get("title", "")) + " " + str(alert.get("description", ""))
            if self.target_zone not in all_text.lower():
                continue

            # Severity check: normal/green means tranquility (no alert)
            severity = str(alert.get("severity", "")).lower()
            color = str(alert.get("color", "")).lower()
            nivel = str(alert.get("nivel", "")).lower()

            if any(s in ["verde", "green", "normal", "0"] for s in [severity, color, nivel] if s):
                continue

            return True, alert

        return False, None

    async def _sat_monitor_loop(self) -> None:
        """Tier 1: Regional SAT polling loop."""
        while self._running:
            logger.info("Polling SAT alerts for target coordinates (%s, %s) and zone '%s'...",
                        self.target_lat, self.target_lon, self.target_zone)

            # 1. Primary: Query official ws1 SMN endpoint for target coordinates
            is_active, matched_alert = await self.client.get_location_sat_alerts_async(
                self.target_lat, self.target_lon
            )

            # 2. Fallback: Query legacy open feed /alerts/type/AL if ws1 did not return active alert
            if not is_active:
                legacy_alerts = await self.client.get_regional_alerts_async()
                is_active, matched_alert = self.check_sat_matches(legacy_alerts)

            self.sat_active = is_active
            self.active_sat_alert = matched_alert

            if self.sat_active:
                alert_title = matched_alert.get("title") or matched_alert.get("level_name") or "Alerta Meteorológica"
                logger.warning(
                    "SAT state: ACTIVE for zone '%s'. Details: %s. Enabling Tier 2 radar polling.",
                    self.target_zone,
                    alert_title,
                )
                self._sat_event.set()
            else:
                logger.info(
                    "SAT state: INACTIVE for zone '%s'. Radar polling dormant.",
                    self.target_zone,
                )
                self._sat_event.clear()

            try:
                await asyncio.sleep(self.sat_interval_seconds)
            except asyncio.CancelledError:
                break

    async def _acp_monitor_loop(self) -> None:
        """Tier 2: Short-term radar warnings polling loop."""
        while self._running:
            # If SAT is not active and force_acp_poll is False, wait for SAT event or check periodically
            if not self.sat_active and not self.force_acp_poll:
                try:
                    # Wait until SAT becomes active or 60s timeout to re-check
                    await asyncio.wait_for(self._sat_event.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

            logger.debug("Polling ACP warnings (/alerts/type/ACP)...")
            warnings = await self.client.get_short_term_warnings_async()
            current_active_ids: Set[str] = set()

            for item in warnings:
                event_id = generate_event_id(item)
                current_active_ids.add(event_id)

                raw_poly = item.get("polygon")
                if not raw_poly:
                    continue

                coords = parse_polygon_coords(raw_poly)
                if not coords:
                    continue

                # Point-in-polygon containment check
                inside = is_point_in_polygon(coords, self.target_lat, self.target_lon)
                if inside:
                    logger.warning(
                        "Target location (%s, %s) is INSIDE storm polygon for ACP event '%s': %s",
                        self.target_lat,
                        self.target_lon,
                        event_id,
                        item.get("title"),
                    )
                    if event_id not in self.seen_acp_ids:
                        self.seen_acp_ids.add(event_id)
                        logger.info("New storm cell detected! Triggering alert broadcast for %s", event_id)
                        if self.broadcast_callback:
                            try:
                                await self.broadcast_callback(item)
                            except Exception as err:
                                logger.error("Broadcast callback failed for event %s: %s", event_id, err, exc_info=True)
                    else:
                        logger.debug("Event %s already notified. Skipping duplicate broadcast.", event_id)

            # Prune seen IDs that are no longer in the active SMN ACP feed
            expired_ids = self.seen_acp_ids - current_active_ids
            if expired_ids:
                logger.debug("Pruning %d expired ACP event IDs from memory: %s", len(expired_ids), expired_ids)
                self.seen_acp_ids.intersection_update(current_active_ids)

            try:
                await asyncio.sleep(self.acp_interval_seconds)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        """Start both monitoring loops concurrently."""
        self._running = True
        logger.info(
            "Starting WeatherAlertWorker (Target: %s, %s | Zone: %s)",
            self.target_lat,
            self.target_lon,
            self.target_zone,
        )
        await asyncio.gather(
            self._sat_monitor_loop(),
            self._acp_monitor_loop(),
        )

    def stop(self) -> None:
        """Stop worker execution."""
        logger.info("Stopping WeatherAlertWorker...")
        self._running = False
        self._sat_event.set()
