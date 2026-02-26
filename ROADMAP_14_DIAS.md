# Roadmap técnico (14 días)

Plan para evolucionar el bot NBA hacia alertas útiles para props en Polymarket, priorizando estabilidad, velocidad de iteración y control de riesgo.

## ¿Qué tan lejos estás hoy? (estimación rápida)

Si tomo como referencia el estado actual del repo, estás aproximadamente en un **35–45% del camino** hacia un sistema realmente operable para props en tiempo real.

### Lo que ya tienes (fortalezas)

- Bot de Telegram funcional con loop de polling configurable.
- Integración con `nba_api` live (`scoreboard`, `boxscore`) y lookup de jugadores.
- Base de scoring/umbrales/cooldowns para no spamear alertas.
- Integración inicial con Polymarket (Gamma API) a nivel de endpoint.
- Persistencia básica en JSON (props, estado de alertas, cachés de ids y gamelog).

### Lo que aún falta para “listo para operar”

- Matching robusto NBA ↔ Polymarket (resolución de aliases y conflictos).
- Probabilidad calibrada (no sólo score) + edge reproducible por señal.
- Registro estructurado de cada señal y resultado (para medir ROI/CLV real).
- Guardrails de riesgo más estrictos por partido/jugador/exposición diaria.
- Observabilidad operativa (health checks, métricas y alertas internas).

### Traducción a tiempo (si ejecutas de forma constante)

- **7–10 días**: versión utilizable en modo paper-trading.
- **2–4 semanas**: versión estable con métricas confiables de desempeño.
- **4–8 semanas**: versión madura para operar con disciplina y ajustes semanales.

### Señal de que “ya llegaste”

Considera que estás listo cuando cumplas estas 5 condiciones durante al menos 2 semanas:

1. Latencia de datos estable y sin caídas críticas en ventanas de juego.
2. Señales deduplicadas, con explicación y timestamp confiable.
3. Trazabilidad completa: entrada, cierre, resultado y CLV por señal.
4. Límites de riesgo activos y respetados automáticamente.
5. Dashboard diario con win rate, edge promedio y ROI por mercado.

## Objetivo del MVP

- Detectar oportunidades de props pregame e in-game con una señal cuantitativa simple.
- Notificar por Telegram con contexto accionable (por qué, confianza, riesgo).
- Medir desempeño real (hit rate, CLV estimado, ROI por tipo de señal).

## Arquitectura propuesta

1. **Ingesta**
   - NBA live boxscore + scoreboard (polling cada 30–60s en vivo).
   - Líneas/mercados de Polymarket (Gamma API + normalización de nombres).
2. **Feature store liviano**
   - Caché caliente para estado en partido.
   - Base persistente para histórico (Postgres recomendado).
3. **Motor de señales**
   - Modelos simples por stat: puntos/rebotes/asistencias.
   - Reglas de activación + cooldown + filtros de calidad.
4. **Distribución**
   - Telegram con niveles: `watch`, `entry`, `avoid`.
5. **Observabilidad**
   - Logs estructurados + métricas básicas por job.

## Métricas clave (desde día 1)

- `signals_sent`, `signals_taken`, `win_rate`.
- `avg_edge`, `avg_clv`, `roi`.
- `latency_data_seconds`, `false_positive_rate`.
- Desglose por mercado: `PTS`, `REB`, `AST` y por tipo `pregame/in-game`.

## Plan por días

### Día 1 — Baseline y contrato de datos
- Definir esquema único de jugador (`player_id`, aliases) y partido (`game_id`).
- Crear tabla/JSON canónico para señal:
  - `timestamp`, `player`, `market`, `line`, `side`, `model_prob`, `implied_prob`, `edge`, `confidence`, `reason_codes`.
- Acordar formato de mensajes Telegram (plantilla única).

### Día 2 — Normalización de nombres y matching
- Resolver matching robusto NBA ↔ Polymarket:
  - lower-case, quitar acentos, sufijos (`Jr.`, `III`).
- Registrar score de matching y fallback manual.
- Guardar conflictos para revisión.

