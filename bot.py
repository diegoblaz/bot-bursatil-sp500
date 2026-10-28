
import os, sys, json, time, argparse
from io import StringIO
from pathlib import Path
from datetime import datetime, timezone
import requests, pandas as pd, numpy as np
import yfinance as yf

ROOT=Path(__file__).resolve().parent
STATE=ROOT/"state/state.json"; REPORTS=ROOT/"reports"
REPORTS.mkdir(exist_ok=True); STATE.parent.mkdir(exist_ok=True)
KEY=os.getenv("TWELVE_DATA_API_KEY","").strip()
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()
DELAY=8.2

def load_state():
    try: return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception: return {"last_buy_candidates":[],"last_alert_keys":[],"last_prices":{}}

def save_state(s): STATE.write_text(json.dumps(s,indent=2,ensure_ascii=False),encoding="utf-8")

def universe():
    url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=30)
    r.raise_for_status()
    tabs=pd.read_html(StringIO(r.text))
    t=next(x for x in tabs if "Symbol" in x.columns and "Security" in x.columns)
    return list(dict.fromkeys(t["Symbol"].astype(str).str.strip().str.replace(".","-",regex=False).tolist()))[:505]

def get_data(sym):
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": sym,
                "interval": "1day",
                "outputsize": 260,
                "apikey": KEY,
            },
            timeout=30,
        )

        try:
            d = r.json()
        except ValueError:
            return None, f"respuesta no JSON (HTTP {r.status_code})"

        if r.status_code != 200 or "values" not in d:
            motivo = d.get("message", str(d.get("code", "API error")))
            return None, f"HTTP {r.status_code}: {motivo}"

        x = pd.DataFrame(d["values"])

        if x.empty:
            return None, "sin datos"

        for c in ["open", "high", "low", "close", "volume"]:
            if c in x.columns:
                x[c] = pd.to_numeric(x[c], errors="coerce")
            elif c != "volume":
                return None, f"falta columna {c}"

        if "volume" not in x.columns:
            x["volume"] = np.nan

        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["datetime", "close"]).sort_values(
            "datetime"
        ).reset_index(drop=True)

        if len(x) < 205:
            return None, f"histórico insuficiente: {len(x)}"

        return x, None

    except requests.exceptions.Timeout:
        return None, "timeout de conexión"
    except requests.exceptions.RequestException as e:
        return None, f"error de conexión: {e}"
    except Exception as e:
        return None, f"error inesperado: {type(e).__name__}: {e}"

def calc(x):
    x=x.copy(); c=x.close
    x["sma20"]=c.rolling(20).mean(); x["sma50"]=c.rolling(50).mean(); x["sma200"]=c.rolling(200).mean()
    delta=c.diff(); gain=delta.clip(lower=0); loss=-delta.clip(upper=0)
    ag=gain.ewm(alpha=1/14,min_periods=14,adjust=False).mean()
    al=loss.ewm(alpha=1/14,min_periods=14,adjust=False).mean()
    x["rsi"]=100-100/(1+ag/al.replace(0,np.nan))
    x["macd"]=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    x["signal"]=x.macd.ewm(span=9,adjust=False).mean()
    x["hist"]=x.macd-x.signal
    x["mom3"]=c/c.shift(63)-1; x["mom6"]=c/c.shift(126)-1; x["mom12"]=c/c.shift(252)-1
    x["rvol"]=x.volume/x.volume.rolling(20).mean()
    return x

def fundamental_analysis(sym):
    try:
        t = yf.Ticker(sym)
        info = t.info or {}

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

        score = 0
        available = 0

        checks = [
            ("roe", lambda v: v >= 0.15, 20),
            ("profit_margin", lambda v: v >= 0.10, 20),
            ("revenue_growth", lambda v: v > 0, 15),
            ("debt_equity", lambda v: v <= 100, 15),
            ("pe", lambda v: 0 < v <= 25, 15),
            ("price_to_book", lambda v: 0 < v <= 5, 15),
        ]

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
            elif final_score >= 70:
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

    except Exception as e:
        print(f"Error fundamental {sym}: {e}")
        return {
            "status": "ERROR AL CONSULTAR FUNDAMENTALES",
            "score": None,
            "coverage": 0,
            "metrics": {},
        }
        
