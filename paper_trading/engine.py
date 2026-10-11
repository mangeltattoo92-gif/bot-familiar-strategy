"""
Motor de paper trading (simulacion) para la cuenta Agentic de Robinhood.

No coloca ninguna orden real. Solo lleva un libro de contabilidad local
(SQLite) que simula el efecto de operaciones sobre un balance virtual.

Metodo de coste: coste medio ponderado (average cost) por posicion.
No se permite venta en corto (no se puede vender mas de lo que se tiene)
ni compras que dejen el cash virtual en negativo, salvo --force.
"""

import sqlite3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_trading.db"


class PaperTradingError(Exception):
    pass


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _position_key(ticker: str, asset_type: str, expiration: str | None,
                   strike: float | None, option_type: str | None) -> str:
    ticker = ticker.upper()
    asset_type = asset_type.lower()
    if asset_type == "option":
        return f"{ticker}|option|{expiration}|{strike}|{option_type}"
    return f"{ticker}|{asset_type}"


def init_db(initial_balance: float, db_path: Path = DEFAULT_DB_PATH, reset: bool = False) -> None:
    """Crea el esquema y siembra el balance virtual inicial.

    Si ya existe una cuenta y reset=False, no hace nada (idempotente).
    Si reset=True, borra todo el historial y vuelve a empezar desde initial_balance.
    """
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_balance REAL NOT NULL,
                cash_balance REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                key TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                option_details TEXT,
                quantity REAL NOT NULL,
                avg_cost REAL NOT NULL,
                multiplier REAL NOT NULL DEFAULT 1,
                opened_at TEXT,
                profit_target_pct REAL,
                stop_loss_pct REAL,
                entry_delta REAL,
                entry_theta REAL,
                entry_strategy TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                multiplier REAL NOT NULL DEFAULT 1,
                cash_effect REAL NOT NULL,
                cash_after REAL NOT NULL,
                realized_pnl_delta REAL NOT NULL DEFAULT 0,
                reason TEXT,
                option_details TEXT,
                entry_delta REAL,
                entry_theta REAL,
                entry_signal TEXT,
                entry_volatility_strength TEXT,
                entry_band_width_pct REAL,
                entry_band_width_percentile REAL,
                entry_volume_ratio REAL,
                entry_consecutive_squeeze_bars INTEGER
            );

            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                contracts_per_trade INTEGER NOT NULL DEFAULT 1,
                max_trades_per_day INTEGER NOT NULL DEFAULT 3,
                watchlist TEXT NOT NULL DEFAULT 'SPY,QQQ,IWM',
                bot_enabled INTEGER NOT NULL DEFAULT 1,
                max_daily_loss_pct REAL NOT NULL DEFAULT 8.0,
                risk_pct_per_trade REAL NOT NULL DEFAULT 2.0,
                updated_at TEXT NOT NULL
            );
            """
        )
        row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            now = _now()
            conn.execute(
                "INSERT INTO account (id, initial_balance, cash_balance, realized_pnl, created_at, updated_at) "
                "VALUES (1, ?, ?, 0, ?, ?)",
                (initial_balance, initial_balance, now, now),
            )
        elif reset:
            now = _now()
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM positions")
            conn.execute(
                "UPDATE account SET initial_balance = ?, cash_balance = ?, realized_pnl = 0, updated_at = ? WHERE id = 1",
                (initial_balance, initial_balance, now),
            )
        conn.commit()
    finally:
        conn.close()


def _get_account(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    if row is None:
        raise PaperTradingError("Cuenta no inicializada. Ejecuta init primero.")
    return row


_ET = ZoneInfo("America/New_York")


def _unsettled_proceeds(conn: sqlite3.Connection, now_iso: str | None = None) -> float:
    """2026-10-05, a pedido explicito ("Robinhood no agrega la ganancia de
    la prima ni el dinero que se uso hasta el otro dia"): en un broker real
    (cuenta de efectivo, no margen -- la real del pilot) las opciones
    liquidan en 1 dia habil, asi que el dinero que vuelve al cerrar una
    posicion (ganancia O perdida, da igual) no se puede volver a usar para
    comprar hasta el dia siguiente. Se simula aca: cada venta/cierre queda
    "congelado" hasta las 5am hora de Nueva York del dia siguiente a esa
    operacion -- se resta del cash disponible para comprar (NO del cash
    balance real que se muestra en el panel, ese sigue siendo el total
    verdadero de la cuenta)."""
    now_dt = datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT timestamp, cash_effect FROM trades WHERE side = 'sell' AND cash_effect > 0"
    ).fetchall()
    frozen = 0.0
    for r in rows:
        trade_et = datetime.fromisoformat(r["timestamp"]).astimezone(_ET)
        release_et = (trade_et + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)
        if release_et.astimezone(timezone.utc) > now_dt:
            frozen += r["cash_effect"]
    return frozen


def _ensure_positions_columns(conn: sqlite3.Connection) -> None:
    """Migracion ligera para bases de datos creadas antes de las columnas
    de day trading (opened_at, profit_target_pct, stop_loss_pct, entry_delta)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(positions)")}
    if "opened_at" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN opened_at TEXT")
    if "profit_target_pct" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN profit_target_pct REAL")
    if "stop_loss_pct" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN stop_loss_pct REAL")
    if "entry_delta" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN entry_delta REAL")
    if "entry_theta" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN entry_theta REAL")
    if "entry_strategy" not in cols:
        # 2026-09-22, a pedido del usuario ("porque smci se fue contrario"):
        # falta esta columna en `positions` (ya existia en `trades`) para que
        # el bucle de salidas de multi_user_entry.py sepa que estrategia abrio
        # cada posicion. Sin esto, la regla 'rechazo_de_ruptura' (que compara
        # el precio contra la banda de Bollinger) se aplicaba a TODAS las
        # posiciones por igual, aunque solo squeeze_breakout/_temprano abren
        # realmente rompiendo esa banda -- ver la nota junto a
        # _squeeze_breakout_strategies en multi_user_entry.py.
        conn.execute("ALTER TABLE positions ADD COLUMN entry_strategy TEXT")


def _ensure_trades_columns(conn: sqlite3.Connection) -> None:
    """Migracion ligera para bases de datos creadas antes de los campos de
    diario de operaciones (entry_delta y las condiciones de entrada)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    if "entry_delta" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_delta REAL")
    if "entry_theta" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_theta REAL")
    if "entry_signal" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_signal TEXT")
    if "entry_volatility_strength" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_volatility_strength TEXT")
    if "entry_band_width_pct" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_band_width_pct REAL")
    if "entry_band_width_percentile" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_band_width_percentile REAL")
    if "entry_volume_ratio" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_volume_ratio REAL")
    if "entry_consecutive_squeeze_bars" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_consecutive_squeeze_bars INTEGER")
    if "entry_strategy" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_strategy TEXT")
    if "entry_implied_volatility" not in cols:
        # 2026-09-18, a pedido del usuario ("como arreglamos lo de la
        # prima impredecible"): antes no se guardaba la IV real de la
        # entrada -- sin este dato no se puede validar con datos reales
        # si conviene filtrar por IV inflada al elegir contrato.
        conn.execute("ALTER TABLE trades ADD COLUMN entry_implied_volatility REAL")
    if "entry_clv" not in cols:
        # 2026-09-18, a pedido del usuario tras el caso MU: dos datos de la vela
        # de señal, SOLO REGISTRO (no cambian ninguna decision) para validar en
        # ~2 semanas si conviene filtrar entradas "en extremo":
        # clv = donde cerro la vela dentro de su rango (1 = en el extremo a favor),
        # day_pos = donde esta el precio dentro del rango del dia (1 = en el extremo).
        conn.execute("ALTER TABLE trades ADD COLUMN entry_clv REAL")
    if "entry_day_pos" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_day_pos REAL")
    # 2026-09-27, a pedido del usuario tras el estudio profundo de perdidas
    # ("busca los datos que aun no tenemos... para perfeccionar"): 3 datos
    # que antes se usaban solo en el momento de decidir y se descartaban --
    # sin guardarlos, cada caso de "salto"/liquidez habia que investigarlo
    # a mano cruzando logs y yfinance despues del hecho. Solo registro, no
    # cambian ninguna decision de entrada.
    if "entry_confidence" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_confidence TEXT")
    if "entry_spread_pct" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_spread_pct REAL")
    if "entry_open_interest" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_open_interest INTEGER")
    if "entry_vix" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_vix REAL")


def _record_trade_impl(
    side: str,
    ticker: str,
    asset_type: str,
    price: float,
    reason: str,
    quantity: float | None = None,
    multiplier: float = 1.0,
    expiration: str | None = None,
    strike: float | None = None,
    option_type: str | None = None,
    force: bool = False,
    profit_target_pct: float | None = None,
    stop_loss_pct: float | None = None,
    entry_delta: float | None = None,
    entry_theta: float | None = None,
    entry_signal: str | None = None,
    entry_volatility_strength: str | None = None,
    entry_band_width_pct: float | None = None,
    entry_band_width_percentile: float | None = None,
    entry_volume_ratio: float | None = None,
    entry_consecutive_squeeze_bars: int | None = None,
    entry_strategy: str | None = None,
    entry_implied_volatility: float | None = None,
    entry_clv: float | None = None,
    entry_day_pos: float | None = None,
    entry_confidence: str | None = None,
    entry_spread_pct: float | None = None,
    entry_open_interest: int | None = None,
    entry_vix: float | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    """Registra una operacion simulada y actualiza balance/posiciones/P&L.

    side: 'buy' o 'sell'
    asset_type: 'equity' | 'option' | 'crypto'
    profit_target_pct / stop_loss_pct: plan de salida (day trading) que se
    fija SOLO al abrir una posicion nueva con una compra (ej. 0.20 = +20%).
    entry_signal / entry_volatility_strength / entry_band_width_pct /
    entry_band_width_percentile / entry_volume_ratio /
    entry_consecutive_squeeze_bars: condiciones de la señal Bollinger en el
    momento de la compra (salida de bollinger_strategy.analyze()). Se
    guardan como diario de operaciones para poder analizar despues que
    condiciones dieron mejores resultados (ver trade_journal.py).
    entry_delta: delta del contrato de opcion en el momento de la compra
    (ej. 0.52 para un call, -0.47 para un put). Solo tiene sentido en compras.
    entry_theta: theta del contrato en el momento de la compra (decaimiento
    diario de la prima, siempre negativo para el comprador). Solo tiene
    sentido en compras -- se guarda para poder ver despues cuanto pesaba
    el paso del tiempo en cada operacion, no se usa hoy para filtrar
    contratos (a diferencia del delta).
    entry_strategy: cual de las 3 estrategias genero la señal
    ('squeeze_breakout' | 'gap_fade_apertura' | 'giro_sma20') -- para poder
    comparar el rendimiento de cada estrategia por separado (ver
    trade_journal.performance_by_strategy()), en vez de mezclarlas todas.
    entry_confidence: confianza de la señal ('alta' | 'media' | 'baja') en
    el momento de la compra. entry_spread_pct: spread bid/ask del contrato
    elegido, en % del ask (ej. 5.0 = 5%). entry_open_interest: open
    interest del contrato elegido. entry_vix: nivel de ^VIX en el momento
    de la compra. Los 4 son SOLO registro (2026-09-27, a pedido del
    usuario tras el estudio de perdidas) -- no cambian ninguna decision de
    entrada, se guardan para poder correlacionar despues con casos de
    salto de liquidez o compresion de volatilidad sin tener que
    reconstruir todo a mano con yfinance cada vez.
    """
    side = side.lower()
    asset_type = asset_type.lower()
    if side not in ("buy", "sell"):
        raise PaperTradingError("side debe ser 'buy' o 'sell'")
    if asset_type not in ("equity", "option", "crypto"):
        raise PaperTradingError("asset_type debe ser 'equity', 'option' o 'crypto'")

    settings = get_settings(db_path)
    if quantity is None:
        quantity = float(settings["contracts_per_trade"])

    if quantity <= 0 or price < 0:
        raise PaperTradingError("quantity debe ser > 0 y price >= 0")
    if not reason or not reason.strip():
        raise PaperTradingError("reason es obligatorio: registra la logica detras de la decision")

    if side == "buy" and not force:
        if not settings["bot_enabled"]:
            raise PaperTradingError(
                "El bot esta APAGADO -- no se pueden abrir posiciones nuevas. "
                "Enciendelo desde el panel, o usa force=True para forzar esta entrada."
            )
        trades_today = get_trades_count_today(db_path)
        max_trades = settings["max_trades_per_day"]
        if trades_today >= max_trades:
            raise PaperTradingError(
                f"Limite diario de operaciones alcanzado ({trades_today}/{max_trades}). "
                f"Ajustalo en Configuracion de operativa, o usa force=True para forzar esta entrada."
            )

    option_details = None
    if asset_type == "option":
        if expiration is None or strike is None or option_type is None:
            raise PaperTradingError("Las opciones requieren expiration, strike y option_type")
        option_details = json.dumps({
            "expiration": expiration,
            "strike": strike,
            "option_type": option_type.lower(),
        })

    key = _position_key(ticker, asset_type, expiration, strike, option_type)
    trade_notional = quantity * price * multiplier

    conn = _connect(db_path)
    try:
        _ensure_positions_columns(conn)
        _ensure_trades_columns(conn)
        account = _get_account(conn)
        pos = conn.execute("SELECT * FROM positions WHERE key = ?", (key,)).fetchone()

        realized_delta = 0.0

        if side == "buy":
            new_cash = account["cash_balance"] - trade_notional
            if new_cash < 0 and not force:
                raise PaperTradingError(
                    f"Fondos virtuales insuficientes: cash actual ${account['cash_balance']:.2f}, "
                    f"coste de la operacion ${trade_notional:.2f}. Usa force=True para forzar."
                )
            if pos is None:
                conn.execute(
                    "INSERT INTO positions (key, ticker, asset_type, option_details, quantity, avg_cost, "
                    "multiplier, opened_at, profit_target_pct, stop_loss_pct, entry_delta, entry_theta, "
                    "entry_strategy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, ticker.upper(), asset_type, option_details, quantity, price, multiplier,
                     _now(), profit_target_pct, stop_loss_pct, entry_delta, entry_theta, entry_strategy),
                )
            else:
                old_qty = pos["quantity"]
                old_avg = pos["avg_cost"]
                new_qty = old_qty + quantity
                new_avg = ((old_qty * old_avg) + (quantity * price)) / new_qty
                conn.execute(
                    "UPDATE positions SET quantity = ?, avg_cost = ? WHERE key = ?",
                    (new_qty, new_avg, key),
                )
            cash_effect = -trade_notional
            new_cash_balance = account["cash_balance"] + cash_effect

        else:  # sell
            if pos is None or pos["quantity"] < quantity:
                held = 0 if pos is None else pos["quantity"]
                if not force:
                    raise PaperTradingError(
                        f"No se puede vender {quantity}: solo hay {held} en posicion simulada "
                        f"(no se permite venta en corto). Usa force=True para forzar."
                    )
            # El multiplicador es una propiedad del instrumento (100 para
            # opciones, 1 para equity/crypto), no una eleccion de quien
            # vende -- se toma siempre de la posicion abierta para blindar
            # contra un --multiplier equivocado u olvidado en la venta
            # (bug real: una venta sin --multiplier 100 dejo el cash y el
            # P&L realizado subvalorados 100x).
            if pos is not None:
                multiplier = pos["multiplier"]
            trade_notional = quantity * price * multiplier
            avg_cost = pos["avg_cost"] if pos is not None else price
            realized_delta = (price - avg_cost) * quantity * multiplier
            cash_effect = trade_notional
            new_cash_balance = account["cash_balance"] + cash_effect

            if pos is not None:
                remaining = pos["quantity"] - quantity
                if remaining <= 1e-12:
                    conn.execute("DELETE FROM positions WHERE key = ?", (key,))
                else:
                    conn.execute(
                        "UPDATE positions SET quantity = ? WHERE key = ?",
                        (remaining, key),
                    )

        now = _now()
        is_buy = side == "buy"
        trade_entry_delta = entry_delta if is_buy else None
        trade_entry_theta = entry_theta if is_buy else None
        trade_entry_signal = entry_signal if is_buy else None
        trade_entry_volatility_strength = entry_volatility_strength if is_buy else None
        trade_entry_band_width_pct = entry_band_width_pct if is_buy else None
        trade_entry_band_width_percentile = entry_band_width_percentile if is_buy else None
        trade_entry_volume_ratio = entry_volume_ratio if is_buy else None
        trade_entry_consecutive_squeeze_bars = entry_consecutive_squeeze_bars if is_buy else None
        trade_entry_strategy = entry_strategy if is_buy else None
        trade_entry_implied_volatility = entry_implied_volatility if is_buy else None
        trade_entry_clv = entry_clv if is_buy else None
        trade_entry_day_pos = entry_day_pos if is_buy else None
        trade_entry_confidence = entry_confidence if is_buy else None
        trade_entry_spread_pct = entry_spread_pct if is_buy else None
        trade_entry_open_interest = entry_open_interest if is_buy else None
        trade_entry_vix = entry_vix if is_buy else None
        conn.execute(
            "INSERT INTO trades (timestamp, ticker, asset_type, side, quantity, price, multiplier, "
            "cash_effect, cash_after, realized_pnl_delta, reason, option_details, entry_delta, entry_theta, "
            "entry_signal, entry_volatility_strength, entry_band_width_pct, "
            "entry_band_width_percentile, entry_volume_ratio, entry_consecutive_squeeze_bars, entry_strategy, "
            "entry_implied_volatility, entry_clv, entry_day_pos, entry_confidence, entry_spread_pct, "
            "entry_open_interest, entry_vix) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, ticker.upper(), asset_type, side, quantity, price, multiplier,
             cash_effect, new_cash_balance, realized_delta, reason.strip(), option_details, trade_entry_delta,
             trade_entry_theta, trade_entry_signal, trade_entry_volatility_strength, trade_entry_band_width_pct,
             trade_entry_band_width_percentile, trade_entry_volume_ratio, trade_entry_consecutive_squeeze_bars,
             trade_entry_strategy, trade_entry_implied_volatility, trade_entry_clv, trade_entry_day_pos,
             trade_entry_confidence, trade_entry_spread_pct, trade_entry_open_interest, trade_entry_vix),
        )
        conn.execute(
            "UPDATE account SET cash_balance = ?, realized_pnl = realized_pnl + ?, updated_at = ? WHERE id = 1",
            (new_cash_balance, realized_delta, now),
        )
        conn.commit()

        return {
            "timestamp": now,
            "ticker": ticker.upper(),
            "asset_type": asset_type,
            "side": side,
            "quantity": quantity,
            "price": price,
            "multiplier": multiplier,
            "cash_effect": cash_effect,
            "cash_after": new_cash_balance,
            "realized_pnl_delta": realized_delta,
            "reason": reason.strip(),
            "entry_delta": trade_entry_delta,
            "entry_theta": trade_entry_theta,
        }
    finally:
        conn.close()


def _ensure_error_log_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            side TEXT,
            ticker TEXT,
            asset_type TEXT,
            quantity REAL,
            price REAL,
            reason TEXT,
            message TEXT NOT NULL
        )
        """
    )


