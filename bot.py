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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GHL_API_KEY = os.environ.get("GHL_API_KEY")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID")

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Content-Type": "application/json",
    "Version": "2021-07-28"
}

# ── GHL helpers ──────────────────────────────────────────────────────────────

def ghl_create_subaccount(name: str, email: str = "", phone: str = "") -> dict:
    payload = {
        "name": name,
        "phone": phone,
        "email": email,
        "address": "",
        "city": "",
        "state": "",
        "country": "CA",
        "postalCode": "",
        "timezone": "America/Toronto",
        "prospectInfo": {"email": email, "phone": phone, "name": name},
    }
    r = requests.post(f"{GHL_BASE}/locations/", headers=GHL_HEADERS, json=payload)
    return r.json()


def ghl_create_contact(first: str, last: str = "", email: str = "", phone: str = "",
                       company: str = "", notes: str = "") -> dict:
    payload = {
        "locationId": GHL_LOCATION_ID,
        "firstName": first,
        "lastName": last,
        "email": email,
        "phone": phone,
        "companyName": company,
    }
    r = requests.post(f"{GHL_BASE}/contacts/", headers=GHL_HEADERS, json=payload)
    data = r.json()
    if notes and data.get("contact", {}).get("id"):
        ghl_add_note(data["contact"]["id"], notes)
    return data


def ghl_search_contact(name: str) -> dict:
    r = requests.get(
        f"{GHL_BASE}/contacts/search",
        headers=GHL_HEADERS,
        params={"locationId": GHL_LOCATION_ID, "query": name}
    )
    return r.json()


def ghl_add_note(contact_id: str, note: str) -> dict:
    payload = {"body": note, "userId": ""}
    r = requests.post(f"{GHL_BASE}/contacts/{contact_id}/notes", headers=GHL_HEADERS, json=payload)
    return r.json()


def ghl_update_contact(contact_id: str, fields: dict) -> dict:
    r = requests.put(f"{GHL_BASE}/contacts/{contact_id}", headers=GHL_HEADERS, json=fields)
    return r.json()


def ghl_create_pipeline(name: str, stages: list) -> dict:
    payload = {
        "name": name,
        "locationId": GHL_LOCATION_ID,
        "stages": [{"name": s} for s in stages]
    }
    r = requests.post(f"{GHL_BASE}/opportunities/pipelines", headers=GHL_HEADERS, json=payload)
    return r.json()


def ghl_create_opportunity(contact_id: str, pipeline_id: str, stage_id: str,
                            title: str, value: float = 0) -> dict:
    payload = {
        "locationId": GHL_LOCATION_ID,
        "pipelineId": pipeline_id,
        "stageId": stage_id,
        "contactId": contact_id,
        "name": title,
        "monetaryValue": value,
        "status": "open"
    }
    r = requests.post(f"{GHL_BASE}/opportunities/", headers=GHL_HEADERS, json=payload)
    return r.json()


def ghl_get_pipelines() -> dict:
    r = requests.get(
        f"{GHL_BASE}/opportunities/pipelines",
        headers=GHL_HEADERS,
        params={"locationId": GHL_LOCATION_ID}
    )
    return r.json()


def ghl_book_appointment(contact_id: str, calendar_id: str,
                          start_time: str, end_time: str, title: str = "Rendez-vous") -> dict:
    payload = {
        "calendarId": calendar_id,
        "locationId": GHL_LOCATION_ID,
        "contactId": contact_id,
        "startTime": start_time,
        "endTime": end_time,
        "title": title,
    }
    r = requests.post(f"{GHL_BASE}/calendars/events/appointments", headers=GHL_HEADERS, json=payload)
    return r.json()


# ── Claude ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Tu es un assistant IA pour un gestionnaire de projets de construction au Québec.
Tu interprètes ses commandes vocales (transcrites) ou textuelles en français québécois naturel
et tu exécutes les actions dans GoHighLevel (GHL) via des fonctions.

