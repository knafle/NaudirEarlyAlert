"""Comprehensive automated tests for NaudirEarlyAlert daemon modules."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smn_client import SMNClient
from subscribers import SubscribersDB
from telegram_bot import format_acp_message
from worker import (
    WeatherAlertWorker,
    generate_event_id,
    is_point_in_polygon,
    parse_polygon_coords,
)


class TestSubscribersDB(unittest.TestCase):
    """Test SQLite subscriber persistence."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_subscribers.db"
        self.db = SubscribersDB(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_sync_crud_operations(self) -> None:
        # Initially empty
        self.assertEqual(self.db.get_all_subscribers_sync(), [])
        self.assertEqual(self.db.get_count_sync(), 0)

        # Add new subscriber
        self.assertTrue(self.db.add_subscriber_sync(12345))
        self.assertEqual(self.db.get_count_sync(), 1)
        self.assertTrue(self.db.is_subscribed_sync(12345))

        # Add duplicate subscriber
        self.assertFalse(self.db.add_subscriber_sync(12345))
        self.assertEqual(self.db.get_count_sync(), 1)

        # Add another subscriber
        self.assertTrue(self.db.add_subscriber_sync(67890))
        self.assertEqual(self.db.get_all_subscribers_sync(), [12345, 67890])

        # Remove subscriber
        self.assertTrue(self.db.remove_subscriber_sync(12345))
        self.assertFalse(self.db.is_subscribed_sync(12345))
        self.assertEqual(self.db.get_count_sync(), 1)

        # Remove non-existent subscriber
        self.assertFalse(self.db.remove_subscriber_sync(99999))

    def test_async_operations(self) -> None:
        async def run_async_tests():
            self.assertTrue(await self.db.add_subscriber(111))
            self.assertFalse(await self.db.add_subscriber(111))
            self.assertTrue(await self.db.is_subscribed(111))
            self.assertEqual(await self.db.get_all_subscribers(), [111])
            self.assertEqual(await self.db.get_count(), 1)
            self.assertTrue(await self.db.remove_subscriber(111))
            self.assertFalse(await self.db.is_subscribed(111))

        asyncio.run(run_async_tests())


class TestWorkerGeospatialLogic(unittest.TestCase):
    """Test polygon parsing and Shapely point-in-polygon containment."""

    def test_parse_polygon_coords_string(self) -> None:
        raw_poly = "[-34.12,-58.50],[-34.40,-58.90],[-34.50,-58.40],[-34.12,-58.50]"
        coords = parse_polygon_coords(raw_poly)
        self.assertEqual(len(coords), 4)
        # Should be stored as (lon, lat)
        self.assertAlmostEqual(coords[0][0], -58.50)
        self.assertAlmostEqual(coords[0][1], -34.12)

    def test_point_in_polygon_containment(self) -> None:
        # Construct a box enclosing El Naudir (-34.3100, -58.7391)
        # [-34.20, -58.80] to [-34.40, -58.60]
        raw_enclosing = "[-34.20,-58.80],[-34.20,-58.60],[-34.40,-58.60],[-34.40,-58.80],[-34.20,-58.80]"
        coords_enclosing = parse_polygon_coords(raw_enclosing)

        # Inside target coordinates
        self.assertTrue(is_point_in_polygon(coords_enclosing, -34.3100, -58.7391))

        # Outside coordinates (e.g., La Plata: -34.92, -57.95)
        self.assertFalse(is_point_in_polygon(coords_enclosing, -34.9200, -57.9500))

    def test_outside_polygon_rejection(self) -> None:
        # Polygon located far away (Cordoba: ~ -31.4, -64.1)
        raw_cordoba = "[-31.30,-64.30],[-31.30,-64.00],[-31.50,-64.00],[-31.50,-64.30]"
        coords_cordoba = parse_polygon_coords(raw_cordoba)
        self.assertFalse(is_point_in_polygon(coords_cordoba, -34.3100, -58.7391))

    def test_geojson_polygon_parsing(self) -> None:
        # Real SMN GeoJSON polygon structure
        geojson = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-61.75, -33.15],
                    [-61.49, -33.86],
                    [-60.53, -34.2],
                    [-59.94, -34.01],
                    [-61.11, -32.77],
                    [-61.46, -32.86]
                ]
            ]
        }
        coords = parse_polygon_coords(geojson)
        self.assertEqual(len(coords), 6)
        # Verify El Naudir is outside this Pergamino/San Nicolas storm
        self.assertFalse(is_point_in_polygon(coords, -34.3100, -58.7391))

    def test_generate_event_id_deduplication(self) -> None:
        item_a = {
            "date": "20/09/2026",
            "hour": "15:00",
            "title": "Tormentas fuertes",
            "polygon": "[-34.1,-58.1],[-34.2,-58.2]",
        }
        item_b = dict(item_a)
        # Same data should produce identical event ID
        self.assertEqual(generate_event_id(item_a), generate_event_id(item_b))

        # Different time should produce different event ID
        item_c = dict(item_a)
        item_c["hour"] = "15:30"
        self.assertNotEqual(generate_event_id(item_a), generate_event_id(item_c))

    def test_sat_matches_detection(self) -> None:
        client = SMNClient()
        worker = WeatherAlertWorker(
            smn_client=client,
            target_zone="Escobar",
        )

        mock_sat_alerts = [
            {
                "title": "Alerta por tormentas",
                "severity": "amarillo",
                "zones": {"0": "Campana", "1": "Escobar", "2": "San Fernando"},
            },
            {
                "title": "Alerta por vientos",
                "severity": "naranja",
                "zones": {"0": "Mar del Plata", "1": "Necochea"},
            },
        ]
        is_active, alert = worker.check_sat_matches(mock_sat_alerts)
        self.assertTrue(is_active)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["title"], "Alerta por tormentas")

        # Test tranquility / normal severity is ignored
        mock_normal_alert = [
            {
                "title": "Sin alertas meteorológicas",
                "severity": "verde",
                "zones": {"0": "Escobar"},
            }
        ]
        is_active, _ = worker.check_sat_matches(mock_normal_alert)
        self.assertFalse(is_active)


