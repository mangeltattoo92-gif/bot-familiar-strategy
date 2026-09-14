"""
Diario de operaciones: cruza cada entrada (compra) con su cierre (venta)
correspondiente y calcula estadisticas de rendimiento segun las
condiciones que tenia la señal de Bollinger en el momento de la entrada.

IMPORTANTE sobre lo que es esto y lo que NO es:
Esto NO es un modelo de IA que "aprende" de forma autonoma. Es un motor de
estadisticas sobre TUS PROPIAS operaciones pasadas (registradas en
trades.db). Antes de proponer una nueva entrada, se consulta aqui como les
fue historicamente a entradas con condiciones similares (mismo rango de
delta, misma fuerza de volumen, etc.) para evitar repetir errores -- pero
con pocas operaciones registradas, las conclusiones no son estadisticamente
fiables todavia. Este modulo siempre reporta el tamaño de muestra (n) junto
a cualquier estadistica, y avisa explicitamente cuando n es demasiado bajo.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_trading.engine import DEFAULT_DB_PATH, _connect, _ensure_trades_columns

MIN_SAMPLE_SIZE_FOR_CONFIDENCE = 5
MARKET_TZ = ZoneInfo("America/New_York")


def get_closed_trades(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Empareja cada compra con la venta que cerro esa posicion (misma
    ticker+asset_type+option_details, en orden cronologico -- FIFO simple).

    Pensado para day trading: normalmente 1 compra + 1 venta por posicion.
    Si hay compras parciales/multiples, empareja en el orden en que ocurrieron.
    """
    conn = _connect(db_path)
    try:
        _ensure_trades_columns(conn)
        rows = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()]
    finally:
        conn.close()

    def _pos_key(r: dict) -> str:
        return f"{r['ticker']}|{r['asset_type']}|{r.get('option_details') or ''}"

    open_buys: dict[str, list[dict]] = {}
    closed = []

    for r in rows:
        key = _pos_key(r)
        if r["side"] == "buy":
            open_buys.setdefault(key, []).append(r)
        else:  # sell
            queue = open_buys.get(key)
            if not queue:
                continue  # venta sin compra registrada (no deberia pasar)
            buy = queue.pop(0)

            entry_price = buy["price"]
            exit_price = r["price"]
            qty = min(buy["quantity"], r["quantity"])
            multiplier = buy["multiplier"]
            pnl = (exit_price - entry_price) * qty * multiplier
            pnl_pct = ((exit_price / entry_price) - 1) * 100 if entry_price else 0.0

            opened_at = datetime.fromisoformat(buy["timestamp"])
            closed_at = datetime.fromisoformat(r["timestamp"])
            hold_minutes = round((closed_at - opened_at).total_seconds() / 60)

            option_details = buy.get("option_details")
            if option_details:
                option_details = json.loads(option_details)

            closed.append({
                "ticker": buy["ticker"],
                "asset_type": buy["asset_type"],
                "option_details": option_details,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": qty,
                "multiplier": multiplier,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "win": pnl > 0,
                "opened_at": buy["timestamp"],
                "closed_at": r["timestamp"],
                "hold_minutes": hold_minutes,
                "entry_signal": buy.get("entry_signal"),
                "entry_delta": buy.get("entry_delta"),
                "entry_volatility_strength": buy.get("entry_volatility_strength"),
                "entry_band_width_pct": buy.get("entry_band_width_pct"),
                "entry_band_width_percentile": buy.get("entry_band_width_percentile"),
                "entry_volume_ratio": buy.get("entry_volume_ratio"),
                "entry_consecutive_squeeze_bars": buy.get("entry_consecutive_squeeze_bars"),
                "entry_strategy": buy.get("entry_strategy"),
                "entry_reason": buy.get("reason"),
            })

    return closed


def _summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_pnl": None, "avg_pnl_pct": None, "total_pnl": 0.0}
    wins = sum(1 for t in trades if t["win"])
    total_pnl = sum(t["pnl"] for t in trades)
    avg_pnl = total_pnl / n
    avg_pnl_pct = sum(t["pnl_pct"] for t in trades) / n
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "avg_pnl": round(avg_pnl, 2),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "total_pnl": round(total_pnl, 2),
        "reliable": n >= MIN_SAMPLE_SIZE_FOR_CONFIDENCE,
    }


