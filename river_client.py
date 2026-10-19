"""Prefectura Naval Argentina (PNA) River Level Scraper Client.

Retrieves real-time hydrometric river levels, trends, and alert/evacuation thresholds
from official PNA tables: https://contenidosweb.prefecturanaval.gob.ar/alturas/index.php
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
import re
import time
from typing import Dict, List, Optional
import urllib3

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Suppress insecure HTTPS warnings if government SSL certificates fail verification
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

PNA_ALTURAS_URL = "https://contenidosweb.prefecturanaval.gob.ar/alturas/index.php"

# Station definitions categorized by hydrological impact on El Naudir (Escobar)
STATIONS_LOCAL = ["ESCOBAR", "DIQUE LUJAN"]
STATIONS_UPSTREAM = ["CAMPANA", "ZARATE", "BARADERO", "SAN PEDRO", "RAMALLO", "SAN NICOLAS", "ROSARIO"]
STATIONS_DOWNSTREAM = ["TIGRE", "SAN FERNANDO", "SAN ISIDRO", "BUENOS AIRES"]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}


def _clean_str(text: Optional[str]) -> str:
    """Clean and normalize string values from HTML table."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _parse_float(val: Optional[str]) -> Optional[float]:
    """Parse string float representation handling commas and missing symbols."""
    if not val:
        return None
    val_clean = _clean_str(val).replace(",", ".").replace("+", "")
    if val_clean in ("-", "S/E", "s/e", "N/A", "", "SE"):
        return None
    try:
        return float(val_clean)
    except ValueError:
        return None


def _parse_int(val: Optional[str]) -> Optional[int]:
    """Parse string integer representation."""
    if not val:
        return None
    val_clean = _clean_str(val)
    if val_clean in ("-", "S/E", "N/A", ""):
        return None
    try:
        return int(val_clean)
    except ValueError:
        return None


@dataclass
class RiverStation:
    """Represents real-time hydrometric measurements for a specific river port."""

    name: str
    river: str
    level: Optional[float]
    variation: Optional[float]
    period_hours: Optional[int]
    timestamp: str
    state: str
    prev_level: Optional[float]
    prev_timestamp: Optional[str]
    alert_level: Optional[float]
    evac_level: Optional[float]
    station_id: Optional[str] = None

    @property
    def is_alerta(self) -> bool:
        """Check if station is at or above official alert level."""
        if self.alert_level is not None and self.level is not None and self.level >= self.alert_level:
            return True
        return "ALERTA" in self.state.upper()

    @property
    def is_evacuacion(self) -> bool:
        """Check if station is at or above official evacuation level."""
        if self.evac_level is not None and self.level is not None and self.level >= self.evac_level:
            return True
        return "EVACUACION" in self.state.upper() or "EVACUACIÓN" in self.state.upper()

    @property
    def trend_emoji(self) -> str:
        """Return visual emoji indicating trend direction."""
        s = self.state.upper()
        if "CRECE" in s or (self.variation is not None and self.variation > 0):
            return "↗️"
        elif "BAJA" in s or (self.variation is not None and self.variation < 0):
            return "↘️"
        return "➡️"

    @property
    def status_summary(self) -> str:
        """Return a formatted status badge."""
        if self.is_evacuacion:
            return "🔴 EVACUACIÓN"
        elif self.is_alerta:
            return "🟠 ALERTA"
        s = self.state.upper()
        if "CRECE" in s:
            return "CRECE ↗️"
        elif "BAJA" in s:
            return "BAJA ↘️"
        elif "ESTAC" in s:
            return "ESTAC. ➡️"
        return self.state or "NORMAL"


