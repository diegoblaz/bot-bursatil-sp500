import os
import sys
import json
import time
import argparse
from io import StringIO
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
import yfinance as yf

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "state.json"
REPORTS = ROOT / "reports"
STATE.parent.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Descarga por bloques para reducir el número de peticiones a Yahoo Finance.
BATCH_SIZE = 40
BATCH_DELAY = 2.0
FUNDAMENTAL_DELAY = 1.0
MADRID = ZoneInfo("Europe/Madrid")

INDEX_CONFIG = {
    "SP500": {
        "name": "S&P 500",
        "url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "min_constituents": 450,
        "max_constituents": 510,
        "suffix": "",
        "source": "Wikipedia",
    },
    "CAC40": {
        "name": "CAC 40",
        "url": "https://en.wikipedia.org/wiki/CAC_40",
        "min_constituents": 30,
        "max_constituents": 45,
        "suffix": ".PA",
        "source": "Wikipedia",
    },
    "IBEX35": {
        "name": "IBEX 35",
        "url": "https://en.wikipedia.org/wiki/IBEX_35",
        "min_constituents": 28,
        "max_constituents": 40,
        "suffix": ".MC",
        "source": "Wikipedia",
    },
    "DAX40": {
        "name": "DAX 40",
        "url": "https://en.wikipedia.org/wiki/DAX",
        "min_constituents": 35,
        "max_constituents": 45,
        "suffix": ".DE",
        "source": "Wikipedia",
    },
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketResearchBot/1.0)"}


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("El estado no es un objeto JSON")
        data.setdefault("last_alert_keys", [])
        data.setdefault("last_buy_candidates", [])
        data.setdefault("last_scan", None)
        return data
    except Exception:
        return {
            "last_alert_keys": [],
            "last_buy_candidates": [],
            "last_scan": None,
        }


def save_state(state):
    STATE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def flatten_columns(frame):
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [
            "_".join(str(part) for part in col if str(part) not in ("", "None"))
            for col in frame.columns
        ]
    return frame


def find_symbol_column(table):
    normalized = {str(col).strip().lower(): col for col in table.columns}
    preferred = [
        "symbol", "ticker", "ticker symbol", "ticker-symbol",
        "stock symbol", "code", "ric",
    ]
    for name in preferred:
        if name in normalized:
            return normalized[name]
    for col in table.columns:
        label = str(col).strip().lower()
        if "ticker" in label or label == "symbol":
            return col
    return None


def clean_symbol(value):
    if pd.isna(value):
        return None
    symbol = str(value).strip()
    # Algunas tablas añaden notas o referencias entre corchetes.
    symbol = symbol.split("[")[0].strip()
    symbol = symbol.replace("\u00a0", " ")
    symbol = symbol.replace(" ", "")
    if not symbol or symbol.lower() in {"nan", "none", "—", "-"}:
        return None
    return symbol


def get_index_constituents(index_key):
    config = INDEX_CONFIG[index_key]
    response = requests.get(config["url"], headers=HEADERS, timeout=40)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))

    candidates = []
    for table in tables:
        symbol_col = find_symbol_column(table)
        if symbol_col is None:
            continue
        values = [clean_symbol(x) for x in table[symbol_col].tolist()]
        values = [x for x in values if x]
        # Se exige un tamaño razonable para evitar seleccionar una tabla equivocada.
        if config["min_constituents"] <= len(values) <= config["max_constituents"]:
            candidates.append(values)

    if not candidates:
        raise RuntimeError(
            f"No se pudo identificar una tabla de componentes válida para "
            f"{config['name']}. La página puede haber cambiado."
        )

    raw_symbols = max(candidates, key=len)
    suffix = config["suffix"]
    symbols = []
    for symbol in raw_symbols:
        if index_key == "SP500":
            # Yahoo Finance usa guion para algunas clases de acciones estadounidenses.
            yahoo_symbol = symbol.replace(".", "-")
        elif suffix:
            # Algunas fuentes ya publican el ticker Yahoo completo (p. ej. AIR.PA o AIR.PA);
            # no añadir un segundo sufijo a esos símbolos.
            yahoo_symbol = symbol if "." in symbol else symbol + suffix
        else:
            yahoo_symbol = symbol
        symbols.append(yahoo_symbol)

    symbols = list(dict.fromkeys(symbols))
    if not config["min_constituents"] <= len(symbols) <= config["max_constituents"]:
        raise RuntimeError(
            f"Universo inesperado para {config['name']}: {len(symbols)} símbolos."
        )
    return symbols


