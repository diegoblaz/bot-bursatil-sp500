import os
import sys
import json
import time
import argparse
from io import StringIO
from pathlib import Path
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import yfinance as yf

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "state.json"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)
STATE.parent.mkdir(exist_ok=True)

KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DELAY = 8.2


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("El estado no es un objeto JSON")
        data.setdefault("last_buy_candidates", [])
        data.setdefault("last_alert_keys", [])
        data.setdefault("last_prices", {})
        return data
    except Exception:
        return {
            "last_buy_candidates": [],
            "last_alert_keys": [],
            "last_prices": {},
        }


def save_state(state):
    STATE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def universe():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    table = next(
        item for item in tables
        if "Symbol" in item.columns and "Security" in item.columns
    )
    symbols = (
        table["Symbol"]
        .astype(str)
        .str.strip()
        .str.replace(".", "-", regex=False)
        .tolist()
    )
    return list(dict.fromkeys(symbols))[:505]


def get_data(symbol):
    try:
        # Twelve Data usa punto para estas dos clases de acciones.
        api_symbol = {"BRK-B": "BRK.B", "BF-B": "BF.B"}.get(symbol, symbol)
        response = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": api_symbol,
                "interval": "1day",
                "outputsize": 260,
                "apikey": KEY,
            },
            timeout=30,
        )
        data = response.json()

        if response.status_code != 200 or "values" not in data:
            reason = data.get("message", str(data.get("code", "API error")))
            print(f"ERROR API [{symbol}]: HTTP {response.status_code} - {reason}")
            return None, reason

        frame = pd.DataFrame(data["values"])
        if frame.empty:
            return None, "sin datos"

        for column in ["open", "high", "low", "close", "volume"]:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            elif column == "volume":
                frame[column] = np.nan
            else:
                return None, f"falta columna {column}"

        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
        frame = (
            frame.dropna(subset=["datetime", "close"])
            .sort_values("datetime")
            .reset_index(drop=True)
        )

        if len(frame) < 205:
            return None, f"histórico insuficiente: {len(frame)}"

        return frame, None

    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
        print(f"ERROR CONEXIÓN [{symbol}]: {reason}")
        return None, reason


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

    return frame


def fundamental_analysis(symbol):
    """Filtro orientativo; no sustituye análisis por sector ni revisión manual."""
    try:
        info = yf.Ticker(symbol).info or {}

        def number(key):
            value = info.get(key)
            if value is None:
                return None
            try:
                value = float(value)
                return value if np.isfinite(value) else None
            except (TypeError, ValueError):
                return None

        metrics = {
            "pe": number("trailingPE"),
            "roe": number("returnOnEquity"),
            "profit_margin": number("profitMargins"),
            "revenue_growth": number("revenueGrowth"),
            "debt_equity": number("debtToEquity"),
            "price_to_book": number("priceToBook"),
        }

        checks = [
            ("roe", lambda value: value >= 0.15, 20),
            ("profit_margin", lambda value: value >= 0.10, 20),
            ("revenue_growth", lambda value: value > 0, 15),
            ("debt_equity", lambda value: value <= 100, 15),
            ("pe", lambda value: 0 < value <= 25, 15),
            ("price_to_book", lambda value: 0 < value <= 5, 15),
        ]

        score = 0
        available = 0
        for key, test, points in checks:
            value = metrics[key]
            if value is not None:
                available += points
                if test(value):
                    score += points

        if available == 0:
            status = "SIN DATOS FUNDAMENTALES"
            final_score = None
        else:
            final_score = round(score / available * 100)
            if available < 80:
                status = "DATOS INSUFICIENTES"
            elif final_score >= 80:
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


def telegram(message):
    if not (TG_TOKEN and TG_CHAT):
        print("Telegram no configurado; se omite el envío.")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": message[:4000]},
            timeout=20,
        )
        if not response.ok:
            print(f"Telegram HTTP {response.status_code}: {response.text[:300]}")
            return False
        result = response.json()
        if not result.get("ok", False):
            print(f"Telegram informó de un error: {result}")
            return False
        return True
    except Exception as error:
        print(f"Telegram error: {error}")
        return False


