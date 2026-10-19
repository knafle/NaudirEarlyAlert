"""Comprehensive automated tests for river flood alert modules in NaudirEarlyAlert."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from river_client import RiverClient, RiverStation
from river_worker import RiverAlertEvent, RiverAlertWorker
from subscribers import SubscribersDB
from telegram_bot import (
    TelegramAlertBot,
    format_river_alert_message,
    format_river_dashboard,
)

SAMPLE_PNA_HTML = """
<!doctype html>
<html>
<body>
<table>
    <tr>
        <th>Puerto</th>
        <th>Río</th>
        <th>Ult. registro</th>
        <th>Variación</th>
        <th>Periodo</th>
        <th>Fecha Hora</th>
        <th>Estado</th>
        <th></th>
        <th>Registro Anterior</th>
        <th>Fecha Anterior</th>
        <th>Alerta</th>
        <th>Evacuación</th>
        <th>Hist.</th>
    </tr>
    <tr>
        <td>ESCOBAR</td>
        <td>DELTA PARANA</td>
        <td>1.50</td>
        <td>0.20</td>
        <td>12</td>
        <td>19/OCT/26 - 1000</td>
        <td>CRECE</td>
        <td></td>
        <td>1.30</td>
        <td>19/OCT/26 - 0800</td>
        <td>3.00</td>
        <td>3.50</td>
        <td><a href="/alturas/?page=historico&tiempo=7&id=360">ver</a></td>
    </tr>
    <tr>
        <td>DIQUE LUJAN</td>
        <td>DELTA PARANA</td>
        <td>2.35</td>
        <td>0.15</td>
        <td>12</td>
        <td>19/OCT/26 - 1000</td>
        <td>ALERTA CRECE</td>
        <td></td>
        <td>2.20</td>
        <td>19/OCT/26 - 0800</td>
        <td>2.30</td>
        <td>2.80</td>
        <td><a href="/alturas/?page=historico&tiempo=7&id=440">ver</a></td>
    </tr>
    <tr>
        <td>ZARATE</td>
        <td>DELTA PARANA</td>
        <td>2.25</td>
        <td>0.10</td>
        <td>12</td>
        <td>19/OCT/26 - 1000</td>
        <td>ALERTA CRECE</td>
        <td></td>
        <td>2.15</td>
        <td>19/OCT/26 - 0800</td>
        <td>2.20</td>
        <td>2.40</td>
        <td><a href="/alturas/?page=historico&tiempo=7&id=340">ver</a></td>
    </tr>
    <tr>
        <td>CAMPANA</td>
        <td>DELTA PARANA</td>
        <td>2.10</td>
        <td>0.05</td>
        <td>12</td>
        <td>19/OCT/26 - 1000</td>
        <td>CRECE</td>
        <td></td>
        <td>2.05</td>
        <td>19/OCT/26 - 0800</td>
        <td>2.20</td>
        <td>2.45</td>
        <td><a href="/alturas/?page=historico&tiempo=7&id=350">ver</a></td>
    </tr>
    <tr>
        <td>TIGRE</td>
        <td>DELTA PARANA</td>
        <td>3.40</td>
        <td>0.40</td>
        <td>12</td>
        <td>19/OCT/26 - 1000</td>
        <td>ALERTA CRECE</td>
        <td></td>
        <td>3.00</td>
        <td>19/OCT/26 - 0800</td>
        <td>3.30</td>
        <td>3.80</td>
        <td><a href="/alturas/?page=historico&tiempo=7&id=430">ver</a></td>
    </tr>
    <tr>
        <td>SAN PEDRO</td>
        <td>DELTA PARANA</td>
        <td>3.70</td>
        <td>0.10</td>
        <td>12</td>
        <td>19/OCT/26 - 1000</td>
        <td>EVACUACION CRECE</td>
        <td></td>
        <td>3.60</td>
        <td>19/OCT/26 - 0800</td>
        <td>3.40</td>
        <td>3.60</td>
        <td><a href="/alturas/?page=historico&tiempo=7&id=320">ver</a></td>
    </tr>
    <tr>
        <td>BOUVIER</td>
        <td>PARAGUAY</td>
        <td>S/E</td>
        <td>-</td>
        <td>-</td>
        <td>19/OCT/26 - 0000</td>
        <td>S/E.</td>
        <td></td>
        <td>-</td>
        <td>-</td>
        <td>6.00</td>
        <td>6.40</td>
        <td></td>
    </tr>
