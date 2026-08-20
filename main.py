from fastapi import FastAPI, Request, HTTPException
import httpx
import os
import logging
import random
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AttioSignals")

# ---CONFIGURACION---
ATTIO_API_KEY = os.getenv("ATTIO_API_KEY")
LIST_SLUG = os.getenv("LIST_SLUG")
LATAM_LIST_SLUG = "startups_deal_flow_2"
MEXICO_STAGES = {"Mexico 2026", "Leads Mexico 2026"}
BASE_URL = "https://api.attio.com/v2"
HEADERS = {
    "Authorization": f"Bearer {ATTIO_API_KEY}",
    "Content-Type": "application/json"
}

def get_active_list(deal_stage: str) -> str:
    return LATAM_LIST_SLUG if deal_stage in MEXICO_STAGES else LIST_SLUG

# Pools de evaluadores
TIER1_ANALYSTS = ["Diego", "Carlota", "Luiza"]
TIER2_SENIORS  = ["Lorenzo", "Raquel"]
CALL_EVALUATORS = ["Diego", "Carlota", "Lorenzo", "Raquel"]  # Luiza no está en las opciones de call_evaluator

# Constantes de índices - layout ORIGINAL (sin la pregunta de stage)
# Si el form incluye "Form Completion Stage" en la posición 2, todos los
# índices a partir de FLAGS_START se desplazan +1 automáticamente.
REVIEWER_INDEX = 0
DOMAIN_INDEX = 1
FLAGS_START = 2
FLAGS_END = 9
MULTI_FLAGS_START = 9
MULTI_FLAGS_END = 11
COMMENTS_INDEX = 11

# Nombre de la pregunta nueva en Fillout (case-insensitive)
STAGE_QUESTION_NAME = "form completion stage"

app = FastAPI()

# ---DETECCION DE LAYOUT---

def _question_label(q: dict) -> str:
    """Fillout/Tally usan claves distintas para el enunciado."""
    for key in ("name", "label", "title", "question"):
        val = q.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""

def detect_layout(questions: list) -> tuple:
    """
    Devuelve (shift, stage_value).
    shift = 1 si el form incluye la pregunta de stage, 0 si es el form antiguo.
    """
    for i, q in enumerate(questions):
        if _question_label(q).lower() == STAGE_QUESTION_NAME:
            stage_val = q.get("value", "") or ""
            if isinstance(stage_val, list):
                stage_val = ", ".join(str(v) for v in stage_val)
            shift = 1 if i > DOMAIN_INDEX else 0
            return shift, str(stage_val).strip()

    # Fallback: no encontramos la pregunta por nombre, pero llegan más
    # respuestas de las esperadas -> asumimos layout nuevo para no
    # desalinear las flags. Sin stage en el texto, pero veredicto correcto.
    if len(questions) > COMMENTS_INDEX + 1:
        logger.warning(
            "Pregunta de stage no encontrada por nombre pero llegan "
            f"{len(questions)} respuestas. Asumiendo layout nuevo sin stage."
        )
        return 1, ""

    return 0, ""

# ---FUNCIONES AUXILIARES---

async def find_company_id_from_domain(domain: str) -> str:
    url = f"{BASE_URL}/objects/companies/records/query"
    payload = {"filter": {"domains": {"domain": domain}}, "limit": 1}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(url, headers=HEADERS, json=payload)
            res.raise_for_status()
            data = res.json().get("data", [])
            return data[0].get("id", {}).get("record_id", "") if data else ""
        except Exception as e:
            logger.error(f"Error buscando compañía: {e}")
            return ""

