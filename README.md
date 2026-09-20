# NaudirEarlyAlert 🌩️🤖

> Daemon autónomo 24/7 y Bot de Telegram para detección temprana y alerta inmediata de tormentas severas y avisos a muy corto plazo (ACP) del Servicio Meteorológico Nacional (SMN) argentino para el **Barrio El Naudir – Aguas Privadas** (Belén de Escobar, Buenos Aires).

---

## 📍 Coordenadas y Zona de Monitoreo

- **Ubicación:** Barrio El Naudir – Aguas Privadas (Escobar, Bs. As.)
- **Latitud:** `-34.3100`
- **Longitud:** `-58.7391`
- **Zona Administrativa SAT:** `"Escobar"`

---

## ⚙️ Arquitectura del Sistema (Máquina de Estados de 2 Niveles)

El daemon optimiza el consumo de ancho de banda y procesamiento mediante un flujo inteligente de dos niveles:

1. **Nivel 1 – Alertas Regionales (SAT):**
   - Consulta el endpoint público `https://ws.smn.gob.ar/alerts/type/AL` al iniciar y cada **12 horas**.
   - Analiza si el partido de **Escobar** se encuentra bajo alerta meteorológica (amarillo, naranja o rojo).
   - Si no hay alertas activas, el monitoreo por radar se mantiene **dormido** para evitar tráfico innecesario.
   - Si se detecta alerta activa en la zona, el estado pasa a **ACTIVO**.

2. **Nivel 2 – Avisos a Muy Corto Plazo por Radar (ACP):**
   - Cuando el estado SAT está activo, consulta `https://ws.smn.gob.ar/alerts/type/ACP` cada **3 minutos**.
   - **Geometría Espacial (Shapely):** Parsea el polígono de coordenadas de cada celda de tormenta y evalúa si las coordenadas de El Naudir se encuentran **dentro** (`Polygon.contains(Point)`).
   - **Deduplicación:** Registra los IDs de celdas ya notificadas en memoria para no repetir alertas del mismo evento.
   - **Difusión Inmediata:** Si una tormenta intersecta El Naudir, emite la alerta con formato HTML a todos los usuarios suscritos vía Telegram.

---

## 📱 Comandos del Bot en Telegram

| Comando | Descripción |
| :--- | :--- |
| `/start` | Suscribe al usuario para recibir alertas de tormentas en El Naudir. |
| `/stop` | Cancela la suscripción y elimina al usuario de la base de datos. |
| `/estado` | Muestra el estado del daemon, estado de alerta SAT y total de suscriptores. |
| `/help` | Muestra la ayuda y lista de comandos disponibles. |

### Formato de Alerta Difundida
```html
⚠️ ALERTA METEOROLÓGICA (ACP) ⚠️

Emisión: 14:15
Detalle: Tormentas fuertes con ráfagas y ocasional caída de granizo
Duración: Válido por 3 horas desde su emisión

📍 Tormenta severa detectada sobre nuestras coordenadas (El Naudir).

🔗 Ver radar oficial
```

---

## 🚀 Guía de Despliegue en Servidor Linux (Ubuntu 24.04 LTS / GCP e2-micro)

### 1. Preparar el Servidor y Dependencias del Sistema

Conéctate por SSH a tu máquina virtual e instala Python 3 y `venv`:

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

### 2. Clonar el Repositorio

```bash
sudo git clone https://github.com/tu-usuario/NaudirEarlyAlert.git /opt/NaudirEarlyAlert
sudo chown -R ubuntu:ubuntu /opt/NaudirEarlyAlert
cd /opt/NaudirEarlyAlert
```

*(Si utilizas otro usuario distinto a `ubuntu`, reemplaza `ubuntu:ubuntu` por tu usuario actual).*

### 3. Crear Entorno Virtual e Instalar Dependencias

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configurar Variables de Entorno

Copia la plantilla y configura tu token de Telegram:

```bash
cp .env.example .env
nano .env
```

Contenido del archivo `.env`:
```ini
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
ALERT_LAT=-34.3100
ALERT_LON=-58.7391
ALERT_ZONE=Escobar
DB_PATH=subscribers.db
SAT_POLL_INTERVAL_HOURS=12
ACP_POLL_INTERVAL_MINUTES=3
FORCE_ACP_POLL=false
```

### 5. Probar el Funcionamiento

Ejecuta el script de prueba para validar que la geometría, la base de datos y la plantilla funcionen:

```bash
# Prueba simulada sin enviar mensajes a Telegram:
venv/bin/python test_alert.py --dry-run

# Prueba enviando un mensaje real a tu chat de Telegram:
venv/bin/python test_alert.py --chat-id TU_CHAT_ID_DE_TELEGRAM
```

---

## 🛡️ Configuración como Servicio 24/7 con `systemd`

Para garantizar que el bot se ejecute continuamente de fondo y se reinicie automáticamente ante caídas o reinicios del servidor:

### 1. Copiar el Archivo de Servicio

```bash
sudo cp /opt/NaudirEarlyAlert/smn-bot.service /etc/systemd/system/
```

Verifica que el usuario y las rutas en `/etc/systemd/system/smn-bot.service` coincidan con tu instalación:
```ini
User=ubuntu
WorkingDirectory=/opt/NaudirEarlyAlert
ExecStart=/opt/NaudirEarlyAlert/venv/bin/python bot.py
```

### 2. Habilitar e Iniciar el Servicio

```bash
sudo systemctl daemon-reload
sudo systemctl enable smn-bot
sudo systemctl start smn-bot
```

### 3. Comandos Útiles de Administración

- **Ver estado del servicio:**
  ```bash
  sudo systemctl status smn-bot
  ```

- **Ver logs en tiempo real:**
  ```bash
  journalctl -u smn-bot -f
  ```

- **Reiniciar el bot:**
  ```bash
  sudo systemctl restart smn-bot
  ```

- **Detener el bot:**
  ```bash
  sudo systemctl stop smn-bot
  ```

---

## 🧪 Pruebas Unitarias y Automatizadas

El proyecto incluye tests integrados para validar:
- Contención espacial de polígonos mediante `shapely`.
- Operaciones seguras y concurrentes en base de datos SQLite `subscribers.db`.
- Aislamiento de excepciones y desuscripción automática de usuarios bloqueados.

Ejecutar tests:
```bash
python -m unittest discover tests/
```

---

## 📄 Licencia

Desarrollado para la comunidad de **El Naudir – Aguas Privadas**.
Distribuido bajo la licencia MIT.