Réponds TOUJOURS en JSON avec ce format exact:
{
  "action": "<nom_de_l_action>",
  "params": { ... },
  "confirmation": "<message de confirmation en français pour l'utilisateur>"
}

Actions disponibles:
- create_subaccount: créer un nouveau compte client GHL
  params: name (obligatoire), email, phone
- create_contact: créer un nouveau contact
  params: first (obligatoire), last, email, phone, company, notes
- search_contact: chercher un contact existant
  params: name (obligatoire)
- add_note: ajouter une note à un contact (cherche d'abord le contact par nom)
  params: contact_name (obligatoire), note (obligatoire)
- update_contact: modifier un contact existant
  params: contact_name (obligatoire), fields (dict avec les champs à modifier)
- create_pipeline: créer un pipeline avec des étapes
  params: name (obligatoire), stages (liste d'étapes)
- get_pipelines: lister les pipelines existants
  params: {}
- unknown: si tu ne comprends pas la commande
  params: {}
  confirmation: demande de clarification

Exemples de commandes et réponses:
- "Nouveau client Construction Tremblay" → create_subaccount {name: "Construction Tremblay"}
- "Ajoute Jean Tremblay entrepreneur 514-555-0101" → create_contact {first: "Jean", last: "Tremblay", phone: "514-555-0101", company: "Entrepreneur"}
- "Note pour Jean Tremblay: rappel vendredi pour soumission" → add_note {contact_name: "Jean Tremblay", note: "Rappel vendredi pour soumission"}
- "Crée un pipeline Construction avec Prospect, Soumission, Contrat signé, En cours, Complété" → create_pipeline {name: "Construction", stages: ["Prospect", "Soumission", "Contrat signé", "En cours", "Complété"]}

Interprète le langage naturel québécois de façon flexible. "Fait une note" = add_note, "nouveau client" = create_subaccount, etc.
Réponds UNIQUEMENT en JSON, rien d'autre."""


def ask_claude(text: str) -> dict:
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": text}]
    }
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json=payload
    )
    raw = r.json()["content"][0]["text"]
    # Strip markdown fences if present
    raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
    return json.loads(raw)


def transcribe_audio(file_path: str) -> str:
    """Transcribe audio using OpenAI Whisper API — or fallback to a simple message."""
    # We use the Whisper endpoint via requests
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        return "[transcription non disponible — ajoute une clé OPENAI_API_KEY pour les vocaux]"
    with open(file_path, "rb") as f:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {openai_key}"},
            files={"file": (os.path.basename(file_path), f, "audio/ogg")},
            data={"model": "whisper-1", "language": "fr"}
        )
    return r.json().get("text", "")


# ── Action executor ───────────────────────────────────────────────────────────

