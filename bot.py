import os
import json
import logging
import re
import tempfile
import requests
from datetime import datetime, timedelta, timezone
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

def ghl_create_task(contact_id, title, due_date=None):
    if due_date is None:
        due_date = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    payload = {"title": title, "dueDate": due_date, "completed": False}
    r = requests.post(f"{GHL_BASE}/contacts/{contact_id}/tasks", headers=GHL_HEADERS, json=payload)
    logger.info(f"GHL create_task status={r.status_code} body={r.text[:300]}")
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

def ghl_get_conversations(hours=24):
    """Récupère les conversations d'appels des dernières N heures."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    params = {
        "locationId": GHL_LOCATION_ID,
        "startAfterDate": int(since.timestamp() * 1000),
        "sort": "desc",
        "limit": 20,
    }
    r = requests.get(f"{GHL_BASE}/conversations/search", headers=GHL_HEADERS, params=params)
    logger.info(f"GHL get_conversations status={r.status_code} body={r.text[:300]}")
    return r.json()

def ghl_get_messages(conversation_id):
    """Récupère les messages d'une conversation."""
    r = requests.get(f"{GHL_BASE}/conversations/{conversation_id}/messages", headers=GHL_HEADERS)
    logger.info(f"GHL get_messages({conversation_id}) status={r.status_code} body={r.text[:300]}")
    return r.json()

# ── OpenAI : transcription vocale + GPT ──────────────────────────────────────

def transcribe_audio(file_path):
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
- process_calls: traiter les appels récents (résumés + notes + tâches)
  params: hours (défaut: 24)
- get_calls: lister les appels récents sans les traiter
  params: hours (défaut: 24)
- unknown: commande incomprise
  params: {}

Exemples:
"Nouveau client Construction Tremblay" → {"action":"create_subaccount","params":{"name":"Construction Tremblay"},"confirmation":"Je crée le sub-account Construction Tremblay..."}
"Ajoute Jean Tremblay entrepreneur 514-555-0101" → {"action":"create_contact","params":{"first":"Jean","last":"Tremblay","phone":"514-555-0101","company":"Entrepreneur"},"confirmation":"Je crée le contact Jean Tremblay..."}
"Note pour Jean Tremblay: rappel vendredi soumission Laval" → {"action":"add_note","params":{"contact_name":"Jean Tremblay","note":"Rappel vendredi pour soumission Laval"},"confirmation":"J'ajoute la note à Jean Tremblay..."}
"Crée pipeline Construction: Prospect, Soumission, Contrat signé, En cours, Complété" → {"action":"create_pipeline","params":{"name":"Construction","stages":["Prospect","Soumission","Contrat signé","En cours","Complété"]},"confirmation":"Je crée le pipeline Construction..."}
"Traite les appels des dernières 24h" → {"action":"process_calls","params":{"hours":24},"confirmation":"Je traite les appels des dernières 24 heures..."}
"Montre les appels récents" → {"action":"get_calls","params":{"hours":24},"confirmation":"Je récupère la liste des appels récents..."}

Réponds UNIQUEMENT en JSON valide. Jamais de markdown, jamais de texte avant ou après."""

SUMMARY_PROMPT = """Tu es un assistant spécialisé dans l'analyse d'appels clients pour un gestionnaire de projets de construction au Québec.

À partir du transcript ou des messages d'un appel, génère un résumé structuré ET une liste de tâches de suivi en JSON.

Réponds UNIQUEMENT en JSON valide avec ce format:
{
  "summary": "## Résumé d'appel — [Nom du client] — [Date]\\n\\n**Durée :** X minutes\\n**Contact :** [info]\\n\\n### Points discutés\\n- ...\\n\\n### Objections / préoccupations\\n- ...\\n\\n### Décisions prises\\n- ...\\n\\n### Prochaines étapes\\n- [ ] Tâche 1\\n- [ ] Tâche 2",
  "tasks": [
    {"title": "Titre de la tâche 1", "due_days": 3},
    {"title": "Titre de la tâche 2", "due_days": 7}
  ]
}

