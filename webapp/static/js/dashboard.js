/* ---------- PWA: registra el service worker para que el navegador
   ofrezca "Instalar app" (junto con manifest.json enlazado en el head).
   No cachea nada -- ver la nota en service-worker.js. ---------- */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/static/service-worker.js").catch(() => {});
  });
}

const fmtUsd = (n) => `$${Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const fmtPct = (n) => `${n >= 0 ? "+" : ""}${Number(n).toFixed(2)}%`;
const pnlClass = (n) => (n >= 0 ? "positive" : "negative");
const arrow = (n) => (n >= 0 ? "▲" : "▼");

function callPutBadge(optionType) {
  const isCall = (optionType || "").toLowerCase() === "call";
  const cls = isCall ? "badge-buy" : "badge-sell";
  return `<span class="badge ${cls}">${isCall ? "CALL" : "PUT"}</span>`;
}

let accountValueChart, etfVolumeChart, knownEtfChart, technicalChart;
let lastAccountHistory = [], lastEtfs = [], lastKnownEtfs = [], lastTechnicalSeries = [];
let lastAccountValue = 0;

/* ---------- Theme ---------- */
const root = document.documentElement;
const themeToggle = document.getElementById("theme-toggle");
const iconSun = document.getElementById("icon-sun");
const iconMoon = document.getElementById("icon-moon");

function applyTheme(theme) {
  // Oscuro es el default del sitio (2026-09-13) -- sin preferencia
  // guardada, no dependemos del tema del sistema operativo (la mayoria
  // arranca en claro por defecto, lo que hacia que el panel se viera
  // distinto al mockup oscuro en la primera visita).
  if (theme) {
    root.setAttribute("data-theme", theme);
  } else {
    root.removeAttribute("data-theme");
  }
  const isDark = theme !== "light";
  iconSun.hidden = isDark;
  iconMoon.hidden = !isDark;
  refreshChartThemes();
}

(function initTheme() {
  const saved = localStorage.getItem("mapi-theme");
  applyTheme(saved);
})();

themeToggle.addEventListener("click", () => {
  const current = root.getAttribute("data-theme") || "dark";
  const next = current === "dark" ? "light" : "dark";
  localStorage.setItem("mapi-theme", next);
  applyTheme(next);
});

/* ---------- Top-level view switch (Resumen/Posiciones/Mercado vs Historial) ---------- */
const navLinks = document.querySelectorAll(".topnav a[data-target]");
const mainView = document.getElementById("main-view");
const historialView = document.getElementById("historial");

const navDropdown = document.getElementById("nav-dropdown");
const navDropdownBtn = document.getElementById("nav-dropdown-btn");
const navDropdownMenu = document.getElementById("nav-dropdown-menu");
const navDropdownLabel = document.getElementById("nav-dropdown-label");
const NAV_DROPDOWN_TARGETS = { posiciones: "Posiciones", historial: "Historial", mercado: "Mercado" };
const NAV_DROPDOWN_DEFAULT_LABEL = "Más";

function setNavDropdownOpen(open) {
  navDropdownMenu.classList.toggle("open", open);
  navDropdownBtn.setAttribute("aria-expanded", String(open));
}

function showView(target) {
  const isHistorial = target === "historial";
  mainView.hidden = isHistorial;
  historialView.hidden = !isHistorial;
  navLinks.forEach((link) => link.classList.toggle("active", link.dataset.target === target));

  const inDropdown = target in NAV_DROPDOWN_TARGETS;
  navDropdownBtn.classList.toggle("active", inDropdown);
  navDropdownLabel.textContent = inDropdown ? NAV_DROPDOWN_TARGETS[target] : NAV_DROPDOWN_DEFAULT_LABEL;

  if (!isHistorial) {
    const el = document.getElementById(target);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

navLinks.forEach((link) => {
  link.addEventListener("click", (ev) => {
    ev.preventDefault();
    showView(link.dataset.target);
    setNavDropdownOpen(false);
  });
});

navDropdownBtn.addEventListener("click", (ev) => {
  ev.stopPropagation();
  setNavDropdownOpen(!navDropdownMenu.classList.contains("open"));
});

document.addEventListener("click", (ev) => {
  if (!navDropdown.contains(ev.target)) setNavDropdownOpen(false);
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") setNavDropdownOpen(false);
});

/* ---------- Market analysis tabs ---------- */
const MARKET_TAB_KEY = "mapi-market-tab";
const tabButtons = document.querySelectorAll(".tab-btn");
const tabPanes = document.querySelectorAll(".tab-pane");

function setActiveTab(tabId) {
  tabButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabId));
  tabPanes.forEach((pane) => pane.classList.toggle("active", pane.id === tabId));
  localStorage.setItem(MARKET_TAB_KEY, tabId);
}

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
});

setActiveTab(localStorage.getItem(MARKET_TAB_KEY) || "tab-tecnico");

/* ---------- Collapsible account value panel ---------- */
const ACCOUNT_PANEL_COLLAPSED_KEY = "mapi-account-panel-collapsed";
const accountChartBox = document.getElementById("account-chart-box");
const accountChartToggle = document.getElementById("account-chart-toggle");

function setAccountPanelCollapsed(collapsed) {
  accountChartBox.classList.toggle("collapsed", collapsed);
  accountChartToggle.classList.toggle("collapsed", collapsed);
  accountChartToggle.setAttribute("aria-expanded", String(!collapsed));
  accountChartToggle.title = collapsed ? "Maximizar" : "Minimizar";
  localStorage.setItem(ACCOUNT_PANEL_COLLAPSED_KEY, collapsed ? "1" : "0");
}

setAccountPanelCollapsed(localStorage.getItem(ACCOUNT_PANEL_COLLAPSED_KEY) === "1");

accountChartToggle.addEventListener("click", () => {
  setAccountPanelCollapsed(!accountChartBox.classList.contains("collapsed"));
});

/* ---------- Collapsible watchlist panel ---------- */
const WATCHLIST_PANEL_COLLAPSED_KEY = "mapi-watchlist-panel-collapsed";
const watchlistBox = document.getElementById("watchlist-box");
const watchlistToggle = document.getElementById("watchlist-toggle");
let watchlistLoaded = false;

function setWatchlistPanelCollapsed(collapsed) {
  watchlistBox.classList.toggle("collapsed", collapsed);
  watchlistToggle.classList.toggle("collapsed", collapsed);
  watchlistToggle.setAttribute("aria-expanded", String(!collapsed));
  watchlistToggle.title = collapsed ? "Expandir" : "Minimizar";
  localStorage.setItem(WATCHLIST_PANEL_COLLAPSED_KEY, collapsed ? "1" : "0");
  if (!collapsed && !watchlistLoaded) {
    watchlistLoaded = true;
    refreshWatchlist();
  }
}

setWatchlistPanelCollapsed(localStorage.getItem(WATCHLIST_PANEL_COLLAPSED_KEY) !== "0");

watchlistToggle.addEventListener("click", () => {
  setWatchlistPanelCollapsed(!watchlistBox.classList.contains("collapsed"));
});

/* ---------- Collapsible fast-watchlist panel ---------- */
const FAST_WATCHLIST_COLLAPSED_KEY = "mapi-fast-watchlist-panel-collapsed";
const fastWatchlistBox = document.getElementById("fast-watchlist-box");
const fastWatchlistToggle = document.getElementById("fast-watchlist-toggle");
let fastWatchlistLoaded = false;

function setFastWatchlistPanelCollapsed(collapsed) {
  fastWatchlistBox.classList.toggle("collapsed", collapsed);
  fastWatchlistToggle.classList.toggle("collapsed", collapsed);
  fastWatchlistToggle.setAttribute("aria-expanded", String(!collapsed));
  fastWatchlistToggle.title = collapsed ? "Expandir" : "Minimizar";
  localStorage.setItem(FAST_WATCHLIST_COLLAPSED_KEY, collapsed ? "1" : "0");
  if (!collapsed && !fastWatchlistLoaded) {
    fastWatchlistLoaded = true;
    refreshFastWatchlist();
  }
}

setFastWatchlistPanelCollapsed(localStorage.getItem(FAST_WATCHLIST_COLLAPSED_KEY) !== "0");

fastWatchlistToggle.addEventListener("click", () => {
  setFastWatchlistPanelCollapsed(!fastWatchlistBox.classList.contains("collapsed"));
});

/* ---------- Collapsible mercado panel ---------- */
const MERCADO_COLLAPSED_KEY = "mapi-mercado-panel-collapsed";
const mercadoBox = document.getElementById("mercado-box");
const mercadoToggle = document.getElementById("mercado-toggle");

function setMercadoCollapsed(collapsed) {
  mercadoBox.classList.toggle("collapsed", collapsed);
  mercadoToggle.classList.toggle("collapsed", collapsed);
  mercadoToggle.setAttribute("aria-expanded", String(!collapsed));
  mercadoToggle.title = collapsed ? "Expandir" : "Minimizar";
  localStorage.setItem(MERCADO_COLLAPSED_KEY, collapsed ? "1" : "0");
}

setMercadoCollapsed(localStorage.getItem(MERCADO_COLLAPSED_KEY) !== "0");

mercadoToggle.addEventListener("click", () => {
  setMercadoCollapsed(!mercadoBox.classList.contains("collapsed"));
});

/* ---------- Collapsible error log panel ---------- */
const ERROR_LOG_COLLAPSED_KEY = "mapi-error-log-collapsed";
const errorLogBox = document.getElementById("error-log-box");
const errorLogToggle = document.getElementById("error-log-toggle");
let errorLogLoaded = false;

function setErrorLogCollapsed(collapsed) {
  errorLogBox.classList.toggle("collapsed", collapsed);
  errorLogToggle.classList.toggle("collapsed", collapsed);
  errorLogToggle.setAttribute("aria-expanded", String(!collapsed));
  errorLogToggle.title = collapsed ? "Expandir" : "Minimizar";
  localStorage.setItem(ERROR_LOG_COLLAPSED_KEY, collapsed ? "1" : "0");
  if (!collapsed && !errorLogLoaded) {
    errorLogLoaded = true;
    refreshErrorLog();
  }
}

setErrorLogCollapsed(localStorage.getItem(ERROR_LOG_COLLAPSED_KEY) !== "0");

errorLogToggle.addEventListener("click", () => {
  setErrorLogCollapsed(!errorLogBox.classList.contains("collapsed"));
});

/* ---------- Cierre manual individual de una posicion ---------- */
document.querySelector("#positions-table tbody").addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".btn-close-position");
  if (!btn) return;
  const key = btn.dataset.key;
  if (!key) return;
  if (!confirm("¿Cerrar esta posicion ahora al precio actual? Solo afecta esta posicion, ninguna otra.")) return;

  btn.disabled = true;
  btn.textContent = "Cerrando…";
  try {
    const res = await fetch("/api/positions/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.error || "No se pudo cerrar la posicion.");
      btn.disabled = false;
      btn.textContent = "Cerrar";
      return;
    }
    refreshStatusAndHistory();
  } catch (e) {
    alert("Error de red al intentar cerrar la posicion.");
    btn.disabled = false;
    btn.textContent = "Cerrar";
  }
});

function renderErrorLog(rows) {
  document.getElementById("error-log-count").textContent = `(${rows.length})`;
  document.getElementById("error-log-empty").hidden = rows.length > 0;

  const tbody = document.querySelector("#error-log-table tbody");
  tbody.innerHTML = "";
  for (const e of rows) {
    const dt = new Date(e.timestamp);
    const fecha = `${dt.toLocaleDateString("es-ES")} ${dt.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" })}`;
    const sideLabel = e.side === "buy" ? "Compra" : e.side === "sell" ? "Venta" : (e.side || "—");
    const intento = e.ticker ? `${sideLabel} ${e.quantity ?? ""} ${e.ticker} @ ${e.price != null ? fmtUsd(e.price) : "—"}` : sideLabel;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fecha}</td>
      <td>${intento}</td>
      <td class="negative">${e.message}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function refreshErrorLog() {
  const data = await fetchJSON("/api/error-log?limit=50");
  if (data) renderErrorLog(data);
}