def build_universes(mode, state):
    if mode == "diagnostic":
        return {"SP500": ["KO", "AAPL", "MSFT"]}
    if mode == "watch":
        saved = state.get("last_buy_candidates", [])
        result = {}
        for item in saved:
            if isinstance(item, dict) and item.get("market") in INDEX_CONFIG:
                result.setdefault(item["market"], []).append(item["symbol"])
        return result

    universes = {}
    failures = []
    for index_key in INDEX_CONFIG:
        try:
            universes[index_key] = get_index_constituents(index_key)
            print(f"{INDEX_CONFIG[index_key]['name']}: {len(universes[index_key])} componentes")
        except Exception as error:
            failures.append(f"{INDEX_CONFIG[index_key]['name']}: {type(error).__name__}: {error}")
            print(f"ERROR UNIVERSO {index_key}: {error}")

    if not universes:
        raise RuntimeError("No se pudo obtener ningún universo. " + " | ".join(failures))
    return universes


def extract_ticker_frame(downloaded, ticker):
    if downloaded is None or downloaded.empty:
        return None

    if not isinstance(downloaded.columns, pd.MultiIndex):
        frame = downloaded.copy()
    else:
        frame = None
        # yfinance puede devolver (Price, Ticker) o (Ticker, Price).
        for level in range(downloaded.columns.nlevels):
            values = downloaded.columns.get_level_values(level)
            if ticker in values:
                try:
                    frame = downloaded.xs(ticker, axis=1, level=level, drop_level=True).copy()
                    break
                except Exception:
                    pass
        if frame is None:
            return None
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)

    frame.columns = [str(col).lower().replace(" ", "_") for col in frame.columns]
    required = {"close", "high", "low"}
    if not required.issubset(set(frame.columns)):
        return None
    if "volume" not in frame.columns:
        frame["volume"] = np.nan

    for col in ["open", "high", "low", "close", "volume"]:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.reset_index()
    date_col = next((c for c in frame.columns if str(c).lower() in ("date", "datetime")), None)
    if date_col is None:
        date_col = frame.columns[0]
    frame["datetime"] = pd.to_datetime(frame[date_col], errors="coerce")
    frame = (
        frame.dropna(subset=["datetime", "close"])
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    if len(frame) < 205:
        return None
    return frame


def download_market_data(symbols):
    data = {}
    errors = []
    symbols = list(dict.fromkeys(symbols))

    for start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[start:start + BATCH_SIZE]
        try:
            downloaded = yf.download(
                tickers=batch,
                period="2y",
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
                timeout=35,
            )
            for symbol in batch:
                frame = extract_ticker_frame(downloaded, symbol)
                if frame is None:
                    errors.append(f"{symbol}: histórico insuficiente o formato de datos no reconocido")
                else:
                    data[symbol] = frame
        except Exception as error:
            errors.extend([f"{symbol}: descarga fallida ({error})" for symbol in batch])
        if start + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_DELAY)

    return data, errors


def calc(frame):
    frame = frame.copy()
    close = frame["close"]

    frame["sma20"] = close.rolling(20).mean()
    frame["sma50"] = close.rolling(50).mean()
    frame["sma200"] = close.rolling(200).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    relative_strength = avg_gain / avg_loss.replace(0, np.nan)
    frame["rsi"] = 100 - 100 / (1 + relative_strength)
    frame.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100

    frame["macd"] = (
        close.ewm(span=12, adjust=False).mean()
        - close.ewm(span=26, adjust=False).mean()
    )
    frame["signal"] = frame["macd"].ewm(span=9, adjust=False).mean()
    frame["hist"] = frame["macd"] - frame["signal"]

    frame["mom3"] = close / close.shift(63) - 1
    frame["mom6"] = close / close.shift(126) - 1
    frame["mom12"] = close / close.shift(252) - 1

    if frame["volume"].notna().any():
        average_volume = frame["volume"].rolling(20).mean()
        frame["rvol"] = frame["volume"] / average_volume.replace(0, np.nan)
    else:
        frame["rvol"] = np.nan

    frame["resistance20"] = frame["high"].rolling(20).max()
    frame["support20"] = frame["low"].rolling(20).min()
    frame["resistance60"] = frame["high"].rolling(60).max()
    frame["support60"] = frame["low"].rolling(60).min()
    return frame