def log_error(
    message: str,
    side: str | None = None,
    ticker: str | None = None,
    asset_type: str | None = None,
    quantity: float | None = None,
    price: float | None = None,
    reason: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Registra un intento de operacion que fue rechazado, y por que."""
    conn = _connect(db_path)
    try:
        _ensure_error_log_table(conn)
        conn.execute(
            "INSERT INTO error_log (timestamp, side, ticker, asset_type, quantity, price, reason, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), side, ticker, asset_type, quantity, price, reason, message),
        )
        conn.commit()
    finally:
        conn.close()


def get_error_log(limit: int = 50, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    conn = _connect(db_path)
    try:
        _ensure_error_log_table(conn)
        rows = conn.execute(
            "SELECT * FROM error_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_trade(
    side: str,
    ticker: str,
    asset_type: str,
    price: float,
    reason: str,
    quantity: float | None = None,
    **kwargs,
) -> dict:
    """Registra una operacion simulada (ver _record_trade_impl para los
    parametros completos). Si se rechaza, el motivo queda guardado en el
    registro de errores (ver get_error_log) antes de propagar la excepcion."""
    try:
        return _record_trade_impl(side=side, ticker=ticker, asset_type=asset_type, price=price,
                                   reason=reason, quantity=quantity, **kwargs)
    except PaperTradingError as e:
        db_path = kwargs.get("db_path", DEFAULT_DB_PATH)
        log_error(
            message=str(e),
            side=side,
            ticker=ticker,
            asset_type=asset_type,
            quantity=quantity,
            price=price,
            reason=reason,
            db_path=db_path,
        )
        raise


def get_position_by_key(key: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    """Busca una posicion abierta por su key (ver _position_key). Usado por
    el cierre manual desde el panel web: identifica exactamente que
    posicion cerrar sin afectar ninguna otra."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM positions WHERE key = ?", (key,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def get_status(current_prices: dict[str, float] | None = None, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Devuelve balance virtual, posiciones abiertas y P&L acumulado.

    current_prices: dict opcional {ticker_o_key: precio_actual} para marcar
    a mercado. Si no se da precio para una posicion, se usa su avg_cost
    (P&L no realizado de esa posicion sale como 0).
    """
    current_prices = current_prices or {}
    conn = _connect(db_path)
    try:
        _ensure_positions_columns(conn)
        account = _get_account(conn)
        positions = conn.execute("SELECT * FROM positions ORDER BY opened_at ASC").fetchall()

        pos_list = []
        total_market_value = 0.0
        total_unrealized_pnl = 0.0
        for p in positions:
            lookup_key = p["ticker"] if p["asset_type"] != "option" else p["key"]
            market_price = current_prices.get(lookup_key, current_prices.get(p["ticker"], p["avg_cost"]))
            market_value = p["quantity"] * market_price * p["multiplier"]
            cost_value = p["quantity"] * p["avg_cost"] * p["multiplier"]
            unrealized = market_value - cost_value
            total_market_value += market_value
            total_unrealized_pnl += unrealized

            entry = {
                "key": p["key"],
                "ticker": p["ticker"],
                "asset_type": p["asset_type"],
                "quantity": p["quantity"],
                "avg_cost": p["avg_cost"],
                "multiplier": p["multiplier"],
                "market_price": market_price,
                "market_value": market_value,
                "unrealized_pnl": unrealized,
                "priced_live": lookup_key in current_prices or p["ticker"] in current_prices,
                "opened_at": p["opened_at"],
                "profit_target_pct": p["profit_target_pct"],
                "stop_loss_pct": p["stop_loss_pct"],
                "entry_delta": p["entry_delta"],
                "entry_theta": p["entry_theta"],
                "entry_strategy": p["entry_strategy"],
            }
            if p["option_details"]:
                entry["option_details"] = json.loads(p["option_details"])
            pos_list.append(entry)

        total_account_value = account["cash_balance"] + total_market_value
        total_pnl = total_account_value - account["initial_balance"]
        unsettled = _unsettled_proceeds(conn)
        cash_balance_available = max(0.0, account["cash_balance"] - unsettled)

        return {
            "initial_balance": account["initial_balance"],
            "cash_balance": account["cash_balance"],
            "cash_balance_available": cash_balance_available,
            "unsettled_cash": unsettled,
            "positions": pos_list,
            "total_market_value": total_market_value,
            "realized_pnl": account["realized_pnl"],
            "unrealized_pnl": total_unrealized_pnl,
            "total_account_value": total_account_value,
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / account["initial_balance"] * 100) if account["initial_balance"] else 0.0,
            "updated_at": account["updated_at"],
        }
    finally:
        conn.close()


def get_account_value_history(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Serie temporal simple del valor de cuenta tras cada operacion.

    Valora las posiciones abiertas a su COSTE MEDIO (no a precio de mercado
    en vivo) en cada punto historico, ya que no guardamos precios de mercado
    pasados. Sirve para una grafica simple de evolucion, no para P&L exacto
    intradia entre operaciones.
    """
    conn = _connect(db_path)
    try:
        account = _get_account(conn)
        trades = conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()

        history = [{"timestamp": account["created_at"], "account_value": account["initial_balance"]}]
        positions: dict[str, dict] = {}

        for t in trades:
            option_details = json.loads(t["option_details"]) if t["option_details"] else None
            key = _position_key(
                t["ticker"], t["asset_type"],
                option_details["expiration"] if option_details else None,
                option_details["strike"] if option_details else None,
                option_details["option_type"] if option_details else None,
            )
            if t["side"] == "buy":
                pos = positions.get(key, {"quantity": 0.0, "avg_cost": 0.0, "multiplier": t["multiplier"]})
                new_qty = pos["quantity"] + t["quantity"]
                new_avg = ((pos["quantity"] * pos["avg_cost"]) + (t["quantity"] * t["price"])) / new_qty
                positions[key] = {"quantity": new_qty, "avg_cost": new_avg, "multiplier": t["multiplier"]}
            else:
                pos = positions.get(key)
                if pos is not None:
                    remaining = pos["quantity"] - t["quantity"]
                    if remaining <= 1e-12:
                        positions.pop(key, None)
                    else:
                        pos["quantity"] = remaining

            positions_value = sum(p["quantity"] * p["avg_cost"] * p["multiplier"] for p in positions.values())
            history.append({"timestamp": t["timestamp"], "account_value": t["cash_after"] + positions_value})

        return history
    finally:
        conn.close()


def get_history(limit: int = 50, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    conn = _connect(db_path)
    try:
        _ensure_trades_columns(conn)
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("option_details"):
                d["option_details"] = json.loads(d["option_details"])
            result.append(d)
        return result
    finally:
        conn.close()


# 2026-09-27, a pedido del usuario ("la idea es poner los tickers que mas
# fluctuan diarios con mayor volatilidad... asi evitamos mas analisis y
# mejores resultados"): rankeado con datos reales (rango alto-bajo %
# promedio de 20 dias habiles via yfinance) -- coincide fuerte con el
# estudio de perdidas del mismo dia: los 15 mas volatiles son casi
# exactamente los mejores tickers historicos (RIOT +350pts, MSTR +182pts,
# HOOD +221pts, SOXL +130pts); los de la lista vieja (COST, MSFT, V, y
# ETFs anchos como SPY/QQQ) eran justo los perdedores netos o de baja
# volatilidad real, poco propensos a una ruptura de bandas genuina.
#
# 2026-09-28, ACTUALIZADO tras el backtest profundo de 207 tickers
# (radar de señales, 60 dias de velas de 15m con la logica real de las 5
# estrategias -- ver memoria): se sacaron los que dieron negativo o casi
# plano en ese backtest (SOXL -0.10%, AMC +0.09%, LCID -1.94% -- el peor
# de los 207 --, SMCI +0.03%, SNAP -0.11%, QCOM -0.08%, SHOP -1.04%) y se
# agregaron los mejores candidatos nuevos que el backtest encontro fuera
# de la lista (MRNA +1.17%, QBTS +0.90%, RGTI +0.85%, GDX +0.62%,
# GDXJ +0.59%, NET +0.56%, ADBE +0.54%, BE +0.54%). RIOT se mantuvo pese a
# salir debil en este backtest (-0.18%) porque su historial REAL de
# operaciones (con filtros y P&L real de opciones) es el mejor de todos
# los tickers (+350pts) -- el dato real pesa mas que el proxy direccional
# sin filtros. Solo afecta cuentas NUEVAS -- las 7 activas se actualizaron
# aparte, directo en su propia columna settings.watchlist.
DEFAULT_WATCHLIST = "SOXS,MARA,RIOT,MSTR,COIN,HOOD,INTC,SOFI,MRNA,QBTS,RGTI,GDX,GDXJ,NET,ADBE,BE"
# 2026-09-28, mismo backtest de 207 tickers, aplicado a la lista de 35
# (a pedido del usuario: "en la lista de 35 cambiaste las que no tenian
# buen resultado" -- se habia limpiado DEFAULT_WATCHLIST pero se habia
# quedado esta sin tocar). Se sacaron 15 con resultado negativo o casi
# plano (SOXL -0.10%, SMCI +0.03%, NIO -0.43%, AMC +0.09%, SNAP -0.11%,
# AAPL -0.04%, LCID -1.94%, MSFT -0.17%, JPM -0.10%, XLF -0.07%,
# SHOP -1.04%, DIS -0.42%, COST -0.04%, QCOM -0.08%, MU -0.09%). Sin
# repetir NINGUN ticker de DEFAULT_WATCHLIST (a pedido del usuario, "no
# repitas los que ya estan por defecto" -- SOXS/MARA/INTC/SOFI/RIOT/MSTR
# se sacaron de aca por estar duplicados con la lista principal) -- se
# agregaron 21 candidatos nuevos sin overlap, todos con buen resultado en
# el mismo backtest (NVDA +0.42%, UPST +0.50%, TEAM +0.48%, ARKK +0.47%,
# ORCL +0.47%, NOW +0.46%, TNA +0.43%, NCLH +0.41%, AAL +0.38%,
# UVXY +0.37%, CRWD +0.33%, OXY +0.33%, ENPH +0.23%, FCEL +0.22%,
# VXX +0.22%, MDB +0.12%, XLY +0.11%, GS +0.10%, TZA +0.10%, FCX +0.08%,
# SPXU +0.07%).
DEFAULT_FAST_WATCHLIST = (
    "PLTR,F,TQQQ,SQQQ,AVGO,NFLX,AMD,AMZN,XLE,XLK,UBER,WMT,CRM,IWM,NVDA,"
    "UPST,TEAM,ARKK,ORCL,NOW,TNA,NCLH,AAL,UVXY,CRWD,OXY,ENPH,FCEL,VXX,"
    "MDB,XLY,GS,TZA,FCX,SPXU"
)


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            contracts_per_trade INTEGER NOT NULL DEFAULT 1,
            max_trades_per_day INTEGER NOT NULL DEFAULT 3,
            watchlist TEXT NOT NULL DEFAULT 'SPY,QQQ,IWM',
            fast_watchlist TEXT NOT NULL DEFAULT '',
            bot_enabled INTEGER NOT NULL DEFAULT 1,
            max_daily_loss_pct REAL NOT NULL DEFAULT 8.0,
            updated_at TEXT NOT NULL
        )
        """
    )
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "watchlist" not in cols:
        conn.execute(f"ALTER TABLE settings ADD COLUMN watchlist TEXT NOT NULL DEFAULT '{DEFAULT_WATCHLIST}'")
    if "fast_watchlist" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN fast_watchlist TEXT NOT NULL DEFAULT ''")
    if "bot_enabled" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN bot_enabled INTEGER NOT NULL DEFAULT 1")
    if "max_daily_loss_pct" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN max_daily_loss_pct REAL NOT NULL DEFAULT 8.0")
    if "risk_pct_per_trade" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN risk_pct_per_trade REAL NOT NULL DEFAULT 2.0")
    if "breaker_override_date" not in cols:
        # 2026-09-18: fecha UTC (YYYY-MM-DD) del dia en que el usuario volvio
        # a encender el bot A MANO despues de un circuit breaker. Ver
        # multi_user_entry._check_circuit_breaker.
        conn.execute("ALTER TABLE settings ADD COLUMN breaker_override_date TEXT")
    if "watchlist_enabled" not in cols:
        # 2026-10-05, a pedido explicito ("que cada usuario elija con
        # cual watchlist trabajar"): cada cuenta tiene DOS listas
        # (watchlist propia + fast_watchlist compartida de ETFs) que
        # antes el motor siempre unia sin que nadie pudiera apagar una
        # sin vaciarla. Ahora cada lista tiene su propio interruptor --
        # ver multi_user_entry.py donde se arma my_watchlist.
        conn.execute("ALTER TABLE settings ADD COLUMN watchlist_enabled INTEGER NOT NULL DEFAULT 1")
    if "fast_watchlist_enabled" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN fast_watchlist_enabled INTEGER NOT NULL DEFAULT 1")
    if "default_capital_list_enabled" not in cols:
        # 2026-10-05, a pedido explicito ("que por defecto salga una lista
        # basada en su capital, con la opcion de usarla o no... si la usa,
        # la watchlist y la lista de ETF de preferencia se desactivan
        # automaticamente hasta que la vuelva a activar"): modo alternativo
        # al par watchlist/fast_watchlist de siempre -- cuando esta
        # ENCENDIDO, el motor arma la lista de escaneo de esta cuenta
        # filtrando TODO el universo por defecto (DEFAULT_WATCHLIST +
        # DEFAULT_FAST_WATCHLIST) por su poder de compra real, en vez de
        # usar sus listas personalizadas (que update_settings() apaga y
        # prende en cascada junto con este interruptor, ver mas abajo).
        conn.execute("ALTER TABLE settings ADD COLUMN default_capital_list_enabled INTEGER NOT NULL DEFAULT 0")
    if "sizing_mode" not in cols:
        # 2026-09-14, a pedido del usuario: modo de dimensionamiento
        # elegible por cuenta -- 'auto' (% de riesgo sobre el valor de
        # la cuenta) o 'manual' (cantidad FIJA de contratos, reactivando
        # el uso original de contracts_per_trade -- ese campo quedo sin
        # uso real cuando se agrego risk_pct_per_trade el 2026-09-13,
        # ver family_sizing.py). DEFAULT 'manual' -- a pedido EXPLICITO
        # del usuario el mismo dia ("eliminalo de la faz de la tierra"
        # sobre el modo automatico): cualquier cuenta NUEVA (familiar
        # recien registrado y activado) tiene que arrancar en manual sin
        # que nadie tenga que abrir Configuracion a mano. El selector de
        # modo automatico ya se saco de la pantalla; esto cierra el
        # ultimo hueco (una cuenta que nunca guarda settings hubiera
        # quedado en 'auto' igual, por el default viejo de la columna).
        conn.execute("ALTER TABLE settings ADD COLUMN sizing_mode TEXT NOT NULL DEFAULT 'manual'")
    row = conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone()
    if row is None:
        # 2026-09-17, a pedido del usuario ("30 operaciones diarias y 1
        # contrato por operacion hasta que ellos lo cambien"): antes 3.
        conn.execute(
            "INSERT INTO settings (id, contracts_per_trade, max_trades_per_day, watchlist, fast_watchlist, bot_enabled, updated_at) "
            "VALUES (1, 1, 30, ?, ?, 1, ?)",
            (DEFAULT_WATCHLIST, DEFAULT_FAST_WATCHLIST, _now()),
        )
        conn.commit()


def _settings_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    raw = d.get("watchlist") or DEFAULT_WATCHLIST
    d["watchlist"] = [t.strip().upper() for t in raw.split(",") if t.strip()]
    raw_fast = d.get("fast_watchlist") or DEFAULT_FAST_WATCHLIST
    d["fast_watchlist"] = [t.strip().upper() for t in raw_fast.split(",") if t.strip()]
    d["bot_enabled"] = bool(d.get("bot_enabled", 1))
    d["watchlist_enabled"] = bool(d.get("watchlist_enabled", 1))
    d["fast_watchlist_enabled"] = bool(d.get("fast_watchlist_enabled", 1))
    d["default_capital_list_enabled"] = bool(d.get("default_capital_list_enabled", 0))
    return d


def get_settings(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Parametros de operativa: sizing_mode ('auto' = contracts_per_trade
    se ignora y la cantidad sale de risk_pct_per_trade; 'manual' =
    contracts_per_trade es la cantidad FIJA a comprar, risk_pct_per_trade
    se ignora -- ver family_sizing.compute_quantity()), limite diario de
    operaciones, watchlist (lista principal de tickers a vigilar/escanear),
    fast_watchlist (segunda lista para entradas rapidas, sin solaparse con
    la principal), bot_enabled (interruptor maestro: si esta apagado, no
    se pueden abrir posiciones nuevas), y max_daily_loss_pct (circuit
    breaker: % de perdida del dia -- realizada + no realizada -- sobre el
    balance inicial a partir del cual position_monitor.py apaga bot_enabled
    solo, ver _check_daily_loss_circuit_breaker())."""
    conn = _connect(db_path)
    try:
        _ensure_settings_table(conn)
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return _settings_row_to_dict(row)
    finally:
        conn.close()


def update_settings(
    contracts_per_trade: int | None = None,
    max_trades_per_day: int | None = None,
    watchlist: list[str] | str | None = None,
    fast_watchlist: list[str] | str | None = None,
    watchlist_enabled: bool | None = None,
    fast_watchlist_enabled: bool | None = None,
    default_capital_list_enabled: bool | None = None,
    bot_enabled: bool | None = None,
    max_daily_loss_pct: float | None = None,
    risk_pct_per_trade: float | None = None,
    sizing_mode: str | None = None,
    breaker_override_today: bool | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    conn = _connect(db_path)
    try:
        _ensure_settings_table(conn)
        current = dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())
        new_contracts = contracts_per_trade if contracts_per_trade is not None else current["contracts_per_trade"]
        new_max_trades = max_trades_per_day if max_trades_per_day is not None else current["max_trades_per_day"]
        if new_contracts < 1 or new_max_trades < 1:
            raise PaperTradingError("contracts_per_trade y max_trades_per_day deben ser >= 1")
        new_max_daily_loss_pct = (
            current["max_daily_loss_pct"] if max_daily_loss_pct is None else float(max_daily_loss_pct)
        )
        if new_max_daily_loss_pct <= 0:
            raise PaperTradingError("max_daily_loss_pct debe ser > 0 (es un limite de PERDIDA, se compara en positivo)")
        new_risk_pct_per_trade = (
            current["risk_pct_per_trade"] if risk_pct_per_trade is None else float(risk_pct_per_trade)
        )
        if not (0.5 <= new_risk_pct_per_trade <= 10.0):
            raise PaperTradingError("risk_pct_per_trade debe estar entre 0.5 y 10.0 (% del valor de la cuenta)")
        new_sizing_mode = current.get("sizing_mode", "auto") if sizing_mode is None else sizing_mode
        if new_sizing_mode not in ("auto", "manual"):
            raise PaperTradingError("sizing_mode debe ser 'auto' o 'manual'")

        def _normalize_list(value, allow_empty):
            if isinstance(value, str):
                tickers = [t.strip().upper() for t in value.split(",") if t.strip()]
            else:
                tickers = [t.strip().upper() for t in value if t.strip()]
            if not tickers and not allow_empty:
                raise PaperTradingError("watchlist no puede quedar vacia")
            return ",".join(tickers)

        new_watchlist = current["watchlist"] if watchlist is None else _normalize_list(watchlist, allow_empty=False)
        new_fast_watchlist = current["fast_watchlist"] if fast_watchlist is None else _normalize_list(fast_watchlist, allow_empty=True)

        new_bot_enabled = current["bot_enabled"] if bot_enabled is None else (1 if bot_enabled else 0)
        new_watchlist_enabled = (
            current.get("watchlist_enabled", 1) if watchlist_enabled is None else (1 if watchlist_enabled else 0)
        )
        new_fast_watchlist_enabled = (
            current.get("fast_watchlist_enabled", 1) if fast_watchlist_enabled is None else (1 if fast_watchlist_enabled else 0)
        )
        new_default_capital_list_enabled = (
            current.get("default_capital_list_enabled", 0) if default_capital_list_enabled is None
            else (1 if default_capital_list_enabled else 0)
        )
        # 2026-10-05, a pedido explicito ("si la watchlist esta activa las
        # otras se desactivan, a no ser que el usuario las active" --
        # confirmado simetrico para las 3: watchlist propia, fast_watchlist/
        # ETF compartida, y la lista automatica por capital): activar
        # CUALQUIERA de las 3 en esta misma llamada apaga las otras 2 por
        # defecto -- el usuario puede volver a prenderlas despues a mano
        # en una llamada posterior, esto no es un bloqueo permanente, solo
        # el comportamiento automatico al activar una.
        if watchlist_enabled is True:
            new_fast_watchlist_enabled = 0
            new_default_capital_list_enabled = 0
        if fast_watchlist_enabled is True:
            new_watchlist_enabled = 0
            new_default_capital_list_enabled = 0
        if default_capital_list_enabled is True:
            new_watchlist_enabled = 0
            new_fast_watchlist_enabled = 0

        if breaker_override_today is None:
            new_override_date = current.get("breaker_override_date")
        else:
            new_override_date = _now()[:10] if breaker_override_today else None

        conn.execute(
            "UPDATE settings SET contracts_per_trade = ?, max_trades_per_day = ?, watchlist = ?, "
            "fast_watchlist = ?, watchlist_enabled = ?, fast_watchlist_enabled = ?, default_capital_list_enabled = ?, "
            "bot_enabled = ?, max_daily_loss_pct = ?, risk_pct_per_trade = ?, sizing_mode = ?, "
            "breaker_override_date = ?, updated_at = ? WHERE id = 1",
            (new_contracts, new_max_trades, new_watchlist, new_fast_watchlist,
             new_watchlist_enabled, new_fast_watchlist_enabled, new_default_capital_list_enabled, new_bot_enabled,
             new_max_daily_loss_pct, new_risk_pct_per_trade, new_sizing_mode, new_override_date, _now()),
        )
        conn.commit()
        return _settings_row_to_dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())
    finally:
        conn.close()


def get_daily_realized_pnl(db_path: Path = DEFAULT_DB_PATH) -> float:
    """Suma de realized_pnl_delta de HOY (fecha UTC, mismo criterio que
    get_trades_count_today) -- P&L ya cerrado/realizado hoy en dolares.
    No incluye P&L no realizado de posiciones que siguen abiertas (eso se
    calcula aparte via get_status(current_prices=...), que necesita
    precios en vivo). Usado por el circuit breaker de perdida diaria en
    position_monitor.py."""
    conn = _connect(db_path)
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_delta), 0) AS total FROM trades WHERE substr(timestamp, 1, 10) = ?",
            (today,),
        ).fetchone()
        return float(row["total"]) if row else 0.0
    finally:
        conn.close()


