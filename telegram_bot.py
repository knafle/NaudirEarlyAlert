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


def format_acp_message(item: Dict[str, Any], location_name: str = "El Naudir") -> str:
    """Format an ACP radar alert dictionary into an HTML Telegram message."""
    raw_date = str(item.get("date") or "")
    hora = ""
    if "T" in raw_date:
        try:
            hora = raw_date.split("T")[1][:5]
        except Exception:
            pass
    if not hora:
        hora = str(item.get("hour") or item.get("hora") or "Reciente")

    titulo = str(item.get("title") or item.get("titulo") or item.get("description") or "Tormentas severas").strip()
    detalle = html.escape(titulo)

    # Resolve radar animated GIF or link
    radar_url = None
    images = item.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict) and img.get("url"):
                title_img = str(img.get("title", "")).lower()
                if "ezeiza" in title_img or "general" in title_img:
                    radar_url = img["url"]
                    break
        if not radar_url:
            for img in images:
                if isinstance(img, dict) and img.get("url"):
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
        filtered_zones = [z.strip() for z in zones if z.strip()][:3]
        if filtered_zones:
            zones_text = "<b>Zonas bajo aviso:</b>\n" + "\n".join(f"• {html.escape(z)}" for z in filtered_zones) + "\n\n"

    message = (
        f"⚠️ <b>ALERTA METEOROLÓGICA (ACP)</b> ⚠️\n\n"
        f"<b>Emisión:</b> {html.escape(hora)}\n"
        f"<b>Detalle:</b> {detalle}\n\n"
        f"{zones_text}"
        f"📍 <i>Tormenta severa detectada sobre nuestras coordenadas ({html.escape(location_name)}).</i>\n\n"
        f'🔗 <a href="{radar_url}">Ver radar oficial</a>'
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
        self.application.add_handler(CommandHandler("help", self._cmd_help))

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

            lines.append(
                f"<b>{idx}. ID {html.escape(str(event_id))}:</b> {title}\n"
                f"• <b>Zonas:</b> {zones_esc}\n"
                f"• <b>Motivo de descarte:</b> ❌ {reason}\n"
            )

        lines.append(
            f"📍 <i>Coordenadas protegidas: {html.escape(self.location_name)} (-34.3100, -58.7391).</i>\n"
            "💡 <i>Si cualquier celda de tormenta se desplaza hacia El Naudir, el bot te alertará al instante.</i>"
        )

        msg = "\n".join(lines)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

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