def number(value):
    try:
        result = float(value)
        return result if np.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def fundamental_analysis(symbol):
    """Puntuación fundamental orientativa; los datos ausentes no se consideran positivos."""
    try:
        info = yf.Ticker(symbol).info or {}

        metrics = {
            "pe": number(info.get("trailingPE")),
            "forward_pe": number(info.get("forwardPE")),
            "roe": number(info.get("returnOnEquity")),
            "profit_margin": number(info.get("profitMargins")),
            "revenue_growth": number(info.get("revenueGrowth")),
            "debt_equity": number(info.get("debtToEquity")),
            "price_to_book": number(info.get("priceToBook")),
            "free_cashflow": number(info.get("freeCashflow")),
        }

        checks = [
            ("roe", lambda x: x >= 0.12, 15),
            ("profit_margin", lambda x: x >= 0.08, 15),
            ("revenue_growth", lambda x: x > 0, 15),
            ("debt_equity", lambda x: x <= 150, 15),
            ("pe", lambda x: 0 < x <= 30, 15),
            ("price_to_book", lambda x: 0 < x <= 6, 10),
            ("free_cashflow", lambda x: x > 0, 15),
        ]

        score = 0
        available = 0
        for key, test, points in checks:
            value = metrics.get(key)
            if value is not None:
                available += points
                if test(value):
                    score += points

        if available == 0:
            status, final_score = "SIN DATOS FUNDAMENTALES", None
        else:
            final_score = round(score / available * 100)
            if available < 60:
                status = "DATOS INSUFICIENTES"
            elif final_score >= 75:
                status = "FUNDAMENTALES FAVORABLES"
            elif final_score >= 45:
                status = "FUNDAMENTALES MIXTOS"
            else:
                status = "FUNDAMENTALES DÉBILES"

        return {
            "status": status,
            "score": final_score,
            "coverage": available,
            "metrics": metrics,
        }
    except Exception as error:
        print(f"Error fundamental {symbol}: {error}")
        return {
            "status": "ERROR AL CONSULTAR FUNDAMENTALES",
            "score": None,
            "coverage": 0,
            "metrics": {},
        }


def conditions(row):
    required = ["close", "sma50", "sma200", "rsi", "macd", "signal", "hist", "mom6", "rvol"]
    if any(pd.isna(row.get(key)) for key in required):
        return False
    return bool(
        row["close"] > row["sma50"] > row["sma200"]
        and 52 <= row["rsi"] <= 68
        and row["macd"] > row["signal"]
        and row["hist"] > 0
        and row["mom6"] > 0
        and row["rvol"] >= 1.1
    )


def weak_conditions(current, previous):
    required = ["close", "sma50", "macd", "signal", "mom3"]
    if any(pd.isna(current.get(k)) or pd.isna(previous.get(k)) for k in required):
        return False
    return bool(
        current["close"] < current["sma50"]
        and current["macd"] < current["signal"]
        and current["mom3"] < 0
        and previous["close"] < previous["sma50"]
        and previous["macd"] < previous["signal"]
        and previous["mom3"] < 0
    )


def fundamentals_summary(fundamentals):
    fundamentals = fundamentals or {}
    score = fundamentals.get("score")
    score_text = f"{score}/100" if score is not None else "N/D"
    return (
        f"{fundamentals.get('status', 'SIN DATOS')}; puntuación {score_text}; "
        f"cobertura {fundamentals.get('coverage', 0)}/100"
    )


def classify_weak(row):
    fundamentals = row.get("fundamentals", {})
    status = fundamentals.get("status", "")
    score = fundamentals.get("score")
    coverage = fundamentals.get("coverage", 0)
    if status == "FUNDAMENTALES DÉBILES" and score is not None and coverage >= 60:
        return "MAYOR RIESGO: debilidad técnica y fundamentales débiles"
    if status == "FUNDAMENTALES FAVORABLES" and score is not None and coverage >= 60:
        return "ESTUDIAR: debilidad técnica con fundamentales favorables"
    return "VIGILAR: confirmar fundamentales/datos"


def ratio_percent(value):
    value = number(value)
    return "N/D" if value is None else f"{value * 100:.1f}%"


def metric_value(value, suffix=""):
    value = number(value)
    return "N/D" if value is None else f"{value:.2f}{suffix}"


def pct(value):
    value = number(value)
    return "N/D" if value is None else f"{value * 100:+.1f}%"