class RiverClient:
    """Client for querying real-time river levels from Prefectura Naval Argentina."""

    def __init__(
        self,
        timeout: float = 15.0,
        cache_ttl_seconds: float = 180.0,
        verify_ssl: bool = False,
        scrapedo_key: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ) -> None:
        self.timeout = timeout
        self.cache_ttl = cache_ttl_seconds
        self.verify_ssl = verify_ssl
        self.scrapedo_key = scrapedo_key or os.getenv("SCRAPEDO_API_KEY")
        self.proxy_url = proxy_url or os.getenv("RIVER_PROXY_URL")

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._cached_data: Dict[str, RiverStation] = {}
        self._cached_time: float = 0.0
        self._lock = asyncio.Lock()

    def fetch_all_stations_sync(self) -> Dict[str, RiverStation]:
        """Fetch and parse all stations from PNA HTML table."""
        # 1. If Scrape.do key is configured, use it directly (bypasses Google Cloud / AWS IP blocking by PNA)
        if self.scrapedo_key:
            try:
                logger.info("Fetching PNA river data via Scrape.do proxy...")
                resp = self.session.get(
                    "https://api.scrape.do",
                    params={"token": self.scrapedo_key, "url": PNA_ALTURAS_URL},
                    timeout=self.timeout + 10,
                )
                if resp.status_code == 200:
                    stations = self.parse_pna_html(resp.text)
                    if stations:
                        self._cached_data = stations
                        self._cached_time = time.time()
                        logger.info("Successfully fetched %d river stations via Scrape.do.", len(stations))
                        return stations
                logger.warning("Scrape.do returned HTTP %s for PNA. Falling back to direct fetch...", resp.status_code)
            except Exception as err:
                logger.warning("Scrape.do fetch failed: %s. Falling back to direct fetch...", err)

        # 2. If a custom proxy/relay URL is configured (e.g. Cloudflare Worker or reverse proxy)
        if self.proxy_url:
            try:
                logger.info("Fetching PNA river data via proxy URL: %s", self.proxy_url)
                target = self.proxy_url if "{url}" not in self.proxy_url else self.proxy_url.format(url=PNA_ALTURAS_URL)
                resp = self.session.get(target, timeout=self.timeout)
                if resp.status_code == 200:
                    stations = self.parse_pna_html(resp.text)
                    if stations:
                        self._cached_data = stations
                        self._cached_time = time.time()
                        return stations
            except Exception as err:
                logger.warning("Proxy URL fetch failed: %s", err)

        # 3. Direct fetch
        try:
            resp = self.session.get(
                PNA_ALTURAS_URL,
                timeout=(5.0, self.timeout),
                verify=self.verify_ssl,
            )
            resp.raise_for_status()
            html_content = resp.text
        except Exception as err:
            logger.error(
                "Error connecting to PNA heights page: %s. "
                "(If running on a Cloud VM, Prefectura's firewall blocks foreign datacenter IPs. "
                "Set SCRAPEDO_API_KEY in .env to route through proxy).",
                err,
            )
            # If we have cached data, return it even if expired rather than failing completely
            if self._cached_data:
                logger.warning("Returning stale cached river data due to fetch error.")
                return self._cached_data
            raise

        stations = self.parse_pna_html(html_content)
        if stations:
            self._cached_data = stations
            self._cached_time = time.time()
            logger.info("Successfully fetched %d river stations from PNA.", len(stations))
        return stations

    @staticmethod
    def parse_pna_html(html_content: str) -> Dict[str, RiverStation]:
        """Parse PNA HTML table into a dictionary of RiverStation keyed by normalized uppercase name."""
        soup = BeautifulSoup(html_content, "html.parser")
        table = soup.find("table")
        if not table:
            logger.warning("No table found in PNA HTML.")
            return {}

        results: Dict[str, RiverStation] = {}
        rows = table.find_all("tr")
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 12:
                continue

            tds_text = [_clean_str(c.get_text()) for c in cells]
            name = tds_text[0].upper()
            river = tds_text[1].upper()
            level = _parse_float(tds_text[2])
            variation = _parse_float(tds_text[3])
            period_hours = _parse_int(tds_text[4])
            timestamp = tds_text[5]
            state = tds_text[6].upper()
            prev_level = _parse_float(tds_text[8])
            prev_timestamp = tds_text[9]
            alert_level = _parse_float(tds_text[10])
            evac_level = _parse_float(tds_text[11])

            # Extract station ID if link exists
            station_id = None
            link = row.find("a")
            if link and link.get("href"):
                match = re.search(r"id=(\d+)", link["href"])
                if match:
                    station_id = match.group(1)

            station = RiverStation(
                name=name,
                river=river,
                level=level,
                variation=variation,
                period_hours=period_hours,
                timestamp=timestamp,
                state=state,
                prev_level=prev_level,
                prev_timestamp=prev_timestamp,
                alert_level=alert_level,
                evac_level=evac_level,
                station_id=station_id,
            )
            results[name] = station

        return results

    async def get_all_stations_async(self, force_refresh: bool = False) -> Dict[str, RiverStation]:
        """Asynchronously retrieve all river stations with TTL caching."""
        async with self._lock:
            now = time.time()
            if not force_refresh and self._cached_data and (now - self._cached_time < self.cache_ttl):
                return self._cached_data

            data = await asyncio.to_thread(self.fetch_all_stations_sync)
            return data

    async def get_station_async(self, name_or_id: str) -> Optional[RiverStation]:
        """Retrieve a single station by uppercase name or numeric ID."""
        stations = await self.get_all_stations_async()
        target = name_or_id.strip().upper()
        if target in stations:
            return stations[target]

        for s in stations.values():
            if s.station_id == target or s.name.startswith(target):
                return s
        return None

    async def get_relevant_stations_async(self) -> Dict[str, List[RiverStation]]:
        """Retrieve stations classified into local, upstream, and downstream groups for El Naudir."""
        all_stations = await self.get_all_stations_async()

        def collect(names: List[str]) -> List[RiverStation]:
            items: List[RiverStation] = []
            for n in names:
                for k, v in all_stations.items():
                    if n == k or k.startswith(n):
                        items.append(v)
                        break
            return items

        return {
            "local": collect(STATIONS_LOCAL),
            "upstream": collect(STATIONS_UPSTREAM),
            "downstream": collect(STATIONS_DOWNSTREAM),
        }