def execute_action(action: str, params: dict) -> str:
    try:
        if action == "create_subaccount":
            result = ghl_create_subaccount(
                params.get("name", ""),
                params.get("email", ""),
                params.get("phone", "")
            )
            if result.get("id") or result.get("location"):
                return "✅ Sub-account créé avec succès dans GHL!"
            return f"⚠️ Réponse GHL: {json.dumps(result)[:200]}"

        elif action == "create_contact":
            result = ghl_create_contact(
                params.get("first", ""),
                params.get("last", ""),
                params.get("email", ""),
                params.get("phone", ""),
                params.get("company", ""),
                params.get("notes", "")
            )
            if result.get("contact"):
                return f"✅ Contact créé: {result['contact'].get('firstName', '')} {result['contact'].get('lastName', '')}"
            return f"⚠️ Réponse GHL: {json.dumps(result)[:200]}"

        elif action == "search_contact":
            result = ghl_search_contact(params.get("name", ""))
            contacts = result.get("contacts", [])
            if not contacts:
                return f"🔍 Aucun contact trouvé pour: {params.get('name')}"
            lines = [f"🔍 {len(contacts)} contact(s) trouvé(s):"]
            for c in contacts[:5]:
                lines.append(f"  • {c.get('firstName','')} {c.get('lastName','')} — {c.get('phone','')}")
            return "\n".join(lines)

        elif action == "add_note":
            search = ghl_search_contact(params.get("contact_name", ""))
            contacts = search.get("contacts", [])
            if not contacts:
                return f"❌ Contact introuvable: {params.get('contact_name')}"
            contact_id = contacts[0]["id"]
            name = f"{contacts[0].get('firstName','')} {contacts[0].get('lastName','')}".strip()
            ghl_add_note(contact_id, params.get("note", ""))
            return f"✅ Note ajoutée à {name}"

        elif action == "update_contact":
            search = ghl_search_contact(params.get("contact_name", ""))
            contacts = search.get("contacts", [])
            if not contacts:
                return f"❌ Contact introuvable: {params.get('contact_name')}"
            contact_id = contacts[0]["id"]
            name = f"{contacts[0].get('firstName','')} {contacts[0].get('lastName','')}".strip()
            ghl_update_contact(contact_id, params.get("fields", {}))
            return f"✅ Contact {name} mis à jour"

        elif action == "create_pipeline":
            result = ghl_create_pipeline(
                params.get("name", ""),
                params.get("stages", [])
            )
            if result.get("pipeline") or result.get("id"):
                stages = params.get("stages", [])
                return f"✅ Pipeline '{params.get('name')}' créé avec {len(stages)} étapes: {', '.join(stages)}"
            return f"⚠️ Réponse GHL: {json.dumps(result)[:200]}"

        elif action == "get_pipelines":
            result = ghl_get_pipelines()
            pipelines = result.get("pipelines", [])
            if not pipelines:
                return "📋 Aucun pipeline trouvé"
            lines = [f"📋 {len(pipelines)} pipeline(s):"]
            for p in pipelines:
                stages = p.get("stages", [])
                lines.append(f"  • {p.get('name','')} ({len(stages)} étapes)")
            return "\n".join(lines)

        elif action == "unknown":
            return "❓ Je n'ai pas compris ta commande. Peux-tu reformuler?"

        else:
            return f"❓ Action inconnue: {action}"

    except Exception as e:
        return f"❌ Erreur: {str(e)}"


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await update.message.reply_text("⏳ Je traite ta commande...")
    try:
        parsed = ask_claude(text)
        action = parsed.get("action", "unknown")
        params = parsed.get("params", {})
        confirmation = parsed.get("confirmation", "")
        result = execute_action(action, params)
        await update.message.reply_text(f"{confirmation}\n\n{result}")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur: {str(e)}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎤 Message vocal reçu, je transcris...")
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            text = transcribe_audio(tmp.name)

        if not text or text.startswith("[transcription"):
            await update.message.reply_text(
                "⚠️ Transcription vocale non disponible.\n"
                "Envoie ta commande en texte pour l'instant.\n\n"
                "_Pour activer les vocaux, ajoute une clé OpenAI dans Railway._"
            )
            return

        await update.message.reply_text(f"📝 Compris: _{text}_", parse_mode="Markdown")
        parsed = ask_claude(text)
        action = parsed.get("action", "unknown")
        params = parsed.get("params", {})
        confirmation = parsed.get("confirmation", "")
        result = execute_action(action, params)
        await update.message.reply_text(f"{confirmation}\n\n{result}")

    except Exception as e:
        await update.message.reply_text(f"❌ Erreur vocal: {str(e)}")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Bonjour! Je suis ton assistant GHL.*\n\n"
        "Envoie-moi une commande en français naturel, par exemple:\n\n"
        "• _Nouveau client Construction Tremblay_\n"
        "• _Ajoute Jean Dupont entrepreneur 514-555-0101_\n"
        "• _Note pour Jean Dupont: rappel vendredi soumission_\n"
        "• _Crée un pipeline Construction avec Prospect, Soumission, Contrat_\n"
        "• _Montre-moi mes pipelines_\n\n"
        "Tu peux aussi envoyer un 🎤 message vocal!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Regex(r'^/start'), handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Bot démarré...")
    app.run_polling()


if __name__ == "__main__":
    main()
