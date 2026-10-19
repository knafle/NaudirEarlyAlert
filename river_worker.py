"""Background worker daemon for monitoring river levels and flood risks for El Naudir.

Monitors real-time hydrometric measurements across three tiers:
1. Local stations (Escobar, Dique Luján) - Direct risk of canal/lake inundation.
2. Upstream stations (Campana, Zárate, Baradero, San Pedro) - Advance warning for Paraná river crecidas (El Niño).
3. Downstream stations (Tigre, San Fernando, San Isidro) - Advance warning for Sudestada and estuarine storm surges.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from river_client import RiverClient, RiverStation
from subscribers import SubscribersDB

logger = logging.getLogger(__name__)


import os

# Empirical PNA station thresholds (calibrated above Dec 2023 benchmark)
# 1. Local Escobar (Canal Arias / Paraná de las Palmas - PNA scale):
# Calibrated for El Naudir: Alert at 3.80m (drain saturation), Extreme at 4.10m (low street level)
DEFAULT_ESCOBAR_ALERT = float(os.getenv("RIVER_THRESHOLD_ESCOBAR_ALERT", "3.80"))
DEFAULT_ESCOBAR_EXTREME = float(os.getenv("RIVER_THRESHOLD_ESCOBAR_EXTREME", "4.10"))

# 2. Local Dique Luján (Río Luján / Canal Villanueva - PNA scale):
DEFAULT_DIQUE_LUJAN_ALERT = float(os.getenv("RIVER_THRESHOLD_DIQUE_LUJAN_ALERT", "3.00"))
DEFAULT_DIQUE_LUJAN_EXTREME = float(os.getenv("RIVER_THRESHOLD_DIQUE_LUJAN_EXTREME", "3.30"))

# 3. Downstream Tigre (Sudestada / Río de la Plata - Dec 2023 reached 3.70m without flooding El Naudir):
DEFAULT_TIGRE_ALERT = float(os.getenv("RIVER_THRESHOLD_TIGRE_ALERT", "3.80"))
DEFAULT_TIGRE_EXTREME = float(os.getenv("RIVER_THRESHOLD_TIGRE_EXTREME", "4.05"))

# 4. Downstream San Fernando:
DEFAULT_SAN_FERNANDO_ALERT = float(os.getenv("RIVER_THRESHOLD_SAN_FERNANDO_ALERT", "3.80"))
DEFAULT_SAN_FERNANDO_EXTREME = float(os.getenv("RIVER_THRESHOLD_SAN_FERNANDO_EXTREME", "4.05"))

# 5. Upstream Zárate (6 to 12 hours advance warning):
# Default: Early alert at 2.50m (if downstream blocked), Major at 3.10m, Ridiculous volume safety trigger at 3.80m
DEFAULT_ZARATE_EARLY = float(os.getenv("RIVER_THRESHOLD_ZARATE_EARLY", "2.50"))
DEFAULT_ZARATE_MAJOR = float(os.getenv("RIVER_THRESHOLD_ZARATE_MAJOR", "3.10"))
DEFAULT_ZARATE_EXTREME = float(os.getenv("RIVER_THRESHOLD_ZARATE_EXTREME", "3.80"))

# 6. Upstream Campana:
DEFAULT_CAMPANA_EARLY = float(os.getenv("RIVER_THRESHOLD_CAMPANA_EARLY", "2.55"))
DEFAULT_CAMPANA_MAJOR = float(os.getenv("RIVER_THRESHOLD_CAMPANA_MAJOR", "3.15"))
DEFAULT_CAMPANA_EXTREME = float(os.getenv("RIVER_THRESHOLD_CAMPANA_EXTREME", "3.85"))


@dataclass
class RiverAlertEvent:
    """Represents an active river alert event to be formatted and broadcast."""

    event_id: str
    threat_type: str  # 'LOCAL', 'CUENCA_ARRIBA', 'SUDESTADA'
    severity: str  # 'AVISO_PREVENTIVO', 'CRECIDA_MASIVA', 'ALERTA_LOCAL', 'EMERGENCIA_EXTREMA'
    trigger_station: RiverStation
    summary: str
    situation: str
    estimated_arrival: str
    impact_projection: str
    secondary_stations: List[RiverStation]


class RiverAlertWorker:
    """Background state machine daemon for river flood monitoring."""

    def __init__(
        self,
        river_client: RiverClient,
        poll_interval_minutes: float = 45.0,
        subscribers_db: Optional[SubscribersDB] = None,
        broadcast_callback: Optional[Callable[[RiverAlertEvent], Awaitable[None]]] = None,
        location_name: str = "El Naudir",
        escobar_alert: float = DEFAULT_ESCOBAR_ALERT,
        escobar_extreme: float = DEFAULT_ESCOBAR_EXTREME,
        dique_alert: float = DEFAULT_DIQUE_LUJAN_ALERT,
        dique_extreme: float = DEFAULT_DIQUE_LUJAN_EXTREME,
        tigre_alert: float = DEFAULT_TIGRE_ALERT,
        tigre_extreme: float = DEFAULT_TIGRE_EXTREME,
        zarate_early: float = DEFAULT_ZARATE_EARLY,
        zarate_major: float = DEFAULT_ZARATE_MAJOR,
        zarate_extreme: float = DEFAULT_ZARATE_EXTREME,
    ) -> None:
        self.client = river_client
        self.interval_seconds = max(poll_interval_minutes * 60.0, 120.0)
        self.db = subscribers_db
        self.broadcast_callback = broadcast_callback
        self.location_name = location_name

        # Calibrated empirical thresholds
        self.escobar_alert = escobar_alert
        self.escobar_extreme = escobar_extreme
        self.dique_alert = dique_alert
        self.dique_extreme = dique_extreme
        self.tigre_alert = tigre_alert
        self.tigre_extreme = tigre_extreme
        self.zarate_early = zarate_early
        self.zarate_major = zarate_major
        self.zarate_extreme = zarate_extreme

        # State tracking
        self.last_checked_time: Optional[float] = None
        self.current_stations: Dict[str, RiverStation] = {}
        self.active_alerts: List[RiverAlertEvent] = []
        self.station_previous_severity: Dict[str, str] = {}
        self.station_last_notified_level: Dict[str, float] = {}
        self._running = False

    def evaluate_threats(self, stations: Dict[str, RiverStation]) -> List[RiverAlertEvent]:
        """Evaluate real-time station levels and identify extraordinary flood threats only.

        Strictly silent for normal, pre-alerta, or routine alert levels.
        Triggers only on levels calibrated above historic benchmarks:
        - Local Escobar: >= 3.50 m (PNA Evacuacion) or >= 3.80 m (Extremo)
        - Local Dique Luján: >= 2.80 m or >= 3.10 m
        - Sudestada Tigre: >= 3.80 m (supera record dic-2023 de 3.70m) or >= 4.05 m
        - Upstream Zárate: >= 2.80 m (temprana), >= 3.20 m (masiva), >= 3.80 m (extrema)
        """
        events: List[RiverAlertEvent] = []

        # ---------------------------------------------------------
        # Threat 1: Local Stations (Escobar, Dique Luján)
        # ---------------------------------------------------------
        local_names = ["ESCOBAR", "DIQUE LUJAN"]

        # 1.1 Escobar (Canal Arias / Paraná de las Palmas)
        st_escobar = stations.get("ESCOBAR")
        if st_escobar and st_escobar.level is not None:
            other_local = [stations[n] for n in local_names if n != "ESCOBAR" and n in stations]
            if st_escobar.level >= self.escobar_extreme:
                events.append(
                    RiverAlertEvent(
                        event_id=f"LOCAL:ESCOBAR:EMERGENCIA_EXTREMA:{int(st_escobar.level * 10)}",
                        threat_type="LOCAL",
                        severity="EMERGENCIA_EXTREMA",
                        trigger_station=st_escobar,
                        summary=f"Crecida local crítica en Escobar: {st_escobar.level:.2f} m (Escala PNA).",
                        situation="El río superó los 4.10 m (escala PNA), alcanzando cotas de calzadas bajas en el barrio.",
                        estimated_arrival="Inmediato / En curso en la zona",
                        impact_projection="Riesgo de agua sobre calzadas bajas y accesos.",
                        secondary_stations=other_local,
                    )
                )
            elif st_escobar.level >= self.escobar_alert:
                events.append(
                    RiverAlertEvent(
                        event_id=f"LOCAL:ESCOBAR:ALERTA_LOCAL:{int(st_escobar.level * 10)}",
                        threat_type="LOCAL",
                        severity="ALERTA_LOCAL",
                        trigger_station=st_escobar,
                        summary=f"Nivel de advertencia local en Escobar: {st_escobar.level:.2f} m (Escala PNA).",
                        situation="El río superó el registro de 2023 (3.80 m PNA), con posible saturación de desagües pluviales.",
                        estimated_arrival="Inmediato / En curso en la zona",
                        impact_projection="Posible reflujo en desagües pluviales bajos.",
                        secondary_stations=other_local,
                    )
                )

        # 1.2 Dique Luján (Río Luján / Canal Villanueva)
        st_dique = stations.get("DIQUE LUJAN")
        if st_dique and st_dique.level is not None:
            other_local = [stations[n] for n in local_names if n != "DIQUE LUJAN" and n in stations]
            if st_dique.level >= self.dique_extreme:
                events.append(
                    RiverAlertEvent(
                        event_id=f"LOCAL:DIQUE_LUJAN:EMERGENCIA_EXTREMA:{int(st_dique.level * 10)}",
                        threat_type="LOCAL",
                        severity="EMERGENCIA_EXTREMA",
                        trigger_station=st_dique,
                        summary=f"Crecida extrema en Dique Luján: {st_dique.level:.2f} m (Escala PNA).",
                        situation="Inundación severa afectando la cuenca baja del Río Luján y Canal Villanueva.",
                        estimated_arrival="Inmediato / En curso en la zona",
                        impact_projection="Riesgo de anegamiento en sectores bajos y accesos al barrio.",
                        secondary_stations=other_local,
                    )
                )
            elif st_dique.level >= self.dique_alert:
                events.append(
                    RiverAlertEvent(
                        event_id=f"LOCAL:DIQUE_LUJAN:ALERTA_LOCAL:{int(st_dique.level * 10)}",
                        threat_type="LOCAL",
                        severity="ALERTA_LOCAL",
                        trigger_station=st_dique,
                        summary=f"Nivel de evacuación alcanzado en Dique Luján: {st_dique.level:.2f} m.",
                        situation="Desborde en sectores costeros bajos de Dique Luján y Canal Arias.",
                        estimated_arrival="Inmediato / En curso en la zona",
                        impact_projection="Posible complicación en accesos viales y sectores bajos.",
                        secondary_stations=other_local,
                    )
                )

        # ---------------------------------------------------------
        # Threat 2: Upstream Paraná River (Campana, Zárate, Baradero, San Pedro)
        # ---------------------------------------------------------
        # Evaluacion de Estado de Desagüe / Represamiento Aguas Abajo:
        # El Paraná solo inunda El Naudir si el Delta / Río de la Plata está
        # cargado o no desagotando (salvo volumen colosal ridículo/extremo).
        st_tigre = stations.get("TIGRE")
        st_sanfer = stations.get("SAN FERNANDO")
        st_escobar = stations.get("ESCOBAR")

        is_downstream_blocked = False
        block_reasons: List[str] = []

        if st_tigre and st_tigre.level is not None:
            if st_tigre.level >= 3.00:
                is_downstream_blocked = True
                block_reasons.append(f"Tigre alto: {st_tigre.level:.2f} m")
            elif st_tigre.level >= 2.50 and "CRECE" in (st_tigre.state or "").upper():
                is_downstream_blocked = True
                block_reasons.append(f"Tigre subiendo: {st_tigre.level:.2f} m CRECE")

        if not is_downstream_blocked and st_sanfer and st_sanfer.level is not None:
            if st_sanfer.level >= 2.90:
                is_downstream_blocked = True
                block_reasons.append(f"San Fernando alto: {st_sanfer.level:.2f} m")

        if not is_downstream_blocked and st_escobar and st_escobar.level is not None:
            if st_escobar.level >= 2.50:
                is_downstream_blocked = True
                block_reasons.append(f"Escobar cargado: {st_escobar.level:.2f} m")

        # Immediate upstream: Zárate, Campana (6 to 12 hours advance notice)
        near_upstream = [
            ("ZARATE", self.zarate_early, self.zarate_major, self.zarate_extreme),
            ("CAMPANA", DEFAULT_CAMPANA_EARLY, DEFAULT_CAMPANA_MAJOR, DEFAULT_CAMPANA_EXTREME),
        ]
        for name, early_th, major_th, extreme_th in near_upstream:
            st = stations.get(name)
            if not st or st.level is None:
                continue

            # Caso 1: Trigger de volumen ridículo por seguridad (Fuerza Bruta sin importar aguas abajo)
            if st.level >= extreme_th:
                sev = "EMERGENCIA_EXTREMA"
                summary = f"Crecida colosal extraordinaria por volumen extremo en {st.name}: {st.level:.2f} m."
                situation = f"Volumen hídrico descomunal sin precedentes bajando por el Paraná (supera los {extreme_th:.2f} m)."
                impact = ""
            # Caso 2: Crecida río arriba coordinada con río cargado o no desagotando
            elif st.level >= early_th:
                if not is_downstream_blocked:
                    logger.debug(
                        "Crecida en %s (%.2f m) no alertada: el Delta y Río de la Plata drenan libremente sin represamiento.",
                        st.name,
                        st.level,
                    )
                    continue

                block_desc = ", ".join(block_reasons)
                if st.level >= major_th:
                    sev = "CRECIDA_MASIVA"
                    summary = f"Crecida de gran magnitud en {st.name} ({st.level:.2f} m) con río frenado aguas abajo."
                    situation = f"Onda mayor avanzando por el Paraná con salida comprometida al Río de la Plata ({block_desc})."
                else:
                    sev = "AVISO_PREVENTIVO"
                    summary = f"Aviso por crecida en {st.name} ({st.level:.2f} m) con río cargado aguas abajo."
                    situation = f"Onda en el Paraná desplazándose hacia el sur con desagüe dificultado ({block_desc})."
                impact = ""
            else:
                continue

            event_id = f"CUENCA_ARRIBA:{st.name}:{sev}:{int(st.level * 10)}"
            related = [stations[n] for n in ["CAMPANA", "ZARATE", "ESCOBAR", "TIGRE"] if n != name and n in stations]

            events.append(
                RiverAlertEvent(
                    event_id=event_id,
                    threat_type="CUENCA_ARRIBA",
                    severity=sev,
                    trigger_station=st,
                    summary=summary,
                    situation=situation,
                    estimated_arrival="Aproximadamente 6 a 12 horas hacia Escobar",
                    impact_projection=impact,
                    secondary_stations=related,
                )
            )

        # Mid upstream: Baradero, San Pedro (24 to 48 hours notice)
        mid_upstream = [
            ("BARADERO", 2.80, 3.10, 4.00),
            ("SAN PEDRO", 3.60, 3.90, 4.30),
        ]
        for name, evac_th, extra_th, colossal_th in mid_upstream:
            st = stations.get(name)
            if not st or st.level is None:
                continue

            # Caso 1: Colosal extremo unilateral
            if st.level >= colossal_th:
                sev = "EMERGENCIA_EXTREMA"
                summary = f"Crecida colosal extraordinaria en {st.name} ({st.level:.2f} m)."
                situation = f"Caudal récord masivo en cuenca media del Paraná (supera los {colossal_th:.2f} m)."
                impact = ""
            elif st.level >= evac_th:
                # Caso 2: Solo si aguas abajo está cargado o no desagota
                if not is_downstream_blocked:
                    logger.debug(
                        "Crecida en %s (%.2f m) no alertada: salida franca hacia el estuario.",
                        st.name,
                        st.level,
                    )
                    continue

                block_desc = ", ".join(block_reasons)
                if st.level >= extra_th:
                    sev = "CRECIDA_MASIVA"
                    summary = f"Crecida masiva del Paraná en {st.name} ({st.level:.2f} m) con río cargado."
                    situation = f"Onda mayor en la cuenca media con desagüe comprometido aguas abajo ({block_desc})."
                else:
                    sev = "AVISO_PREVENTIVO"
                    summary = f"Crecida en {st.name} ({st.level:.2f} m) con desagüe dificultado aguas abajo."
                    situation = f"Onda en cuenca media avanzando con río cargado ({block_desc})."
                impact = ""
            else:
                continue

            event_id = f"CUENCA_ARRIBA:{st.name}:{sev}:{int(st.level * 10)}"
            related = [stations[n] for n in ["ZARATE", "CAMPANA", "ESCOBAR", "TIGRE"] if n in stations]

            events.append(
                RiverAlertEvent(
                    event_id=event_id,
                    threat_type="CUENCA_ARRIBA",
                    severity=sev,
                    trigger_station=st,
                    summary=summary,
                    situation=situation,
                    estimated_arrival="24 a 48 horas hacia el Delta y Escobar",
                    impact_projection=impact,
                    secondary_stations=related,
                )
            )

        # ---------------------------------------------------------
        # Threat 3: Downstream / Estuary (Sudestada: Tigre, San Fernando)
        # ---------------------------------------------------------
        downstream_stations = [
            ("TIGRE", self.tigre_alert, self.tigre_extreme),
            ("SAN FERNANDO", DEFAULT_SAN_FERNANDO_ALERT, DEFAULT_SAN_FERNANDO_EXTREME),
        ]
        for name, alert_th, extreme_th in downstream_stations:
            st = stations.get(name)
            if not st or st.level is None:
                continue

            if st.level >= extreme_th:
                sev = "EMERGENCIA_EXTREMA"
                summary = f"Sudestada histórica excepcional: Marea en {st.name} ({st.level:.2f} m)."
                situation = f"Marea extrema del Río de la Plata superando marcas históricas (> {extreme_th:.2f} m)."
                impact = "Represamiento masivo de desagües con riesgo de anegamiento en el barrio."
            elif st.level >= alert_th:
                sev = "ALERTA_LOCAL"
                summary = f"Sudestada en nivel crítico en {st.name} ({st.level:.2f} m)."
                situation = "Marea del Río de la Plata superando el registro histórico de dic-2023 (3.70 m)."
                impact = "Represamiento de desagües con riesgo de anegamiento en calzadas y accesos."
            else:
                continue

            event_id = f"SUDESTADA:{st.name}:{sev}:{int(st.level * 10)}"
            related = [stations[n] for n in ["TIGRE", "SAN FERNANDO", "DIQUE LUJAN", "ESCOBAR"] if n != name and n in stations]

            events.append(
                RiverAlertEvent(
                    event_id=event_id,
                    threat_type="SUDESTADA",
                    severity=sev,
                    trigger_station=st,
                    summary=summary,
                    situation=situation,
                    estimated_arrival="Ingreso activo de marea desde el estuario hacia el Delta",
                    impact_projection=impact,
                    secondary_stations=related,
                )
            )

        return events

    async def _process_cycle(self) -> None:
        """Run a single polling and evaluation cycle."""
        logger.info("🌊 Consultando alturas de ríos en Prefectura Naval Argentina...")
        try:
            stations = await self.client.get_all_stations_async(force_refresh=True)
            self.current_stations = stations
            self.last_checked_time = time.time()
        except Exception as err:
            logger.error("Error fetching river stations from PNA: %s", err)
            return

        events = self.evaluate_threats(stations)
        self.active_alerts = events

        if not events:
            logger.info("ℹ️ Monitoreo de ríos: Niveles normales en todas las estaciones de referencia.")
            # Clear previous severities if all returned to normal
            for st_name in list(self.station_previous_severity.keys()):
                prev_sev = self.station_previous_severity.get(st_name)
                if prev_sev in ("AVISO_PREVENTIVO", "CRECIDA_MASIVA", "ALERTA_LOCAL", "EMERGENCIA_EXTREMA"):
                    logger.info("Estación %s regresó a niveles por debajo de umbrales críticos.", st_name)
                self.station_previous_severity.pop(st_name, None)
            return

        logger.warning("🚨 Se detectaron %d eventos de alerta hídrica para El Naudir.", len(events))

        for event in events:
            st = event.trigger_station
            st_name = st.name
            prev_sev = self.station_previous_severity.get(st_name, "NORMAL")
            current_sev = event.severity
            last_notified_level = self.station_last_notified_level.get(st_name, 0.0)

            should_notify = False

            # Severity rank: NORMAL -> AVISO_PREVENTIVO -> CRECIDA_MASIVA -> ALERTA_LOCAL -> EMERGENCIA_EXTREMA
            sev_rank = {
                "NORMAL": 0,
                "AVISO_PREVENTIVO": 1,
                "CRECIDA_MASIVA": 2,
                "ALERTA_LOCAL": 3,
                "EMERGENCIA_EXTREMA": 4,
            }
            if sev_rank.get(current_sev, 0) > sev_rank.get(prev_sev, 0):
                should_notify = True
                logger.info("Estación %s subió a severidad crítica: %s -> %s", st_name, prev_sev, current_sev)
            elif current_sev == "EMERGENCIA_EXTREMA":
                # In extreme emergency, only re-notify if water rises another +20 cm
                if st.level is not None and (st.level - last_notified_level) >= 0.20:
                    should_notify = True
                    logger.info("Estación %s en emergencia extrema siguió subiendo: %.2fm vs %.2fm",
                                st_name, st.level, last_notified_level)

            # Check database persistence
            if self.db:
                already_notified = await self.db.is_river_alert_notified(event.event_id)
                if already_notified:
                    should_notify = False

            if should_notify:
                logger.warning("📢 Disparando difusión de alerta hídrica: %s (%s)", event.event_id, st_name)
                self.station_previous_severity[st_name] = current_sev
                if st.level is not None:
                    self.station_last_notified_level[st_name] = st.level

                if self.db:
                    try:
                        await self.db.mark_river_alert_notified(
                            alert_key=event.event_id,
                            station_name=st_name,
                            alert_type=event.threat_type,
                            severity_level=current_sev,
                            water_level=st.level,
                        )
                    except Exception as err:
                        logger.error("Error saving river alert to DB: %s", err)

                if self.broadcast_callback:
                    try:
                        await self.broadcast_callback(event)
                    except Exception as err:
                        logger.error("Error broadcasting river alert %s: %s", event.event_id, err, exc_info=True)
            else:
                logger.info("ℹ️ Alerta hídrica %s ya notificada o sin cambios suficientes.", event.event_id)

    async def start(self) -> None:
        """Run the background river monitoring loop."""
        self._running = True
        logger.info(
            "Starting RiverAlertWorker daemon (Poll interval: %.1f min | Target: %s)",
            self.interval_seconds / 60.0,
            self.location_name,
        )
        while self._running:
            try:
                await self._process_cycle()
            except Exception as err:
                logger.error("Unexpected error in RiverAlertWorker loop: %s", err, exc_info=True)

            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    def stop(self) -> None:
        """Stop worker execution."""
        logger.info("Stopping RiverAlertWorker daemon...")
        self._running = False
