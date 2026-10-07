"""Cliente de Tradier para cuentas de opciones.

La URL base y el token salen de variables de entorno, nunca del codigo:
  TRADIER_ENV     -- "sandbox" (default) o "production"
  TRADIER_TOKEN   -- token de API o de sandbox de la cuenta

Uso propio (sin OAuth) esta permitido por Tradier; para cuentas de otros
usuarios hace falta OAuth y aprobacion de socio (ver docs.tradier.com).
"""
import os
from typing import Any

import requests

BASE_URLS = {
    "sandbox": "https://sandbox.tradier.com",
    "production": "https://api.tradier.com",
}
TIMEOUT_SECONDS = 10


class TradierError(RuntimeError):
    pass


class TradierClient:
    def __init__(self, token: str | None = None, env: str | None = None, session: requests.Session | None = None):
        self.env = (env or os.environ.get("TRADIER_ENV", "sandbox")).lower()
        if self.env not in BASE_URLS:
            raise ValueError("TRADIER_ENV debe ser 'sandbox' o 'production'")
        self.token = token or os.environ.get("TRADIER_TOKEN", "")
        if not self.token:
            raise TradierError("Falta TRADIER_TOKEN")
        self.base = BASE_URLS[self.env]
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        r = self.session.get(f"{self.base}{path}", headers=self._headers(), params=params, timeout=TIMEOUT_SECONDS)
        if r.status_code >= 400:
            raise TradierError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def _post(self, path: str, data: dict[str, Any]) -> dict:
        r = self.session.post(f"{self.base}{path}", headers=self._headers(), data=data, timeout=TIMEOUT_SECONDS)
        if r.status_code >= 400:
            raise TradierError(f"POST {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def profile(self) -> dict:
        return self._get("/v1/user/profile")

    def balances(self, account_id: str) -> dict:
        return self._get(f"/v1/accounts/{account_id}/balances")

    def positions(self, account_id: str) -> dict:
        return self._get(f"/v1/accounts/{account_id}/positions")

    def option_quotes(self, option_symbols: list[str]) -> dict:
        return self._get("/v1/markets/quotes", {"symbols": ",".join(option_symbols), "greeks": "false"})

    def order_status(self, account_id: str, order_id: int) -> dict:
        return self._get(f"/v1/accounts/{account_id}/orders/{order_id}")

    def cancel_order(self, account_id: str, order_id: int) -> dict:
        r = self.session.delete(f"{self.base}/v1/accounts/{account_id}/orders/{order_id}",
                                headers=self._headers(), timeout=TIMEOUT_SECONDS)
        if r.status_code >= 400:
            raise TradierError(f"DELETE orders/{order_id} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def timesales(self, symbol: str, start: str, end: str, interval: str = "15min") -> dict:
        """Velas intradia. Tradier solo da 1, 5 y 15 minutos; hasta 40 dias con sesion abierta.

        start/end son OBLIGATORIOS (formato 'YYYY-MM-DD HH:MM'): confirmado en vivo
        2026-10-07 que sin fechas devuelve `series: null` cuando el dia todavia no
        tiene datos (ej. antes de la apertura), no una lista vacia."""
        if interval not in ("1min", "5min", "15min"):
            raise ValueError("interval debe ser 1min, 5min o 15min")
        params = {"symbol": symbol, "interval": interval, "session_filter": "open", "start": start, "end": end}
        return self._get("/v1/markets/timesales", params)

    def option_chain(self, underlying: str, expiration: str, greeks: bool = False) -> dict:
        """Contratos de un vencimiento con bid, ask, volumen e interes abierto."""
        params = {"symbol": underlying, "expiration": expiration}
        if greeks:
            params["greeks"] = "true"
        return self._get("/v1/markets/options/chains", params)

    def place_oco(self, account_id: str, legs: list[dict[str, Any]], duration: str = "gtc",
                 preview: bool = False) -> dict:
        """Orden OCO para salir de una posicion: meta (limit) y stop, una cancela a la otra.

        Formato confirmado en vivo 2026-10-07: NO es legs[i][campo] (eso da
        'Invalid parameter, type: is not valid.' con 400). Tradier usa el indice
        al FINAL del nombre de cada campo: option_symbol[i], side[i], quantity[i],
        type[i], price[i]/stop[i] -- sin 'legs' envolviendo nada."""
        data: dict[str, Any] = {"class": "oco", "duration": duration, "preview": "true" if preview else "false"}
        for i, leg in enumerate(legs):
            for key, value in leg.items():
                if key == "duration":
                    continue  # la duration va una sola vez, a nivel del pedido entero
                data[f"{key}[{i}]"] = value
        return self._post(f"/v1/accounts/{account_id}/orders", data)

    def preview_option_order(self, account_id: str, option_symbol: str, underlying: str, side: str,
                             quantity: int, price: float, duration: str = "day") -> dict:
        """Simula la orden sin enviarla (preview=true)."""
        return self._place(account_id, option_symbol, underlying, side, quantity, price, duration, preview=True)

    def place_option_order(self, account_id: str, option_symbol: str, underlying: str, side: str,
                           quantity: int, price: float, duration: str = "day") -> dict:
        return self._place(account_id, option_symbol, underlying, side, quantity, price, duration, preview=False)

    def _place(self, account_id, option_symbol, underlying, side, quantity, price, duration, preview: bool) -> dict:
        if side not in ("buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"):
            raise ValueError(f"side invalido: {side}")
        if quantity < 1:
            raise ValueError("quantity debe ser al menos 1")
        data = {
            "class": "option", "symbol": underlying, "option_symbol": option_symbol,
            "side": side, "quantity": str(quantity), "type": "limit", "duration": duration,
            "price": f"{price:.2f}", "preview": "true" if preview else "false",
        }
        return self._post(f"/v1/accounts/{account_id}/orders", data)
