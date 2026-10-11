"""
Cobros de Metabot Pro -- estructura completa lista para Stripe
(2026-10-05, a pedido explicito: "hazlo profesional... crea toda la
estructura, despues agregamos stripe").

NO HAY NINGUNA CREDENCIAL DE STRIPE CONFIGURADA TODAVIA. Hasta que se
configuren las variables de entorno de abajo, is_configured() devuelve
False y create_checkout_session()/cancel_subscription() no hacen nada
(devuelven None) en vez de fallar -- el resto de la app sigue andando
normal sin Stripe instalado ni configurado.

Para conectar Stripe de verdad, en este orden:
  1. pip install stripe  (agregar a requirements.txt)
  2. Crear una cuenta de Stripe, crear un Producto "Metabot Pro" con un
     Price recurrente mensual de $300.00 USD.
  3. Configurar estas variables de entorno (NUNCA hardcodear las keys
     en el codigo ni commitearlas):
       STRIPE_SECRET_KEY       -- sk_live_... (o sk_test_... para probar)
       STRIPE_PUBLISHABLE_KEY  -- pk_live_... (no es secreta, va al frontend si hace falta)
       STRIPE_WEBHOOK_SECRET   -- whsec_... (del endpoint configurado en el Dashboard de Stripe)
       STRIPE_PRO_PRICE_ID     -- price_... (el Price recurrente creado en el paso 2)
  4. En el Dashboard de Stripe, agregar un webhook endpoint apuntando a
     https://<dominio>/webhooks/stripe escuchando (minimo):
       checkout.session.completed
       customer.subscription.updated
       customer.subscription.deleted
       invoice.payment_failed
  5. Metodos de pago (tarjeta, PayPal, Google Pay, Apple Pay, Link):
     create_checkout_session() de abajo NO fija "payment_method_types" a
     proposito -- deja que Stripe Checkout decida solo segun lo que este
     habilitado en Dashboard > Settings > Payment methods. Tarjeta viene
     activada por default; PayPal, Google Pay y Apple Pay se activan ahi
     con un toggle, sin tocar este codigo. PayPal en particular requiere
     que la cuenta de Stripe este en un pais/moneda elegible (USD + EE.UU.
     lo son) y puede pedir habilitarlo aparte con soporte de Stripe la
     primera vez.
  6. Probar primero en modo test (sk_test_/pk_test_) con una tarjeta de
     prueba de Stripe (4242 4242 4242 4242, cualquier fecha futura/CVC)
     antes de pasar a las keys live. El modo test tambien simula PayPal
     con una cuenta sandbox de PayPal sin mover plata real.
"""

import os

# 2026-10-05, a pedido explicito ("al impuesto que cobraria un profesional,
# sumaselo a los 300 y que se vea sin decir que es el impuesto"): el precio
# que se MUESTRA es un numero unico todo-incluido ($300 base + ~7%, la tasa
# combinada de impuesto a las ventas que se usaria en Florida -- la misma
# jurisdiccion ya confirmada para los Terminos y Condiciones). PRO_PLAN_PRICE_USD
# sigue siendo el precio BASE que se le va a pasar a Stripe (el Price real del
# producto); PRO_PLAN_DISPLAY_PRICE_USD es el numero final que ve el usuario.
# Cuando se conecte Stripe de verdad falta decidir como facturar esto (Price
# de Stripe en modo "tax inclusive" a $321, o $300 + automatic_tax real segun
# el estado del cliente) -- por ahora es solo el numero que se muestra.
PRO_PLAN_PRICE_USD = 300
PRO_PLAN_TAX_RATE = 0.07
PRO_PLAN_DISPLAY_PRICE_USD = round(PRO_PLAN_PRICE_USD * (1 + PRO_PLAN_TAX_RATE))
PRO_PLAN_PERIOD_LABEL = "mes"

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRO_PRICE_ID = os.environ.get("STRIPE_PRO_PRICE_ID", "").strip()

try:
    import stripe  # type: ignore
except ImportError:
    stripe = None


