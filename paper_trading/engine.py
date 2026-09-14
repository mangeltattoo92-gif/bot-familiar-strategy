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
from datetime import datetime, timezone
from pathlib import Path

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
                entry_theta REAL
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
                    "multiplier, opened_at, profit_target_pct, stop_loss_pct, entry_delta, entry_theta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, ticker.upper(), asset_type, option_details, quantity, price, multiplier,
                     _now(), profit_target_pct, stop_loss_pct, entry_delta, entry_theta),
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
        conn.execute(
            "INSERT INTO trades (timestamp, ticker, asset_type, side, quantity, price, multiplier, "
            "cash_effect, cash_after, realized_pnl_delta, reason, option_details, entry_delta, entry_theta, "
            "entry_signal, entry_volatility_strength, entry_band_width_pct, "
            "entry_band_width_percentile, entry_volume_ratio, entry_consecutive_squeeze_bars, entry_strategy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, ticker.upper(), asset_type, side, quantity, price, multiplier,
             cash_effect, new_cash_balance, realized_delta, reason.strip(), option_details, trade_entry_delta,
             trade_entry_theta, trade_entry_signal, trade_entry_volatility_strength, trade_entry_band_width_pct,
             trade_entry_band_width_percentile, trade_entry_volume_ratio, trade_entry_consecutive_squeeze_bars,
             trade_entry_strategy),
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
            }
            if p["option_details"]:
                entry["option_details"] = json.loads(p["option_details"])
            pos_list.append(entry)

        total_account_value = account["cash_balance"] + total_market_value
        total_pnl = total_account_value - account["initial_balance"]

        return {
            "initial_balance": account["initial_balance"],
            "cash_balance": account["cash_balance"],
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


DEFAULT_WATCHLIST = "GOOGL,META,NVDA,COIN,V,QQQ,HOOD,SPY,TSLA,GLD"
DEFAULT_FAST_WATCHLIST = (
    "SOXL,SOXS,MARA,INTC,SMCI,NIO,AMC,PLTR,SOFI,F,SNAP,TQQQ,RIOT,MSTR,SQQQ,"
    "AAPL,AVGO,NFLX,LCID,AMD,MSFT,AMZN,JPM,XLF,XLE,XLK,UBER,SHOP,DIS,WMT,"
    "COST,CRM,QCOM,MU,IWM"
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
    row = conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO settings (id, contracts_per_trade, max_trades_per_day, watchlist, fast_watchlist, bot_enabled, updated_at) "
            "VALUES (1, 1, 3, ?, ?, 1, ?)",
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
    return d


def get_settings(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Parametros de operativa: contratos por operacion, limite diario de
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
    bot_enabled: bool | None = None,
    max_daily_loss_pct: float | None = None,
    risk_pct_per_trade: float | None = None,
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

        conn.execute(
            "UPDATE settings SET contracts_per_trade = ?, max_trades_per_day = ?, watchlist = ?, "
            "fast_watchlist = ?, bot_enabled = ?, max_daily_loss_pct = ?, risk_pct_per_trade = ?, "
            "updated_at = ? WHERE id = 1",
            (new_contracts, new_max_trades, new_watchlist, new_fast_watchlist, new_bot_enabled,
             new_max_daily_loss_pct, new_risk_pct_per_trade, _now()),
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
