
import os, sys, json, time, argparse
from io import StringIO
from pathlib import Path
from datetime import datetime, timezone
import requests, pandas as pd, numpy as np

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
    r=requests.get("https://api.twelvedata.com/time_series",params={"symbol":sym,"interval":"1day","outputsize":260,"apikey":KEY},timeout=30)
    d=r.json()
    if r.status_code!=200 or "values" not in d: return None, d.get("message",str(d.get("code","API error")))
    x=pd.DataFrame(d["values"])
    if x.empty: return None,"sin datos"
    for c in ["open","high","low","close","volume"]: x[c]=pd.to_numeric(x[c],errors="coerce")
    x["datetime"]=pd.to_datetime(x["datetime"],errors="coerce")
    x=x.dropna(subset=["datetime","close"]).sort_values("datetime").reset_index(drop=True)
    if len(x)<205: return None,f"histórico insuficiente: {len(x)}"
    return x,None

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

def conditions(r):
    return bool(r["close"]>r["sma50"]>r["sma200"] and 52<=r["rsi"]<=68 and r["macd"]>r["signal"] and r["hist"]>0 and r["mom6"]>0 and r["rvol"]>=1.1)
    
def telegram(msg):
    if not (TG_TOKEN and TG_CHAT): return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",json={"chat_id":TG_CHAT,"text":msg[:4000]},timeout=20)
    except Exception as e: print("Telegram error:",e)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--mode",choices=["full","watch"],default="full"); mode=ap.parse_args().mode
    if not KEY: print("Falta secreto TWELVE_DATA_API_KEY"); return 2
    s=load_state(); syms=universe()
        telegram("✅ Conexión con GitHub correcta. Tu bot bursátil S&P 500 se ha iniciado.")
        print("Mensaje de prueba de Telegram enviado.")
    if mode=="watch": syms=s.get("last_buy_candidates",[])[:100]
    if not syms: print("No hay candidatos guardados para revisar."); return 0
    rows=[]; errors=[]; alerts=[]; candidates=[]
    prevkeys=set(s.get("last_alert_keys",[]))
    for i,sym in enumerate(syms):
        x,err=get_data(sym)
        if err: errors.append(f"{sym}: {err}")
        else:
            x=calc(x); a=x.iloc[-1]; b=x.iloc[-2]
            if pd.isna(a.sma200) or pd.isna(a.rsi) or pd.isna(a.rvol): errors.append(f"{sym}: indicadores incompletos")
            else:
                date=str(a.datetime.date())
                buy=conditions(a) and conditions(b)
                weak=(a.close<a.sma50 and a.macd<a.signal and a.mom3<0 and b.close<b.sma50 and b.macd<b.signal and b.mom3<0)
                row={"symbol":sym,"date":date,"close":float(a.close),"sma50":float(a.sma50),"sma200":float(a.sma200),"rsi":float(a.rsi),"macd":float(a.macd),"signal":float(a.signal),"mom3":float(a.mom3),"mom6":float(a.mom6),"rvol":float(a.rvol),"buy":buy,"weak":weak}
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
    lines=["# Informe bot bursátil S&P 500",f"- Ejecución: {now}",f"- Modo: {mode}",f"- Símbolos: {len(syms)}; con datos válidos: {len(rows)}; errores: {len(errors)}","","> Prototipo técnico: no consulta fundamentales y no emite compras/ventas confirmadas. Valida valoración, resultados y noticias antes de operar.","","## Candidatos técnicos (validación fundamental necesaria)"]
    lines += [f"- **{r['symbol']}** cierre {r['close']:.2f}; SMA50 {r['sma50']:.2f}; SMA200 {r['sma200']:.2f}; RSI {r['rsi']:.1f}; MACD {r['macd']:.3f} vs señal {r['signal']:.3f}; momentum 6m {r['mom6']*100:+.1f}%; volumen relativo {r['rvol']:.2f}x" for r in sorted(buys,key=lambda z:z["mom6"],reverse=True)[:50]] or ["Sin candidatos que cumplan todas las reglas en dos sesiones consecutivas."]
    lines += ["","## Debilidad persistente (vigilancia, no orden de venta)"]
    lines += [f"- **{r['symbol']}** cierre {r['close']:.2f}; RSI {r['rsi']:.1f}; momentum 3m {r['mom3']*100:+.1f}%; MACD bajo señal." for r in sorted(weak,key=lambda z:z["mom3"])[:50]] or ["Sin señales de debilidad persistente según estas reglas."]
    if errors: lines += ["","## Errores (máximo 30)"]+[f"- {e}" for e in errors[:30]]
    report="\n".join(lines)+"\n"; (REPORTS/"latest_report.md").write_text(report,encoding="utf-8")
    (REPORTS/f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}_{mode}.md").write_text(report,encoding="utf-8")
    keys=s.get("last_alert_keys",[])
    for k,kind,r in alerts:
        if kind=="BUY": msg=f"🟡 CANDIDATO TÉCNICO — NO COMPRA CONFIRMADA: {r['symbol']} ({r['date']})\nCierre {r['close']:.2f}; SMA50/200 {r['sma50']:.2f}/{r['sma200']:.2f}; RSI {r['rsi']:.1f}; MACD {r['macd']:.3f} > señal {r['signal']:.3f}; momentum 6m {r['mom6']*100:+.1f}%; volumen {r['rvol']:.2f}x.\nAcción: validar fundamentales, valoración y noticias antes de decidir."
        else: msg=f"⚠️ VIGILANCIA TÉCNICA (no orden de venta): {r['symbol']} ({r['date']})\nCierre {r['close']:.2f}; SMA50 {r['sma50']:.2f}; RSI {r['rsi']:.1f}; MACD bajo señal; momentum 3m {r['mom3']*100:+.1f}%.\nRevisar soporte y fundamentales; no vender solo por esta alerta."
        telegram(msg); keys.append(k)
    s["last_alert_keys"]=keys[-2000:]; s["last_buy_candidates"]=candidates[:100]; s["last_scan"]=now; save_state(s)
    print(f"Completado: {len(rows)}/{len(syms)} datos válidos; errores={len(errors)}; candidatos={len(buys)}; debilidad={len(weak)}")
    if errors and len(rows)<max(20,int(len(syms)*.5)): print("ATENCIÓN: escaneo incompleto; ausencia de alertas no significa ausencia de oportunidades.")
    return 0 if len(rows)>=max(20,int(len(syms)*.5)) else 2

if __name__=="__main__": sys.exit(main())
