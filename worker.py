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
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from shapely.geometry import Point, Polygon
from shapely.validation import make_valid

from smn_client import SMNClient

logger = logging.getLogger(__name__)


def parse_polygon_coords(raw: Any) -> List[Tuple[float, float]]:
    """Parse raw polygon data from SMN ACP feed into (longitude, latitude) tuples.

    Handles:
    - GeoJSON geometry dict: {"type": "Polygon", "coordinates": [[[lon, lat], ...]]}
    - Nested coordinate rings: [[[lon, lat], ...]]
    - String representations: "[-34.12,-58.50],[-34.40,-58.90]" or "[[...]]"
    - List of coordinates: [[-34.12, -58.50], ...]
    - Auto-detects [lat, lon] vs [lon, lat] ordering based on Argentine coordinate ranges.
    """
    pairs: List[Tuple[float, float]] = []

    if isinstance(raw, dict):
        if "coordinates" in raw:
            raw = raw["coordinates"]
        elif "geometry" in raw and isinstance(raw["geometry"], dict):
            raw = raw["geometry"].get("coordinates", [])

    # Unwrap nested rings (GeoJSON Polygon: [ [ [lon, lat], ... ] ])
    while (
        isinstance(raw, (list, tuple))
        and len(raw) > 0
        and isinstance(raw[0], (list, tuple))
        and len(raw[0]) > 0
        and isinstance(raw[0][0], (list, tuple))
    ):
        raw = raw[0]

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


def check_containment_and_distance(
    coords: List[Tuple[float, float]], lat: float, lon: float
) -> Tuple[bool, float, Tuple[float, float]]:
    """Check if point is inside polygon and calculate approximate distance in km to storm centroid.

    Returns:
        (inside: bool, distance_km: float, centroid: (lat, lon))
    """
    if len(coords) < 3:
        return False, 9999.0, (0.0, 0.0)
    try:
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = make_valid(poly)
        point = Point(lon, lat)
        inside = bool(poly.contains(point))

        centroid_lon, centroid_lat = poly.centroid.x, poly.centroid.y
        # Approx distance in km: 1 deg lat ~ 111 km, 1 deg lon at lat -34 ~ 92 km
        d_lat_km = (centroid_lat - lat) * 111.0
        d_lon_km = (centroid_lon - lon) * 92.0
        dist_km = (d_lat_km ** 2 + d_lon_km ** 2) ** 0.5

        return inside, dist_km, (centroid_lat, centroid_lon)
    except Exception as err:
        logger.warning("Error evaluating polygon containment: %s", err)
        return False, 9999.0, (0.0, 0.0)


