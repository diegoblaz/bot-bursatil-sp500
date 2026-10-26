# Bot bursátil gratuito — prototipo S&P 500

Este paquete es un prototipo para GitHub Actions + Twelve Data + Telegram. Descarga velas diarias, calcula indicadores localmente y guarda informes. **No ejecuta operaciones y no emite compras/ventas confirmadas**: no consulta fundamentales en esta versión; valida valoración, resultados y noticias antes de operar.

## Qué analiza
- Componentes S&P 500 obtenidos desde la tabla pública de Wikipedia.
- SMA20/50/200, RSI14, MACD, momentum 3/6/12 meses y volumen relativo a 20 sesiones.
- Candidato técnico solo si las condiciones se cumplen en dos sesiones consecutivas.
- Debilidad persistente como vigilancia, nunca como orden de venta.
- Deduplificación mediante `state/state.json`.
- Informes en `reports/latest_report.md`; avisos Telegram opcionales.

## Límites de coste cero
- Twelve Data Basic publica 800 créditos/día y 8 créditos/minuto; una serie diaria suele consumir 1 crédito por símbolo. El escaneo completo ronda 500 créditos y tarda aproximadamente 70 minutos con la pausa para respetar el límite por minuto. La revisión de las 07:00 UTC solo mira hasta 100 candidatos guardados.
- La cobertura del plan gratuito no equivale a cobertura global completa; esta versión solo escanea S&P 500.
- Algunos tickers de clase pueden requerir ajustes y algunas consultas pueden fallar. Un escaneo incompleto no significa que no existan señales.
- El workflow se ejecuta en UTC y GitHub puede retrasar ejecuciones programadas. Revisa límites vigentes del plan gratuito de GitHub.
- Uso personal/no comercial; revisa las condiciones actuales de Twelve Data y no redistribuyas sus datos.
- El repositorio público permite aprovechar mejor la cuota gratuita de GitHub Actions, pero el código e informes serán visibles públicamente. No incluyas datos privados ni claves.

## Pasos sin programar

### 1. Crear cuentas
1. GitHub: https://github.com/
2. Twelve Data: https://twelvedata.com/pricing — copia tu API key privada.
3. Opcional: crea un bot de Telegram con https://t.me/BotFather usando `/newbot`, pulsa Start en el bot y guarda el token.

Para conocer tu `chat_id`, abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` en el navegador, sustituyendo el token. Busca `message.chat.id` en el JSON. Trata la URL como contraseña y no la compartas.

### 2. Crear repositorio
1. En GitHub, `+` → `New repository`.
2. Nombre: `bot-bursatil-sp500`.
3. Para reducir costes de Actions, puedes elegir `Public`; el código e informes serán públicos. No incluyas datos personales ni la cartera privada.
4. Crea el repositorio.

### 3. Subir archivos
1. Descomprime el ZIP.
2. En el repositorio pulsa `Add file` → `Upload files`.
3. Sube todo el contenido de la carpeta, incluidos `.github/workflows/scan.yml`, `bot.py`, `requirements.txt`, `config.json`, `state/state.json` y `reports/README.txt`.
4. Pulsa `Commit changes`.

### 4. Añadir claves
En el repositorio: `Settings` → `Secrets and variables` → `Actions` → `New repository secret`.
- `TWELVE_DATA_API_KEY`: clave Twelve Data.
- `TELEGRAM_BOT_TOKEN`: token Telegram (opcional).
- `TELEGRAM_CHAT_ID`: ID de chat (opcional).

Nunca pegues las claves en el código ni en conversaciones.

### 5. Permitir guardar informes
`Settings` → `Actions` → `General` → `Workflow permissions` → `Read and write permissions` → guardar.

### 6. Primera prueba
1. Abre `Actions`.
2. Selecciona `Bot bursatil S&P 500`.
3. Pulsa `Run workflow` y elige `full`.
4. La primera ejecución puede tardar más de una hora. Revisa el resultado y abre `reports/latest_report.md`.
5. Si fallan muchas consultas, comprueba los logs; no interpretes cobertura parcial como ausencia de señales.

### Horario
- 07:00 UTC de lunes a viernes: modo `watch`, hasta 100 candidatos guardados.
- 22:00 UTC de lunes a viernes: modo `full`, escaneo S&P 500.
- En España peninsular, la hora local cambia con el horario de verano.

## Reglas técnicas
Candidato técnico (no compra confirmada): cierre > SMA50 > SMA200; RSI 52–68; MACD por encima de señal e histograma positivo; momentum 6 meses positivo; volumen relativo >= 1,1; condiciones en dos sesiones consecutivas.

Debilidad persistente (solo vigilancia): cierre por debajo de SMA50, MACD bajo señal y momentum 3 meses negativo, durante dos sesiones.

No se incluye filtro fundamental automatizado ni recomendación automática de venta. El bot no ejecuta órdenes.