def telegram(message):
    if not (TG_TOKEN and TG_CHAT):
        print("Telegram no configurado; se omite el envío.")
        return False

    # Telegram limita sendMessage a 4096 caracteres; se divide sin truncar.
    chunks = []
    remaining = message
    while len(remaining) > 3900:
        split_at = remaining.rfind("\n", 0, 3900)
        if split_at < 1000:
            split_at = 3900
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)

    all_ok = True
    for chunk in chunks:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": chunk, "disable_web_page_preview": True},
                timeout=25,
            )
            result = response.json() if response.content else {}
            if not response.ok or not result.get("ok", False):
                print(f"Telegram error HTTP {response.status_code}: {response.text[:300]}")
                all_ok = False
        except Exception as error:
            print(f"Telegram error: {error}")
            all_ok = False
    return all_ok


def score_opportunity(row):
    """Ranking comparativo; no es una recomendación personalizada ni una orden."""
    technical = 0
    technical += 20 if row["close"] > row["sma200"] else 0
    technical += 15 if row["sma50"] > row["sma200"] else 0
    technical += 15 if row["macd"] > row["signal"] else 0
    technical += 15 if 50 <= row["rsi"] <= 70 else 0
    technical += 15 if row["mom6"] > 0 else 0
    technical += 10 if row["mom3"] > 0 else 0
    technical += 10 if row["rvol"] is not None and row["rvol"] >= 1 else 0

    fund = row.get("fundamentals", {})
    fund_score = fund.get("score")
    if fund_score is not None and fund.get("coverage", 0) >= 60:
        combined = 0.65 * technical + 0.35 * fund_score
    else:
        combined = technical
    return round(combined, 1)


def make_row(market, symbol, frame):
    frame = calc(frame)
    if len(frame) < 2:
        return None, "menos de dos sesiones"
    current = frame.iloc[-1]
    previous = frame.iloc[-2]

    required = ["sma200", "sma50", "rsi", "mom3", "mom6", "macd", "signal"]
    if any(pd.isna(current.get(key)) for key in required):
        return None, "indicadores incompletos"

    buy = conditions(current) and conditions(previous)
    weak = weak_conditions(current, previous)

    row = {
        "market": market,
        "market_name": INDEX_CONFIG[market]["name"],
        "symbol": symbol,
        "date": str(pd.Timestamp(current["datetime"]).date()),
        "close": float(current["close"]),
        "sma20": float(current["sma20"]) if pd.notna(current["sma20"]) else None,
        "sma50": float(current["sma50"]),
        "sma200": float(current["sma200"]),
        "rsi": float(current["rsi"]),
        "macd": float(current["macd"]),
        "signal": float(current["signal"]),
        "mom3": float(current["mom3"]),
        "mom6": float(current["mom6"]),
        "mom12": float(current["mom12"]) if pd.notna(current["mom12"]) else None,
        "rvol": float(current["rvol"]) if pd.notna(current["rvol"]) else None,
        "support20": float(current["support20"]) if pd.notna(current["support20"]) else None,
        "resistance20": float(current["resistance20"]) if pd.notna(current["resistance20"]) else None,
        "support60": float(current["support60"]) if pd.notna(current["support60"]) else None,
        "resistance60": float(current["resistance60"]) if pd.notna(current["resistance60"]) else None,
        "buy": bool(buy),
        "weak": bool(weak),
        "fundamentals": {
            "status": "NO CONSULTADO (sin señal técnica)",
            "score": None,
            "coverage": 0,
            "metrics": {},
        },
    }
    return row, None


