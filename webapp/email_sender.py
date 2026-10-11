"""
Envio de correo real para los codigos de activacion de 6 digitos --
antes el admin tenia que pasarselos a mano a cada familiar (2026-09-13,
a pedido explicito del usuario: "conecta el envio de correo real").

Usa Gmail via SMTP con una "contraseña de aplicacion" (no la contraseña
normal de la cuenta -- un codigo de 16 caracteres generado en
myaccount.google.com/apppasswords, revocable en cualquier momento sin
afectar el login normal de Gmail). Credenciales SIEMPRE por variable de
entorno, nunca en el codigo:

  GMAIL_ADDRESS       -- la cuenta de Gmail que manda el correo
  GMAIL_APP_PASSWORD  -- la contraseña de aplicacion de 16 caracteres

Si esas variables no estan seteadas (ej. en desarrollo local sin
configurar), send_activation_email() devuelve False sin lanzar
excepcion -- el llamador (app.py) cae al comportamiento anterior:
mostrarle el codigo al admin para que lo pase a mano.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
ALERT_ALLOWED_RECIPIENTS = {"miguelangelperezizquierdo160@gmail.com"}


def _credentials() -> tuple[str, str] | None:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not address or not app_password:
        return None
    return address, app_password


def send_activation_email(to_email: str, username: str, code: str, brand_name: str = "Metabot") -> bool:
    """Manda el codigo de activacion por correo. Devuelve True si se
    mando (no garantiza que llegue a destino, solo que el servidor SMTP
    lo acepto), False si no se pudo mandar (credenciales no
    configuradas, o error de red/autenticacion -- se loguea el motivo
    a stderr para diagnosticar sin exponer la contraseña).

    2026-10-11, a pedido explicito ("que los usuarios de metabot reciban
    de metabot"): brand_name controla el texto Y el nombre del remitente
    -- por defecto "Metabot" porque admin_approve() SIEMPRE es un alta
    publica de Metabot (Bot Familiar no tiene autorregistro); solo
    admin_reconnect() pasa "Bot Familiar" explicitamente cuando la
    cuenta que se reconecta tiene familiar_access aprobado. Antes esta
    funcion mandaba el texto hardcodeado "Bot Familiar" sin importar
    para quien fuera -- bug real, quedaba de antes de que existiera la
    marca dual Metabot/Bot Familiar."""
    creds = _credentials()
    if creds is None:
        print("[email_sender] GMAIL_ADDRESS/GMAIL_APP_PASSWORD no configurados -- no se manda correo.")
        return False
    address, app_password = creds

    body = (
        f"Hola {username},\n\n"
        f"Tu cuenta de {brand_name} fue aprobada. Tu codigo de activacion es:\n\n"
        f"    {code}\n\n"
        f"Entra a la app, pone tu usuario y este codigo para activar tu cuenta. "
        f"El codigo vence en 48 horas.\n\n"
        f"-- {brand_name}"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Tu codigo de activacion -- {brand_name}"
    msg["From"] = formataddr((brand_name, address))
    msg["To"] = to_email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(address, app_password)
            server.sendmail(address, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email_sender] fallo el envio a {to_email}: {e}")
        return False


def send_alert_email(to_email: str, subject: str, body: str, allow_user: bool = False, from_name: str | None = None) -> bool:
    """Correo generico (no ligado a activacion de cuenta) -- 2026-09-18,
    a pedido del usuario ("las alertas no pueden depender de que yo este
    conectado"): reusa exactamente el mismo mecanismo SMTP ya probado en
    produccion (send_activation_email), para que critical_alert_watch.py
    pueda avisar por correo real sin necesitar una sesion de Claude Code
    activa mirando el log.

    2026-09-19, a pedido del usuario ("que los mensajes solo lleguen a los
    usuarios a la seccion de notificaciones de la app y que no se refleje en
    el correo personal de cada usuario"): este correo generico SOLO puede ir
    a los destinatarios de ALERT_ALLOWED_RECIPIENTS (el administrador) por
    defecto. Los avisos a usuarios van unicamente por la seccion de
    notificaciones (webapp/news.py), salvo el codigo de activacion de
    cuenta (send_activation_email).

    2026-10-11, excepcion explicita a pedido del usuario ("entran a traves
    de un enlace por correo que se les envia"): allow_user=True habilita
    mandarselo a un usuario puntual -- SOLO para el link de invitacion/
    aprobacion al plan privado (Bot Familiar), que el usuario pidio
    explicitamente por correo real. Nunca usar allow_user=True para
    alertas/avisos generales -- esos siguen yendo solo al admin.

    2026-10-11, a pedido explicito ("quiero que el correo diga bot
    familiar y no mi correo personal"): from_name pone un nombre visible
    en el remitente (ej. "Bot Familiar <direccion@gmail.com>") en vez de
    mostrar solo la direccion de Gmail cruda -- la direccion real sigue
    siendo la misma (es la que de verdad manda el correo), solo cambia
    el nombre que ve el destinatario en su bandeja de entrada."""
    if not allow_user and to_email.strip().lower() not in ALERT_ALLOWED_RECIPIENTS:
        print(f"[email_sender] BLOQUEADO: los avisos a usuarios no se mandan por correo ({to_email}); usar notificaciones de la app.")
        return False
    creds = _credentials()
    if creds is None:
        print("[email_sender] GMAIL_ADDRESS/GMAIL_APP_PASSWORD no configurados -- no se manda correo.")
        return False
    address, app_password = creds

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, address)) if from_name else address
    msg["To"] = to_email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(address, app_password)
            server.sendmail(address, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email_sender] fallo el envio de alerta a {to_email}: {e}")
        return False
