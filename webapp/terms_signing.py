"""Firma de Terminos y Condiciones con PDF, para Usuarios que van a conectar
dinero real (Metabot Pro + Corretaje Conectado).

2026-10-07, a pedido explicito del usuario: antes de poder pagar el plan
Pro y conectar su broker, cada Usuario tiene que leer el documento
completo, escribir su nombre completo, dibujar su firma, y enviarlo --
eso genera un PDF que queda guardado (a) en una carpeta del servidor,
identificado por usuario, y (b) referenciado en la base de datos para que
el propio Usuario y el administrador lo puedan descargar despues.

El texto legal de este PDF debe mantenerse en sincronia con
webapp/templates/terms.html (TERMS_TEXT_VERSION aca = la version que
aparece en el encabezado de ese documento) -- son dos archivos porque
terms.html es HTML/Jinja y este modulo necesita texto plano para
reportlab, no por tener contenido distinto.
"""
import base64
import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "users.db"
PDF_DIR = Path(__file__).resolve().parent.parent / "data" / "terms_signed"

TERMS_TEXT_VERSION = "3.0-2026-10-07"

# Texto plano del documento completo (ver terms.html -- misma version,
# mismo contenido legal, solo sin HTML). Mantener sincronizado a mano.
TERMS_SECTIONS: list[tuple[str, str]] = [
    ("1. Definiciones",
     "\"Servicio\" / \"Bot\": la herramienta de software Metabot, incluyendo su panel web, sus procesos "
     "automatizados de escaneo y ejecucion (simulada o real) de operaciones, y toda comunicacion generada "
     "por ella.\n\n"
     "\"Operador\": la persona que desarrolla, mantiene y aloja el Servicio en su propia infraestructura.\n\n"
     "\"Usuario\" / \"Cuenta\": cualquier persona que se registra y utiliza el Servicio bajo su propio "
     "nombre y capital.\n\n"
     "\"Corretaje Conectado\": la cuenta de corretaje de terceros (por ejemplo, Tradier) que un Usuario "
     "decide vincular al Servicio, mediante un token de API personal que el propio Usuario genera desde su "
     "cuenta de ese corretaje y pega en el Servicio, por su propia cuenta y bajo su propia autorizacion, "
     "para operar con dinero real."),
    ("2. Elegibilidad",
     "Para usar el Servicio, el Usuario declara y garantiza que: tiene al menos 18 anos de edad y "
     "capacidad legal para celebrar contratos vinculantes; es residente de Estados Unidos; toda la "
     "informacion provista al registrarse es veraz; y si conecta un Corretaje Conectado, esa cuenta esta a "
     "su propio nombre, en cumplimiento de los terminos de esa institucion, y el/ella es el unico titular "
     "autorizado para operarla."),
    ("3. Descripcion del Servicio",
     "Metabot escanea datos de mercado publicos y ejecuta (en modo simulado, o real cuando este "
     "habilitado y conectado por el propio Usuario) operaciones de opciones sobre acciones segun reglas "
     "tecnicas predefinidas y no discrecionales. Cada Usuario opera bajo su propio nombre, con su propio "
     "capital, y asume su propio riesgo; no delega la gestion de su dinero a nadie, incluido el Operador. "
     "Metabot opera bajo un modelo comercial de suscripcion, con renovacion automatica salvo cancelacion."),
    ("4. No es Asesoria Financiera, Legal ni de Inversion",
     "Ninguna senal, operacion ni resultado mostrado en el panel constituye una recomendacion "
     "personalizada de inversion. El Operador no es un asesor de inversiones registrado ni un "
     "broker-dealer registrado ante la SEC. Cada Usuario es el unico responsable de evaluar si el uso de "
     "esta herramienta es adecuado para su propia situacion."),
    ("5. Reconocimiento de Riesgo -- Operaciones con Opciones",
     "Operar opciones sobre acciones implica un riesgo significativo, incluyendo la posibilidad de "
     "PERDIDA TOTAL del capital destinado a esta herramienta en un periodo corto de tiempo. El rendimiento "
     "pasado -- incluido cualquier resultado en modo simulado -- no garantiza resultados futuros. Los "
     "resultados simulados pueden diferir sustancialmente de los resultados con dinero real."),
    ("6. Modo Simulado vs. Dinero Real",
     "Por defecto el Servicio opera en modo simulado, sin mover dinero real. Cuando el Usuario conecta un "
     "Corretaje Conectado (por ejemplo, Tradier) mediante un token de API personal, esa conexion es "
     "siempre iniciada y autorizada por el propio Usuario, opera solo sobre el capital que el decida "
     "depositar, y puede desconectarse en cualquier momento. Conectar el Corretaje Conectado por si solo "
     "NO activa operaciones reales: el Usuario debe activar explicitamente, con un interruptor separado, "
     "la opcion de \"operar con dinero real\" antes de que el Servicio coloque cualquier orden real -- y "
     "puede desactivarla cuando quiera."),
    ("7. Servicios y Marcas de Terceros",
     "El Servicio utiliza datos de mercado de terceros (incluyendo Yahoo Finance y, cuando corresponda, "
     "Tradier) y se conecta a la infraestructura de Tradier LLC mediante el token de API personal del "
     "Usuario. El Servicio no esta afiliado, patrocinado, respaldado ni verificado por Tradier LLC ni por "
     "Yahoo Finance."),
    ("8. Exclusion de Garantias",
     "EL SERVICIO SE PROVEE \"TAL CUAL\" Y \"SEGUN DISPONIBILIDAD\", SIN GARANTIAS DE NINGUN TIPO. EL "
     "OPERADOR NO GARANTIZA QUE EL SERVICIO SEA ININTERRUMPIDO, SEGURO, LIBRE DE ERRORES, NI QUE LAS "
     "SENALES GENERADAS RESULTEN EN OPERACIONES RENTABLES."),
    ("9. Limitacion de Responsabilidad",
     "EN LA MAXIMA MEDIDA PERMITIDA POR LA LEY, EL OPERADOR NO SERA RESPONSABLE POR DANOS INDIRECTOS, "
     "INCIDENTALES O POR PERDIDA DE CAPITAL DERIVADOS DEL USO DEL SERVICIO. LA RESPONSABILIDAD TOTAL DEL "
     "OPERADOR HACIA UN USUARIO NUNCA EXCEDERA CIEN DOLARES ESTADOUNIDENSES (US$100)."),
    ("10. Indemnizacion",
     "El Usuario acepta indemnizar al Operador frente a reclamos que surjan de su uso del Servicio, su "
     "conexion de un Corretaje Conectado, su incumplimiento de estos Terminos, o su violacion de la ley."),
    ("11. Privacidad de los Datos de Cada Cuenta",
     "El panel de administrador no muestra balance, ganancias/perdidas ni posiciones de ningun Usuario en "
     "el uso normal del sistema. El Operador no vende ni comparte datos de los Usuarios con terceros con "
     "fines comerciales."),
    ("12. Propiedad Intelectual",
     "El codigo, diseno, estrategias y demas contenido del Servicio son propiedad del Operador."),
    ("13. Suspension y Terminacion",
     "El Operador puede suspender o terminar el acceso de cualquier Usuario en cualquier momento, con o "
     "sin causa. El Usuario puede dejar de usar el Servicio y desconectar su Corretaje Conectado cuando "
     "quiera."),
    ("14. Resolucion de Disputas -- Arbitraje",
     "Cualquier disputa se resolvera mediante arbitraje vinculante e individual. Tanto el Usuario como el "
     "Operador renuncian a su derecho a un juicio por jurado y a una accion colectiva."),
    ("15. Fuerza Mayor",
     "El Operador no sera responsable por incumplimientos causados por circunstancias fuera de su control "
     "razonable."),
    ("16. Divisibilidad",
     "Si alguna disposicion fuera invalida, el resto de los Terminos permanece en pleno vigor."),
    ("17. Acuerdo Completo",
     "Estos Terminos constituyen el acuerdo completo entre el Usuario y el Operador respecto del "
     "Servicio."),
    ("18. Modificaciones a Estos Terminos",
     "Estos Terminos pueden actualizarse; los cambios sustantivos se reflejan con una nueva version y "
     "fecha."),
    ("19. Ley Aplicable y Jurisdiccion",
     "Estos Terminos se rigen por las leyes federales de Estados Unidos y las leyes del Estado de "
     "Florida."),
    ("20. Consentimiento de Comunicaciones Electronicas",
     "El Usuario consiente recibir comunicaciones electronicamente en lugar de en papel."),
    ("21. Contacto",
     "Consultas sobre estos Terminos pueden dirigirse al Operador a traves del correo electronico "
     "registrado en el Servicio."),
]


