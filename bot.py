import os
import json
import logging
import re
import tempfile
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GHL_API_KEY = os.environ.get("GHL_API_KEY")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID")

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Content-Type": "application/json",
    "Version": "2021-07-28"
}

# ── GHL helpers ──────────────────────────────────────────────────────────────

def ghl_create_subaccount(name, email="", phone=""):
    payload = {
        "name": name, "phone": phone, "email": email,
        "address": "", "city": "", "state": "", "country": "CA",
        "postalCode": "", "timezone": "America/Toronto",
        "prospectInfo": {"email": email, "phone": phone, "name": name},
    }
    r = requests.post(f"{GHL_BASE}/locations/", headers=GHL_HEADERS, json=payload)
    logger.info(f"GHL create_subaccount status={r.status_code} body={r.text[:300]}")
    return r.json()

def ghl_create_contact(first, last="", email="", phone="", company="", notes=""):
    payload = {
        "locationId": GHL_LOCATION_ID,
        "firstName": first,
        "lastName": last,
        "phone": phone,
        "companyName": company,
    }
    # N'inclure email que s'il est valide — GHL refuse un email vide
    if email and "@" in email:
        payload["email"] = email
    r = requests.post(f"{GHL_BASE}/contacts/", headers=GHL_HEADERS, json=payload)
    logger.info(f"GHL create_contact status={r.status_code} body={r.text[:300]}")
    data = r.json()
    if notes and data.get("contact", {}).get("id"):
        ghl_add_note(data["contact"]["id"], notes)
    return data

def ghl_search_contact(name):
    r = requests.get(f"{GHL_BASE}/contacts/search", headers=GHL_HEADERS,
                     params={"locationId": GHL_LOCATION_ID, "query": name})
    logger.info(f"GHL search status={r.status_code} body={r.text[:300]}")
    return r.json()

def ghl_add_note(contact_id, note):
    payload = {"body": note, "userId": ""}
    r = requests.post(f"{GHL_BASE}/contacts/{contact_id}/notes", headers=GHL_HEADERS, json=payload)
    logger.info(f"GHL add_note status={r.status_code} body={r.text[:300]}")
    return r.json()

def ghl_update_contact(contact_id, fields):
    r = requests.put(f"{GHL_BASE}/contacts/{contact_id}", headers=GHL_HEADERS, json=fields)
    logger.info(f"GHL update_contact status={r.status_code} body={r.text[:300]}")
    return r.json()

def ghl_create_pipeline(name, stages):
    payload = {
        "name": name,
        "locationId": GHL_LOCATION_ID,
        "stages": [{"name": s} for s in stages]
    }
    r = requests.post(f"{GHL_BASE}/opportunities/pipelines", headers=GHL_HEADERS, json=payload)
    logger.info(f"GHL create_pipeline status={r.status_code} body={r.text[:300]}")
    return r.json()

def ghl_get_pipelines():
    r = requests.get(f"{GHL_BASE}/opportunities/pipelines", headers=GHL_HEADERS,
                     params={"locationId": GHL_LOCATION_ID})
    logger.info(f"GHL get_pipelines status={r.status_code} body={r.text[:300]}")
    return r.json()

def ghl_find_pipeline_by_name(pipeline_name):
    data = ghl_get_pipelines()
    pipelines = data.get("pipelines", [])
    name_lower = pipeline_name.lower()
    for p in pipelines:
        if p.get("name", "").lower() == name_lower:
            return p
    for p in pipelines:
        if name_lower in p.get("name", "").lower():
            return p
    return None

def ghl_add_lead_to_pipeline(contact_id, pipeline_id, stage_id="", title="", status="open"):
    payload = {
        "locationId": GHL_LOCATION_ID,
        "pipelineId": pipeline_id,
        "contactId": contact_id,
        "name": title or "Nouveau lead",
        "status": status,
    }
    if stage_id:
        payload["pipelineStageId"] = stage_id
    r = requests.post(f"{GHL_BASE}/opportunities/", headers=GHL_HEADERS, json=payload)
    logger.info(f"GHL add_lead_to_pipeline status={r.status_code} body={r.text[:300]}")
    return r.json()

# ── OpenAI : transcription vocale + GPT ──────────────────────────────────────

def transcribe_audio(file_path):
    """Transcrit un fichier audio en texte via Whisper."""
    with open(file_path, "rb") as f:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (os.path.basename(file_path), f, "audio/ogg")},
            data={"model": "whisper-1", "language": "fr"}
        )
    logger.info(f"Whisper status={r.status_code} body={r.text[:200]}")
    return r.json().get("text", "")

