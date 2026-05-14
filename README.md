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

## Notas

- El orden y número de preguntas del formulario deben coincidir con los índices definidos en `main.py` (`DOMAIN_INDEX`, `FLAGS_START`, etc.).
- Si cambias el formulario, revisa esas constantes antes de desplegar.
