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
    hora = html.escape(str(item.get("hour") or item.get("hora") or "Desconocida"))
    titulo = str(item.get("title") or item.get("titulo") or item.get("description") or "Tormentas severas")
    detalle = html.escape(titulo)

    # Resolve radar animated GIF or link
    radar_url = (
        item.get("gmp_general")
        or item.get("gmp")
        or item.get("url")
        or DEFAULT_RADAR_URL
    )
    radar_url = html.escape(str(radar_url).strip())

    message = (
        f"⚠️ <b>ALERTA METEOROLÓGICA (ACP)</b> ⚠️\n\n"
        f"<b>Emisión:</b> {hora}\n"
        f"<b>Detalle:</b> {detalle}\n"
        f"<b>Duración:</b> Válido por 3 horas desde su emisión\n\n"
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
        sat_status = "Desconocido"
        if self.worker_ref:
            sat_status = "🔴 ACTIVO" if self.worker_ref.sat_active else "🟢 Inactivo (Normal)"

        msg = (
            f"ℹ️ <b>Estado del Sistema - {html.escape(self.location_name)}</b>\n\n"
            f"• <b>Alerta Regional SAT:</b> {sat_status}\n"
            f"• <b>Usuarios Suscritos:</b> {count}\n"
            f"• <b>Modo:</b> 24/7 Daemon Activo\n\n"
            "Usa /stop si deseas desuscribirte."
        )
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