SYSTEM_PROMPT = """Tu es un assistant IA pour un gestionnaire de projets de construction au Québec.
Tu interprètes ses commandes vocales ou textuelles en français québécois naturel
et tu retournes une action JSON à exécuter dans GoHighLevel (GHL).

Réponds TOUJOURS en JSON valide avec ce format exact, rien d'autre:
{
  "action": "<nom_de_l_action>",
  "params": { ... },
  "confirmation": "<message de confirmation en français>"
}

Actions disponibles:
- create_subaccount: créer un nouveau sub-account client GHL
  params: name (obligatoire), email, phone
- create_contact: créer un nouveau contact
  params: first (obligatoire), last, email, phone, company, notes
- search_contact: chercher un contact existant
  params: name (obligatoire)
- add_note: ajouter une note à un contact
  params: contact_name (obligatoire), note (obligatoire)
- update_contact: modifier un contact
  params: contact_name (obligatoire), fields {}
- create_pipeline: créer un pipeline avec étapes
  params: name (obligatoire), stages [] (liste d'étapes)
- get_pipelines: lister les pipelines existants
  params: {}
- add_to_pipeline: ajouter un lead (contact existant ou nouveau) dans un pipeline
  params: contact_name (obligatoire), pipeline_name (obligatoire), stage_name (optionnel), title (optionnel), first, last, email, phone, company
- add_leads_to_pipeline: ajouter une liste de leads dans un pipeline
  params: pipeline_name (obligatoire), stage_name (optionnel), leads [] (liste d'objets avec first, last, email, phone, company, title)
- unknown: commande incomprise
  params: {}

Exemples:
"Nouveau client Construction Tremblay" → {"action":"create_subaccount","params":{"name":"Construction Tremblay"},"confirmation":"Je crée le sub-account Construction Tremblay..."}
"Ajoute Jean Tremblay entrepreneur 514-555-0101" → {"action":"create_contact","params":{"first":"Jean","last":"Tremblay","phone":"514-555-0101","company":"Entrepreneur"},"confirmation":"Je crée le contact Jean Tremblay..."}
"Note pour Jean Tremblay: rappel vendredi soumission Laval" → {"action":"add_note","params":{"contact_name":"Jean Tremblay","note":"Rappel vendredi pour soumission Laval"},"confirmation":"J'ajoute la note à Jean Tremblay..."}
"Crée pipeline Construction: Prospect, Soumission, Contrat signé, En cours, Complété" → {"action":"create_pipeline","params":{"name":"Construction","stages":["Prospect","Soumission","Contrat signé","En cours","Complété"]},"confirmation":"Je crée le pipeline Construction..."}
"Ajoute Jean Dupont au pipeline Ventes" → {"action":"add_to_pipeline","params":{"contact_name":"Jean Dupont","pipeline_name":"Ventes"},"confirmation":"J'ajoute Jean Dupont dans le pipeline Ventes..."}
"Ajoute ces leads au pipeline Prospects: Jean Tremblay 514-111-0101, Marie Côté marie@example.com" → {"action":"add_leads_to_pipeline","params":{"pipeline_name":"Prospects","leads":[{"first":"Jean","last":"Tremblay","phone":"514-111-0101"},{"first":"Marie","last":"Côté","email":"marie@example.com"}]},"confirmation":"J'ajoute 2 leads dans le pipeline Prospects..."}

Réponds UNIQUEMENT en JSON valide. Jamais de markdown, jamais de texte avant ou après."""

def ask_gpt(text):
    payload = {
        "model": "gpt-4o",
        "max_tokens": 1000,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ]
    }
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json=payload
    )
    logger.info(f"GPT status={r.status_code} body={r.text[:500]}")

    if r.status_code != 200:
        resp = r.json()
        raise Exception(f"OpenAI erreur {r.status_code}: {resp.get('error', {}).get('message', str(resp))}")

    resp = r.json()
    raw = resp["choices"][0]["message"]["content"]
    raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
    return json.loads(raw)

# ── Action executor ───────────────────────────────────────────────────────────

