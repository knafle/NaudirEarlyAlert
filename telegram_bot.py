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

from subscribers import SubscribersDB

logger = logging.getLogger(__name__)

DEFAULT_RADAR_URL = "https://www.smn.gob.ar/radar"
DEFAULT_ACP_WEB_URL = "https://www.smn.gob.ar/avisos_a_muy_corto_plazo"


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

    # Resolve radar and satellite images
    radar_url = None
    topes_url = None
    images = item.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict) and img.get("url"):
                title_img = str(img.get("title", "")).lower()
                if "topes" in title_img or "goes16" in title_img:
                    topes_url = img["url"]
                elif "ezeiza" in title_img or "general" in title_img:
                    if not radar_url:
                        radar_url = img["url"]
        if not radar_url:
            for img in images:
                if isinstance(img, dict) and img.get("url"):
                    title_img = str(img.get("title", "")).lower()
                    if "topes" not in title_img and "goes16" not in title_img:
                        radar_url = img["url"]
                        break

    if not radar_url:
        radar_url = (
            item.get("gmp_general")
            or item.get("gmp")
            or item.get("url")
            or DEFAULT_RADAR_URL
        )
    radar_url = html.escape(str(radar_url).strip())

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

    links = [
        f'📡 <a href="{radar_url}">Ver radar en vivo (animación oficial)</a>'
    ]
    if topes_url:
        links.append(f'🛰️ <a href="{html.escape(str(topes_url).strip())}">Ver satélite GOES-16 (Topes nubosos)</a>')
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


class TelegramAlertBot:
    """Manages Telegram bot application, commands, and broadcast notifications."""

    def __init__(
        self,
        token: str,
        subscribers_db: SubscribersDB,
        location_name: str = "El Naudir",
        worker_ref: Any = None,
    ) -> None:
        self.token = token
        self.db = subscribers_db
        self.location_name = location_name
        self.worker_ref = worker_ref
        self.application: Application = ApplicationBuilder().token(self.token).build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register command handlers."""
        self.application.add_handler(CommandHandler("start", self._cmd_start))
        self.application.add_handler(CommandHandler("stop", self._cmd_stop))
        self.application.add_handler(CommandHandler("estado", self._cmd_estado))
        self.application.add_handler(CommandHandler(["descartes", "avisos", "radar"], self._cmd_descartes))
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

        msg = (
            f"ℹ️ <b>Estado del Sistema - {html.escape(self.location_name)}</b>\n\n"
            f"• <b>Alerta Regional SAT:</b> {sat_status}\n"
            f"• <b>Monitoreo Radar (ACP):</b> {'⚡ Activo (cada 3 min)' if (self.worker_ref and self.worker_ref.sat_active) else '💤 Dormido (esperando alerta)'}\n"
            f"{discarded_line}"
            f"• <b>Usuarios Suscritos:</b> {count}\n"
            f"• <b>Modo:</b> 24/7 Daemon Activo\n\n"
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
                {"title": "topes_nubosos", "url": "https://estaticos.smn.gob.ar/vmsr/goes16/TOP_C13_NOR.jpg"}
            ]
        }

        # If there are real active ACP warnings in the country, adopt real title and severity
        if self.worker_ref and hasattr(self.worker_ref, "current_discarded_acp"):
            discarded = self.worker_ref.current_discarded_acp
            if discarded:
                real_storm = next(iter(discarded.values()))
                if real_storm.get("title"):
                    sample_item["title"] = real_storm["title"]
                if real_storm.get("severity"):
                    sample_item["severity"] = real_storm["severity"]

        alert_msg = format_acp_message(sample_item, location_name=self.location_name)
        banner = (
            "🧪 <b>[MENSAJE DE PRUEBA]</b>\n"
            "<i>Esta alerta es de simulación y fue enviada ÚNICAMENTE a tu chat privado (no a los demás suscriptores).</i>\n\n"
        )
        await update.message.reply_text(banner + alert_msg, parse_mode=ParseMode.HTML)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        if not update.effective_chat:
            return
        msg = (
            f"🤖 <b>Bot de Alertas SMN - {html.escape(self.location_name)}</b>\n\n"
            "Comandos disponibles:\n"
            "/start - Suscribirse a las alertas inmediatas de tormentas\n"
            "/stop - Cancelar tu suscripción\n"
            "/estado - Ver el estado actual del monitoreo y alertas SAT\n"
            "/descartes - Ver avisos ACP activos actualmente descartados (fuera del barrio)\n"
            "/prueba - Recibir una alerta de prueba (se envía SOLO a vos)\n"
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