class TestTelegramMessageFormatting(unittest.TestCase):
    """Test HTML message formatting and escaping."""

    def test_format_acp_message(self) -> None:
        item = {
            "hour": "14:30",
            "title": "Tormentas con <granizo> & fuertes ráfagas",
            "gmp_general": "https://ws.smn.gob.ar/alerts/radar/anim.gif",
        }
        msg = format_acp_message(item, location_name="El Naudir")

        # Must have escaped HTML
        self.assertIn("&lt;granizo&gt; &amp; fuertes ráfagas", msg)
        self.assertIn("14:30", msg)
        self.assertIn("El Naudir", msg)
        self.assertIn("https://ws.smn.gob.ar/alerts/radar/anim.gif", msg)
        self.assertIn("⚠️ <b>ALERTA METEOROLÓGICA (ACP)</b> ⚠️", msg)


class TestWorkerIntegration(unittest.TestCase):
    """Integration test verifying worker state-machine, containment, and deduplication."""

    def test_worker_cycle_and_deduplication(self) -> None:
        async def run_scenario():
            received_alerts = []

            async def mock_broadcast(item):
                received_alerts.append(item)

            mock_client = mock.MagicMock()

            # Cycle 1: SAT active, ACP storm over El Naudir
            sat_payload = [{
                "title": "Alerta Tormenta",
                "severity": "amarillo",
                "zones": {"0": "Escobar"},
            }]
            acp_payload = [{
                "id": "storm-100",
                "title": "Tormenta Severa Escobar",
                "date": "20/09/2026",
                "hour": "16:00",
                "polygon": "[-34.20,-58.80],[-34.20,-58.60],[-34.40,-58.60],[-34.40,-58.80]",
            }]

            mock_client.get_regional_alerts_async = mock.AsyncMock(return_value=sat_payload)
            mock_client.get_short_term_warnings_async = mock.AsyncMock(return_value=acp_payload)

            worker = WeatherAlertWorker(
                smn_client=mock_client,
                target_lat=-34.3100,
                target_lon=-58.7391,
                target_zone="Escobar",
                sat_poll_interval_hours=0.001,
                acp_poll_interval_minutes=0.001,
                broadcast_callback=mock_broadcast,
            )
            worker._running = True

            # Run 1 SAT check and 1 ACP check
            is_active, _ = worker.check_sat_matches(sat_payload)
            worker.sat_active = is_active

            # Simulate one ACP poll iteration
            warnings = await mock_client.get_short_term_warnings_async()
            current_active_ids = set()
            for item in warnings:
                eid = generate_event_id(item)
                current_active_ids.add(eid)
                coords = parse_polygon_coords(item["polygon"])
                if is_point_in_polygon(coords, worker.target_lat, worker.target_lon):
                    if eid not in worker.seen_acp_ids:
                        worker.seen_acp_ids.add(eid)
                        await worker.broadcast_callback(item)

            self.assertEqual(len(received_alerts), 1)
            self.assertEqual(received_alerts[0]["id"], "storm-100")
            self.assertIn("storm-100", worker.seen_acp_ids)

            # Cycle 2: Same storm still active -> should NOT broadcast again
            for item in warnings:
                eid = generate_event_id(item)
                coords = parse_polygon_coords(item["polygon"])
                if is_point_in_polygon(coords, worker.target_lat, worker.target_lon):
                    if eid not in worker.seen_acp_ids:
                        worker.seen_acp_ids.add(eid)
                        await worker.broadcast_callback(item)

            self.assertEqual(len(received_alerts), 1, "Deduplication failed: broadcast was repeated")

            # Cycle 3: Storm passes (feed is empty) -> prune seen IDs
            empty_active_ids = set()
            worker.seen_acp_ids.intersection_update(empty_active_ids)
            self.assertEqual(len(worker.seen_acp_ids), 0, "Pruning failed: seen IDs not cleared")

        asyncio.run(run_scenario())


if __name__ == "__main__":
    unittest.main()