Si le transcript est vide ou incomplet, indique-le dans le résumé et retourne une liste de tâches vide."""

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

def summarize_call(transcript, contact_name, call_date):
    """Génère un résumé structuré d'un appel via GPT."""
    user_content = f"Contact: {contact_name}\nDate: {call_date}\n\nTranscript:\n{transcript}"
    payload = {
        "model": "gpt-4o",
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": user_content}
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
    logger.info(f"GPT summarize status={r.status_code}")

    if r.status_code != 200:
        resp = r.json()
        raise Exception(f"OpenAI erreur {r.status_code}: {resp.get('error', {}).get('message', str(resp))}")

    raw = r.json()["choices"][0]["message"]["content"]
    raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
    return json.loads(raw)

# ── Call processing ───────────────────────────────────────────────────────────

def extract_transcript(messages):
    """Extrait le texte des messages d'une conversation."""
    parts = []
    for msg in messages:
        msg_type = msg.get("type", "")
        body = msg.get("body", "").strip()
        direction = msg.get("direction", "")
        if body:
            prefix = "Agent" if direction == "outbound" else "Client"
            parts.append(f"{prefix}: {body}")
    return "\n".join(parts)

def process_single_call(conv):
    """Traite un appel : récupère messages, résume, crée note + tâches. Retourne un dict résultat."""
    conv_id = conv.get("id", "")
    contact_id = conv.get("contactId", "")
    contact_name = conv.get("contactName") or conv.get("fullName") or "Inconnu"
    date_created = conv.get("dateUpdated") or conv.get("dateCreated") or ""

    if date_created:
        try:
            ts = int(date_created) / 1000
            call_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError):
            call_date = str(date_created)
    else:
        call_date = "Date inconnue"

    msg_data = ghl_get_messages(conv_id)
    messages = msg_data.get("messages", {}).get("messages", []) or msg_data.get("messages", [])
    transcript = extract_transcript(messages)

    if not transcript:
        transcript = "(Pas de transcript disponible)"

    result = summarize_call(transcript, contact_name, call_date)
    summary_text = result.get("summary", "")
    tasks = result.get("tasks", [])

    note_result = {"status": "skipped"}
    if contact_id and summary_text:
        note_resp = ghl_add_note(contact_id, summary_text)
        note_result = {"status": "ok" if note_resp.get("note") or note_resp.get("id") else "error",
                       "raw": str(note_resp)[:200]}

    task_results = []
    if contact_id and tasks:
        for task in tasks:
            due_days = task.get("due_days", 3)
            due_date = (datetime.now(timezone.utc) + timedelta(days=due_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            t_resp = ghl_create_task(contact_id, task.get("title", "Suivi"), due_date)
            task_results.append({
                "title": task.get("title"),
                "status": "ok" if t_resp.get("task") or t_resp.get("id") else "error"
            })

    return {
        "contact_name": contact_name,
        "call_date": call_date,
        "summary": summary_text,
        "note": note_result,
        "tasks": task_results,
    }

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

        elif action == "get_calls":
            hours = int(params.get("hours", 24))
            data = ghl_get_conversations(hours)
            conversations = data.get("conversations", [])
            if not conversations:
                return f"📞 Aucun appel trouvé dans les dernières {hours}h"
            lines = [f"📞 {len(conversations)} appel(s) dans les dernières {hours}h:"]
            for conv in conversations[:10]:
                name = conv.get("contactName") or conv.get("fullName") or "Inconnu"
                ts = conv.get("dateUpdated") or conv.get("dateCreated") or ""
                try:
                    call_date = datetime.fromtimestamp(int(ts)/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    call_date = "?"
                lines.append(f"  • {name} — {call_date}")
            return "\n".join(lines)

        elif action == "process_calls":
            hours = int(params.get("hours", 24))
            data = ghl_get_conversations(hours)
            conversations = data.get("conversations", [])
            if not conversations:
                return f"📞 Aucun appel trouvé dans les dernières {hours}h"

            lines = [f"📞 {len(conversations)} appel(s) à traiter dans les dernières {hours}h:\n"]
            for conv in conversations:
                name = conv.get("contactName") or conv.get("fullName") or "Inconnu"
                lines.append(f"  • {name}")
            lines.append("\n⏳ Traitement en cours...")
            return "\n".join(lines)

        elif action == "unknown":
            return "❓ Je n'ai pas compris. Peux-tu reformuler?"

        else:
            return f"❓ Action inconnue: {action}"

    except Exception as e:
        logger.error(f"execute_action error: {e}")
        return f"❌ Erreur: {str(e)}"

async def execute_process_calls(params, update):
    """Traite les appels et envoie les résultats progressivement via Telegram."""
    hours = int(params.get("hours", 24))
    data = ghl_get_conversations(hours)
    conversations = data.get("conversations", [])

    if not conversations:
        await update.message.reply_text(f"📞 Aucun appel trouvé dans les dernières {hours}h")
        return

    await update.message.reply_text(
        f"📞 {len(conversations)} appel(s) trouvé(s) dans les dernières {hours}h. Je génère les résumés..."
    )

    processed = []
    errors = []
    for conv in conversations:
        try:
            result = process_single_call(conv)
            processed.append(result)

            task_count = len(result["tasks"])
            note_status = "✅" if result["note"].get("status") == "ok" else "⚠️"
            msg = (
                f"✅ *{result['contact_name']}* — {result['call_date']}\n"
                f"Note: {note_status} | Tâches créées: {task_count}\n\n"
                f"{result['summary'][:600]}{'...' if len(result['summary']) > 600 else ''}"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Erreur traitement appel {conv.get('id')}: {e}")
            errors.append(str(e))

    summary_lines = [
        f"\n📊 *Traitement terminé*",
        f"✅ Appels traités: {len(processed)}",
        f"❌ Erreurs: {len(errors)}",
    ]
    total_tasks = sum(len(r["tasks"]) for r in processed)
    summary_lines.append(f"📋 Tâches créées au total: {total_tasks}")
    if errors:
        summary_lines.append(f"\nErreurs:\n" + "\n".join(f"• {e[:100]}" for e in errors))

    await update.message.reply_text("\n".join(summary_lines), parse_mode="Markdown")

# ── Telegram handlers ─────────────────────────────────────────────────────────

async def process_command(text, update):
    try:
        parsed = ask_gpt(text)
        action = parsed.get("action", "unknown")
        params = parsed.get("params", {})
        confirmation = parsed.get("confirmation", "")

        if action == "process_calls":
            await update.message.reply_text(confirmation)
            await execute_process_calls(params, update)
        else:
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
        "• _Traite les appels des dernières 24h_\n"
        "• _Montre les appels récents_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Regex(r'^/start'), handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Bot démarré avec OpenAI (GPT-4o + Whisper) + GHL API intégration...")
    app.run_polling()

if __name__ == "__main__":
    main()