def is_configured() -> bool:
    """True solo si el paquete stripe esta instalado Y las 4 variables
    de entorno estan seteadas. Mientras sea False, el boton de
    "Suscribirme a Pro" del panel muestra un aviso profesional en vez
    de intentar cobrar algo."""
    return bool(stripe and STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and STRIPE_PRO_PRICE_ID)


def create_checkout_session(user: dict, success_url: str, cancel_url: str) -> str | None:
    """Crea (o reusa) el Customer de Stripe del Usuario y arma una
    Checkout Session de suscripcion recurrente. Devuelve la URL a la
    que redirigir al Usuario (el checkout hospedado por Stripe, NUNCA
    un formulario de tarjeta propio) -- o None si Stripe todavia no
    esta configurado."""
    if not is_configured():
        return None
    stripe.api_key = STRIPE_SECRET_KEY

    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(email=user["email"], name=user["username"])
        customer_id = customer["id"]
        from webapp.auth import set_stripe_customer_id
        set_stripe_customer_id(user["id"], customer_id)

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": STRIPE_PRO_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        # automatic_tax requiere direccion fiscal de negocio configurada
        # en Stripe Y un tax_code de producto (exigido ademas por
        # Managed Payments, activo por defecto en cuentas nuevas) -- no
        # aplica para un piloto familiar de ~10 usuarios, asi que se
        # desactivan los dos (2026-10-11, InvalidRequestError "product
        # tax code is missing" / "valid head office address").
        managed_payments={"enabled": False},
    )
    return session.url


def cancel_subscription(stripe_subscription_id: str) -> bool:
    """Cancela AL FINAL DEL PERIODO ya pagado (no corta el acceso al
    toque -- consistente con lo que ya dice terms.html seccion 3: "la
    cancelacion surte efecto al final del periodo ya pagado"). El
    estado local se actualiza recien cuando llega el webhook
    customer.subscription.updated confirmando el cambio, no aca."""
    if not is_configured() or not stripe_subscription_id:
        return False
    stripe.api_key = STRIPE_SECRET_KEY
    stripe.Subscription.modify(stripe_subscription_id, cancel_at_period_end=True)
    return True


def handle_webhook_event(payload: bytes, sig_header: str) -> tuple[bool, str]:
    """Verifica la firma del webhook y aplica el cambio de plan/estado
    correspondiente via auth.apply_subscription_update(). Devuelve
    (ok, mensaje) -- el caller (ver /webhooks/stripe en app.py) usa el
    primer valor para decidir el codigo HTTP a devolverle a Stripe."""
    from webapp.auth import PLAN_FREE, PLAN_PRO, SUB_STATUS_ACTIVE, SUB_STATUS_CANCELED, SUB_STATUS_PAST_DUE, apply_subscription_update

    if not is_configured():
        return False, "Stripe no configurado"
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return False, f"Firma invalida: {e}"

    etype = event["type"]
    obj = event["data"]["object"]
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()

    if etype == "checkout.session.completed":
        apply_subscription_update(
            obj["customer"], plan=PLAN_PRO, status=SUB_STATUS_ACTIVE,
            stripe_subscription_id=obj.get("subscription"),
        )
    elif etype == "customer.subscription.updated":
        status = SUB_STATUS_ACTIVE if obj.get("status") == "active" else SUB_STATUS_PAST_DUE
        if obj.get("cancel_at_period_end"):
            from webapp.auth import SUB_STATUS_CANCELING
            status = SUB_STATUS_CANCELING
        period_end = obj.get("current_period_end")
        from datetime import datetime, timezone
        period_end_iso = datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat() if period_end else None
        apply_subscription_update(
            obj["customer"], plan=PLAN_PRO, status=status,
            stripe_subscription_id=obj.get("id"), current_period_end=period_end_iso,
        )
    elif etype == "customer.subscription.deleted":
        apply_subscription_update(obj["customer"], plan=PLAN_FREE, status=SUB_STATUS_CANCELED)
    elif etype == "invoice.payment_failed":
        apply_subscription_update(obj["customer"], plan=PLAN_PRO, status=SUB_STATUS_PAST_DUE)

    return True, "ok"