</table>
</body>
</html>
"""


class TestRiverClient(unittest.TestCase):
    """Test PNA table scraping, normalization and station groupings."""

    def test_parse_pna_html(self) -> None:
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        self.assertIn("ESCOBAR", stations)
        self.assertIn("DIQUE LUJAN", stations)
        self.assertIn("ZARATE", stations)
        self.assertIn("TIGRE", stations)
        self.assertIn("BOUVIER", stations)

        escobar = stations["ESCOBAR"]
        self.assertEqual(escobar.name, "ESCOBAR")
        self.assertEqual(escobar.river, "DELTA PARANA")
        self.assertEqual(escobar.level, 1.50)
        self.assertEqual(escobar.variation, 0.20)
        self.assertEqual(escobar.alert_level, 3.00)
        self.assertEqual(escobar.evac_level, 3.50)
        self.assertEqual(escobar.station_id, "360")
        self.assertFalse(escobar.is_alerta)
        self.assertFalse(escobar.is_evacuacion)
        self.assertEqual(escobar.trend_emoji, "↗️")

        dique = stations["DIQUE LUJAN"]
        self.assertTrue(dique.is_alerta)
        self.assertFalse(dique.is_evacuacion)
        self.assertEqual(dique.status_summary, "🟠 ALERTA")

        san_pedro = stations["SAN PEDRO"]
        self.assertTrue(san_pedro.is_evacuacion)
        self.assertEqual(san_pedro.status_summary, "🔴 EVACUACIÓN")

        bouvier = stations["BOUVIER"]
        self.assertIsNone(bouvier.level)
        self.assertIsNone(bouvier.variation)

    def test_relevant_stations_grouping(self) -> None:
        client = RiverClient()
        client._cached_data = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        client._cached_time = 9999999999.0

        res = asyncio.run(client.get_relevant_stations_async())
        self.assertIn("local", res)
        self.assertIn("upstream", res)
        self.assertIn("downstream", res)

        local_names = [s.name for s in res["local"]]
        self.assertIn("ESCOBAR", local_names)
        self.assertIn("DIQUE LUJAN", local_names)

        upstream_names = [s.name for s in res["upstream"]]
        self.assertIn("ZARATE", upstream_names)
        self.assertIn("CAMPANA", upstream_names)
        self.assertIn("SAN PEDRO", upstream_names)

        downstream_names = [s.name for s in res["downstream"]]
        self.assertIn("TIGRE", downstream_names)


class TestRiverAlertWorker(unittest.TestCase):
    """Test threat evaluation, severity escalation and deduplication in RiverAlertWorker."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_river.db"
        self.db = SubscribersDB(self.db_path)

        self.mock_client = mock.MagicMock(spec=RiverClient)
        self.worker = RiverAlertWorker(
            river_client=self.mock_client,
            poll_interval_minutes=15.0,
            subscribers_db=self.db,
            location_name="El Naudir",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_threat_evaluation_silent_on_alerta_and_triggers_on_evacuacion(self) -> None:
        """Verify that routine 'Alerta' levels are SILENT, and only Evacuacion/Emergencia trigger."""
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        events = self.worker.evaluate_threats(stations)

        # In SAMPLE_PNA_HTML:
        # - Dique Lujan is at 2.35m (Alerta 2.30m, Evac 2.80m) -> MUST BE SILENT!
        # - Zarate is at 2.25m (Alerta 2.20m, Evac 2.40m) -> MUST BE SILENT!
        # - Tigre is at 3.40m (Alerta 3.30m, Evac 3.80m) -> MUST BE SILENT!
        # - Only San Pedro is at 3.70m (Evac 3.60m) -> MUST TRIGGER AVISO_CUENCA_ARRIBA!
        trigger_names = [e.trigger_station.name for e in events]
        self.assertNotIn("DIQUE LUJAN", trigger_names)
        self.assertNotIn("ZARATE", trigger_names)
        self.assertNotIn("TIGRE", trigger_names)
        self.assertIn("SAN PEDRO", trigger_names)

        sp_event = next(e for e in events if e.trigger_station.name == "SAN PEDRO")
        self.assertEqual(sp_event.threat_type, "CUENCA_ARRIBA")
        self.assertEqual(sp_event.severity, "AVISO_PREVENTIVO")
        self.assertIn("24 a 48 horas", sp_event.estimated_arrival)

    def test_tier1_anegamiento_calles(self) -> None:
        """Verify local Escobar reaches alert level (3.80 m) triggering ALERTA_LOCAL."""
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        stations["ESCOBAR"] = RiverStation(
            name="ESCOBAR",
            river="DELTA PARANA",
            level=3.85,
            variation=0.30,
            period_hours=12,
            timestamp="19/OCT/26 - 1200",
            state="EVACUACION CRECE",
            prev_level=3.55,
            prev_timestamp="19/OCT/26 - 1000",
            alert_level=3.00,
            evac_level=3.50,
            station_id="360",
        )
        events = self.worker.evaluate_threats(stations)
        escobar_event = next(e for e in events if e.trigger_station.name == "ESCOBAR")
        self.assertEqual(escobar_event.threat_type, "LOCAL")
        self.assertEqual(escobar_event.severity, "ALERTA_LOCAL")
        self.assertIn("advertencia local en Escobar", escobar_event.summary)
        self.assertIn("desagües", escobar_event.impact_projection.lower())

    def test_tier2_emergencia_casa_flood_danger(self) -> None:
        """Verify extreme level triggers EMERGENCIA_EXTREMA when water level >= 4.10 m."""
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        stations["ESCOBAR"] = RiverStation(
            name="ESCOBAR",
            river="DELTA PARANA",
            level=4.15,
            variation=0.50,
            period_hours=12,
            timestamp="19/OCT/26 - 1200",
            state="EVACUACION CRECE",
            prev_level=3.65,
            prev_timestamp="19/OCT/26 - 1000",
            alert_level=3.00,
            evac_level=3.50,
            station_id="360",
        )

        events = self.worker.evaluate_threats(stations)
        escobar_event = next(e for e in events if e.trigger_station.name == "ESCOBAR")
        self.assertEqual(escobar_event.threat_type, "LOCAL")
        self.assertEqual(escobar_event.severity, "EMERGENCIA_EXTREMA")
        self.assertIn("crítica en Escobar", escobar_event.summary)
        self.assertIn("calzadas", escobar_event.impact_projection.lower())

    def test_extravagant_upstream_crest_triggers_house_emergency(self) -> None:
        """Verify that an extravagant level in Zárate (>= 3.80m) triggers EMERGENCIA_EXTREMA on its own."""
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        stations["ZARATE"] = RiverStation(
            name="ZARATE",
            river="DELTA PARANA",
            level=3.95,
            variation=0.80,
            period_hours=12,
            timestamp="19/OCT/26 - 1200",
            state="EVACUACION CRECE",
            prev_level=3.15,
            prev_timestamp="19/OCT/26 - 1000",
            alert_level=2.20,
            evac_level=2.40,
            station_id="340",
        )
        events = self.worker.evaluate_threats(stations)
        zarate_event = next(e for e in events if e.trigger_station.name == "ZARATE")
        self.assertEqual(zarate_event.threat_type, "CUENCA_ARRIBA")
        self.assertEqual(zarate_event.severity, "EMERGENCIA_EXTREMA")
        self.assertIn("colosal", zarate_event.summary.lower())
        self.assertIn("descomunal", zarate_event.situation.lower())

    def test_upstream_crest_discarded_when_downstream_drains_freely(self) -> None:
        """Verify that a crest in Zárate (e.g. 2.85m) is SILENT if the downstream river is draining freely."""
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        # Set downstream stations to normal/low
        stations["TIGRE"] = RiverStation(name="TIGRE", river="DELTA PARANA", level=1.20, variation=-0.10, period_hours=12, timestamp="", state="BAJA", prev_level=1.30, prev_timestamp="", alert_level=3.30, evac_level=3.80)
        stations["SAN FERNANDO"] = RiverStation(name="SAN FERNANDO", river="DELTA PARANA", level=1.10, variation=-0.10, period_hours=12, timestamp="", state="BAJA", prev_level=1.20, prev_timestamp="", alert_level=3.00, evac_level=3.50)
        stations["ESCOBAR"] = RiverStation(name="ESCOBAR", river="DELTA PARANA", level=1.30, variation=-0.05, period_hours=12, timestamp="", state="BAJA", prev_level=1.35, prev_timestamp="", alert_level=3.00, evac_level=3.50)

        # Zárate in crest (2.85m)
        stations["ZARATE"] = RiverStation(name="ZARATE", river="DELTA PARANA", level=2.85, variation=0.30, period_hours=12, timestamp="", state="EVACUACION CRECE", prev_level=2.55, prev_timestamp="", alert_level=2.20, evac_level=2.40)

        events = self.worker.evaluate_threats(stations)
        # Zárate must NOT trigger because downstream is draining freely
        trigger_names = [e.trigger_station.name for e in events]
        self.assertNotIn("ZARATE", trigger_names)

    def test_upstream_crest_triggers_when_downstream_blocked(self) -> None:
        """Verify that Zárate in crest triggers an alert when the downstream river is blocked/rising."""
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        stations["TIGRE"] = RiverStation(name="TIGRE", river="DELTA PARANA", level=2.60, variation=0.40, period_hours=12, timestamp="", state="CRECE", prev_level=2.20, prev_timestamp="", alert_level=3.30, evac_level=3.80)
        stations["ZARATE"] = RiverStation(name="ZARATE", river="DELTA PARANA", level=2.85, variation=0.30, period_hours=12, timestamp="", state="EVACUACION CRECE", prev_level=2.55, prev_timestamp="", alert_level=2.20, evac_level=2.40)

        events = self.worker.evaluate_threats(stations)
        trigger_names = [e.trigger_station.name for e in events]
        self.assertIn("ZARATE", trigger_names)
        zarate_event = next(e for e in events if e.trigger_station.name == "ZARATE")
        self.assertEqual(zarate_event.severity, "AVISO_PREVENTIVO")

    def test_cycle_broadcast_and_deduplication(self) -> None:
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        self.mock_client.get_all_stations_async = mock.AsyncMock(return_value=stations)

        broadcast_events: List[RiverAlertEvent] = []

        async def mock_broadcast(event: RiverAlertEvent) -> None:
            broadcast_events.append(event)

        self.worker.broadcast_callback = mock_broadcast

        # Cycle 1: Dispatches only San Pedro (the single station in evacuation >= 3.60m)
        asyncio.run(self.worker._process_cycle())
        initial_broadcast_count = len(broadcast_events)
        self.assertEqual(initial_broadcast_count, 1)
        self.assertEqual(broadcast_events[0].trigger_station.name, "SAN PEDRO")

        # Cycle 2: Same levels, should not duplicate
        asyncio.run(self.worker._process_cycle())
        self.assertEqual(len(broadcast_events), initial_broadcast_count)

        # Cycle 3: Escalation in Zárate to Early Warning threshold (>= 2.80m)
        stations_escalated = dict(stations)
        stations_escalated["ZARATE"] = RiverStation(
            name="ZARATE",
            river="DELTA PARANA",
            level=2.85,
            variation=0.20,
            period_hours=12,
            timestamp="19/OCT/26 - 1200",
            state="EVACUACION CRECE",
            prev_level=2.25,
            prev_timestamp="19/OCT/26 - 1000",
            alert_level=2.20,
            evac_level=2.40,
            station_id="340",
        )
        self.mock_client.get_all_stations_async = mock.AsyncMock(return_value=stations_escalated)

        asyncio.run(self.worker._process_cycle())
        self.assertEqual(len(broadcast_events), initial_broadcast_count + 1)
        last_event = broadcast_events[-1]
        self.assertEqual(last_event.trigger_station.name, "ZARATE")
        self.assertEqual(last_event.severity, "AVISO_PREVENTIVO")


class TestTelegramRiverMessages(unittest.TestCase):
    """Test HTML formatting for river alerts and dashboard."""

    def test_format_river_alert_message_tier1_anegamiento_calles(self) -> None:
        st = RiverStation(
            name="ESCOBAR",
            river="DELTA PARANA",
            level=3.55,
            variation=0.25,
            period_hours=12,
            timestamp="19/OCT/26 - 1000",
            state="EVACUACION CRECE",
            prev_level=3.30,
            prev_timestamp="19/OCT/26 - 0800",
            alert_level=3.00,
            evac_level=3.50,
            station_id="360",
        )
        event = RiverAlertEvent(
            event_id="LOCAL:ESCOBAR:ALERTA_LOCAL:35",
            threat_type="LOCAL",
            severity="ALERTA_LOCAL",
            trigger_station=st,
            summary="Nivel de evacuación de Prefectura alcanzado en Escobar: 3.55 m.",
            situation="El río alcanzó el nivel oficial de evacuación en la escala local.",
            estimated_arrival="Inmediato / En curso en la zona",
            impact_projection="Posible inicio de anegamientos en calzadas bajas y accesos.",
            secondary_stations=[
                RiverStation(name="DIQUE LUJAN", river="DELTA PARANA", level=2.85, variation=0.15, period_hours=12, timestamp="", state="CRECE", prev_level=None, prev_timestamp=None, alert_level=2.30, evac_level=2.80),
            ],
        )

        msg = format_river_alert_message(event, location_name="El Naudir")
        self.assertIn("ALERTA HIDROLÓGICA LOCAL", msg)
        self.assertIn("ESCOBAR", msg.upper())
        self.assertIn("3.55 m", msg)
        self.assertIn("Escala PNA", msg)
        self.assertIn("El Naudir", msg)

    def test_format_river_alert_message_tier2_emergencia_casa(self) -> None:
        st = RiverStation(
            name="ESCOBAR",
            river="DELTA PARANA",
            level=3.90,
            variation=0.45,
            period_hours=12,
            timestamp="19/OCT/26 - 1100",
            state="EVACUACION CRECE",
            prev_level=3.45,
            prev_timestamp="19/OCT/26 - 0900",
            alert_level=3.00,
            evac_level=3.50,
            station_id="360",
        )
        event = RiverAlertEvent(
            event_id="LOCAL:ESCOBAR:EMERGENCIA_EXTREMA:39",
            threat_type="LOCAL",
            severity="EMERGENCIA_EXTREMA",
            trigger_station=st,
            summary="Crecida local crítica en Escobar: 3.90 m (Escala PNA).",
            situation="Superó ampliamente el nivel oficial de evacuación de Prefectura (3.50 m).",
            estimated_arrival="Inmediato / En curso en la zona",
            impact_projection="Riesgo severo de desborde y anegamiento en calzadas del barrio.",
            secondary_stations=[
                RiverStation(name="DIQUE LUJAN", river="DELTA PARANA", level=3.20, variation=0.30, period_hours=12, timestamp="", state="EVACUACION CRECE", prev_level=None, prev_timestamp=None, alert_level=2.30, evac_level=2.80),
            ],
        )

        msg = format_river_alert_message(event, location_name="El Naudir")
        self.assertIn("ALERTA HIDROLÓGICA LOCAL CRÍTICA", msg)
        self.assertIn("NIVEL CRÍTICO LOCAL (> 3.80 m)", msg)
        self.assertIn("3.90 m", msg)

    def test_format_river_dashboard(self) -> None:
        stations = RiverClient.parse_pna_html(SAMPLE_PNA_HTML)
        client = RiverClient()
        client._cached_data = stations
        client._cached_time = 9999999999.0

        rel = asyncio.run(client.get_relevant_stations_async())
        dash = format_river_dashboard(rel, location_name="El Naudir")

        self.assertIn("MONITOREO DE ALTURA DE RÍOS (El Naudir)", dash)
        self.assertIn("Alturas oficiales de escala hidrométrica", dash)
        self.assertIn("ZONA LOCAL", dash)
        self.assertIn("CUENCA ARRIBA", dash)
        self.assertIn("CUENCA ABAJO", dash)
        self.assertIn("ESCOBAR", dash)
        self.assertIn("DIQUE LUJAN", dash)
        self.assertIn("Prefectura Naval Argentina", dash)
        self.assertIn("Prefectura Naval Argentina", dash)

    def test_subscribers_db_river_alert_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "subscribers_test.db"
            db = SubscribersDB(db_path)

            key = "CUENCA_ARRIBA:ZARATE:EVACUACION:24"
            self.assertFalse(db.is_river_alert_notified_sync(key))

            db.mark_river_alert_notified_sync(
                alert_key=key,
                station_name="ZARATE",
                alert_type="CUENCA_ARRIBA",
                severity_level="EVACUACION",
                water_level=2.45,
            )
            self.assertTrue(db.is_river_alert_notified_sync(key))

            # Async check
            async_result = asyncio.run(db.is_river_alert_notified(key))
            self.assertTrue(async_result)


if __name__ == "__main__":
    unittest.main()
