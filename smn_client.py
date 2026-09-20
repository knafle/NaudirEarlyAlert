"""SMN (Servicio Meteorológico Nacional) alert endpoints client.

Supports:
1. Official web API: https://ws1.smn.gob.ar/v1/ with auto-extracted JWT token
   for coordinate georeferenced SAT and short-term warnings (ACP).
2. Legacy public feeds: https://ws.smn.gob.ar/alerts/type/AL and /ACP
   with automatic curl_cffi Chrome impersonation to bypass Cloudflare on cloud environments.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Ensure .env from project directory is loaded
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# Optional curl_cffi for browser TLS fingerprint impersonation (essential on Cloud VMs)
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
                self._cffi_session = cffi_requests.Session(impersonate="chrome124")
                logger.info("Initialized curl_cffi session with Chrome 124 impersonation.")
            except Exception as err:
                logger.debug("Could not initialize curl_cffi session: %s", err)

        # JWT Token caching
        self._jwt_token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._cached_location_id: Optional[int] = None

    def _http_get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Perform HTTP GET using curl_cffi if available (to pass Cloudflare), else requests."""
        h = headers or self._get_auth_headers()
        if self._cffi_session:
            try:
                resp = self._cffi_session.get(url, params=params, headers=h, timeout=self.timeout)
                return resp
            except Exception as err:
                logger.debug("curl_cffi request failed for %s: %s. Falling back to requests.", url, err)
        return self.session.get(url, params=params, headers=h, timeout=self.timeout)

    def get_jwt_token(self, force_refresh: bool = False) -> Optional[str]:
        """Fetch or refresh JWT token from SMN website."""
        now = time.time()
        if not force_refresh and self._jwt_token and now < self._token_expiry - 300:
            return self._jwt_token

        # 1. Check if Scrape.do API key is configured (auto-refresh 24/7)
        scrapedo_key = (
            os.getenv("SCRAPEDO_API_KEY")
            or os.getenv("SCRAPE_DO_API_KEY")
            or os.getenv("SCRAPEDO_KEY")
            or os.getenv("SCRAPEDO_TOKEN")
        )
        if scrapedo_key:
            scrapedo_key = scrapedo_key.strip().strip('"').strip("'")
        if scrapedo_key:
            try:
                logger.info("Fetching fresh SMN token via Scrape.do proxy...")
                r = self.session.get(
                    "https://api.scrape.do",
                    params={"token": scrapedo_key, "url": "https://www.smn.gob.ar/"},
                    timeout=self.timeout,
                )
                if r.status_code == 200:
                    match = re.search(r"localStorage\.setItem\(['\"]token['\"]\s*,\s*['\"]([^'\"]+)['\"]", r.text)
                    if match:
                        token = match.group(1)
                        self._jwt_token = token
                        self._token_expiry = now + 3600
                        logger.info("Successfully obtained SMN JWT token via Scrape.do (valid 1 hour).")
                        return token
                    else:
                        logger.warning("Scrape.do returned HTML but token regex did not match.")
                else:
                    logger.warning("Scrape.do returned HTTP %s: %s", r.status_code, r.text[:120])
            except Exception as err:
                logger.warning("Scrape.do token fetch failed: %s", err)

        # 2. Check if a custom token relay (e.g. Cloudflare Worker) is configured
        relay_url = os.getenv("TOKEN_RELAY_URL")
        if relay_url:
            try:
                logger.info("Fetching token from relay: %s", relay_url)
                r = self.session.get(relay_url, timeout=self.timeout)
                if r.status_code == 200:
                    data = r.json() if "application/json" in r.headers.get("content-type", "") else {}
                    token = data.get("token") or r.text.strip()
                    if token and len(token) > 50:
                        self._jwt_token = token
                        self._token_expiry = now + 3600
                        logger.info("Successfully obtained SMN JWT token from relay URL.")
                        return token
            except Exception as err:
                logger.warning("Token relay %s failed: %s", relay_url, err)

        # 3. Check if manual token is explicitly provided via environment
        env_token = os.getenv("SMN_JWT_TOKEN")
        if env_token and not force_refresh:
            self._jwt_token = env_token.strip()
            self._token_expiry = now + 86400
            logger.info("Using SMN JWT token from environment variable.")
            return self._jwt_token

        logger.info("Fetching fresh SMN JWT token directly from https://www.smn.gob.ar/...")

        # 4. Direct fetch using multiple browser impersonation profiles
        impersonation_profiles = ["chrome124", "safari17_0", "chrome120"] if HAS_CURL_CFFI else []

        for profile in impersonation_profiles:
            try:
                resp = cffi_requests.get("https://www.smn.gob.ar/", impersonate=profile, timeout=self.timeout)
                if resp.status_code == 200:
                    html = resp.text
                    match = re.search(r"localStorage\.setItem\(['\"]token['\"]\s*,\s*['\"]([^'\"]+)['\"]", html)
                    if match:
                        token = match.group(1)
                        self._jwt_token = token
                        try:
                            parts = token.split(".")
                            if len(parts) >= 2:
                                payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                                payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
                                self._token_expiry = float(payload.get("exp", now + 3600))
                        except Exception:
                            self._token_expiry = now + 3600
                        logger.info("Successfully obtained SMN JWT token using %s (valid for %ds)",
                                    profile, int(self._token_expiry - now))
                        return token
                else:
                    logger.debug("Profile %s received status %s from smn.gob.ar", profile, resp.status_code)
            except Exception as err:
                logger.debug("Profile %s failed: %s", profile, err)

        # Fallback to standard requests if cffi failed
        try:
            resp = self.session.get("https://www.smn.gob.ar/", timeout=self.timeout)
            if resp.status_code == 200:
                html = resp.text
                match = re.search(r"localStorage\.setItem\(['\"]token['\"]\s*,\s*['\"]([^'\"]+)['\"]", html)
                if match:
                    token = match.group(1)
                    self._jwt_token = token
                    self._token_expiry = now + 3600
                    return token
        except Exception as err:
            logger.warning("Standard session failed to fetch token: %s", err)

        logger.warning("Could not extract SMN JWT token from website.")
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
            logger.warning("Cannot resolve location ID without JWT token.")
            return None

        url = f"{WS1_BASE_URL}/georef/location/coord"
        try:
            resp = self._http_get(url, params={"lat": lat, "lon": lon})
            if resp.status_code == 200:
                data = resp.json()
                loc_id = data.get("id") or data.get("_id")
                if loc_id:
                    self._cached_location_id = int(loc_id)
                    logger.info(
                        "Resolved coordinates (%s, %s) to SMN Location: ID=%s, Name=%s (%s)",
                        lat, lon, self._cached_location_id, data.get("name"), data.get("department")
                    )
                    return self._cached_location_id
            else:
                logger.warning("Georef coordinate lookup returned status %s: %s", resp.status_code, resp.text[:150])
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
            logger.warning("Location ID could not be resolved for SAT check.")
            return False, None

        url = f"{WS1_BASE_URL}/warning/alert/location/{loc_id}"
        try:
            resp = self._http_get(url)

            # If 401, refresh token and retry
            if resp.status_code == 401:
                logger.info("JWT token expired (401). Refreshing token...")
                self.get_jwt_token(force_refresh=True)
                resp = self._http_get(url)

            if resp.status_code == 200:
                data = resp.json()
                warnings = data.get("warnings", [])
                for w in warnings:
                    max_level = w.get("max_level", 1)
                    # Levels 3 (Amarillo), 4 (Naranja), 5 (Rojo) represent active SAT alerts
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
                        logger.warning(
                            "ACTIVE SAT alert detected via ws1: Level=%s (%s), Event=%s, Date=%s",
                            max_level, level_label, event_desc, alert_info["date"]
                        )
                        return True, alert_info
                logger.info("Location %s has 0 active SAT warnings above normal (max_level < 3).", loc_id)
            else:
                logger.warning("SAT alert check returned status %s: %s", resp.status_code, resp.text[:150])
        except Exception as err:
            logger.warning("Error fetching location SAT alerts from %s: %s", url, err)

        return False, None

    def _fetch_endpoint(self, url: str) -> List[Dict[str, Any]]:
        """Fetch and parse JSON from legacy open feeds with fallback."""
        try:
            response = self._http_get(url)
            if response.status_code == 200:
                data = response.json()
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
