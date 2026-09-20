"""SMN (Servicio Meteorológico Nacional) public alert endpoints client.

Queries:
- Broad Regional Alerts (SAT): https://ws.smn.gob.ar/alerts/type/AL
- Short-Term Radar Warnings (ACP): https://ws.smn.gob.ar/alerts/type/ACP
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional curl_cffi for browser TLS fingerprint impersonation if Cloudflare challenges standard requests
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)

URL_SAT_ALERTS = "https://ws.smn.gob.ar/alerts/type/AL"
URL_ACP_WARNINGS = "https://ws.smn.gob.ar/alerts/type/ACP"

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


class SMNClient:
    """Client for retrieving SAT and ACP alerts from public SMN endpoints."""

    def __init__(
        self,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
    ) -> None:
        """Initialize the client session with retry adapter."""
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

    def _fetch_endpoint(self, url: str) -> List[Dict[str, Any]]:
        """Fetch and parse JSON from a given SMN URL.

        Attempts standard requests first. If a 503 challenge is encountered and
        curl_cffi is available, falls back to impersonating a desktop browser TLS fingerprint.
        """
        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    return [data]
                return []
            elif response.status_code == 503 and self._cffi_session:
                logger.warning(
                    "Endpoint %s returned 503. Attempting fallback via browser impersonation...", url
                )
                cffi_resp = self._cffi_session.get(url, timeout=self.timeout)
                if cffi_resp.status_code == 200:
                    data = cffi_resp.json()
                    return data if isinstance(data, list) else [data]
                logger.warning(
                    "Fallback to %s failed with status %s", url, cffi_resp.status_code
                )
            else:
                logger.warning(
                    "SMN endpoint %s returned unexpected HTTP status %s",
                    url,
                    response.status_code,
                )
        except requests.exceptions.JSONDecodeError as err:
            logger.warning("SMN endpoint %s returned non-JSON payload: %s", url, err)
        except requests.exceptions.RequestException as err:
            logger.warning("Network error fetching SMN endpoint %s: %s", url, err)
        except Exception as err:
            logger.error("Unexpected error querying SMN %s: %s", url, err, exc_info=True)

        return []

    def get_regional_alerts(self) -> List[Dict[str, Any]]:
        """Retrieve active regional SAT alerts (/alerts/type/AL)."""
        return self._fetch_endpoint(URL_SAT_ALERTS)

    def get_short_term_warnings(self) -> List[Dict[str, Any]]:
        """Retrieve active short-term radar storm warnings (/alerts/type/ACP)."""
        return self._fetch_endpoint(URL_ACP_WARNINGS)

    async def get_regional_alerts_async(self) -> List[Dict[str, Any]]:
        """Non-blocking async wrapper for get_regional_alerts."""
        return await asyncio.to_thread(self.get_regional_alerts)

    async def get_short_term_warnings_async(self) -> List[Dict[str, Any]]:
        """Non-blocking async wrapper for get_short_term_warnings."""
        return await asyncio.to_thread(self.get_short_term_warnings)