function rankByProfitabilityAndVolatility(rows) {
  const byProfit = [...rows].sort((a, b) => b.day_change_pct - a.day_change_pct);
  const byVolatility = [...rows].sort((a, b) => b.avg_daily_range_pct - a.avg_daily_range_pct);
  const profitRank = new Map(byProfit.map((r, i) => [r.symbol, i]));
  const volatilityRank = new Map(byVolatility.map((r, i) => [r.symbol, i]));
  return [...rows].sort((a, b) => {
    const scoreA = profitRank.get(a.symbol) + volatilityRank.get(a.symbol);
    const scoreB = profitRank.get(b.symbol) + volatilityRank.get(b.symbol);
    return scoreA - scoreB;
  });
}

function renderWatchlistInto(rows, prefix) {
  rows = rankByProfitabilityAndVolatility(rows);
  document.getElementById(`${prefix}-count`).textContent = `(${rows.length})`;
  document.getElementById(`${prefix}-updated`).textContent =
    `Actualizado ${new Date().toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" })}`;

  const tbody = document.querySelector(`#${prefix}-table tbody`);
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${r.symbol}</strong></td>
      <td>${fmtUsd(r.last_close)}</td>
      <td class="${pnlClass(r.day_change_pct)}">${arrow(r.day_change_pct)} ${fmtPct(r.day_change_pct)}</td>
      <td class="${pnlClass(r.week_change_pct)}">${fmtPct(r.week_change_pct)}</td>
      <td>${r.avg_daily_range_pct.toFixed(2)}%</td>
      <td>${r.avg_volume.toLocaleString("en-US")}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderWatchlist(rows) {
  renderWatchlistInto(rows, "watchlist");
}

async function refreshWatchlist() {
  const data = await fetchJSON("/api/watchlist");
  if (data) renderWatchlist(data);
}

function renderFastWatchlist(rows) {
  renderWatchlistInto(rows, "fast-watchlist");
}

async function refreshFastWatchlist() {
  const data = await fetchJSON("/api/fast-watchlist");
  if (data) renderFastWatchlist(data);
}

/* ---------- Settings modal (contratos por operacion / limite diario) ---------- */
const settingsBtn = document.getElementById("settings-btn");
const settingsModal = document.getElementById("settings-modal");
const settingsClose = document.getElementById("settings-close");
const settingsSave = document.getElementById("settings-save");
const settingsRiskPctInput = document.getElementById("setting-risk-pct");
const riskPctDisplay = document.getElementById("risk-pct-display");
const riskLevelTag = document.getElementById("risk-level-tag");
const riskAmountDisplay = document.getElementById("risk-amount-display");
const settingsMaxTradesInput = document.getElementById("setting-max-trades");
const settingsWatchlistInput = document.getElementById("setting-watchlist");
const settingsMaxDailyLossInput = document.getElementById("setting-max-daily-loss");
const settingsCounter = document.getElementById("settings-counter");
const settingsError = document.getElementById("settings-error");

function riskLevelFor(pct) {
  if (pct <= 2) return { label: "Conservador", cls: "conservador" };
  if (pct <= 4.5) return { label: "Moderado", cls: "moderado" };
  if (pct <= 7.5) return { label: "Agresivo", cls: "agresivo" };
  return { label: "Muy agresivo", cls: "muy-agresivo" };
}

function updateRiskSliderDisplay() {
  const pct = parseFloat(settingsRiskPctInput.value);
  const fillPct = ((pct - 0.5) / (10 - 0.5)) * 100;
  settingsRiskPctInput.style.setProperty("--fill", `${fillPct}%`);
  riskPctDisplay.textContent = `${pct.toFixed(1)}%`;
  const level = riskLevelFor(pct);
  riskLevelTag.textContent = level.label;
  riskLevelTag.className = `risk-level-tag ${level.cls}`;
  riskAmountDisplay.textContent = fmtUsd(lastAccountValue * pct / 100);
}
settingsRiskPctInput.addEventListener("input", updateRiskSliderDisplay);

function renderSettings(data) {
  settingsRiskPctInput.value = data.risk_pct_per_trade;
  settingsMaxTradesInput.value = data.max_trades_per_day;
  settingsWatchlistInput.value = (data.watchlist || []).join(",");
  settingsMaxDailyLossInput.value = data.max_daily_loss_pct;
  settingsCounter.textContent = `Operaciones hoy: ${data.trades_today} / ${data.max_trades_per_day}`;
  updateRiskSliderDisplay();
}

async function openSettingsModal() {
  settingsError.hidden = true;
  const data = await fetchJSON("/api/settings");
  if (data) renderSettings(data);
  settingsModal.hidden = false;
}

function closeSettingsModal() {
  settingsModal.hidden = true;
}

/* ---------- Dinero real (banner + modal informativo, sin funcionalidad
   real todavia) ---------- */
const realMoneyModal = document.getElementById("real-money-modal");
const realMoneyOpenBtn = document.getElementById("real-money-open-btn");
const realMoneyNavLink = document.getElementById("real-money-nav-link");
const realMoneyClose = document.getElementById("real-money-close");
const realMoneyOk = document.getElementById("real-money-ok");
function openRealMoneyModal(e) {
  if (e) e.preventDefault();
  realMoneyModal.hidden = false;
}
function closeRealMoneyModal() {
  realMoneyModal.hidden = true;
}
if (realMoneyOpenBtn) realMoneyOpenBtn.addEventListener("click", openRealMoneyModal);
if (realMoneyNavLink) realMoneyNavLink.addEventListener("click", openRealMoneyModal);
if (realMoneyClose) realMoneyClose.addEventListener("click", closeRealMoneyModal);
if (realMoneyOk) realMoneyOk.addEventListener("click", closeRealMoneyModal);

const robinhoodStatusConnected = document.getElementById("robinhood-status-connected");
const robinhoodStatusPending = document.getElementById("robinhood-status-pending");
const robinhoodMarkBtn = document.getElementById("robinhood-mark-btn");
const robinhoodUnmarkBtn = document.getElementById("robinhood-unmark-btn");

async function setRobinhoodConnected(connected) {
  const res = await fetch("/api/robinhood-connected", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connected }),
  });
  const data = await res.json();
  if (data.ok) {
    robinhoodStatusConnected.hidden = !data.connected;
    robinhoodStatusPending.hidden = data.connected;
  }
}
if (robinhoodMarkBtn) robinhoodMarkBtn.addEventListener("click", () => setRobinhoodConnected(true));
if (robinhoodUnmarkBtn) robinhoodUnmarkBtn.addEventListener("click", () => setRobinhoodConnected(false));