def get_unsettled_cash_today(db_path: Path = DEFAULT_DB_PATH) -> float:
    """Suma de cash_effect de las VENTAS (side='sell') de HOY (fecha UTC,
    mismo criterio que get_trades_count_today) -- plata que el cash_balance
    ya contabiliza pero que un broker real (ej. Robinhood) todavia no deja
    usar como buying power hasta la liquidacion (T+1: recien disponible en
    la siguiente sesion de bolsa). No usado por defecto en get_status ni en
    la seleccion de contratos de las cuentas familiares normales (ahi el
    cash simulado siempre estuvo disponible al instante, por diseno) --
    existe para que quien necesite simular buying power real de un broker
    (ej. el piloto de dinero real) pueda restarlo del cash_balance antes de
    dimensionar una entrada nueva el mismo dia."""
    conn = _connect(db_path)
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT COALESCE(SUM(cash_effect), 0) AS total FROM trades "
            "WHERE side = 'sell' AND substr(timestamp, 1, 10) = ?",
            (today,),
        ).fetchone()
        return float(row["total"]) if row else 0.0
    finally:
        conn.close()


def get_trades_count_today(db_path: Path = DEFAULT_DB_PATH) -> int:
    """Cuenta las operaciones de APERTURA (compras) registradas hoy (fecha UTC).

    Las salidas (ventas) no cuentan para el limite diario: una vez abierta
    una posicion, siempre se debe poder cerrarla.
    """
    conn = _connect(db_path)
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE side = 'buy' AND substr(timestamp, 1, 10) = ?",
            (today,),
        ).fetchone()
        return row["c"] if row else 0
    finally:
        conn.close()