def main():
    parser = argparse.ArgumentParser(description="Bot mult mercado: S&P 500, CAC 40, IBEX 35 y DAX 40")
    parser.add_argument("--mode", choices=["full", "watch", "diagnostic"], default="full")
    mode = parser.parse_args().mode

    state = load_state()
    try:
        universes = build_universes(mode, state)
    except Exception as error:
        print(f"ERROR UNIVERSOS: {error}")
        return 2

    if not universes or not any(universes.values()):
        print("No hay símbolos para analizar.")
        return 0

    rows = []
    errors = []
    alerts = []
    previous_keys = set(state.get("last_alert_keys", []))
    all_candidates = []

    for market, symbols in universes.items():
        if not symbols:
            continue
        print(f"\nDescargando {INDEX_CONFIG[market]['name']}: {len(symbols)} valores")
        data, download_errors = download_market_data(symbols)
        errors.extend([f"{market} {error}" for error in download_errors])

        market_rows = []
        for symbol in symbols:
            frame = data.get(symbol)
            if frame is None:
                continue
            row, error = make_row(market, symbol, frame)
            if error:
                errors.append(f"{market} {symbol}: {error}")
                continue
            market_rows.append(row)

        # Fundamentales solo para señales técnicas, para evitar consultas innecesarias.
        for row in market_rows:
            if row["buy"] or row["weak"]:
                row["fundamentals"] = fundamental_analysis(row["symbol"])
                time.sleep(FUNDAMENTAL_DELAY)
            if row["buy"]:
                all_candidates.append({"market": market, "symbol": row["symbol"]})
                key = f"BUY:{market}:{row['symbol']}:{row['date']}"
                if key not in previous_keys:
                    alerts.append((key, "BUY", row))
            if row["weak"]:
                key = f"WEAK:{market}:{row['symbol']}:{row['date']}"
                if key not in previous_keys:
                    alerts.append((key, "WEAK", row))

        rows.extend(market_rows)
        print(f"{INDEX_CONFIG[market]['name']}: {len(market_rows)}/{len(symbols)} con datos válidos")

    now_utc = datetime.now(timezone.utc)
    now_madrid = now_utc.astimezone(MADRID)
    now_text = now_madrid.strftime("%Y-%m-%d %H:%M %Z")
    buys = [row for row in rows if row["buy"]]
    weak_rows = [row for row in rows if row["weak"]]

    for row in rows:
        row["opportunity_score"] = score_opportunity(row)

    ranked = sorted(rows, key=lambda x: x["opportunity_score"], reverse=True)
    ranked_buy = sorted(buys, key=lambda x: x["opportunity_score"], reverse=True)

    lines = [
        "# Informe bot bursátil — S&P 500, CAC 40, IBEX 35 y DAX 40",
        f"- Ejecución: {now_text}",
        f"- Modo: {mode}",
        f"- Universo solicitado: {sum(len(v) for v in universes.values())}",
        f"- Valores con datos válidos: {len(rows)}",
        f"- Errores o datos incompletos: {len(errors)}",
        "",
        "> Informe cuantitativo orientativo. Las señales y puntuaciones no son órdenes de compra o venta. "
        "La puntuación fundamental es simplificada, depende de la disponibilidad de Yahoo Finance y no "
        "está ajustada por sector. Comprueba resultados, noticias, valoración, divisa y riesgos antes de operar.",
        "",
        "## Cobertura por índice",
    ]

    for market, symbols in universes.items():
        valid = sum(1 for row in rows if row["market"] == market)
        lines.append(f"- **{INDEX_CONFIG[market]['name']}**: {valid}/{len(symbols)} valores con datos válidos.")

    lines += ["", "## Ranking global — 20 mejores puntuaciones"]
    for position, row in enumerate(ranked[:20], start=1):
        fund = fundamentals_summary(row["fundamentals"])
        lines.append(
            f"{position}. **{row['symbol']}** ({row['market_name']}) — puntuación "
            f"{row['opportunity_score']}/100; cierre {row['close']:.2f}; RSI {row['rsi']:.1f}; "
            f"momentum 6m {pct(row['mom6'])}; fundamentales: {fund}."
        )

    lines += ["", "## Candidatos técnicos (no son compras confirmadas)"]
    if ranked_buy:
        for row in ranked_buy[:60]:
            lines.append(
                f"- **{row['symbol']}** ({row['market_name']}) — puntuación "
                f"{row['opportunity_score']}/100; cierre {row['close']:.2f}; "
                f"SMA50 {row['sma50']:.2f}; SMA200 {row['sma200']:.2f}; "
                f"RSI {row['rsi']:.1f}; MACD {row['macd']:.3f} vs señal {row['signal']:.3f}; "
                f"momentum 6m {pct(row['mom6'])}; volumen relativo {metric_value(row['rvol'], 'x')}; "
                f"fundamentales: {fundamentals_summary(row['fundamentals'])}."
            )
    else:
        lines.append("Sin candidatos que cumplan todas las reglas durante dos sesiones consecutivas.")

    lines += [
        "",
        "## Debilidad persistente (vigilancia, no orden de venta)",
        "Clasificación orientativa; no implica que deba venderse una posición.",
    ]
    if weak_rows:
        for row in sorted(weak_rows, key=lambda x: x["mom3"])[:60]:
            metrics = row["fundamentals"].get("metrics", {})
            lines.append(
                f"- **{row['symbol']}** ({row['market_name']}) — {classify_weak(row)}. "
                f"Cierre {row['close']:.2f}; RSI {row['rsi']:.1f}; momentum 3m {pct(row['mom3'])}; "
                f"fundamentales: {fundamentals_summary(row['fundamentals'])}; "
                f"ROE {ratio_percent(metrics.get('roe'))}; margen {ratio_percent(metrics.get('profit_margin'))}; "
                f"crecimiento ingresos {ratio_percent(metrics.get('revenue_growth'))}; "
                f"deuda/capital {metric_value(metrics.get('debt_equity'))}; "
                f"PER {metric_value(metrics.get('pe'))}."
            )
    else:
        lines.append("Sin señales de debilidad persistente según estas reglas.")

    if errors:
        lines += ["", "## Errores y cobertura incompleta (máximo 100)"]
        lines += [f"- {error}" for error in errors[:100]]

    report = "\n".join(lines) + "\n"
    (REPORTS / "latest_report.md").write_text(report, encoding="utf-8")
    report_name = now_utc.strftime("%Y%m%d_%H%M")
    (REPORTS / f"report_{report_name}_{mode}.md").write_text(report, encoding="utf-8")

    # Solo se registra una alerta como procesada cuando el envío a Telegram tiene éxito.
    keys = list(state.get("last_alert_keys", []))
    sent_count = 0
    for alert_key, kind, row in alerts:
        fund = row.get("fundamentals", {})
        if kind == "BUY":
            msg = (
                f"🟡 CANDIDATO TÉCNICO — NO COMPRA CONFIRMADA\n"
                f"{row['symbol']} | {row['market_name']} | {row['date']}\n"
                f"Cierre {row['close']:.2f}; SMA50/200 {row['sma50']:.2f}/{row['sma200']:.2f}; "
                f"RSI {row['rsi']:.1f}; MACD {row['macd']:.3f} vs señal {row['signal']:.3f}; "
                f"momentum 6m {pct(row['mom6'])}; volumen {metric_value(row['rvol'], 'x')}.\n"
                f"Fundamentales: {fundamentals_summary(fund)}.\n"
                "Revisar valoración, resultados y noticias antes de decidir."
            )
        else:
            metrics = fund.get("metrics", {})
            msg = (
                f"⚠️ VIGILANCIA TÉCNICA — NO ES ORDEN DE VENTA\n"
                f"{row['symbol']} | {row['market_name']} | {row['date']}\n"
                f"Cierre {row['close']:.2f}; SMA50 {row['sma50']:.2f}; RSI {row['rsi']:.1f}; "
                f"momentum 3m {pct(row['mom3'])}; MACD bajo señal.\n"
                f"Clasificación: {classify_weak(row)}.\n"
                f"Fundamentales: {fundamentals_summary(fund)}.\n"
                f"ROE {ratio_percent(metrics.get('roe'))}; margen {ratio_percent(metrics.get('profit_margin'))}; "
                f"crecimiento ingresos {ratio_percent(metrics.get('revenue_growth'))}.\n"
                "No comprar ni vender solo por esta alerta."
            )
        if telegram(msg):
            keys.append(alert_key)
            sent_count += 1

    state["last_alert_keys"] = keys[-3000:]
    state["last_buy_candidates"] = all_candidates[:200]
    state["last_scan"] = now_text
    save_state(state)

    summary = (
        f"📊 RESUMEN BOT MULTIMERCADO\n"
        f"Hora: {now_text}\n"
        f"Modo: {mode}\n"
        + "\n".join(
            f"{INDEX_CONFIG[market]['name']}: "
            f"{sum(1 for row in rows if row['market'] == market)}/{len(symbols)} válidos"
            for market, symbols in universes.items()
        )
        + f"\nTotal con datos válidos: {len(rows)}"
        f"\nErrores: {len(errors)}"
        f"\nCandidatos técnicos: {len(buys)}"
        f"\nSeñales de debilidad: {len(weak_rows)}"
        f"\nAlertas nuevas enviadas: {sent_count}/{len(alerts)}"
        "\nConsulta reports/latest_report.md para el detalle."
        "\nLas señales son informativas, no órdenes de compra o venta."
    )
    telegram(summary)
    print(summary)

    expected = sum(len(v) for v in universes.values())
    if expected and len(rows) < max(1, int(expected * 0.5)):
        print("ATENCIÓN: escaneo incompleto. La ausencia de alertas no significa ausencia de oportunidades.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