settingsBtn.addEventListener("click", openSettingsModal);
settingsClose.addEventListener("click", closeSettingsModal);
settingsSave.addEventListener("click", async () => {
  settingsError.hidden = true;
  const riskPct = parseFloat(settingsRiskPctInput.value);
  const maxTrades = parseInt(settingsMaxTradesInput.value, 10);
  const watchlist = settingsWatchlistInput.value.trim();
  const maxDailyLoss = parseFloat(settingsMaxDailyLossInput.value);
  if (!Number.isFinite(riskPct) || riskPct < 0.5 || riskPct > 10) {
    settingsError.textContent = "El riesgo por operacion debe estar entre 0.5% y 10%.";
    settingsError.hidden = false;
    return;
  }
  if (!Number.isInteger(maxTrades) || maxTrades < 1) {
    settingsError.textContent = "Las operaciones maximas por dia deben ser un numero entero de al menos 1.";
    settingsError.hidden = false;
    return;
  }
  if (!watchlist) {
    settingsError.textContent = "La watchlist no puede quedar vacia.";
    settingsError.hidden = false;
    return;
  }
  if (!Number.isFinite(maxDailyLoss) || maxDailyLoss <= 0) {
    settingsError.textContent = "El limite de perdida diaria debe ser un numero mayor a 0.";
    settingsError.hidden = false;
    return;
  }
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        risk_pct_per_trade: riskPct, max_trades_per_day: maxTrades, watchlist,
        max_daily_loss_pct: maxDailyLoss,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      settingsError.textContent = data.error || "No se pudo guardar la configuracion.";
      settingsError.hidden = false;
      return;
    }
    renderSettings(data);
    closeSettingsModal();
  } catch (err) {
    settingsError.textContent = "Error de conexion al guardar.";
    settingsError.hidden = false;
  }
});