def pct(value):
    return "N/D" if value is None or pd.isna(value) else f"{value * 100:+.1f}%"


def metric_value(value, suffix=""):
    if value is None:
        return "N/D"
    try:
        if not np.isfinite(float(value)):
            return "N/D"
        return f"{float(value):.1f}{suffix}"
    except (TypeError, ValueError):
        return "N/D"


def ratio_percent(value):
    """Yahoo Finance expresa ROE, márgenes y crecimiento como fracciones (0.25 = 25%)."""
    if value is None:
        return "N/D"
    try:
        numeric = float(value)
        if not np.isfinite(numeric):
            return "N/D"
        return f"{numeric * 100:.1f}%"
    except (TypeError, ValueError):
        return "N/D"


def fundamentals_summary(fundamentals):
    fundamentals = fundamentals or {}
    metrics = fundamentals.get("metrics", {})
    score = fundamentals.get("score")
    score_text = f"{score}/100" if score is not None else "N/D"
    coverage = fundamentals.get("coverage", 0)
    return (
        f"{fundamentals.get('status', 'SIN DATOS')}; puntuación {score_text}; "
        f"datos disponibles {coverage}/100 puntos"
    )


def classify_weak(row):
    fundamentals = row.get("fundamentals", {})
    status = fundamentals.get("status", "")
    score = fundamentals.get("score")
    coverage = fundamentals.get("coverage", 0)

    if status == "FUNDAMENTALES DÉBILES" and score is not None and coverage >= 80:
        return "MAYOR RIESGO: debilidad técnica y fundamentales débiles"
    if status == "FUNDAMENTALES FAVORABLES" and score is not None and coverage >= 80:
        return "ESTUDIAR: debilidad técnica con fundamentales favorables"
    return "VIGILAR: confirmar fundamentales/datos"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["full", "watch", "diagnostic"], default="full")
    mode = parser.parse_args().mode

    if not KEY:
        print("Falta el secreto TWELVE_DATA_API_KEY")
        return 2

    state = load_state()
    symbols = universe()

    if mode == "diagnostic":
        symbols = ["KO", "AAPL", "MSFT"]
    elif mode == "watch":
        symbols = state.get("last_buy_candidates", [])[:100]

    if not symbols:
        print("No hay candidatos guardados para revisar.")
        return 0

    rows = []
    errors = []
    alerts = []
    candidates = []
    previous_keys = set(state.get("last_alert_keys", []))

    for index, symbol in enumerate(symbols):
        frame, error = get_data(symbol)

        if error:
            errors.append(f"{symbol}: {error}")
        else:
            frame = calc(frame)
            if len(frame) < 2:
                errors.append(f"{symbol}: no hay dos sesiones para comparar")
            else:
                current = frame.iloc[-1]
                previous = frame.iloc[-2]
                required = [
                    current.get("sma200"), current.get("rsi"), current.get("mom3"),
                    current.get("mom6"), current.get("macd"), current.get("signal"),
                ]

                if any(pd.isna(value) for value in required):
                    errors.append(f"{symbol}: indicadores incompletos")
                else:
                    date = str(current["datetime"].date())
                    buy = conditions(current) and conditions(previous)
                    weak = bool(
                        current["close"] < current["sma50"]
                        and current["macd"] < current["signal"]
                        and current["mom3"] < 0
                        and previous["close"] < previous["sma50"]
                        and previous["macd"] < previous["signal"]
                        and previous["mom3"] < 0
                    )

                    fundamentals = (
                        fundamental_analysis(symbol)
                        if buy or weak
                        else {
                            "status": "NO CONSULTADO (sin señal técnica)",
                            "score": None,
                            "coverage": 0,
                            "metrics": {},
                        }
                    )

                    row = {
                        "symbol": symbol,
                        "date": date,
                        "close": float(current["close"]),
                        "sma50": float(current["sma50"]),
                        "sma200": float(current["sma200"]),
                        "rsi": float(current["rsi"]),
                        "macd": float(current["macd"]),
                        "signal": float(current["signal"]),
                        "mom3": float(current["mom3"]),
                        "mom6": float(current["mom6"]),
                        "rvol": (
                            float(current["rvol"])
                            if pd.notna(current["rvol"]) else None
                        ),
                        "buy": buy,
                        "weak": weak,
                        "fundamentals": fundamentals,
                    }
                    rows.append(row)

                    if buy:
                        candidates.append(symbol)
                        alert_key = f"BUY:{symbol}:{date}"
                        if alert_key not in previous_keys:
                            alerts.append((alert_key, "BUY", row))
                    if weak:
                        alert_key = f"WEAK:{symbol}:{date}"
                        if alert_key not in previous_keys:
                            alerts.append((alert_key, "WEAK", row))

        if index < len(symbols) - 1:
            time.sleep(DELAY)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    buys = [row for row in rows if row["buy"]]
    weak_rows = [row for row in rows if row["weak"]]

    lines = [
        "# Informe bot bursátil S&P 500",
        f"- Ejecución: {now}",
        f"- Modo: {mode}",
        f"- Símbolos: {len(symbols)}; con datos válidos: {len(rows)}; errores: {len(errors)}",
        "",
        "> Herramienta orientativa. Las señales no son órdenes de compra/venta. "
        "La puntuación fundamental es simplificada, no está ajustada por sector y depende "
        "de los datos disponibles. Revisa resultados, valoración, noticias y riesgos antes de operar.",
        "",
        "## Candidatos técnicos (no son compras confirmadas)",
    ]

    if buys:
        for row in sorted(buys, key=lambda item: item["mom6"], reverse=True)[:50]:
            volume = metric_value(row["rvol"], "x")
            lines.append(
                f"- **{row['symbol']}** cierre {row['close']:.2f}; "
                f"SMA50 {row['sma50']:.2f}; SMA200 {row['sma200']:.2f}; "
                f"RSI {row['rsi']:.1f}; MACD {row['macd']:.3f} vs señal {row['signal']:.3f}; "
                f"momentum 6m {pct(row['mom6'])}; volumen relativo {volume}; "
                f"fundamentales: {fundamentals_summary(row['fundamentals'])}."
            )
    else:
        lines.append("Sin candidatos que cumplan todas las reglas en dos sesiones consecutivas.")

    lines += [
        "",
        "## Debilidad persistente (vigilancia, no orden de venta)",
        "",
        "Clasificación orientativa; no implica que una acción esté barata ni que vaya a rebotar.",
    ]

    if weak_rows:
        for row in sorted(weak_rows, key=lambda item: item["mom3"])[:50]:
            fundamentals = row.get("fundamentals", {})
            metrics = fundamentals.get("metrics", {})
            lines.append(
                f"- **{row['symbol']}** — {classify_weak(row)}. "
                f"Cierre {row['close']:.2f}; RSI {row['rsi']:.1f}; "
                f"momentum 3m {pct(row['mom3'])}; MACD bajo señal. "
                f"Fundamentales: {fundamentals_summary(fundamentals)}. "
                f"ROE {ratio_percent(metrics.get('roe'))}; "
                f"margen {ratio_percent(metrics.get('profit_margin'))}; "
                f"crecimiento ingresos {ratio_percent(metrics.get('revenue_growth'))}; "
                f"deuda/capital {metric_value(metrics.get('debt_equity'))}; "
                f"PER {metric_value(metrics.get('pe'))}; "
                f"precio/valor contable {metric_value(metrics.get('price_to_book'))}."
            )
    else:
        lines.append("Sin señales de debilidad persistente según estas reglas.")

    if errors:
        lines += ["", "## Errores (máximo 30)"]
        lines += [f"- {error}" for error in errors[:30]]

    report = "\n".join(lines) + "\n"
    (REPORTS / "latest_report.md").write_text(report, encoding="utf-8")
    report_name = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    (REPORTS / f"report_{report_name}_{mode}.md").write_text(report, encoding="utf-8")

    keys = list(state.get("last_alert_keys", []))
    sent_count = 0
    for alert_key, kind, row in alerts:
        fund = row.get("fundamentals", {})
        if kind == "BUY":
            msg = (
                f"🟡 CANDIDATO TÉCNICO — NO COMPRA CONFIRMADA: {row['symbol']} ({row['date']})\n"
                f"Cierre {row['close']:.2f}; SMA50/200 {row['sma50']:.2f}/{row['sma200']:.2f}; "
                f"RSI {row['rsi']:.1f}; MACD {row['macd']:.3f} vs señal {row['signal']:.3f}; "
                f"momentum 6m {pct(row['mom6'])}; volumen {metric_value(row['rvol'], 'x')}.\n"
                f"Fundamentales: {fundamentals_summary(fund)}.\n"
                "Revisar valoración, resultados y noticias antes de decidir."
            )
        else:
            metrics = fund.get("metrics", {})
            msg = (
                f"⚠️ VIGILANCIA TÉCNICA (no orden de venta): {row['symbol']} ({row['date']})\n"
                f"Cierre {row['close']:.2f}; SMA50 {row['sma50']:.2f}; RSI {row['rsi']:.1f}; "
                f"momentum 3m {pct(row['mom3'])}; MACD bajo señal.\n"
                f"Clasificación orientativa: {classify_weak(row)}.\n"
                f"Fundamentales: {fundamentals_summary(fund)}.\n"
                f"ROE {ratio_percent(metrics.get('roe'))}; "
                f"margen {ratio_percent(metrics.get('profit_margin'))}; "
                f"crecimiento ingresos {ratio_percent(metrics.get('revenue_growth'))}.\n"
                "No vender ni comprar solo por esta alerta."
            )

        if telegram(msg):
            sent_count += 1
        keys.append(alert_key)

    state["last_alert_keys"] = keys[-2000:]
    state["last_buy_candidates"] = candidates[:100]
    state["last_scan"] = now
    save_state(state)

    summary = (
        f"📊 RESUMEN S&P 500\n"
        f"Modo: {mode}\n"
        f"Acciones con datos válidos: {len(rows)}/{len(symbols)}\n"
        f"Errores: {len(errors)}\n"
        f"Candidatos técnicos: {len(buys)}\n"
        f"Señales de debilidad: {len(weak_rows)}\n"
        f"Alertas nuevas detectadas: {len(alerts)}\n"
        f"Fecha: {now}\n\n"
        "Consulta reports/latest_report.md para el detalle. "
        "Las señales son informativas, no órdenes de compra o venta."
    )

    # Se conserva el resumen en cada ejecución para confirmar que el bot terminó.
    telegram(summary)
    print(summary)
    print(f"Alertas nuevas procesadas: {len(alerts)}; mensajes de alerta enviados: {sent_count}")

    if errors and len(rows) < max(20, int(len(symbols) * 0.5)):
        print("ATENCIÓN: escaneo incompleto; ausencia de alertas no significa ausencia de oportunidades.")

    return 0 if len(rows) >= max(1, int(len(symbols) * 0.5)) else 2


if __name__ == "__main__":
    if os.getenv("TEST_API") == "1":
        if not KEY:
            print("Falta el secreto TWELVE_DATA_API_KEY")
            sys.exit(2)
        print("PRUEBA API TWELVE DATA: KO")
        data, error = get_data("KO")
        if error:
            print(f"ERROR KO: {error}")
            sys.exit(2)
        print(f"OK KO: {len(data)} filas")
        print(f"Última fecha: {data['datetime'].iloc[-1]}")
        print(f"Último cierre: {data['close'].iloc[-1]}")
    elif os.getenv("TEST_FUNDAMENTALS") == "1":
        result = fundamental_analysis("KO")
        print("PRUEBA FUNDAMENTALES COCA-COLA")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        sys.exit(main())