def conditions(r):
    return bool(r["close"]>r["sma50"]>r["sma200"] and 52<=r["rsi"]<=68 and r["macd"]>r["signal"] and r["hist"]>0 and r["mom6"]>0 and r["rvol"]>=1.1)
    
def telegram(msg):
    if not (TG_TOKEN and TG_CHAT): return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT,"text":msg[:4000]},timeout=20)
    except Exception as e: print("Telegram error:",e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "watch", "diagnostic"], default="full")
    mode = ap.parse_args().mode

    if not KEY:
        print("Falta secreto TWELVE_DATA_API_KEY")
        return 2

    s = load_state()
    syms = universe()

    if mode == "diagnostic":
        syms = ["KO", "AAPL", "MSFT"]
    elif mode == "watch":
        syms = s.get("last_buy_candidates", [])[:100]

    if not syms:
        print("No hay candidatos guardados para revisar.")
        return 0

    rows = []
    errors = []
    alerts = []
    candidates = []

    prevkeys=set(s.get("last_alert_keys",[]))
    for i,sym in enumerate(syms):
        x,err=get_data(sym)
        if err:
            errors.append(f"{sym}: {err}")
            print(f"ERROR API [{sym}]: {err}")
        else:
            x=calc(x); a=x.iloc[-1]; b=x.iloc[-2]
            if pd.isna(a.sma200) or pd.isna(a.rsi) or pd.isna(a.rvol): errors.append(f"{sym}: indicadores incompletos")
            else:
                date=str(a.datetime.date())
                buy=conditions(a) and conditions(b)
                weak=(a.close<a.sma50 and a.macd<a.signal and a.mom3<0 and b.close<b.sma50 and b.macd<b.signal and b.mom3<0)
                fund = fundamental_analysis(sym) if (buy or weak) else {"status": "NO CONSULTADO (sin señal técnica)", "score": None, "coverage": 0, "metrics": {}}
                row={"symbol":sym,"date":date,"close":float(a.close),"sma50":float(a.sma50),"sma200":float(a.sma200),"rsi":float(a.rsi),"macd":float(a.macd),"signal":float(a.signal),"mom3":float(a.mom3),"mom6":float(a.mom6),"rvol":float(a.rvol),"buy":buy,"weak":weak,"fundamentals":fund}
                rows.append(row)
                if buy:
                    candidates.append(sym); k=f"BUY:{sym}:{date}"
                    if k not in prevkeys: alerts.append((k,"BUY",row))
                if weak:
                    k=f"WEAK:{sym}:{date}"
                    if k not in prevkeys: alerts.append((k,"WEAK",row))
        if i<len(syms)-1: time.sleep(DELAY)
    now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    buys=[r for r in rows if r["buy"]]; weak=[r for r in rows if r["weak"]]
    lines=["# Informe bot bursátil S&P 500",f"- Ejecución: {now}",f"- Modo: {mode}",f"- Símbolos: {len(syms)}; con datos válidos: {len(rows)}; errores: {len(errors)}","","> Prototipo técnico y filtro fundamental orientativo. Los datos pueden faltar o ser incompletos; no emite compras/ventas confirmadas. Valida resultados, valoración y noticias antes de operar.","","## Candidatos técnicos (validación fundamental necesaria)"]
    lines += [f"- **{r['symbol']}** cierre {r['close']:.2f}; SMA50 {r['sma50']:.2f}; SMA200 {r['sma200']:.2f}; RSI {r['rsi']:.1f}; MACD {r['macd']:.3f} vs señal {r['signal']:.3f}; momentum 6m {r['mom6']*100:+.1f}%; volumen relativo {r['rvol']:.2f}x; fundamentales: {r.get('fundamentals', {}).get('status', 'SIN DATOS')}; puntuación: {r.get('fundamentals', {}).get('score', 'N/D')}/100" for r in sorted(buys,key=lambda z:z["mom6"],reverse=True)[:50]] or ["Sin candidatos que cumplan todas las reglas en dos sesiones consecutivas."]
    lines += ["","## Debilidad persistente (vigilancia, no orden de venta)"]
    lines += [f"- **{r['symbol']}** cierre {r['close']:.2f}; RSI {r['rsi']:.1f}; momentum 3m {r['mom3']*100:+.1f}%; MACD bajo señal." for r in sorted(weak,key=lambda z:z["mom3"])[:50]] or ["Sin señales de debilidad persistente según estas reglas."]
    if errors: lines += ["","## Errores (máximo 30)"]+[f"- {e}" for e in errors[:30]]
    report="\n".join(lines)+"\n"; (REPORTS/"latest_report.md").write_text(report,encoding="utf-8")
    (REPORTS/f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}_{mode}.md").write_text(report,encoding="utf-8")
    keys=s.get("last_alert_keys",[])
    for k,kind,r in alerts:
        if kind=="BUY":
            f=r.get("fundamentals", {})
            nota=f"Fundamentales: {f.get('status', 'SIN DATOS')}; puntuación {f.get('score')}/100; cobertura {f.get('coverage', 0)}/100 puntos." if f.get("score") is not None else f"Fundamentales: {f.get('status', 'SIN DATOS')}; sin puntuación fiable."
            msg=f"🟡 CANDIDATO TÉCNICO — NO COMPRA CONFIRMADA: {r['symbol']} ({r['date']})\nCierre {r['close']:.2f}; SMA50/200 {r['sma50']:.2f}/{r['sma200']:.2f}; RSI {r['rsi']:.1f}; MACD {r['macd']:.3f} > señal {r['signal']:.3f}; momentum 6m {r['mom6']*100:+.1f}%; volumen {r['rvol']:.2f}x.\n{nota}\nValidar resultados, valoración y noticias antes de decidir."
        else:
            f=r.get("fundamentals", {})
            msg=f"⚠️ VIGILANCIA TÉCNICA (no orden de venta): {r['symbol']} ({r['date']})\nCierre {r['close']:.2f}; SMA50 {r['sma50']:.2f}; RSI {r['rsi']:.1f}; MACD bajo señal; momentum 3m {r['mom3']*100:+.1f}%.\nFundamentales: {f.get('status', 'SIN DATOS')}; puntuación {f.get('score') if f.get('score') is not None else 'N/D'}/100.\nRevisar soporte y fundamentales; no vender solo por esta alerta."
        telegram(msg); keys.append(k)
    s["last_alert_keys"]=keys[-2000:]; s["last_buy_candidates"]=candidates[:100]; s["last_scan"]=now; save_state(s)
    resumen = (
        f"📊 RESUMEN S&P 500\n"
        f"Modo: {mode}\n"
        f"Acciones con datos válidos: {len(rows)}/{len(syms)}\n"
        f"Errores: {len(errors)}\n"
        f"Candidatos técnicos: {len(buys)}\n"
        f"Señales de debilidad: {len(weak)}\n"
        f"Fecha: {now}\n\n"
        "Los candidatos requieren validar fundamentales, valoración y noticias. "
        "No son órdenes de compra ni de venta."
    )
    telegram(resumen)
    print(resumen)
    if errors and len(rows)<max(20,int(len(syms)*.5)):
        print("ATENCIÓN: escaneo incompleto; ausencia de alertas no significa ausencia de oportunidades.")
    return 0 if len(rows)>=max(1,int(len(syms)*.5)) else 2

if __name__ == "__main__":
    test_mode = os.getenv("TEST_FUNDAMENTALS") == "1"
    test_api = os.getenv("TEST_API") == "1"

    if test_api:
        print("PRUEBA API TWELVE DATA: KO")
        data, error = get_data("KO")
        if error:
            print(f"ERROR KO: {error}")
        else:
            print(f"OK KO: {len(data)} filas")
            print(f"Última fecha: {data['datetime'].iloc[-1]}")
            print(f"Último cierre: {data['close'].iloc[-1]}")
    elif test_mode:
        result = fundamental_analysis("KO")
        print("PRUEBA FUNDAMENTALES COCA-COLA")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        sys.exit(main())