def execute_action(action, params):
    try:
        if action == "create_subaccount":
            result = ghl_create_subaccount(params.get("name",""), params.get("email",""), params.get("phone",""))
            if result.get("location") or result.get("id"):
                return "✅ Sub-account créé avec succès dans GHL!"
            return f"⚠️ Réponse GHL: {json.dumps(result)[:300]}"

        elif action == "create_contact":
            result = ghl_create_contact(
                params.get("first",""), params.get("last",""),
                params.get("email",""), params.get("phone",""),
                params.get("company",""), params.get("notes","")
            )
            c = result.get("contact")
            if c:
                return f"✅ Contact créé: {c.get('firstName','')} {c.get('lastName','')}"
            return f"⚠️ Réponse GHL: {json.dumps(result)[:300]}"

        elif action == "search_contact":
            result = ghl_search_contact(params.get("name",""))
            contacts = result.get("contacts", [])
            if not contacts:
                return f"🔍 Aucun contact trouvé pour: {params.get('name')}"
            lines = [f"🔍 {len(contacts)} contact(s) trouvé(s):"]
            for c in contacts[:5]:
                lines.append(f"  • {c.get('firstName','')} {c.get('lastName','')} — {c.get('phone','')}")
            return "\n".join(lines)

        elif action == "add_note":
            search = ghl_search_contact(params.get("contact_name",""))
            contacts = search.get("contacts", [])
            if not contacts:
                return f"❌ Contact introuvable: {params.get('contact_name')}"
            contact_id = contacts[0]["id"]
            name = f"{contacts[0].get('firstName','')} {contacts[0].get('lastName','')}".strip()
            ghl_add_note(contact_id, params.get("note",""))
            return f"✅ Note ajoutée à {name}"

        elif action == "update_contact":
            search = ghl_search_contact(params.get("contact_name",""))
            contacts = search.get("contacts", [])
            if not contacts:
                return f"❌ Contact introuvable: {params.get('contact_name')}"
            contact_id = contacts[0]["id"]
            name = f"{contacts[0].get('firstName','')} {contacts[0].get('lastName','')}".strip()
            ghl_update_contact(contact_id, params.get("fields", {}))
            return f"✅ Contact {name} mis à jour"

        elif action == "create_pipeline":
            result = ghl_create_pipeline(params.get("name",""), params.get("stages",[]))
            if result.get("pipeline") or result.get("id"):
                stages = params.get("stages",[])
                return f"✅ Pipeline '{params.get('name')}' créé avec {len(stages)} étapes: {', '.join(stages)}"
            return f"⚠️ Réponse GHL: {json.dumps(result)[:300]}"

        elif action == "get_pipelines":
            result = ghl_get_pipelines()
            pipelines = result.get("pipelines", [])
            if not pipelines:
                return "📋 Aucun pipeline trouvé"
            lines = [f"📋 {len(pipelines)} pipeline(s):"]
            for p in pipelines:
                lines.append(f"  • {p.get('name','')} ({len(p.get('stages',[]))} étapes)")
            return "\n".join(lines)

        elif action == "add_to_pipeline":
            pipeline_name = params.get("pipeline_name", "")
            pipeline = ghl_find_pipeline_by_name(pipeline_name)
            if not pipeline:
                return f"❌ Pipeline introuvable: {pipeline_name}. Utilise 'Montre mes pipelines' pour voir la liste."

            stage_id = ""
            stage_name = params.get("stage_name", "")
            if stage_name:
                for s in pipeline.get("stages", []):
                    if stage_name.lower() in s.get("name", "").lower():
                        stage_id = s.get("id", "")
                        break

            contact_name = params.get("contact_name", "")
            search = ghl_search_contact(contact_name)
            contacts = search.get("contacts", [])
            if contacts:
                contact_id = contacts[0]["id"]
                name = f"{contacts[0].get('firstName','')} {contacts[0].get('lastName','')}".strip()
            else:
                parts = contact_name.split()
                created = ghl_create_contact(
                    parts[0] if parts else contact_name,
                    " ".join(parts[1:]) if len(parts) > 1 else "",
                    params.get("email", ""), params.get("phone", ""),
                    params.get("company", "")
                )
                c = created.get("contact", {})
                if not c or not c.get("id"):
                    return f"❌ Impossible de créer/trouver le contact: {contact_name}"
                contact_id = c["id"]
                name = f"{c.get('firstName','')} {c.get('lastName','')}".strip()

            title = params.get("title", "") or name
            result = ghl_add_lead_to_pipeline(contact_id, pipeline["id"], stage_id, title)
            if result.get("opportunity") or result.get("id"):
                stage_info = f" (étape: {stage_name})" if stage_name else ""
                return f"✅ {name} ajouté au pipeline '{pipeline.get('name')}'{stage_info}"
            return f"⚠️ Réponse GHL: {json.dumps(result)[:300]}"

        elif action == "add_leads_to_pipeline":
            pipeline_name = params.get("pipeline_name", "")
            pipeline = ghl_find_pipeline_by_name(pipeline_name)
            if not pipeline:
                return f"❌ Pipeline introuvable: {pipeline_name}. Utilise 'Montre mes pipelines' pour voir la liste."

            stage_id = ""
            stage_name = params.get("stage_name", "")
            if stage_name:
                for s in pipeline.get("stages", []):
                    if stage_name.lower() in s.get("name", "").lower():
                        stage_id = s.get("id", "")
                        break

            leads = params.get("leads", [])
            if not leads:
                return "❌ Aucun lead fourni dans la liste."

            success, errors = [], []
            for lead in leads:
                try:
                    contact_name = f"{lead.get('first','')} {lead.get('last','')}".strip()
                    search = ghl_search_contact(contact_name)
                    existing = search.get("contacts", [])
                    if existing:
                        contact_id = existing[0]["id"]
                    else:
                        created = ghl_create_contact(
                            lead.get("first", ""), lead.get("last", ""),
                            lead.get("email", ""), lead.get("phone", ""),
                            lead.get("company", "")
                        )
                        c = created.get("contact", {})
                        if not c or not c.get("id"):
                            errors.append(contact_name)
                            continue
                        contact_id = c["id"]

                    title = lead.get("title", "") or contact_name
                    result = ghl_add_lead_to_pipeline(contact_id, pipeline["id"], stage_id, title)
                    if result.get("opportunity") or result.get("id"):
                        success.append(contact_name)
                    else:
                        errors.append(contact_name)
                except Exception as e:
                    errors.append(f"{lead.get('first','')} {lead.get('last','')} ({e})")

            lines = [f"📋 Pipeline '{pipeline.get('name')}' — résultat:"]
            if success:
                lines.append(f"✅ {len(success)} lead(s) ajouté(s): {', '.join(success)}")
            if errors:
                lines.append(f"❌ {len(errors)} échec(s): {', '.join(errors)}")
            return "\n".join(lines)

        elif action == "unknown":
            return "❓ Je n'ai pas compris. Peux-tu reformuler?"

        else:
            return f"❓ Action inconnue: {action}"

    except Exception as e:
        logger.error(f"execute_action error: {e}")
        return f"❌ Erreur: {str(e)}"

