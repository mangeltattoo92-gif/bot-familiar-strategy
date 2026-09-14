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

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _credentials() -> tuple[str, str] | None:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not address or not app_password:
        return None
    return address, app_password


def send_activation_email(to_email: str, username: str, code: str) -> bool:
    """Manda el codigo de activacion por correo. Devuelve True si se
    mando (no garantiza que llegue a destino, solo que el servidor SMTP
    lo acepto), False si no se pudo mandar (credenciales no
    configuradas, o error de red/autenticacion -- se loguea el motivo
    a stderr para diagnosticar sin exponer la contraseña)."""
    creds = _credentials()
    if creds is None:
        print("[email_sender] GMAIL_ADDRESS/GMAIL_APP_PASSWORD no configurados -- no se manda correo.")
        return False
    address, app_password = creds

    body = (
        f"Hola {username},\n\n"
        f"Tu cuenta de Bot Familiar fue aprobada. Tu codigo de activacion es:\n\n"
        f"    {code}\n\n"
        f"Entra a la app, pone tu usuario y este codigo para activar tu cuenta. "
        f"El codigo vence en 48 horas.\n\n"
        f"-- Bot Familiar"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "Tu codigo de activacion -- Bot Familiar"
    msg["From"] = address
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