function chartColors() {
  const styles = getComputedStyle(root);
  return {
    text: styles.getPropertyValue("--muted").trim(),
    grid: styles.getPropertyValue("--border").trim(),
    accent: styles.getPropertyValue("--accent").trim(),
    accentSoft: styles.getPropertyValue("--accent-soft").trim(),
    green: styles.getPropertyValue("--green").trim(),
    red: styles.getPropertyValue("--red").trim(),
    font: "Inter, sans-serif",
  };
}

function refreshChartThemes() {
  if (lastAccountHistory.length) renderAccountValueChart(lastAccountHistory);
  if (lastEtfs.length) renderEtfVolumeChart(lastEtfs.map((e) => e.label), lastEtfs.map((e) => e.value));
  if (lastKnownEtfs.length) renderKnownEtfChart(lastKnownEtfs.map((e) => e.label), lastKnownEtfs.map((e) => e.value));
  if (lastTechnicalSeries.length) renderTechnicalChart(lastTechnicalSeries);
}

/* ---------- Data fetch ---------- */
async function fetchJSON(url) {
  const res = await fetch(url);
  if (res.status === 401 || res.redirected) {
    window.location.href = "/login";
    return null;
  }
  return res.json();
}

function setLastUpdated(elId, isoString) {
  const el = document.getElementById(elId);
  if (!el) return;
  const dt = new Date(isoString);
  el.textContent = `Actualizado ${dt.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" })}`;
}