def is_point_in_polygon(coords: List[Tuple[float, float]], lat: float, lon: float) -> bool:
    """Check if the given (lat, lon) point falls inside the polygon coordinates."""
    inside, _, _ = check_containment_and_distance(coords, lat, lon)
    return inside


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
        subscribers_db: Optional[Any] = None,
    ) -> None:
        self.client = smn_client
        self.target_lat = target_lat
        self.target_lon = target_lon
        self.target_zone = target_zone.strip().lower()
        self.sat_interval_seconds = sat_poll_interval_hours * 3600.0
        self.acp_interval_seconds = acp_poll_interval_minutes * 60.0
        self.force_acp_poll = force_acp_poll
        self.broadcast_callback = broadcast_callback
        self.db = subscribers_db

        # State tracking
        self.sat_active: bool = False
        self.active_sat_alert: Optional[Dict[str, Any]] = None
        self.seen_acp_ids: Set[str] = set()
        self.current_discarded_acp: Dict[str, Dict[str, Any]] = {}
        self.last_acp_check_time: Optional[float] = None
        self._sat_event = asyncio.Event()
        self._running = False

        # Pre-load previously notified IDs from database if available
        if self.db and hasattr(self.db, "get_all_notified_acp_sync"):
            try:
                persisted_ids = self.db.get_all_notified_acp_sync()
                self.seen_acp_ids.update(persisted_ids)
                if persisted_ids:
                    logger.info("Loaded %d previously notified ACP event IDs from database.", len(persisted_ids))
            except Exception as err:
                logger.warning("Could not pre-load notified ACP IDs from DB: %s", err)

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

            logger.info("📡 Consultando avisos de radar a muy corto plazo (ACP)...")
            warnings = await self.client.get_short_term_warnings_async()
            current_active_ids: Set[str] = set()
            active_discarded: Dict[str, Dict[str, Any]] = {}

            if not warnings:
                logger.info("ℹ️ No hay avisos ACP activos reportados por el SMN en este momento.")
            else:
                logger.info("🔍 Se encontraron %d avisos ACP activos en el país. Analizando geometrías...", len(warnings))

            for item in warnings:
                event_id = generate_event_id(item)
                current_active_ids.add(event_id)

                title = str(item.get("title") or item.get("description") or "Aviso ACP").strip()
                raw_zones = item.get("zones") or []
                zones_summary = "; ".join(raw_zones) if isinstance(raw_zones, list) else str(raw_zones)

                raw_poly = item.get("geometry") or item.get("polygon")
                if not raw_poly:
                    logger.warning("⚠️ Aviso ACP ID %s (%s) sin geometría. Descartado.", event_id, title[:30])
                    active_discarded[event_id] = {
                        "id": event_id,
                        "title": title,
                        "zones": zones_summary,
                        "reason": "Sin geometría válida",
                        "dist_km": None,
                        "date": item.get("date"),
                    }
                    continue

                coords = parse_polygon_coords(raw_poly)
                if not coords:
                    logger.warning("⚠️ Aviso ACP ID %s sin coordenadas legibles. Descartado.", event_id)
                    active_discarded[event_id] = {
                        "id": event_id,
                        "title": title,
                        "zones": zones_summary,
                        "reason": "Coordenadas no legibles",
                        "dist_km": None,
                        "date": item.get("date"),
                    }
                    continue

                # Geospatial containment and distance check
                inside, dist_km, centroid = check_containment_and_distance(
                    coords, self.target_lat, self.target_lon
                )

                if inside:
                    logger.warning(
                        "🎯 ¡TORMENTA SOBRE EL NAUDIR! Aviso ACP ID %s (%s) CUBRE el barrio. "
                        "Coordenadas (%s, %s) dentro del polígono.",
                        event_id,
                        title,
                        self.target_lat,
                        self.target_lon,
                    )
                    if event_id not in self.seen_acp_ids:
                        self.seen_acp_ids.add(event_id)
                        if self.db and hasattr(self.db, "mark_acp_notified_sync"):
                            try:
                                self.db.mark_acp_notified_sync(event_id)
                            except Exception as err:
                                logger.warning("Could not persist notified ACP ID %s: %s", event_id, err)
                        logger.info("📢 Nueva tormenta detectada para El Naudir. Disparando difusión a suscriptores...")
                        if self.broadcast_callback:
                            try:
                                await self.broadcast_callback(item)
                            except Exception as err:
                                logger.error("Broadcast callback failed for event %s: %s", event_id, err, exc_info=True)
                    else:
                        logger.info("ℹ️ Aviso ACP ID %s ya notificado anteriormente. Omitiendo duplicado.", event_id)
                else:
                    active_discarded[event_id] = {
                        "id": event_id,
                        "title": title,
                        "zones": zones_summary,
                        "reason": f"Fuera de El Naudir (~{int(dist_km)} km)",
                        "dist_km": round(dist_km, 1),
                        "centroid": centroid,
                        "date": item.get("date"),
                    }
                    logger.info(
                        "ℹ️ ACP ID %s descartado: '%s' | Zonas: %s | Centroide: (%.2f, %.2f) a ~%.0f km de El Naudir | Fuera de cobertura.",
                        event_id,
                        title[:40],
                        zones_summary[:50] if zones_summary else "N/A",
                        centroid[0],
                        centroid[1],
                        dist_km,
                    )

            self.current_discarded_acp = active_discarded
            self.last_acp_check_time = time.time()

            # Prune seen IDs that are no longer in the active SMN ACP feed
            expired_ids = self.seen_acp_ids - current_active_ids
            if expired_ids:
                logger.info("Pruning %d expired ACP event IDs from memory: %s", len(expired_ids), expired_ids)
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
