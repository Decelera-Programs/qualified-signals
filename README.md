# Qualified Signals

Servicio webhook en **FastAPI** que recibe envíos de un formulario (por ejemplo Tally), evalúa las respuestas de screening y actualiza la entrada correspondiente en **Attio** (lista, señales, estado del funnel y votos por tier).

## Qué hace

1. Expone `POST /webhook` y espera un JSON con la estructura típica de Tally (`submission.questions`).
2. Obtiene el dominio del formulario, busca la compañía en Attio, el deal asociado y la entrada de la lista configurada.
3. Calcula un veredicto a partir de las preguntas (thesis, criterios críticos y complementarios) y construye textos para señales, flags verdes/rojos y comentarios.
4. Acumula payloads y comentarios con los ya guardados en la entrada.
5. Actualiza el estado del funnel según el tier actual (Tier 1 / Tier 2) y los contadores de OK/KO; si en Tier 1 hay un OK y un KO, marca la necesidad de pasar a Tier 2.
6. Hace `PATCH` a la entrada de Attio con los campos configurados (`signals_qualified`, `status`, `screening_conviction`, etc.).

## Requisitos

- Python 3.10+ (recomendado)
- Cuenta Attio con API key y una lista cuyo slug conozcas

## Configuración

Variables de entorno:

| Variable        | Descripción                                      |
|-----------------|--------------------------------------------------|
| `ATTIO_API_KEY` | Token Bearer de la API v2 de Attio               |
| `LIST_SLUG`     | Slug de la lista donde viven las entradas      |
| `PORT`          | Puerto del servidor (por defecto `8000`)       |

## Instalación y ejecución

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set ATTIO_API_KEY=tu_clave
set LIST_SLUG=tu_list_slug
python main.py
```

En Linux o macOS, sustituye `set` por `export`.

El servidor queda en `http://0.0.0.0:8000` (o el puerto indicado en `PORT`). El webhook está en:

`POST http://<host>:<puerto>/webhook`

## Documentación de la API

Con el servidor en marcha puedes abrir la documentación interactiva de FastAPI en `/docs`.

## Tiering automático desde signals (compañías sin form score)

Además de acumular el veredicto en `screening_conviction`, el webhook calcula automáticamente
el campo **Tier** (`tier_5`) para compañías que **no** tienen `form_score` en Attio (es decir,
que no llegaron por el formulario de founder application: referrals, intros de inversores,
outbound). Si la entrada tiene `form_score`, esta capa no se ejecuta y todo se comporta como
antes.

El cálculo toma el **último veredicto de cada reviewer** (el historial acumulado en
`screening_conviction`) y aplica:

| Situación                                              | Resultado       |
|---------------------------------------------------------|-----------------|
| Unanimidad en 🔥 STRONG YES                              | Tier 1          |
| Solo "yes" pero sin unanimidad en STRONG YES (mezcla con 🤢 WEAK YES) | Tier 2 |
| Algún 🛑 STRONG NO (sin ningún "yes")                     | Killed (kill automático) |
| Solo 🤔 WEAK NO (sin STRONG NO ni "yes")                  | Tier 3          |
| Solo ❓ INDEFINIDO                                        | Tier 3          |
| Mezcla real de "yes" y "no" entre reviewers distintos     | Review Flag     |

El kill de esta capa usa `reason = "Screening conviction"`, igual que el resto del sistema.
No pisa nunca el status en el caso de tier/Review Flag — solo escribe `tier_5`.

## Notas

- El orden y número de preguntas del formulario deben coincidir con los índices definidos en `main.py` (`DOMAIN_INDEX`, `FLAGS_START`, etc.).
- Si cambias el formulario, revisa esas constantes antes de desplegar.