def _connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signed_terms_pro (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            full_name TEXT NOT NULL,
            terms_version TEXT NOT NULL,
            pdf_path TEXT NOT NULL,
            signed_at TEXT NOT NULL,
            ip_address TEXT
        )
        """
    )
    conn.commit()
    return conn


def has_signed(user_id: int, db_path: Path = DEFAULT_DB) -> dict[str, Any] | None:
    """Ultima firma de este usuario para la version ACTUAL de los Terminos, o None."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM signed_terms_pro WHERE user_id = ? AND terms_version = ? ORDER BY id DESC LIMIT 1",
            (user_id, TERMS_TEXT_VERSION),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_signature_by_id(signing_id: int, db_path: Path = DEFAULT_DB) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM signed_terms_pro WHERE id = ?", (signing_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_all_signed(db_path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    """Para el panel de administrador -- la firma mas reciente de cada usuario."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM signed_terms_pro WHERE id IN "
            "(SELECT MAX(id) FROM signed_terms_pro GROUP BY user_id) ORDER BY signed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _build_pdf(full_name: str, username: str, signature_png: bytes, signed_at: datetime, ip_address: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch, topMargin=0.8 * inch, bottomMargin=0.8 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TermsTitle", parent=styles["Title"], fontSize=15, spaceAfter=4)
    meta_style = ParagraphStyle("TermsMeta", parent=styles["Normal"], fontSize=9, textColor=colors.grey, spaceAfter=14)
    h2_style = ParagraphStyle("TermsH2", parent=styles["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4)
    body_style = ParagraphStyle("TermsBody", parent=styles["Normal"], fontSize=9.5, leading=13.5)

    story = [
        Paragraph("Términos y Condiciones · Aviso de Riesgo", title_style),
        Paragraph(f"Metabot · Versión {TERMS_TEXT_VERSION} · Documento firmado electrónicamente", meta_style),
    ]
    for heading, body in TERMS_SECTIONS:
        story.append(Paragraph(heading, h2_style))
        for para in body.split("\n\n"):
            story.append(Paragraph(para.replace("\n", " "), body_style))

    story.append(Spacer(1, 18))
    story.append(Paragraph("22. Aceptación y Firma", h2_style))
    story.append(Paragraph(
        "Declaro que leí este documento en su totalidad, entiendo los riesgos descritos, y acepto "
        "operar bajo mi propia responsabilidad y bajo estos Términos.", body_style))
    story.append(Spacer(1, 10))

    # 2026-10-11, a pedido explicito ("la firma se ve bien feo... has que
    # se vea bien profesional dentro de rectangulo"): la firma ya no va
    # como celda suelta de una tabla -- va en su propio recuadro con
    # borde y fondo, como un bloque de firma de contrato real, separado
    # de los datos de texto.
    meta_table = Table(
        [
            ["Nombre completo:", full_name],
            ["Usuario:", username],
            ["Fecha y hora (UTC):", signed_at.isoformat(timespec="seconds")],
            ["Dirección IP:", ip_address or "—"],
        ],
        colWidths=[1.6 * inch, 4.2 * inch],
    )
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.lightgrey),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 16))

    sig_image = Image(io.BytesIO(signature_png), width=2.8 * inch, height=1.1 * inch)
    sig_box = Table([[sig_image]], colWidths=[3.2 * inch], rowHeights=[1.35 * inch])
    sig_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#444444")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(sig_box)
    story.append(Spacer(1, 4))
    caption_style = ParagraphStyle(
        "SigCaption", parent=styles["Normal"], fontSize=8.5, textColor=colors.grey,
    )
    story.append(Paragraph(f"Firma de {full_name}", caption_style))

    doc.build(story)
    return buf.getvalue()