/* ---------- Renderers ---------- */
function renderStatus(status) {
  lastAccountValue = status.total_account_value;
  document.getElementById("cash-balance").textContent = fmtUsd(status.cash_balance);
  document.getElementById("total-value").textContent = fmtUsd(status.total_account_value);

  const pnlEl = document.getElementById("total-pnl");
  pnlEl.textContent = `${arrow(status.total_pnl)} ${fmtUsd(Math.abs(status.total_pnl))}`;
  pnlEl.className = `card-value ${pnlClass(status.total_pnl)}`;

  const pnlPctEl = document.getElementById("total-pnl-pct");
  pnlPctEl.textContent = `${fmtPct(status.total_pnl_pct)} desde el inicio`;
  pnlPctEl.className = `card-sub ${pnlClass(status.total_pnl_pct)}`;

  const breakdown = document.getElementById("pnl-breakdown");
  breakdown.textContent = `Realizado ${fmtUsd(status.realized_pnl)} · No realizado ${fmtUsd(status.unrealized_pnl)}`;

  const tbody = document.querySelector("#positions-table tbody");
  tbody.innerHTML = "";
  const emptyMsg = document.getElementById("positions-empty");
  const closedToday = status.closed_today || [];
  emptyMsg.hidden = status.positions.length > 0 || closedToday.length > 0;

  for (const p of status.positions) {
    const isOption = p.asset_type === "option";
    const label = isOption ? `${p.ticker} $${p.option_details.strike}` : p.ticker;
    const tr = document.createElement("tr");
    const contractBadge = isOption ? callPutBadge(p.option_details.option_type) : `<span class="badge badge-type">${p.asset_type}</span>`;
    const pnlPct = p.avg_cost ? (p.market_price / p.avg_cost - 1) * 100 : 0;
    tr.innerHTML = `
      <td><strong>${label}</strong><br>${contractBadge}</td>
      <td data-label="Vencimiento">${isOption ? p.option_details.expiration : "—"}</td>
      <td data-label="Cantidad">${p.quantity}</td>
      <td data-label="Valor">${fmtUsd(p.market_value)}</td>
      <td data-label="Ganancia/Pérdida" class="${pnlClass(p.unrealized_pnl)}">${arrow(p.unrealized_pnl)} ${fmtUsd(Math.abs(p.unrealized_pnl))} (${fmtPct(pnlPct)})</td>
      <td data-label="Precio entrada" class="small">${fmtUsd(p.avg_cost)}</td>
      <td data-label="Precio actual" class="small">${fmtUsd(p.market_price)}${p.priced_live ? "" : " *"}</td>
      <td data-label="Estado"><span class="badge badge-buy">Abierta</span></td>
      <td class="td-action"><button class="btn-close-position" data-key="${p.key}">Cerrar</button></td>
    `;
    tbody.appendChild(tr);
  }

  for (const t of closedToday) {
    const isOption = t.asset_type === "option";
    const label = isOption ? `${t.ticker} $${t.option_details.strike}` : t.ticker;
    const contractBadge = isOption ? callPutBadge(t.option_details.option_type) : `<span class="badge badge-type">${t.asset_type}</span>`;
    const value = t.exit_price * t.quantity * t.multiplier;
    const tr = document.createElement("tr");
    tr.className = "closed-row";
    tr.innerHTML = `
      <td><strong>${label}</strong><br>${contractBadge}</td>
      <td data-label="Vencimiento">${isOption ? t.option_details.expiration : "—"}</td>
      <td data-label="Cantidad">${t.quantity}</td>
      <td data-label="Valor">${fmtUsd(value)}</td>
      <td data-label="Ganancia/Pérdida" class="${pnlClass(t.pnl)}">${arrow(t.pnl)} ${fmtUsd(Math.abs(t.pnl))} (${fmtPct(t.pnl_pct)})</td>
      <td data-label="Precio entrada" class="small">${fmtUsd(t.entry_price)}</td>
      <td data-label="Precio salida" class="small">${fmtUsd(t.exit_price)} <span class="muted small">(salida)</span></td>
      <td data-label="Estado"><span class="badge badge-sell">Cerrada</span></td>
      <td class="td-action"></td>
    `;
    tbody.appendChild(tr);
  }

  setLastUpdated("last-updated", status.updated_at);
}

function _fmtDateTime(iso) {
  const dt = new Date(iso);
  return `${dt.toLocaleDateString("es-ES")} ${dt.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" })}`;
}