### Día 3 — Persistencia y trazabilidad
- Migrar estados críticos desde JSON a Postgres (o SQLite temporal si necesitas velocidad).
- Crear tablas mínimas:
  - `signals`, `markets_snapshot`, `player_game_state`, `results`.
- Añadir IDs determinísticos para deduplicación de alertas.

### Día 4 — Modelo pregame v1 (rápido)
- Proyección base por jugador/stat:
  - media ponderada últimos N juegos,
  - ajuste por minutos esperados,
  - ajuste por ritmo rival.
- Convertir proyección a probabilidad Over/Under (normal simple con desviación histórica).
- Activar señal si `edge >= umbral`.

### Día 5 — Gestión de riesgo v1
- Definir stake sugerido por buckets de confianza (no Kelly completo al inicio).
- Reglas hard stop:
  - máximo señales por día,
  - exposición por jugador/partido,
  - no entrar si spread de mercado es demasiado amplio.

### Día 6 — In-game features v1
- Features en vivo:
  - minutos jugados vs esperados,
  - usage aproximado,
  - faltas,
  - score margin/blowout risk.
- Recalcular probabilidad cada ciclo de polling.

### Día 7 — Alertas en vivo con hysteresis
- Implementar niveles:
  - `watch` (señal débil),
  - `entry` (umbral fuerte),
  - `avoid` (cambio adverso).
- Añadir hysteresis para evitar spam por oscilación de probabilidad.

### Día 8 — Backtest corto + sanity checks
- Backtest de 2–4 semanas de datos recientes (si disponibles).
- Validar:
  - calibración básica,
  - señales por partido,
  - sensibilidad a umbrales.
- Ajustar sólo parámetros de primer orden (sin sobreoptimizar).

### Día 9 — Calidad operativa
- Health checks por fuente de datos.
- Reintentos con jitter y circuit breaker simple.
- Alertas internas si falla fetch de odds o live feed.

### Día 10 — Evaluación de edge real
- Comparar señal vs precio de entrada y vs cierre (CLV proxy).
- Separar resultados por ventana temporal:
  - pregame,
  - Q1–Q2,
  - Q3–Q4.

### Día 11 — Refuerzo del modelo (sin complejidad excesiva)
- Añadir variables contextuales:
  - back-to-back,
  - home/away,
  - lesión reportada del quinteto.
- Mantener interpretabilidad (reason codes obligatorios).

### Día 12 — UX del bot
- Comandos Telegram recomendados:
  - `/status`, `/hoy`, `/open_signals`, `/resultados`, `/risk`.
- Mensajes compactos con semáforo y resumen de riesgo.

### Día 13 — Hardening de producción
- Control de rate limits por API.
- Rotación de logs + snapshots diarios.
- Plan de recuperación en reinicio (rehidratar estado del día).

### Día 14 — Go-live controlado
- Lanzar en modo “paper + observación” 3–7 días.
- Luego habilitar alertas operativas con límites conservadores.
- Definir criterio de éxito para pasar a v2.

## Reglas de señal sugeridas (arranque)

- **Pregame**:
  - enviar sólo si `edge >= 4%` y `confidence >= 70/100`.
- **In-game**:
  - enviar `entry` si `edge >= 6%`, `minutes_projection_valid = true`, sin red flags.
- **Bloqueos automáticos**:
  - jugador con minutos inciertos,
  - partido con blowout alto,
  - mercado sin liquidez mínima.

## Formato de alerta (Telegram)

```text
🟢 ENTRY | NBA Props
Jugador: J. Brunson
Mercado: PTS Over 27.5
Precio implícito: 54%
Prob modelo: 62%
Edge: +8.0%
Confianza: 78/100
Razones: ritmo alto, uso estable, minutos proyectados 36
Riesgos: foul risk moderado, posible blowout bajo
```

## Riesgos y mitigación

- **Data lag** → marca timestamp de cada fuente y descarta señales viejas.
- **Drift del mercado** → TTL corto por señal, invalidación rápida.
- **Sobreajuste** → recalibración semanal y límites de complejidad.
- **Ruido operativo** → cooldown por jugador/partido.

## Próximo paso inmediato

Implementar una versión v1 con:
- señal pregame + una señal in-game,
- registro de resultados,
- dashboard mínimo de métricas diarias.