async def find_deal_from_company_id(company_id: str) -> tuple:
    url = f"{BASE_URL}/objects/deals/records/query"
    payload = {
        "filter": {
            "associated_company": {
                "target_object": "companies",
                "target_record_id": company_id
            }
        },
        "limit": 1
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(url, headers=HEADERS, json=payload)
            res.raise_for_status()
            data = res.json().get("data", [])
            if not data:
                return "", ""
            deal = data[0]
            deal_id = deal.get("id", {}).get("record_id", "")
            stage_list = deal.get("values", {}).get("stage", [])
            deal_stage = stage_list[0].get("status", {}).get("title", "") if stage_list else ""
            return deal_id, deal_stage
        except Exception as e:
            logger.error(f"Error buscando deal: {e}")
            return "", ""

async def find_entry_from_deal_id(deal_id: str, list_slug: str):
    url = f"{BASE_URL}/lists/{list_slug}/entries/query"
    payload = {
        "filter": {
            "path": [[list_slug, "parent_record"], ["deals", "record_id"]],
            "constraints": {"value": deal_id}
        },
        "limit": 1
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            res = await client.post(url, headers=HEADERS, json=payload)
            res.raise_for_status()
            data = res.json().get("data", [])
            if not data: return "", {}
            return data[0].get("id", {}).get("entry_id", ""), data[0].get("entry_values", {})
        except Exception as e:
            logger.error(f"Error buscando entry: {e}")
            return "", {}

def generar_payload(form_data, tier_actual="Tier 1"):
    questions = form_data.get("submission", {}).get("questions", [])

    shift, stage = detect_layout(questions)

    flags_start = FLAGS_START + shift
    flags_end = FLAGS_END + shift
    multi_start = MULTI_FLAGS_START + shift
    multi_end = MULTI_FLAGS_END + shift
    comments_index = COMMENTS_INDEX + shift

    if len(questions) < comments_index + 1:
        raise ValueError("Form data incompleto")

    logger.info(f"DEBUG LAYOUT: shift={shift} | stage='{stage}' | n_questions={len(questions)}")

    reviewer = questions[REVIEWER_INDEX].get("value", "")
    domain = questions[DOMAIN_INDEX].get("value", "")
    comments_raw = questions[comments_index].get("value", "")

    # Extraemos las 7 preguntas base (P1 a P7)
    # P1: Thesis | P2-P4: Críticos | P5-P7: Complementarios
    base_flags = [q.get("value", "") for q in questions[flags_start:flags_end]]

    # Extraemos multi-flags (P8+) para el detalle visual
    multi_flags = []
    for q in questions[multi_start:multi_end]:
        val = q.get("value")
        if isinstance(val, list): multi_flags.extend(val)
        elif val: multi_flags.append(val)

    def evaluar_veredicto(p_list):
        if len(p_list) < 7: 
            logger.warning(f"Lista de preguntas corta: {len(p_list)}")
            return "⚠️ ERROR: Faltan preguntas", False
        
        p1 = p_list[0]          # Thesis
        criticos = p_list[1:4]  # P2, P3, P4
        compl = p_list[4:7]     # P5, P6, P7

        # Conteos precisos
        c_verdes = sum(1 for v in criticos if "🟢" in v)
        c_rojos = sum(1 for v in criticos if "🔴" in v)
        comp_verdes = sum(1 for v in compl if "🟢" in v)
        comp_rojos = sum(1 for v in compl if "🔴" in v)
        
        # Log para debug (ver esto en la consola)
        logger.info(f"DEBUG EVAL: P1:{p1} | Crit_V:{c_verdes} Crit_R:{c_rojos} | Comp_V:{comp_verdes} Comp_R:{comp_rojos}")

        # 1. 🔥 STRONG YES
        if "🟢" in p1 and c_verdes >= 2 and comp_verdes >= 1 and c_rojos == 0 and comp_rojos == 0:
            return "🔥 STRONG YES (Pre-IC)", True

        # 2. 🤢 WEAK YES (Tu caso: P1 Amarillo + 1 Verde Crit + 1 Verde Compl + 0 Rojas)
        # Ajustado: Ahora solo pide c_verdes >= 1 y comp_verdes >= 1
        if ("🟢" in p1 or "🟡" in p1) and c_verdes >= 1 and comp_verdes >= 1 and c_rojos == 0 and comp_rojos == 0:
            return "🤢 WEAK YES (In play)", True

        # 3. 🤔 WEAK NO
        # Si hay CUALQUIER rojo en complementarios, o si no llegamos al mínimo de verdes
        if ("🟢" in p1 or "🟡" in p1) and (comp_rojos >= 1 or (c_verdes == 0 and comp_verdes == 0)):
            return "🤔 WEAK NO (Descarte)", False

        # 4. 🛑 STRONG NO
        if "🔴" in p1 or c_rojos >= 1:
            return "🛑 STRONG NO (Muerte)", False

        return "❓ INDEFINIDO (sin cambio de status)", None

    veredicto_nombre, es_voto_ok = evaluar_veredicto(base_flags)

    # --- CONSTRUCCIÓN DEL PAYLOAD ---
    if es_voto_ok is True:
        voto_icon = "✅"
    elif es_voto_ok is False:
        voto_icon = "❌"
    else:
        voto_icon = "➖"

    contexto = f"{tier_actual} · {stage}" if stage else tier_actual
    payload = f"Reviewer: {reviewer} ({contexto})\n"
    payload += f"Veredicto: {voto_icon} {veredicto_nombre}\n"
    payload += "\n-- DETALLE --\n"

    all_flags = base_flags + multi_flags
    green_txt, red_txt = f"{reviewer}:\n", f"{reviewer}:\n"
    
    for flag in all_flags:
        if not flag: continue
        payload += f"{flag}\n"
        if "🟢" in flag: green_txt += f"{flag}\n"
        elif "🔴" in flag: red_txt += f"{flag}\n"

    comments = f"{reviewer}: {comments_raw}" if comments_raw else ""

    return domain, payload, green_txt, red_txt, comments, reviewer, veredicto_nombre, es_voto_ok, stage

def calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status=None):
    if tier_actual == "Tier 2":
        # Senior decide: un voto es suficiente
        if t2_ok >= 1: return "In play", True
        if t2_ko >= 1: return "Killed", False
        return default_status or "Qualified", True

    # Tier 1: necesita unanimidad de 2 analistas
    if t1_ok >= 2: return "In play", True
    if t1_ko >= 2: return "Killed", False
    # Empate (1-1) o pendiente (1-0 / 0-1): sin cambio hasta cerrar Tier 1
    return default_status or "Qualified", True

async def upload_reviewer_ko_ok(entry_id, es_voto_ok, reviewer, tier, list_slug):
    url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
    field = ""
    if es_voto_ok is None:
        return
    if tier == "Tier 1":
        field = "tier_1_ok" if es_voto_ok else "tier_1_ko"
    elif tier == "Tier 2":
        field = "tier_2_ok" if es_voto_ok else "tier_2_ko"

    if not field: return
    data = {"data": {"entry_values": {field: [{"option": reviewer}]}}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.patch(url, headers=HEADERS, json=data)

async def upload_senior_needed(entry_id, list_slug):
    url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
    data = {"data": {"entry_values": {"tier_5": [{"status": "Tier 2"}]}}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.patch(url, headers=HEADERS, json=data)

async def upload_attio_entry(entry_id, payload, green, red, comments, status, veredicto_nombre, list_slug, qualified=True):
    url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
    entry_values = {
        "signals_qualified": [{"value": payload}],
        "green_flags_qualified": [{"value": green}],
        "red_flags_qualified": [{"value": red}],
        "status": [{"status": status}],
        "screening_conviction": [{"value": veredicto_nombre}]
    }
    if comments and comments.strip():
        entry_values["signals_comments_qualified"] = [{"value": comments}]
        
    if not qualified:
        entry_values["reason"] = [{"status": "Screening conviction"}]

    data = {"data": {"entry_values": entry_values}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.patch(url, headers=HEADERS, json=data)
        res.raise_for_status()

async def assign_pre_call_evaluators(entry_id: str, tier: str, list_slug: str):
    if tier == "Tier 1":
        selected = random.sample(TIER1_ANALYSTS, 2)
    else:
        selected = [random.choice(TIER2_SENIORS)]
    url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
    data = {"data": {"entry_values": {
        "pre_call_evaluator": [{"option": name} for name in selected]
    }}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.patch(url, headers=HEADERS, json=data)
    logger.info(f"Pre-call evaluators asignados ({tier}): {selected}")

async def assign_call_evaluator(entry_id: str, list_slug: str):
    selected = random.choice(CALL_EVALUATORS)
    url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
    data = {"data": {"entry_values": {
        "call_evaluator": [{"option": selected}]
    }}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.patch(url, headers=HEADERS, json=data)
    logger.info(f"Call evaluator asignado: {selected}")

# --- TIERING AUTOMÁTICO DESDE SIGNALS (compañías sin form score) ---

def has_form_score(entry_values: dict) -> bool:
    values = entry_values.get("form_score", [])
    return bool(values) and values[0].get("value") is not None

def parse_latest_verdict_per_reviewer(conviction_text: str) -> dict:
    latest = {}
    for line in conviction_text.split("\n---\n"):
        line = line.strip()
        if ":" not in line:
            continue
        reviewer, _, verdict = line.partition(":")
        # El nombre puede venir como "Luiza (post-call, deck)" -> nos quedamos con "Luiza"
        reviewer = reviewer.partition("(")[0].strip()
        verdict = verdict.strip()
        if reviewer and reviewer not in latest:
            latest[reviewer] = verdict
    return latest

def classify_verdict(verdict: str) -> str:
    if "STRONG YES" in verdict: return "strong_yes"
    if "WEAK YES" in verdict:   return "weak_yes"
    if "STRONG NO" in verdict:  return "strong_no"
    if "WEAK NO" in verdict:    return "weak_no"
    if "INDEFINIDO" in verdict: return "indefinido"
    return "unknown"

def calculate_signals_tier(conviction_text: str):
    """Devuelve ('tier', 'Tier 1'|'Tier 2'|'Tier 3'|'Review Flag'), ('kill', None) o (None, None)."""
    latest = parse_latest_verdict_per_reviewer(conviction_text)
    categories = [classify_verdict(v) for v in latest.values()]

    has_yes = any(c in ("strong_yes", "weak_yes") for c in categories)
    has_no = any(c in ("strong_no", "weak_no") for c in categories)

    if has_yes and has_no:
        return "tier", "Review Flag"

    if has_no:
        if any(c == "strong_no" for c in categories):
            return "kill", None
        return "tier", "Tier 3"  # solo WEAK NO

    if has_yes:
        yes_categories = [c for c in categories if c in ("strong_yes", "weak_yes")]
        if all(c == "strong_yes" for c in yes_categories):
            return "tier", "Tier 1"  # unanimidad en STRONG YES
        return "tier", "Tier 2"  # mezcla o solo WEAK YES

    if any(c == "indefinido" for c in categories):
        return "tier", "Tier 3"

    return None, None

async def apply_signals_tier(entry_id: str, list_slug: str, action: str, tier_value: str):
    url = f"{BASE_URL}/lists/{list_slug}/entries/{entry_id}"
    if action == "kill":
        data = {"data": {"entry_values": {
            "status": [{"status": "Killed"}],
            "reason": [{"status": "Screening conviction"}],
        }}}
    else:
        data = {"data": {"entry_values": {"tier_5": [{"status": tier_value}]}}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.patch(url, headers=HEADERS, json=data)
    logger.info(f"Signals tiering: entry {entry_id} -> {action} {tier_value}")

# --- WEBHOOK PRINCIPAL ---

@app.post("/webhook")
async def handle_signals(request: Request):
    try:
        form_data = await request.json()

        questions = form_data.get("submission", {}).get("questions", [])
        if len(questions) <= DOMAIN_INDEX:
            raise HTTPException(status_code=400, detail="Form data incompleto: falta domain")
        domain = questions[DOMAIN_INDEX].get("value", "")
        
        company_id = await find_company_id_from_domain(domain)
        deal_id, deal_stage = await find_deal_from_company_id(company_id)
        list_slug = get_active_list(deal_stage)
        entry_id, entry_values = await find_entry_from_deal_id(deal_id, list_slug)

        if not entry_id:
            raise HTTPException(status_code=404, detail="Entry no encontrada")

        tier_list = entry_values.get("tier_5", [])
        tier_actual = tier_list[0].get("status", {}).get("title", "Tier 1") if tier_list else "Tier 1"
        status_list = entry_values.get("status", [])
        status_actual = status_list[0].get("status", {}).get("title") if status_list else "" 

        _, payload, green_flags, red_flags, new_comment, reviewer, veredicto_nombre, es_voto_ok, stage = generar_payload(form_data, tier_actual)
        
        await upload_reviewer_ko_ok(entry_id, es_voto_ok, reviewer, tier_actual, list_slug)

        t1_ok = len(entry_values.get("tier_1_ok", []))
        t1_ko = len(entry_values.get("tier_1_ko", []))
        t2_ok = len(entry_values.get("tier_2_ok", []))
        t2_ko = len(entry_values.get("tier_2_ko", []))

        if tier_actual == "Tier 1":
            if es_voto_ok is True:
                t1_ok += 1
            elif es_voto_ok is False:
                t1_ko += 1
        else:
            if es_voto_ok is True:
                t2_ok += 1
            elif es_voto_ok is False:
                t2_ko += 1

        ex_payload_list = entry_values.get("signals_qualified", [])
        if ex_payload_list:
            ex_p = ex_payload_list[0].get("value", "")
            ex_green_list = entry_values.get("green_flags_qualified", [])
            ex_red_list = entry_values.get("red_flags_qualified", [])
            ex_g = ex_green_list[0].get("value", "") if ex_green_list else ""
            ex_r = ex_red_list[0].get("value", "") if ex_red_list else ""
            payload = f"{payload}\n---\n{ex_p}"
            green_flags = f"{green_flags}\n---\n{ex_g}"
            red_flags = f"{red_flags}\n---\n{ex_r}"

        ex_comments_list = entry_values.get("signals_comments_qualified", [])
        ex_comments = ex_comments_list[0].get("value", "") if ex_comments_list else ""
        
        if new_comment:
            final_comments = f"{new_comment}\n---\n{ex_comments}" if ex_comments else new_comment
        else:
            final_comments = ex_comments

        current_st_list = entry_values.get("status", [])
        default_status = current_st_list[0].get("status", {}).get("title", "") if current_st_list else ""
        status, qualified = calculate_funnel_status(tier_actual, t1_ok, t1_ko, t2_ok, t2_ko, default_status)

        pre_call_assigned = bool(entry_values.get("pre_call_evaluator", []))
        call_eval_assigned = bool(entry_values.get("call_evaluator", []))

        # Primera votación en Tier 1 → asignar 2 analistas al azar
        if tier_actual == "Tier 1" and (t1_ok + t1_ko) == 1 and not pre_call_assigned:
            await assign_pre_call_evaluators(entry_id, "Tier 1", list_slug)

        # Empate en Tier 1 → escalar a Tier 2 y reasignar a un senior
        if tier_actual == "Tier 1" and t1_ok == 1 and t1_ko == 1:
            await upload_senior_needed(entry_id, list_slug)
            await assign_pre_call_evaluators(entry_id, "Tier 2", list_slug)

        # Deal llega a "In play" → asignar call evaluator al azar
        if status == "In play" and not call_eval_assigned:
            await assign_call_evaluator(entry_id, list_slug)

        new_conviction_line = f"{reviewer} ({stage}): {veredicto_nombre}" if stage else f"{reviewer}: {veredicto_nombre}"
        ex_conviction_list = entry_values.get("screening_conviction", [])
        ex_conviction = ex_conviction_list[0].get("value", "") if ex_conviction_list else ""

        final_conviction = f"{new_conviction_line}\n---\n{ex_conviction}" if ex_conviction else new_conviction_line

        await upload_attio_entry(entry_id, payload, green_flags, red_flags, final_comments, status, final_conviction, list_slug, qualified)

        if not has_form_score(entry_values):
            tier_action, tier_value = calculate_signals_tier(final_conviction)
            if tier_action:
                await apply_signals_tier(entry_id, list_slug, tier_action, tier_value)

        if es_voto_ok is True:
            veredicto_webhook = "OK"
        elif es_voto_ok is False:
            veredicto_webhook = "KO"
        else:
            veredicto_webhook = "INDEFINIDO"
        return {"status": "success", "veredicto": veredicto_webhook}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