# ── Telegram handlers ─────────────────────────────────────────────────────────

async def process_command(text, update):
    """Traite une commande texte et répond."""
    try:
        parsed = ask_gpt(text)
        action = parsed.get("action", "unknown")
        params = parsed.get("params", {})
        confirmation = parsed.get("confirmation", "")
        result = execute_action(action, params)
        await update.message.reply_text(f"{confirmation}\n\n{result}")
    except Exception as e:
        logger.error(f"process_command error: {e}")
        await update.message.reply_text(f"❌ Erreur: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text.startswith("/"):
        return
    await update.message.reply_text("⏳ Je traite ta commande...")
    await process_command(text, update)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎤 Message vocal reçu, je transcris...")
    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            text = transcribe_audio(tmp.name)

        if not text:
            await update.message.reply_text("⚠️ Impossible de transcrire le vocal. Essaie en texte.")
            return

        await update.message.reply_text(f"📝 J'ai compris: *{text}*", parse_mode="Markdown")
        await update.message.reply_text("⏳ J'exécute dans GHL...")
        await process_command(text, update)

    except Exception as e:
        logger.error(f"handle_voice error: {e}")
        await update.message.reply_text(f"❌ Erreur vocal: {str(e)}")

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Bonjour! Je suis ton assistant GHL.*\n\n"
        "Envoie-moi une commande texte ou un 🎤 *message vocal*, par exemple:\n\n"
        "• _Nouveau client Construction Tremblay_\n"
        "• _Ajoute Jean Dupont 514-555-0101_\n"
        "• _Note pour Jean Dupont: rappel vendredi soumission_\n"
        "• _Crée pipeline Construction: Prospect, Soumission, Contrat_\n"
        "• _Montre mes pipelines_\n"
        "• _Ajoute Jean Tremblay au pipeline Ventes_\n"
        "• _Ajoute ces leads au pipeline Prospects: Jean Dupont 514-111-0101, Marie Côté marie@test.com_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Regex(r'^/start'), handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Bot démarré avec OpenAI (GPT-4o + Whisper)...")
    app.run_polling()

if __name__ == "__main__":
    main()
