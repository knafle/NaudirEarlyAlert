"""SMN (Servicio Meteorológico Nacional) alert endpoints client.

Supports both:
1. Public feeds: https://ws.smn.gob.ar/alerts/type/AL and /ACP
2. Official web API: https://ws1.smn.gob.ar/v1/ with auto-extracted JWT token
   for location-specific SAT and short-term warnings (ACP).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional curl_cffi for browser TLS fingerprint impersonation
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)

URL_SAT_ALERTS = "https://ws.smn.gob.ar/alerts/type/AL"
URL_ACP_WARNINGS = "https://ws.smn.gob.ar/alerts/type/ACP"
WS1_BASE_URL = "https://ws1.smn.gob.ar/v1"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://www.smn.gob.ar/",
    "Origin": "https://www.smn.gob.ar",
}

ALERT_LEVELS = {
    1: "Verde (Tranquilidad)",
    2: "Violeta (Advertencia)",
    3: "Amarillo (Informate)",
    4: "Naranja (Preparate)",
    5: "Rojo (Seguí instrucciones oficiales)",
}

ALERT_EVENTS = {
    37: "Lluvia",
    39: "Viento",
    40: "Niebla",
    41: "Tormenta",
    42: "Nevada",
    43: "Altas temperaturas",
    44: "Bajas temperaturas",
    45: "Ceniza volcánica",
    46: "Polvo",
    47: "Viento Zonda",
    54: "Humo",
}


class SMNClient:
    """Client for retrieving SAT and ACP alerts from SMN endpoints."""

    def __init__(
        self,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
    ) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._cffi_session: Optional[Any] = None
        if HAS_CURL_CFFI:
            try:
                self._cffi_session = cffi_requests.Session(impersonate="chrome120")
                self._cffi_session.headers.update(DEFAULT_HEADERS)
            except Exception as err:
                logger.debug("Could not initialize curl_cffi session: %s", err)

        # JWT Token caching
        self._jwt_token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._cached_location_id: Optional[int] = None

    def get_jwt_token(self, force_refresh: bool = False) -> Optional[str]:
        """Fetch or refresh JWT token from SMN website."""
        now = time.time()
        if not force_refresh and self._jwt_token and now < self._token_expiry - 300:
            return self._jwt_token

        logger.info("Fetching fresh SMN JWT token from https://www.smn.gob.ar/...")
        try:
            html = ""
            if self._cffi_session:
                resp = self._cffi_session.get("https://www.smn.gob.ar/", timeout=self.timeout)
                if resp.status_code == 200:
                    html = resp.text
            if not html:
                resp = self.session.get("https://www.smn.gob.ar/", timeout=self.timeout)
                if resp.status_code == 200:
                    html = resp.text

            if html:
                match = re.search(r"localStorage\.setItem\(['\"]token['\"]\s*,\s*['\"]([^'\"]+)['\"]", html)
                if match:
                    token = match.group(1)
                    self._jwt_token = token
                    # Parse expiry from JWT payload if possible
                    try:
                        parts = token.split(".")
                        if len(parts) >= 2:
                            payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
                            self._token_expiry = float(payload.get("exp", now + 3600))
                    except Exception:
                        self._token_expiry = now + 3600
                    logger.info("Successfully obtained SMN JWT token (expires in %ds)", int(self._token_expiry - now))
                    return token
        except Exception as err:
            logger.warning("Failed to fetch SMN JWT token: %s", err)

        return self._jwt_token

    def _get_auth_headers(self) -> Dict[str, str]:
        """Return headers with JWT Authorization if available."""
        headers = dict(DEFAULT_HEADERS)
        token = self.get_jwt_token()
        if token:
            headers["Authorization"] = f"JWT {token}"
        return headers

    def resolve_location_id(self, lat: float, lon: float) -> Optional[int]:
        """Resolve geographical coordinates to SMN location ID using georef API."""
        if self._cached_location_id:
            return self._cached_location_id

        token = self.get_jwt_token()
        if not token:
            return None

        url = f"{WS1_BASE_URL}/georef/location/coord"
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, params={"lat": lat, "lon": lon}, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                loc_id = data.get("id") or data.get("_id")
                if loc_id:
                    self._cached_location_id = int(loc_id)
                    logger.info("Resolved coordinates (%s, %s) to SMN Location: %s (%s, %s)",
                                lat, lon, self._cached_location_id, data.get("name"), data.get("department"))
                    return self._cached_location_id
        except Exception as err:
            logger.warning("Error resolving location ID for (%s, %s): %s", lat, lon, err)
        return None

    def get_location_sat_alerts(self, lat: float, lon: float) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Query official ws1 SMN endpoint for SAT alerts at target coordinates.

        Returns:
            (is_active: bool, alert_info: dict | None)
        """
        loc_id = self.resolve_location_id(lat, lon)
        if not loc_id:
            return False, None

        url = f"{WS1_BASE_URL}/warning/alert/location/{loc_id}"
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, timeout=self.timeout)

            # Retry once with refreshed token if 401 Unauthorized
            if resp.status_code == 401:
                logger.info("JWT token expired (401). Refreshing token...")
                self.get_jwt_token(force_refresh=True)
                headers = self._get_auth_headers()
                resp = self.session.get(url, headers=headers, timeout=self.timeout)

            if resp.status_code == 200:
                data = resp.json()
                warnings = data.get("warnings", [])
                for w in warnings:
                    max_level = w.get("max_level", 1)
                    # Levels 3 (Amarillo), 4 (Naranja), 5 (Rojo) are active alerts!
                    if max_level >= 3:
                        events_list = []
                        for e in w.get("events", []):
                            if e.get("max_level", 1) >= 3:
                                ev_name = ALERT_EVENTS.get(e.get("id"), "Alerta Meteorológica")
                                events_list.append(ev_name)
                        event_desc = ", ".join(events_list) if events_list else "Tormentas / Fenómenos severos"
                        level_label = ALERT_LEVELS.get(max_level, f"Nivel {max_level}")

                        alert_info = {
                            "date": w.get("date"),
                            "level": max_level,
                            "level_name": level_label,
                            "events": event_desc,
                            "title": f"Alerta {level_label}: {event_desc}",
                            "source": "ws1.smn.gob.ar",
                        }
                        logger.warning("Active SAT alert found on ws1: Level=%s (%s) for %s",
                                       max_level, level_label, alert_info["date"])
                        return True, alert_info
        except Exception as err:
            logger.warning("Error fetching location SAT alerts from %s: %s", url, err)

        return False, None

    def _fetch_endpoint(self, url: str) -> List[Dict[str, Any]]:
        """Fetch and parse JSON from legacy open feeds with fallback."""
        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                data = response.json()
                return data if isinstance(data, list) else [data]
            elif response.status_code == 503 and self._cffi_session:
                cffi_resp = self._cffi_session.get(url, timeout=self.timeout)
                if cffi_resp.status_code == 200:
                    data = cffi_resp.json()
                    return data if isinstance(data, list) else [data]
        except Exception as err:
            logger.debug("Fetch failed for %s: %s", url, err)
        return []

    def get_regional_alerts(self) -> List[Dict[str, Any]]:
        """Retrieve regional SAT alerts from legacy feed (/alerts/type/AL)."""
        return self._fetch_endpoint(URL_SAT_ALERTS)

    def get_short_term_warnings(self) -> List[Dict[str, Any]]:
        """Retrieve short-term radar warnings (/alerts/type/ACP)."""
        return self._fetch_endpoint(URL_ACP_WARNINGS)

    async def get_location_sat_alerts_async(self, lat: float, lon: float) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Non-blocking async wrapper for get_location_sat_alerts."""
        return await asyncio.to_thread(self.get_location_sat_alerts, lat, lon)

    async def get_regional_alerts_async(self) -> List[Dict[str, Any]]:
        """Non-blocking async wrapper for get_regional_alerts."""
        return await asyncio.to_thread(self.get_regional_alerts)

    async def get_short_term_warnings_async(self) -> List[Dict[str, Any]]:
        """Non-blocking async wrapper for get_short_term_warnings."""
        return await asyncio.to_thread(self.get_short_term_warnings)