function renderHistory(trades) {
  const tbody = document.querySelector("#history-table tbody");
  tbody.innerHTML = "";
  const emptyMsg = document.getElementById("history-empty");
  emptyMsg.hidden = trades.length > 0;

  for (const t of trades) {
    const isOption = t.asset_type === "option";
    const contrato = isOption ? `${t.ticker} $${t.option_details.strike}` : t.ticker;
    const contractBadge = isOption ? callPutBadge(t.option_details.option_type) : `<span class="badge badge-type">${t.asset_type}</span>`;
    const value = t.exit_price * t.quantity * t.multiplier;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${contrato}</strong><br>${contractBadge}</td>
      <td data-label="Vencimiento">${isOption ? t.option_details.expiration : "—"}</td>
      <td data-label="Cantidad" class="small">${t.quantity}</td>
      <td data-label="Valor">${fmtUsd(value)}</td>
      <td data-label="Ganancia/Pérdida" class="${pnlClass(t.pnl)}">${arrow(t.pnl)} ${fmtUsd(Math.abs(t.pnl))} (${fmtPct(t.pnl_pct)})</td>
      <td data-label="Precio entrada" class="small">${fmtUsd(t.entry_price)}</td>
      <td data-label="Precio salida" class="small">${fmtUsd(t.exit_price)}</td>
      <td data-label="Abierta" class="muted small">${_fmtDateTime(t.opened_at)}</td>
      <td data-label="Cerrada" class="muted small">${_fmtDateTime(t.closed_at)}</td>
      <td data-label="Razón" class="muted">${t.entry_reason || ""}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderMarketAnalysis(data) {
  const summaryEl = document.getElementById("market-summary");
  summaryEl.innerHTML = "";

  const tiles = document.createElement("div");
  tiles.className = "market-summary";
  const idx = data.weekly_summary.indexes || {};
  for (const [name, info] of Object.entries(idx)) {
    const tile = document.createElement("div");
    tile.className = "market-tile";
    tile.innerHTML = `
      <div class="name">${name}</div>
      <div class="value ${pnlClass(info.week_change_pct)}">${arrow(info.week_change_pct)} ${fmtPct(info.week_change_pct)}</div>
    `;
    tiles.appendChild(tile);
  }
  summaryEl.appendChild(tiles);

  const tone = document.createElement("div");
  tone.className = "market-tone";
  tone.textContent = `Tendencia semanal general: ${data.weekly_summary.tone} (media índices ${fmtPct(data.weekly_summary.avg_change_pct)})`;
  summaryEl.appendChild(tone);

  const tbody = document.querySelector("#etf-table tbody");
  tbody.innerHTML = "";
  lastEtfs = [];
  for (const etf of data.top_etfs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${etf.symbol}</strong></td>
      <td>${fmtUsd(etf.last_close)}</td>
      <td class="${pnlClass(etf.week_change_pct)}">${fmtPct(etf.week_change_pct)}</td>
      <td>${etf.avg_daily_range_pct.toFixed(2)}%</td>
      <td>${etf.avg_volume.toLocaleString("en-US")}</td>
    `;
    tbody.appendChild(tr);
    lastEtfs.push({ label: etf.symbol, value: etf.avg_volume });
  }
  renderEtfVolumeChart(lastEtfs.map((e) => e.label), lastEtfs.map((e) => e.value));

  renderKnownEtfsTable(data.known_etfs || []);

  setLastUpdated("market-updated", data.weekly_summary.generated_at);
}

const TF_LABELS = { hourly: "1 HORA", daily: "1 DÍA", weekly: "1 SEMANA" };
const TREND_LABELS = { alcista: "Alcista", bajista: "Bajista", lateral: "Lateral" };

/* ---------- Technical outlook: symbol picker ---------- */
const TECH_SYMBOL_KEY = "mapi-technical-symbol";
const techSelect = document.getElementById("technical-symbol-select");
const techCustomInput = document.getElementById("technical-symbol-custom");
const techCustomApply = document.getElementById("technical-symbol-apply");
let currentTechSymbol = localStorage.getItem(TECH_SYMBOL_KEY) || "SPY";

function isKnownTechOption(symbol) {
  return Array.from(techSelect.options).some((o) => o.value === symbol);
}

function setTechControlsForSymbol(symbol) {
  if (isKnownTechOption(symbol)) {
    techSelect.value = symbol;
    techCustomInput.hidden = true;
    techCustomApply.hidden = true;
  } else {
    techSelect.value = "__custom__";
    techCustomInput.value = symbol;
    techCustomInput.hidden = false;
    techCustomApply.hidden = false;
  }
}

async function loadTechnicalOutlook(symbol) {
  const biasBox = document.getElementById("tech-bias");
  const data = await fetchJSON(`/api/technical-outlook?symbol=${encodeURIComponent(symbol)}`);
  if (!data) return;
  if (data.error) {
    biasBox.innerHTML = `<span class="bias-tag bias-mixto">N/D</span><span>${data.error}</span>`;
    document.getElementById("tech-grid").innerHTML = "";
    if (technicalChart) { technicalChart.destroy(); technicalChart = null; }
    return;
  }
  currentTechSymbol = symbol;
  localStorage.setItem(TECH_SYMBOL_KEY, symbol);
  renderTechnicalOutlook(data);
}

techSelect.addEventListener("change", () => {
  if (techSelect.value === "__custom__") {
    techCustomInput.hidden = false;
    techCustomApply.hidden = false;
    techCustomInput.focus();
  } else {
    techCustomInput.hidden = true;
    techCustomApply.hidden = true;
    loadTechnicalOutlook(techSelect.value);
  }
});

techCustomApply.addEventListener("click", () => {
  const symbol = techCustomInput.value.trim().toUpperCase();
  if (symbol) loadTechnicalOutlook(symbol);
});

techCustomInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    ev.preventDefault();
    techCustomApply.click();
  }
});

function renderTechnicalOutlook(outlook) {
  const biasBox = document.getElementById("tech-bias");
  const biasKey = outlook.bias.startsWith("alcista") ? "alcista" : outlook.bias.startsWith("bajista") ? "bajista" : "mixto";
  const biasText = {
    alcista: "Sesgo alcista: la mayoría de plazos apuntan al alza para la próxima semana.",
    bajista: "Sesgo bajista: la mayoría de plazos apuntan a la baja para la próxima semana.",
    mixto: "Sesgo mixto/lateral: señales encontradas entre plazos, probable rango la próxima semana.",
  }[biasKey];
  biasBox.innerHTML = `<span class="bias-tag bias-${biasKey}">${outlook.bias}</span><span>${biasText} (${outlook.symbol})</span>`;

  const grid = document.getElementById("tech-grid");
  grid.innerHTML = "";
  for (const key of ["hourly", "daily", "weekly"]) {
    const tf = outlook.timeframes[key];
    if (!tf) continue;
    const trendClass = pnlClass(tf.trend === "alcista" ? 1 : tf.trend === "bajista" ? -1 : 0);
    const card = document.createElement("div");
    card.className = "tech-card";
    card.innerHTML = `
      <div class="tf-label"><span>${TF_LABELS[key]}</span><span>RSI ${tf.rsi}</span></div>
      <div class="tf-trend ${tf.trend === "lateral" ? "" : trendClass}">${TREND_LABELS[tf.trend]}</div>
      <div class="tf-detail"><span>Precio</span><span>${fmtUsd(tf.last_close)}</span></div>
      <div class="tf-detail"><span>SMA ${tf.sma_fast_window}</span><span>${fmtUsd(tf.sma_fast)}</span></div>
      <div class="tf-detail"><span>SMA ${tf.sma_slow_window}</span><span>${fmtUsd(tf.sma_slow)}</span></div>
      <div class="tf-detail"><span>MACD</span><span>${tf.macd_signal === "alcista" ? "Alcista" : "Bajista"}</span></div>
      <div class="tf-detail"><span>Momentum</span><span>${tf.momentum}</span></div>
    `;
    grid.appendChild(card);
  }

  lastTechnicalSeries = outlook.chart_series || [];
  renderTechnicalChart(lastTechnicalSeries);
}

function renderKnownEtfsTable(etfs) {
  const tbody = document.querySelector("#known-etf-table tbody");
  tbody.innerHTML = "";
  lastKnownEtfs = [];

  for (const etf of etfs) {
    const futuresCell = etf.next_week_change_pct === null
      ? '<span class="na">— sin futuro</span>'
      : `<span class="${pnlClass(etf.next_week_change_pct)}">${arrow(etf.next_week_change_pct)} ${fmtPct(etf.next_week_change_pct)}</span>`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${etf.symbol}</strong></td>
      <td class="muted">${etf.name}</td>
      <td>${fmtUsd(etf.week_close)}</td>
      <td class="${pnlClass(etf.week_change_pct)}">${arrow(etf.week_change_pct)} ${fmtPct(etf.week_change_pct)}</td>
      <td>${futuresCell}</td>
    `;
    tbody.appendChild(tr);
    lastKnownEtfs.push({ label: etf.symbol, value: etf.week_change_pct });
  }
  renderKnownEtfChart(lastKnownEtfs.map((e) => e.label), lastKnownEtfs.map((e) => e.value));
}

/* ---------- Charts ---------- */
function renderAccountValueChart(history) {
  lastAccountHistory = history;
  const c = chartColors();
  const labels = history.map((h) => new Date(h.timestamp).toLocaleDateString("es-ES", { day: "2-digit", month: "short" }));
  const values = history.map((h) => h.account_value);
  const ctx = document.getElementById("chart-account-value");

  if (accountValueChart) accountValueChart.destroy();
  accountValueChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Valor de cuenta",
        data: values,
        borderColor: c.accent,
        backgroundColor: c.accentSoft,
        borderWidth: 2,
        fill: true,
        tension: 0.35,
        pointRadius: 0,
        pointHoverRadius: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: (item) => fmtUsd(item.parsed.y) },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: c.text, font: { family: c.font, size: 11 } } },
        y: {
          grid: { color: c.grid },
          ticks: { color: c.text, font: { family: c.font, size: 11 }, callback: (v) => `$${v}` },
        },
      },
    },
  });
}