def overall_performance(db_path: Path = DEFAULT_DB_PATH) -> dict:
    return _summarize(get_closed_trades(db_path))


def performance_by_volatility_strength(db_path: Path = DEFAULT_DB_PATH) -> dict:
    trades = get_closed_trades(db_path)
    buckets = {"extrema": [], "regular": [], "sin_dato": []}
    for t in trades:
        key = t["entry_volatility_strength"] or "sin_dato"
        buckets.setdefault(key, []).append(t)
    return {k: _summarize(v) for k, v in buckets.items() if v}


def performance_by_delta_bucket(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Nota: los edges cubren tanto el rango objetivo original (0.40-0.60,
    usado hasta el 2026-09-08) como el rango actual mas cercano a ITM
    (0.50-0.65, ver paper_trading/option_selector.py) -- asi los trades de
    antes y despues del cambio se siguen pudiendo comparar en la misma tabla."""
    trades = get_closed_trades(db_path)
    edges = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    buckets: dict[str, list[dict]] = {}
    for t in trades:
        d = t["entry_delta"]
        if d is None:
            buckets.setdefault("sin_dato", []).append(t)
            continue
        ad = abs(d)
        label = "fuera_de_rango"
        for i in range(len(edges) - 1):
            if edges[i] <= ad < edges[i + 1] or (i == len(edges) - 2 and ad == edges[-1]):
                label = f"{edges[i]:.2f}-{edges[i+1]:.2f}"
                break
        buckets.setdefault(label, []).append(t)
    return {k: _summarize(v) for k, v in buckets.items() if v}


def performance_by_strategy(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Agrupa por cual de las 3 estrategias genero la señal
    (squeeze_breakout / gap_fade_apertura / giro_sma20) -- para saber cual
    realmente da resultado en vez de mezclarlas todas en una sola metrica.
    Trades de antes de que existiera este campo (2026-09-08) caen en
    'sin_dato'."""
    trades = get_closed_trades(db_path)
    buckets: dict[str, list[dict]] = {}
    for t in trades:
        key = t.get("entry_strategy") or "sin_dato"
        buckets.setdefault(key, []).append(t)
    return {k: _summarize(v) for k, v in buckets.items() if v}


def performance_by_squeeze_strength(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Agrupa por que tan apretadas estaban las bandas (percentil del ancho)."""
    trades = get_closed_trades(db_path)
    buckets: dict[str, list[dict]] = {"muy_apretado_<10pct": [], "apretado_10-20pct": [], "sin_dato": []}
    for t in trades:
        p = t["entry_band_width_percentile"]
        if p is None:
            buckets["sin_dato"].append(t)
        elif p < 10:
            buckets["muy_apretado_<10pct"].append(t)
        else:
            buckets["apretado_10-20pct"].append(t)
    return {k: _summarize(v) for k, v in buckets.items() if v}


def get_todays_closed_trades(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Operaciones cerradas HOY (fecha de mercado, ET). Pensado para
    mostrarlas junto a las posiciones abiertas en el panel durante todo el
    dia -- al pasar la medianoche (ET) dejan de ser "de hoy" y desaparecen
    solas de esta lista, sin necesidad de borrar nada a mano."""
    today = datetime.now(MARKET_TZ).date()
    trades = get_closed_trades(db_path)
    return [
        t for t in trades
        if datetime.fromisoformat(t["closed_at"]).astimezone(MARKET_TZ).date() == today
    ]


def daily_breakdown(days: int = 14, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Operaciones ganadas/perdidas por dia (segun fecha de cierre EN HORA
    DE NY, no UTC -- closed_at se guarda en UTC, asi que una operacion
    cerrada entre las 8pm y medianoche ET todavia cuenta como el mismo dia
    de mercado, no el siguiente), para los ultimos `days` dias, incluyendo
    dias sin operaciones (en 0)."""
    trades = get_closed_trades(db_path)
    today = datetime.now(MARKET_TZ).date()
    start = today - timedelta(days=days - 1)

    buckets: dict[str, dict] = {}
    for t in trades:
        d = datetime.fromisoformat(t["closed_at"]).astimezone(MARKET_TZ).date()
        if d < start or d > today:
            continue
        key = d.isoformat()
        b = buckets.setdefault(key, {"date": key, "wins": 0, "losses": 0, "trades": 0, "pnl": 0.0})
        b["trades"] += 1
        b["pnl"] += t["pnl"]
        b["wins" if t["win"] else "losses"] += 1

    result = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        entry = buckets.get(d, {"date": d, "wins": 0, "losses": 0, "trades": 0, "pnl": 0.0})
        entry["pnl"] = round(entry["pnl"], 2)
        result.append(entry)
    return result


def _open_positions_since(start_date: date, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Posiciones todavia abiertas (tabla positions) cuya entrada ocurrio
    en o despues de start_date. Se usan para que el contador de
    'operaciones esta semana' sume una entrada en cuanto se abre, sin
    esperar a que se cierre."""
    conn = _connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]
    finally:
        conn.close()
    result = []
    for r in rows:
        opened_at = r.get("opened_at")
        if not opened_at:
            continue
        if datetime.fromisoformat(opened_at).astimezone(MARKET_TZ).date() >= start_date:
            result.append(r)
    return result


def weekly_summary(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Resumen de la semana actual (lunes a hoy, hora de NY). El total de
    operaciones incluye tanto las ya cerradas como las que siguen abiertas
    -- una posicion cuenta desde el momento en que se abre, no solo al
    cerrarse."""
    trades = get_closed_trades(db_path)
    today = datetime.now(MARKET_TZ).date()
    start_of_week = today - timedelta(days=today.weekday())

    week_trades = [t for t in trades if datetime.fromisoformat(t["closed_at"]).astimezone(MARKET_TZ).date() >= start_of_week]
    open_this_week = _open_positions_since(start_of_week, db_path)
    stats = _summarize(week_trades)
    stats["week_start"] = start_of_week.isoformat()
    stats["week_end"] = today.isoformat()
    stats["losses"] = sum(1 for t in week_trades if not t["win"])
    stats["wins"] = sum(1 for t in week_trades if t["win"])
    stats["closed"] = stats["n"]
    stats["open"] = len(open_this_week)
    stats["n"] = stats["closed"] + stats["open"]
    return stats


def recommendation_for(
    volatility_strength: str | None = None,
    entry_delta: float | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> str:
    """Texto breve para consultar ANTES de abrir una posicion nueva: como
    les fue historicamente a entradas con condiciones parecidas."""
    trades = get_closed_trades(db_path)
    overall = _summarize(trades)
    if overall["n"] == 0:
        return "Sin operaciones cerradas todavia en el diario -- no hay historial para comparar esta entrada."

    similar = trades
    if volatility_strength:
        similar = [t for t in similar if t["entry_volatility_strength"] == volatility_strength]
    if entry_delta is not None:
        similar = [t for t in similar if t["entry_delta"] is not None and abs(abs(t["entry_delta"]) - abs(entry_delta)) <= 0.05]

    sim_stats = _summarize(similar)
    lines = [f"Historial general: {overall['n']} operaciones cerradas, win rate {overall['win_rate']}%, P&L medio {overall['avg_pnl_pct']}%."]
    if sim_stats["n"] > 0:
        confidence = "" if sim_stats["reliable"] else " (muestra pequeña, poco fiable todavia)"
        lines.append(
            f"Condiciones similares (volatilidad={volatility_strength or 'cualquiera'}, "
            f"delta~{entry_delta if entry_delta is not None else 'cualquiera'}): "
            f"{sim_stats['n']} operaciones, win rate {sim_stats['win_rate']}%, "
            f"P&L medio {sim_stats['avg_pnl_pct']}%{confidence}."
        )
    else:
        lines.append("No hay operaciones previas con condiciones similares para comparar.")
    return " ".join(lines)