def save_signature(
    user_id: int, username: str, full_name: str, signature_data_url: str, ip_address: str = "",
    db_path: Path = DEFAULT_DB,
) -> dict[str, Any]:
    """Genera el PDF firmado y guarda el registro. signature_data_url: 'data:image/png;base64,...'
    (lo que entrega un <canvas>.toDataURL() de firma en el navegador)."""
    full_name = (full_name or "").strip()
    if not full_name:
        raise ValueError("Falta el nombre completo.")
    if not signature_data_url or "," not in signature_data_url:
        raise ValueError("Falta la firma.")
    signature_png = base64.b64decode(signature_data_url.split(",", 1)[1])

    signed_at = datetime.now(timezone.utc)
    pdf_bytes = _build_pdf(full_name, username, signature_png, signed_at, ip_address)

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    safe_username = "".join(c if c.isalnum() or c in "-_" else "_" for c in username)
    filename = f"{safe_username}_{signed_at.strftime('%Y%m%dT%H%M%SZ')}.pdf"
    pdf_path = PDF_DIR / filename
    pdf_path.write_bytes(pdf_bytes)

    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO signed_terms_pro (user_id, username, full_name, terms_version, pdf_path, signed_at, "
            "ip_address) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, full_name, TERMS_TEXT_VERSION, str(pdf_path), signed_at.isoformat(timespec="seconds"), ip_address),
        )
        conn.commit()
        return {"id": cur.lastrowid, "pdf_path": str(pdf_path), "signed_at": signed_at.isoformat(timespec="seconds")}
    finally:
        conn.close()