function renderTechnicalChart(series) {
  const c = chartColors();
  const ctx = document.getElementById("chart-technical");
  if (technicalChart) technicalChart.destroy();
  if (!series.length) return;

  const labels = series.map((p) => new Date(p.date).toLocaleDateString("es-ES", { day: "2-digit", month: "short" }));
  technicalChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Precio",
          data: series.map((p) => p.close),
          borderColor: c.accent,
          backgroundColor: "transparent",
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.15,
        },
        {
          label: "SMA rápida",
          data: series.map((p) => p.sma_fast),
          borderColor: c.green,
          backgroundColor: "transparent",
          borderWidth: 1.5,
          pointRadius: 0,
          borderDash: [4, 3],
          tension: 0.15,
        },
        {
          label: "SMA lenta",
          data: series.map((p) => p.sma_slow),
          borderColor: c.red,
          backgroundColor: "transparent",
          borderWidth: 1.5,
          pointRadius: 0,
          borderDash: [2, 2],
          tension: 0.15,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { position: "top", labels: { color: c.text, font: { family: c.font, size: 11 }, boxWidth: 14 } },
        tooltip: { callbacks: { label: (item) => `${item.dataset.label}: ${fmtUsd(item.parsed.y)}` } },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: c.text, font: { family: c.font, size: 10 }, maxTicksLimit: 10 } },
        y: { grid: { color: c.grid }, ticks: { color: c.text, font: { family: c.font, size: 11 }, callback: (v) => `$${v}` } },
      },
    },
  });
}

function renderKnownEtfChart(labels, values) {
  const c = chartColors();
  const ctx = document.getElementById("chart-known-etfs");
  if (knownEtfChart) knownEtfChart.destroy();
  if (!labels.length) return;
  knownEtfChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Cambio semanal (%)",
        data: values,
        backgroundColor: values.map((v) => (v >= 0 ? c.green : c.red)),
        borderRadius: 6,
        maxBarThickness: 32,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (item) => fmtPct(item.parsed.y) } },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: c.text, font: { family: c.font, size: 11 } } },
        y: {
          grid: { color: c.grid },
          ticks: { color: c.text, font: { family: c.font, size: 11 }, callback: (v) => `${v}%` },
        },
      },
    },
  });
}

