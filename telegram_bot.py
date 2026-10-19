"""Telegram Bot interface using python-telegram-bot v20+.

Provides:
- Command handlers (/start, /stop, /estado) in Spanish.
- Broadcast alert formatting with HTML layout.
- Resilient broadcasting with automatic pruning of blocked users.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Dict, List, Optional

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from river_client import RiverClient, RiverStation
from river_worker import RiverAlertEvent
from subscribers import SubscribersDB

logger = logging.getLogger(__name__)

DEFAULT_RADAR_URL = "https://www.smn.gob.ar/radar"
DEFAULT_ACP_WEB_URL = "https://www.smn.gob.ar/avisos_a_muy_corto_plazo"
DEFAULT_PNA_URL = "https://contenidosweb.prefecturanaval.gob.ar/alturas/"


def format_acp_message(item: Dict[str, Any], location_name: str = "El Naudir") -> str:
    """Format an ACP radar alert dictionary into an HTML Telegram message."""
    raw_date = str(item.get("date") or "")
    raw_end = str(item.get("end_date") or "")

    hora_emision = ""
    if "T" in raw_date:
        try:
            hora_emision = raw_date.split("T")[1][:5]
        except Exception:
            pass
    if not hora_emision:
        hora_emision = str(item.get("hour") or item.get("hora") or "Reciente")

    hora_fin = ""
    if "T" in raw_end:
        try:
            hora_fin = raw_end.split("T")[1][:5]
        except Exception:
            pass

    duracion_str = ""
    if raw_date and raw_end:
        try:
            from datetime import datetime
            s = datetime.fromisoformat(raw_date)
            e = datetime.fromisoformat(raw_end)
            diff_mins = int((e - s).total_seconds() / 60)
            hours = diff_mins // 60
            mins = diff_mins % 60
            if hours > 0 and mins > 0:
                duracion_str = f" ({hours}h {mins}m de validez)"
            elif hours > 0:
                duracion_str = f" ({hours} {'hora' if hours == 1 else 'horas'} de validez)"
            elif mins > 0:
                duracion_str = f" ({mins} min de validez)"
        except Exception:
            pass

    # Severity level
    sev = str(item.get("severity") or "").upper().strip()
    sev_text = ""
    if sev == "N":
        sev_text = "🟠 <b>Severidad:</b> Naranja (Tormentas fuertes / severas)\n"
    elif sev == "A":
        sev_text = "🟡 <b>Severidad:</b> Amarillo (Tormentas fuertes)\n"
    elif sev == "R":
        sev_text = "🔴 <b>Severidad:</b> Rojo (Tormentas excepcionales)\n"

    titulo = str(item.get("title") or item.get("titulo") or item.get("description") or "Tormentas severas").strip()
    detalle = html.escape(titulo)

    # Resolve radar and satellite images (prioritizing close-up / zoom zonal over far away)
    radar_cerca_url = None
    radar_general_url = None
    topes_url = None
    images = item.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict) and img.get("url"):
                t = str(img.get("title", "")).lower()
                u = str(img.get("url", "")).strip()
                if "topes" in t or "goes" in t:
                    topes_url = u
                elif "general" in t or "gral" in t or "avi_gral" in u.lower():
                    radar_general_url = u
                else:
                    # Specific radar station / close-up zoom (e.g. gmp_ezeiza, aviso.gif)
                    if not radar_cerca_url:
                        radar_cerca_url = u

    # Fallback resolution
    if not radar_cerca_url and not radar_general_url:
        radar_fallback = (
            item.get("gmp")
            or item.get("gmp_general")
            or item.get("url")
            or DEFAULT_RADAR_URL
        )
        radar_cerca_url = str(radar_fallback).strip()

    zones = item.get("zones")
    zones_text = ""
    if zones and isinstance(zones, list):
        filtered_zones = [z.strip() for z in zones if z.strip()][:4]
        if filtered_zones:
            zones_text = "<b>Zonas bajo aviso:</b>\n" + "\n".join(f"• {html.escape(z)}" for z in filtered_zones) + "\n\n"

    vigencia_line = ""
    if hora_fin:
        vigencia_line = f"⏳ <b>Vigencia:</b> Hasta las {html.escape(hora_fin)} hs{duracion_str}\n"
    elif duracion_str:
        vigencia_line = f"⏳ <b>Vigencia:</b>{duracion_str}\n"

    links = []
    if radar_cerca_url:
        links.append(f'📡 <a href="{html.escape(radar_cerca_url)}">Ver radar de cerca (zoom zonal)</a>')
    if radar_general_url:
        links.append(f'🗺️ <a href="{html.escape(radar_general_url)}">Ver mapa general (regional)</a>')
    if topes_url:
        links.append(f'🛰️ <a href="{html.escape(topes_url)}">Ver satélite GOES-16 (Topes nubosos)</a>')
    links.append(f'🌐 <a href="{DEFAULT_ACP_WEB_URL}">Ver Avisos a Muy Corto Plazo en SMN</a>')

    links_text = "\n".join(links)

    message = (
        f"⚠️ <b>ALERTA METEOROLÓGICA (ACP)</b> ⚠️\n\n"
        f"⚡ <b>Fenómeno:</b> {detalle}\n"
        f"{sev_text}"
        f"⏱️ <b>Emisión:</b> {html.escape(hora_emision)} hs\n"
        f"{vigencia_line}\n"
        f"{zones_text}"
        f"📍 <i>Tormenta severa detectada sobre nuestras coordenadas ({html.escape(location_name)}).</i>\n\n"
        f"{links_text}"
    )
    return message


def format_river_alert_message(event: RiverAlertEvent, location_name: str = "El Naudir") -> str:
    """Format a RiverAlertEvent into an HTML Telegram notification message."""
    st = event.trigger_station
    sev = event.severity

    # Dynamic header and origin
    if event.threat_type == "CUENCA_ARRIBA":
        if sev == "EMERGENCIA_EXTREMA":
            header_title = "🚨🌊 <b>CRECIDA EXTRAORDINARIA EN EL PARANÁ (RÍO ARRIBA)</b> 🚨🌊"
            sev_badge = "🔴🔴 <b>SEVERIDAD: ONDA EXTRAORDINARIA (> 3.80 m)</b>"
        elif sev == "CRECIDA_MASIVA":
            header_title = "🚨🌊 <b>CRECIDA MAYOR EN EL PARANÁ (RÍO ARRIBA)</b> 🚨🌊"
            sev_badge = "🟠 <b>SEVERIDAD: CRECIDA DE GRAN MAGNITUD</b>"
        else:
            header_title = "🌊 <b>AVISO PREVENTIVO: CRECIDA EN EL PARANÁ</b> 🌊"
            sev_badge = "🟡 <b>SEVERIDAD: AVISO PREVENTIVO HIDROLÓGICO</b>"
        origin_desc = "⬆️ <b>Origen:</b> Cuenca Arriba (Río Paraná - Onda bajando hacia el Delta)"
    elif event.threat_type == "SUDESTADA":
        if sev == "EMERGENCIA_EXTREMA":
            header_title = "🚨💨 <b>ALERTA POR SUDESTADA HISTÓRICA</b> 🚨💨"
            sev_badge = "🔴🔴 <b>SEVERIDAD: MAREA CRÍTICA EXTRAORDINARIA (> 4.05 m)</b>"
        else:
            header_title = "💨🌊 <b>AVISO POR SUDESTADA (RÍO DE LA PLATA)</b> 🌊"
            sev_badge = "🔴 <b>SEVERIDAD: MAREA CRÍTICA (> 3.80 m)</b>"
        origin_desc = "⬇️ <b>Origen:</b> Cuenca Abajo (Marea del Río de la Plata represando el Delta)"
    else:
        # LOCAL
        if sev == "EMERGENCIA_EXTREMA":
            header_title = "🚨🌊 <b>ALERTA HIDROLÓGICA LOCAL CRÍTICA</b> 🚨🌊"
            sev_badge = "🔴🔴 <b>SEVERIDAD: NIVEL CRÍTICO LOCAL (> 3.80 m)</b>"
        elif sev == "ALERTA_LOCAL":
            header_title = "⚠️🌊 <b>ALERTA HIDROLÓGICA LOCAL</b> ⚠️🌊"
            sev_badge = "🔴 <b>SEVERIDAD: EVACUACIÓN EN ZONA LOCAL (≥ 3.50 m)</b>"
        else:
            header_title = "🌊 <b>AVISO HIDROLÓGICO LOCAL</b> 🌊"
            sev_badge = "🟡 <b>SEVERIDAD: AVISO LOCAL</b>"
        origin_desc = f"📍 <b>Origen:</b> Zona Local ({html.escape(location_name)} / Delta Inferior)"

    summary_block = f"⚠️ <b>{html.escape(event.summary)}</b>\n"

    var_str = ""
    if st.variation is not None:
        sign = "+" if st.variation > 0 else ""
        var_str = f" ({sign}{st.variation:.2f} m)"

    alerta_str = f"{st.alert_level:.2f} m" if st.alert_level is not None else "-"
    evac_str = f"{st.evac_level:.2f} m" if st.evac_level is not None else "-"
    nivel_str = f"{st.level:.2f} m" if st.level is not None else "S/E"

    trigger_block = (
        f"📊 <b>Estación {html.escape(st.name)}</b> ({html.escape(st.river)})\n"
        f"• <b>Altura actual:</b> <code>{nivel_str}</code>{var_str} | {st.status_summary}\n"
        f"• <b>Escala PNA:</b> Evacuación: {evac_str} (Alerta: {alerta_str})\n"
        f"• <b>Registro PNA:</b> {html.escape(st.timestamp or 'Reciente')} hs\n"
    )

    situation_block = ""
    if event.situation:
        situation_block = f"📋 <b>Situación:</b> {html.escape(event.situation)}\n"

    arrival_block = ""
    if event.estimated_arrival:
        arrival_block = f"⏳ <b>Tiempo estimado hacia {html.escape(location_name)}:</b> {html.escape(event.estimated_arrival)}\n"

    secondary_lines = []
    if event.secondary_stations:
        for s in event.secondary_stations[:4]:
            s_lvl = f"{s.level:.2f} m" if s.level is not None else "S/E"
            secondary_lines.append(f"• {html.escape(s.name)}: <code>{s_lvl}</code> ({s.status_summary})")

    context_block = ""
    if secondary_lines:
        context_block = "<b>Estaciones de referencia:</b>\n" + "\n".join(secondary_lines) + "\n\n"

    msg = (
        f"{header_title}\n\n"
        f"{origin_desc}\n"
        f"{sev_badge}\n\n"
        f"{summary_block}\n"
        f"{trigger_block}\n"
        f"{situation_block}"
        f"{arrival_block}\n"
        f"{context_block}"
        f'🌐 <a href="{DEFAULT_PNA_URL}">Ver tabla de alturas oficial en Prefectura Naval</a>'
    )
    return msg


def format_river_dashboard(
    relevant_stations: Dict[str, List[RiverStation]],
    location_name: str = "El Naudir",
) -> str:
    """Format full hydrometric station dashboard for /rio command."""
    lines = [
        f"🚢 <b>MONITOREO DE ALTURA DE RÍOS ({html.escape(location_name)})</b>",
        "📏 <i>Alturas oficiales de escala hidrométrica Prefectura Naval Argentina (PNA)</i>\n",
    ]

    local = relevant_stations.get("local", [])
    if local:
        lines.append("📍 <b>ZONA LOCAL (Impacto Directo en Barrio):</b>")
        for st in local:
            lvl = f"{st.level:.2f} m" if st.level is not None else "S/E"
            var = f" ({'+' if (st.variation or 0) > 0 else ''}{st.variation:.2f} m)" if st.variation is not None else ""
            alert_info = f" [Evac: {st.evac_level:.2f} m]" if st.evac_level else ""
            lines.append(f"• <b>{html.escape(st.name)}:</b> <code>{lvl}</code>{var} - {st.status_summary}{alert_info}")
        lines.append("")

    upstream = relevant_stations.get("upstream", [])
    if upstream:
        lines.append("⬆️ <b>CUENCA ARRIBA (Río Paraná - Alerta Temprana 6-12 hs):</b>")
        for st in upstream:
            lvl = f"{st.level:.2f} m" if st.level is not None else "S/E"
            var = f" ({'+' if (st.variation or 0) > 0 else ''}{st.variation:.2f} m)" if st.variation is not None else ""
            alert_info = f" [Evac: {st.evac_level:.2f} m]" if st.evac_level else ""
            lines.append(f"• <b>{html.escape(st.name)}:</b> <code>{lvl}</code>{var} - {st.status_summary}{alert_info}")
        lines.append("")

    downstream = relevant_stations.get("downstream", [])
    if downstream:
        lines.append("⬇️ <b>CUENCA ABAJO (Sudestada / Río de la Plata):</b>")
        for st in downstream:
            lvl = f"{st.level:.2f} m" if st.level is not None else "S/E"
            var = f" ({'+' if (st.variation or 0) > 0 else ''}{st.variation:.2f} m)" if st.variation is not None else ""
            alert_info = f" [Evac: {st.evac_level:.2f} m]" if st.evac_level else ""
            lines.append(f"• <b>{html.escape(st.name)}:</b> <code>{lvl}</code>{var} - {st.status_summary}{alert_info}")
        lines.append("")

    # Overall situation summary
    any_alert = any(st.is_alerta for group in relevant_stations.values() for st in group)
    any_evac = any(st.is_evacuacion for group in relevant_stations.values() for st in group)
    if any_evac:
        summary_badge = "🔴 <b>Situación:</b> ESTACIONES EN EVACUACIÓN EN LA CUENCA"
    elif any_alert:
        summary_badge = "🟠 <b>Situación:</b> ESTACIONES EN ALERTA EN LA CUENCA"
    else:
        summary_badge = "🟢 <b>Situación:</b> NORMAL (Sin niveles críticos superados)"

    ts = local[0].timestamp if local and local[0].timestamp else "Reciente"
    lines.append(f"{summary_badge}\n<i>Registro PNA: {html.escape(ts)} hs.</i>\n")
    lines.append(f'🌐 <a href="{DEFAULT_PNA_URL}">Tabla completa en Prefectura Naval Argentina</a>')

    return "\n".join(lines)


class TelegramAlertBot:
    """Manages Telegram bot application, commands, and broadcast notifications."""

    def __init__(
        self,
        token: str,
        subscribers_db: SubscribersDB,
        location_name: str = "El Naudir",
        worker_ref: Any = None,
        river_worker_ref: Any = None,
        river_client_ref: Optional[RiverClient] = None,
    ) -> None:
        self.token = token
        self.db = subscribers_db
        self.location_name = location_name
        self.worker_ref = worker_ref
        self.river_worker_ref = river_worker_ref
        self.river_client_ref = river_client_ref
        self.application: Application = ApplicationBuilder().token(self.token).build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register command handlers."""
        self.application.add_handler(CommandHandler("start", self._cmd_start))
        self.application.add_handler(CommandHandler("stop", self._cmd_stop))
        self.application.add_handler(CommandHandler("estado", self._cmd_estado))
        self.application.add_handler(CommandHandler(["descartes", "avisos", "radar"], self._cmd_descartes))
        self.application.add_handler(CommandHandler(["rio", "rios", "alturas"], self._cmd_rio))
        self.application.add_handler(CommandHandler(["testrio", "pruebario"], self._cmd_testrio))
        self.application.add_handler(CommandHandler(["prueba", "test", "testalert"], self._cmd_prueba))
        self.application.add_handler(CommandHandler("help", self._cmd_help))
        self.application.add_error_handler(self._error_handler)

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors during update processing."""
        err_str = str(context.error).lower()
        logger.error("Telegram application error: %s", context.error)
        if "conflict" in err_str and "webhook" in err_str:
            logger.warning("Webhook conflict detected in error handler. Forcing delete_webhook...")
            try:
                await context.bot.delete_webhook(drop_pending_updates=True)
                logger.info("Conflicting webhook successfully deleted.")
            except Exception as err:
                logger.error("Error auto-clearing webhook: %s", err)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command to subscribe."""
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        is_new = await self.db.add_subscriber(chat_id)

        if is_new:
            await update.message.reply_text(
                f"✅ Suscrito a las alertas del SMN para {self.location_name}. "
                "Serás notificado de tormentas a muy corto plazo (ACP)."
            )
        else:
            await update.message.reply_text(
                f"ℹ️ Ya estás suscrito a las alertas para {self.location_name}."
            )

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stop command to unsubscribe."""
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        was_removed = await self.db.remove_subscriber(chat_id)

        if was_removed:
            await update.message.reply_text(
                f"Te has desuscrito de las alertas meteorológicas para {self.location_name}."
            )
        else:
            await update.message.reply_text(
                f"ℹ️ No estabas registrado en la lista de alertas para {self.location_name}."
            )

    async def _cmd_estado(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /estado command to report daemon health and alert state."""
        if not update.effective_chat:
            return

        count = await self.db.get_count()
        sat_status = "🟢 Inactivo (Sin alertas vigentes)"
        if self.worker_ref and self.worker_ref.sat_active:
            alert = self.worker_ref.active_sat_alert or {}
            level = alert.get("level")
            events = alert.get("events") or alert.get("title") or "Fenómenos severos"
            if level == 4 or "naranja" in str(alert.get("level_name", "")).lower():
                sat_status = f"🟠 <b>ALERTA NARANJA ({html.escape(events)})</b>"
            elif level == 3 or "amarillo" in str(alert.get("level_name", "")).lower():
                sat_status = f"🟡 <b>ALERTA AMARILLA ({html.escape(events)})</b>"
            elif level == 5 or "rojo" in str(alert.get("level_name", "")).lower():
                sat_status = f"🔴 <b>ALERTA ROJA ({html.escape(events)})</b>"
            else:
                sat_status = f"⚠️ <b>ACTIVO ({html.escape(events)})</b>"

        discarded_count = 0
        if self.worker_ref:
            discarded_dict = getattr(self.worker_ref, "current_discarded_acp", {})
            discarded_count = len(discarded_dict)

        discarded_line = ""
        if self.worker_ref and self.worker_ref.sat_active:
            discarded_line = f"• <b>Avisos ACP fuera del barrio:</b> {discarded_count} descartados (/descartes)\n"

        river_line = ""
        if self.river_worker_ref and hasattr(self.river_worker_ref, "current_stations"):
            stations = self.river_worker_ref.current_stations
            escobar_st = stations.get("ESCOBAR")
            dique_st = stations.get("DIQUE LUJAN")
            e_lvl = f"{escobar_st.level:.2f} m" if escobar_st and escobar_st.level is not None else "N/D"
            d_lvl = f"{dique_st.level:.2f} m" if dique_st and dique_st.level is not None else "N/D"
            active_river = len(getattr(self.river_worker_ref, "active_alerts", []))
            river_status_str = f"🚨 {active_river} en alerta" if active_river > 0 else "🟢 Normal"
            river_line = (
                f"• <b>Monitoreo de Ríos:</b> Activo ({river_status_str})\n"
                f"  ↳ Escobar: <code>{e_lvl}</code> | Dique Luján: <code>{d_lvl}</code> (/rio)\n"
            )
        elif self.river_client_ref:
            river_line = "• <b>Monitoreo de Ríos:</b> Habilitado (/rio)\n"

        msg = (
            f"ℹ️ <b>Estado del Sistema - {html.escape(self.location_name)}</b>\n\n"
            f"• <b>Alerta Regional SAT:</b> {sat_status}\n"
            f"• <b>Monitoreo Radar (ACP):</b> {'⚡ Activo (cada 3 min)' if (self.worker_ref and self.worker_ref.sat_active) else '💤 Dormido (esperando alerta)'}\n"
            f"{discarded_line}"
            f"{river_line}"
            f"• <b>Usuarios Suscritos:</b> {count}\n"
            f"• <b>Modo:</b> 24/7 Daemon Activo\n\n"
            "Usa /rio para ver el estado de alturas de los ríos.\n"
            "Usa /descartes para ver tormentas activas fuera de cobertura.\n"
            "Usa /stop si deseas desuscribirte."
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def _cmd_descartes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List currently ongoing ACP storm warnings discarded for El Naudir."""
        if not update.effective_chat:
            return

        discarded = {}
        if self.worker_ref:
            discarded = getattr(self.worker_ref, "current_discarded_acp", {})

        if not discarded:
            msg = (
                f"🛰️ <b>Avisos ACP Descartados - {html.escape(self.location_name)}</b>\n\n"
                "ℹ️ No hay avisos de tormenta descartados en este momento.\n"
                "(No hay celdas de tormenta activas registradas fuera del barrio en el radar del SMN).\n\n"
                f"📍 <i>Monitoreo 24/7 activo sobre {html.escape(self.location_name)}.</i>"
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            return

        lines = [
            f"🛰️ <b>Avisos ACP Vigentes Descartados (Fuera de {html.escape(self.location_name)})</b>\n",
            f"Tormentas activas en la región fuera de cobertura: <b>{len(discarded)}</b>\n",
        ]

        for idx, (event_id, data) in enumerate(discarded.items(), 1):
            title = html.escape(str(data.get("title", "Tormenta")).strip())
            reason = html.escape(str(data.get("reason", "Fuera de cobertura")))
            zones = str(data.get("zones", ""))
            if len(zones) > 90:
                zones = zones[:87] + "..."
            zones_esc = html.escape(zones)

            vigencia_info = ""
            end_date = data.get("end_date")
            if end_date and "T" in str(end_date):
                try:
                    hora_fin = str(end_date).split("T")[1][:5]
                    vigencia_info = f"• <b>Vigencia:</b> Hasta las {html.escape(hora_fin)} hs\n"
                except Exception:
                    pass

            lines.append(
                f"<b>{idx}. ID {html.escape(str(event_id))}:</b> {title}\n"
                f"• <b>Zonas:</b> {zones_esc}\n"
                f"{vigencia_info}"
                f"• <b>Motivo de descarte:</b> ❌ {reason}\n"
            )

        lines.append(
            f"📍 <i>Coordenadas protegidas: {html.escape(self.location_name)} (-34.3100, -58.7391).</i>\n"
            "💡 <i>Si cualquier celda de tormenta se desplaza hacia El Naudir, el bot te alertará al instante.</i>\n\n"
            f'🌐 <a href="{DEFAULT_ACP_WEB_URL}">Ver mapa de Avisos a Muy Corto Plazo en SMN</a>'
        )

        msg = "\n".join(lines)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def _cmd_prueba(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a simulated ACP storm alert ONLY to the user invoking the command."""
        if not update.effective_chat:
            return

        from datetime import datetime, timedelta
        now = datetime.now()
        start_iso = now.isoformat()
        end_iso = (now + timedelta(hours=2)).isoformat()

        # Build realistic sample data with duration, severity, radar and satellite
        sample_item = {
            "title": "TORMENTAS FUERTES CON LLUVIAS INTENSAS, RAFAGAS Y OCASIONAL CAIDA DE GRANIZO",
            "date": start_iso,
            "end_date": end_iso,
            "severity": "N",
            "zones": [
                "BUENOS AIRES: Escobar - Campana - Pilar - Tigre.",
                "DELTA DE BUENOS AIRES: Islas del Delta."
            ],
            "images": [
                {"title": "gmp_ezeiza", "url": "https://estaticos.smn.gob.ar/pronosticos/avisomet/radar/ezeiza.gif"},
                {"title": "gmp_general", "url": "https://estaticos.smn.gob.ar/pronosticos/avisomet/radar/avi_gral.gif"},
                {"title": "topes_nubosos", "url": "https://estaticos.smn.gob.ar/vmsr/goes16/TOP_C13_NOR.jpg"}
            ]
        }

        # If there are real active ACP warnings in the country, adopt real title, severity, and images
        if self.worker_ref and hasattr(self.worker_ref, "current_discarded_acp"):
            discarded = self.worker_ref.current_discarded_acp
            if discarded:
                real_storm = next(iter(discarded.values()))
                if real_storm.get("title"):
                    sample_item["title"] = real_storm["title"]
                if real_storm.get("severity"):
                    sample_item["severity"] = real_storm["severity"]
                if real_storm.get("images"):
                    sample_item["images"] = real_storm["images"]

        alert_msg = format_acp_message(sample_item, location_name=self.location_name)
        banner = (
            "🧪 <b>[MENSAJE DE PRUEBA]</b>\n"
            "<i>Esta alerta es de simulación y fue enviada ÚNICAMENTE a tu chat privado (no a los demás suscriptores).</i>\n\n"
        )
        await update.message.reply_text(banner + alert_msg, parse_mode=ParseMode.HTML)

    async def _cmd_rio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /rio command to show live river heights."""
        if not update.effective_chat:
            return

        client = self.river_client_ref
        if not client and self.river_worker_ref and hasattr(self.river_worker_ref, "client"):
            client = self.river_worker_ref.client

        if not client:
            await update.message.reply_text(
                "⚠️ El monitoreo de ríos no está configurado actualmente en el daemon."
            )
            return

        try:
            # Serve instantly from RAM (0 ms delay, 0 Scrape.do credits spent)
            rel = None
            if self.river_worker_ref and getattr(self.river_worker_ref, "current_stations", None):
                stations = self.river_worker_ref.current_stations
                if stations:
                    rel = RiverClient.filter_relevant_stations(stations)

            if not rel:
                client = self.river_client_ref
                if not client and self.river_worker_ref and hasattr(self.river_worker_ref, "client"):
                    client = self.river_worker_ref.client

                if not client:
                    await update.message.reply_text(
                        "⚠️ El monitoreo de ríos no está configurado actualmente en el daemon."
                    )
                    return

                rel = await client.get_relevant_stations_async()

            text = format_river_dashboard(rel, location_name=self.location_name)
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        except Exception as err:
            logger.error("Error fetching river dashboard: %s", err)
            await update.message.reply_text(
                f"❌ Error al consultar las alturas de Prefectura Naval: {err}"
            )

    async def _cmd_testrio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a simulated river alert ONLY to the user invoking the command."""
        if not update.effective_chat:
            return

        args_str = " ".join(context.args or []).lower()
        sim_is_extreme = any(k in args_str for k in ["extrema", "extremo", "casa", "casas", "vivienda", "viviendas", "tier2", "2", "grave"])
        sim_is_sudestada = any(k in args_str for k in ["sude", "marea", "tigre"])
        sim_is_cuenca = any(k in args_str for k in ["cuenca", "arriba", "zarate", "parana"])
        sim_is_masiva = any(k in args_str for k in ["masiva", "mayor"])

        if sim_is_sudestada:
            # Tigre: Dec 2023 was 3.70m (no flood in El Naudir). Alert is 3.80m, Extreme is 4.05m
            level = 4.15 if sim_is_extreme else 3.85
            severity = "EMERGENCIA_EXTREMA" if sim_is_extreme else "ALERTA_LOCAL"
            trigger_st = RiverStation(
                name="TIGRE",
                river="DELTA PARANA",
                level=level,
                variation=0.55,
                period_hours=12,
                timestamp="Hoy - Reciente",
                state="EVACUACION CRECE",
                prev_level=3.30,
                prev_timestamp="Hoy - Anterior",
                alert_level=3.30,
                evac_level=3.80,
                station_id="430",
            )
            secondary = [
                RiverStation(name="SAN FERNANDO", river="DELTA PARANA", level=3.65, variation=0.45, period_hours=12, timestamp="", state="EVACUACION CRECE", prev_level=3.20, prev_timestamp="", alert_level=3.00, evac_level=3.50),
                RiverStation(name="DIQUE LUJAN", river="DELTA PARANA", level=2.95, variation=0.35, period_hours=12, timestamp="", state="EVACUACION CRECE", prev_level=2.60, prev_timestamp="", alert_level=2.30, evac_level=2.80),
                RiverStation(name="ESCOBAR", river="DELTA PARANA", level=3.30, variation=0.40, period_hours=12, timestamp="", state="CRECE", prev_level=2.90, prev_timestamp="", alert_level=3.00, evac_level=3.50),
            ]
            sim_event = RiverAlertEvent(
                event_id=f"TEST:SUDESTADA:{severity}",
                threat_type="SUDESTADA",
                severity=severity,
                trigger_station=trigger_st,
                summary=f"Sudestada excepcional: Marea en Tigre ({level:.2f} m)." if sim_is_extreme else f"Marea crítica de Sudestada en Tigre ({level:.2f} m).",
                situation="Marea extrema del Río de la Plata superando marcas históricas (> 4.05 m)." if sim_is_extreme else "Marea del Río de la Plata superando la cota de diciembre 2023 (3.70 m).",
                estimated_arrival="Ingreso activo de marea desde el estuario hacia el Delta",
                impact_projection="Represamiento masivo de desagües con riesgo de anegamiento en el barrio." if sim_is_extreme else "Posible anegamiento en calzadas bajas y accesos por taponamiento de desagües.",
                secondary_stations=secondary,
            )
            tier_title = "SUDESTADA EXTREMA (> 4.05 m)" if sim_is_extreme else "SUDESTADA CRÍTICA (> 3.80 m)"
        elif sim_is_cuenca:
            if sim_is_extreme:
                level = 3.90
                severity = "EMERGENCIA_EXTREMA"
                summary = f"Crecida colosal extraordinaria en ZARATE: {level:.2f} m."
                situation = "Caudal masivo bajando por el Paraná de las Palmas (supera los 3.80 m)."
                impact = "Impacto severo proyectado sobre el Delta inferior en 6-12 hs aun con marea baja."
                tier_title = "CUENCA ARRIBA - CRECIDA EXTRAORDINARIA (> 3.80 m)"
            elif sim_is_masiva:
                level = 3.25
                severity = "CRECIDA_MASIVA"
                summary = f"Crecida de gran magnitud en ZARATE: {level:.2f} m."
                situation = "Onda mayor avanzando por el Paraná (+85 cm sobre evacuación)."
                impact = "Muy probable repercusión y anegamiento de calzadas bajas en El Naudir en ~6 a 12 hs."
                tier_title = "CUENCA ARRIBA - CRECIDA MAYOR (3.20 m)"
            else:
                level = 2.85
                severity = "AVISO_PREVENTIVO"
                summary = f"Aviso preventivo: Crecida en ZARATE ({level:.2f} m)."
                situation = "Onda de crecida importante (+45 cm sobre evacuación) desplazándose hacia el sur."
                impact = "Posible incremento en zona local en ~6 a 12 hs. Seguimiento preventivo."
                tier_title = "CUENCA ARRIBA - AVISO PREVENTIVO (2.80 m)"

            trigger_st = RiverStation(
                name="ZARATE",
                river="DELTA PARANA",
                level=level,
                variation=0.55 if sim_is_extreme else 0.25,
                period_hours=12,
                timestamp="Hoy - Reciente",
                state="EVACUACION CRECE",
                prev_level=3.35 if sim_is_extreme else 2.60,
                prev_timestamp="Hoy - Anterior",
                alert_level=2.20,
                evac_level=2.40,
                station_id="340",
            )
            secondary = [
                RiverStation(name="CAMPANA", river="DELTA PARANA", level=3.70 if sim_is_extreme else 2.90, variation=0.45, period_hours=12, timestamp="", state="EVACUACION CRECE", prev_level=3.20, prev_timestamp="", alert_level=2.20, evac_level=2.45),
                RiverStation(name="ESCOBAR", river="DELTA PARANA", level=3.10 if sim_is_extreme else 2.10, variation=0.35, period_hours=12, timestamp="", state="CRECE", prev_level=2.75, prev_timestamp="", alert_level=3.00, evac_level=3.50),
            ]
            sim_event = RiverAlertEvent(
                event_id=f"TEST:CUENCA_ARRIBA:{severity}",
                threat_type="CUENCA_ARRIBA",
                severity=severity,
                trigger_station=trigger_st,
                summary=summary,
                situation=situation,
                estimated_arrival="Aproximadamente 6 a 12 horas hacia Escobar",
                impact_projection=impact,
                secondary_stations=secondary,
            )
        else:
            # Default / Local Escobar: alert 3.80m (Drain saturation), extreme 4.10m (Low street level)
            level = 4.15 if sim_is_extreme else 3.85
            severity = "EMERGENCIA_EXTREMA" if sim_is_extreme else "ALERTA_LOCAL"
            trigger_st = RiverStation(
                name="ESCOBAR",
                river="DELTA PARANA",
                level=level,
                variation=0.35,
                period_hours=12,
                timestamp="Hoy - Reciente",
                state="EVACUACION CRECE",
                prev_level=3.50,
                prev_timestamp="Hoy - Anterior",
                alert_level=3.00,
                evac_level=3.50,
                station_id="360",
            )
            secondary = [
                RiverStation(name="DIQUE LUJAN", river="DELTA PARANA", level=3.05 if sim_is_extreme else 2.85, variation=0.25, period_hours=12, timestamp="", state="EVACUACION CRECE", prev_level=2.60, prev_timestamp="", alert_level=2.30, evac_level=2.80),
                RiverStation(name="TIGRE", river="DELTA PARANA", level=3.85 if sim_is_extreme else 3.60, variation=0.30, period_hours=12, timestamp="", state="ALERTA CRECE", prev_level=3.30, prev_timestamp="", alert_level=3.30, evac_level=3.80),
            ]
            sim_event = RiverAlertEvent(
                event_id=f"TEST:LOCAL:ESCOBAR:{severity}",
                threat_type="LOCAL",
                severity=severity,
                trigger_station=trigger_st,
                summary=f"Crecida local crítica en Escobar: {level:.2f} m (Escala PNA)." if sim_is_extreme else f"Nivel de advertencia local en Escobar: {level:.2f} m (Escala PNA).",
                situation="El río superó los 4.10 m (escala PNA), alcanzando cotas de calzadas bajas en el barrio." if sim_is_extreme else "El río superó el registro de 2023 (3.80 m PNA), con posible saturación de desagües pluviales.",
                estimated_arrival="Inmediato / En curso en la zona",
                impact_projection="Riesgo de agua sobre calzadas bajas y accesos." if sim_is_extreme else "Posible reflujo en desagües pluviales bajos.",
                secondary_stations=secondary,
            )
            tier_title = "ZONA LOCAL - EMERGENCIA EXTREMA (4.15 m)" if sim_is_extreme else "ZONA LOCAL - ADVERTENCIA (3.85 m)"

        alert_msg = format_river_alert_message(sim_event, location_name=self.location_name)
        banner = (
            f"🧪 <b>[MENSAJE DE PRUEBA - {tier_title}]</b>\n"
            "<i>Esta simulación fue enviada ÚNICAMENTE a tu chat privado.</i>\n"
            "<i>Opciones: <code>/testrio</code> (Local Escobar 3.85m) | <code>/testrio extrema</code> (Escobar 4.15m) | <code>/testrio cuenca</code> (Zárate 2.80m) | <code>/testrio sudestada</code> (Tigre 3.80m)</i>\n\n"
        )
        await update.message.reply_text(banner + alert_msg, parse_mode=ParseMode.HTML)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        if not update.effective_chat:
            return
        msg = (
            f"🤖 <b>Bot de Alertas SMN e Hidrológicas - {html.escape(self.location_name)}</b>\n\n"
            "Comandos disponibles:\n"
            "/start - Suscribirse a las alertas inmediatas de tormentas y crecidas\n"
            "/stop - Cancelar tu suscripción\n"
            "/estado - Ver el estado del sistema, radar y alturas de ríos\n"
            "/rio - Ver alturas y tendencias de los ríos en vivo (Prefectura Naval)\n"
            "/descartes - Ver avisos ACP activos fuera del barrio\n"
            "/prueba - Recibir una alerta meteorológica de prueba (solo para vos)\n"
            "/testrio - Recibir una alerta hídrica de prueba (solo para vos)\n"
            "/help - Mostrar esta ayuda"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def broadcast_alert(self, acp_item: Dict[str, Any]) -> int:
        """Broadcast an ACP alert to all registered subscribers.

        Returns:
            Number of successfully delivered messages.
        """
        subscribers = await self.db.get_all_subscribers()
        if not subscribers:
            logger.info("No subscribers registered to receive broadcast.")
            return 0

        text = format_acp_message(acp_item, location_name=self.location_name)
        bot: Bot = self.application.bot

        logger.info("Broadcasting ACP alert to %d subscribers...", len(subscribers))
        delivered_count = 0

        for chat_id in subscribers:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                delivered_count += 1
                # Small pause to avoid Telegram API rate limit spikes
                await asyncio.sleep(0.05)
            except Forbidden:
                logger.warning("User %s blocked the bot. Removing from subscribers...", chat_id)
                await self.db.remove_subscriber(chat_id)
            except BadRequest as err:
                if "chat not found" in str(err).lower() or "user is deactivated" in str(err).lower():
                    logger.warning("Chat %s invalid (%s). Removing from subscribers...", chat_id, err)
                    await self.db.remove_subscriber(chat_id)
                else:
                    logger.error("BadRequest sending to %s: %s", chat_id, err)
            except TelegramError as err:
                logger.error("Telegram API error sending to %s: %s", chat_id, err)
            except Exception as err:
                logger.error("Unexpected error delivering to %s: %s", chat_id, err, exc_info=True)

        logger.info("Broadcast finished. Delivered to %d/%d subscribers.", delivered_count, len(subscribers))
        return delivered_count

    async def broadcast_river_alert(self, event: RiverAlertEvent) -> int:
        """Broadcast a river flood alert to all registered subscribers.

        Returns:
            Number of successfully delivered messages.
        """
        subscribers = await self.db.get_all_subscribers()
        if not subscribers:
            logger.info("No subscribers registered to receive river broadcast.")
            return 0

        text = format_river_alert_message(event, location_name=self.location_name)
        bot: Bot = self.application.bot

        logger.info("Broadcasting river alert %s to %d subscribers...", event.event_id, len(subscribers))
        delivered_count = 0

        for chat_id in subscribers:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                delivered_count += 1
                await asyncio.sleep(0.05)
            except Forbidden:
                logger.warning("User %s blocked the bot. Removing from subscribers...", chat_id)
                await self.db.remove_subscriber(chat_id)
            except BadRequest as err:
                if "chat not found" in str(err).lower() or "user is deactivated" in str(err).lower():
                    logger.warning("Chat %s invalid (%s). Removing from subscribers...", chat_id, err)
                    await self.db.remove_subscriber(chat_id)
                else:
                    logger.error("BadRequest sending to %s: %s", chat_id, err)
            except TelegramError as err:
                logger.error("Telegram API error sending to %s: %s", chat_id, err)
            except Exception as err:
                logger.error("Unexpected error delivering to %s: %s", chat_id, err, exc_info=True)

        logger.info("River broadcast finished. Delivered to %d/%d subscribers.", delivered_count, len(subscribers))
        return delivered_count