function renderEtfVolumeChart(labels, values) {
  const c = chartColors();
  const ctx = document.getElementById("chart-etf-volume");
  if (etfVolumeChart) etfVolumeChart.destroy();
  if (!labels.length) return;
  etfVolumeChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Volumen medio diario",
        data: values,
        backgroundColor: c.accent,
        borderRadius: 6,
        maxBarThickness: 28,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (item) => item.parsed.y.toLocaleString("en-US") } },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: c.text, font: { family: c.font, size: 11 } } },
        y: {
          grid: { color: c.grid },
          ticks: { color: c.text, font: { family: c.font, size: 11 }, callback: (v) => `${(v / 1e6).toFixed(0)}M` },
        },
      },
    },
  });
}

/* ---------- Orchestration ---------- */
function renderJournalSummary(data) {
  const weekly = data.weekly;
  document.getElementById("week-trades").textContent = weekly.n;
  document.getElementById("week-trades-sub").textContent =
    `${weekly.closed || 0} cerradas · ${weekly.open || 0} abiertas`;

  const winLossEl = document.getElementById("week-win-loss");
  winLossEl.innerHTML = `<span class="positive">${weekly.wins || 0}</span> / <span class="negative">${weekly.losses || 0}</span>`;

  const winRateEl = document.getElementById("week-win-rate");
  winRateEl.textContent = weekly.win_rate === null ? "Sin operaciones cerradas" : `Win rate ${weekly.win_rate}%${weekly.reliable ? "" : " (muestra pequeña)"}`;

  const pnlEl = document.getElementById("week-pnl");
  const weekPnl = weekly.total_pnl || 0;
  pnlEl.textContent = `${arrow(weekPnl)} ${fmtUsd(Math.abs(weekPnl))}`;
  pnlEl.className = `card-value ${pnlClass(weekPnl)}`;

}

async function refreshJournalSummary() {
  const data = await fetchJSON("/api/journal-summary");
  if (data) renderJournalSummary(data);
}

async function refreshStatusAndHistory() {
  const [status, history, accountValueHistory] = await Promise.all([
    fetchJSON("/api/status"),
    fetchJSON("/api/closed-trades?limit=200"),
    fetchJSON("/api/account-value-history"),
  ]);
  if (accountValueHistory) renderAccountValueChart(accountValueHistory);
  if (status) renderStatus(status);
  if (history) renderHistory(history);
  refreshJournalSummary();
}

async function refreshMarketAnalysis() {
  const data = await fetchJSON("/api/market-analysis");
  if (data) renderMarketAnalysis(data);
  loadTechnicalOutlook(currentTechSymbol);
}

async function refreshMarketStatus() {
  const data = await fetchJSON("/api/market-status");
  if (!data) return;
  const btn = document.getElementById("market-status-btn");
  const label = document.getElementById("market-status-label");
  btn.classList.toggle("open", data.phase === "open");
  btn.classList.toggle("premarket", data.phase === "premarket");
  btn.classList.toggle("closed", data.phase === "closed" || data.phase === "holiday");
  const labels = {
    open: `Abierto (${data.time_et} ET)${data.is_early_close ? " ·  cierre 13h" : ""}`,
    premarket: `Pre (${data.time_et} ET)`,
    closed: `Cerrado (${data.time_et} ET)`,
    holiday: `Festivo (${data.time_et} ET)`,
  };
  label.textContent = labels[data.phase] || data.label;

  if (data.week_days) {
    const strip = document.getElementById("week-strip");
    strip.innerHTML = data.week_days.map((d) => {
      const cls = ["day"];
      if (d.is_trading_day) cls.push("trading");
      if (d.is_today) cls.push("today");
      const title = `${d.date}${d.is_trading_day ? "" : " (sin operar)"}`;
      return `<span class="${cls.join(" ")}" title="${title}">${d.initial}</span>`;
    }).join("");
  }
}

/* ---------- Bot on/off switch ---------- */
const botToggleBtn = document.getElementById("bot-toggle-btn");
const botToggleLabel = document.getElementById("bot-toggle-label");

function renderBotStatus(enabled) {
  botToggleBtn.classList.toggle("on", enabled);
  botToggleBtn.classList.toggle("off", !enabled);
  botToggleLabel.textContent = enabled ? "Bot encendido" : "Bot apagado";
  botToggleBtn.title = enabled ? "Clic para apagar el bot" : "Clic para encender el bot";
}

async function refreshBotStatus() {
  const data = await fetchJSON("/api/bot-status");
  if (data) renderBotStatus(data.enabled);
}

botToggleBtn.addEventListener("click", async () => {
  const turningOn = botToggleBtn.classList.contains("off");
  botToggleBtn.disabled = true;
  try {
    const res = await fetch("/api/bot-status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: turningOn }),
    });
    const data = await res.json();
    renderBotStatus(data.enabled);
  } finally {
    botToggleBtn.disabled = false;
  }
});

setTechControlsForSymbol(currentTechSymbol);

refreshStatusAndHistory();
refreshMarketAnalysis();
refreshMarketStatus();
refreshBotStatus();

setInterval(refreshStatusAndHistory, 10000);
setInterval(refreshMarketAnalysis, 30000);
setInterval(refreshMarketStatus, 30000);
setInterval(() => { if (!watchlistBox.classList.contains("collapsed")) refreshWatchlist(); }, 30000);
setInterval(() => { if (!fastWatchlistBox.classList.contains("collapsed")) refreshFastWatchlist(); }, 30000);
setInterval(() => { if (!errorLogBox.classList.contains("collapsed")) refreshErrorLog(); }, 30000);
