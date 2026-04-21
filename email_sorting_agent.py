"""
Agent pro třídění e-mailů pomocí Claude AI a Gmail API.

Automaticky analyzuje e-maily v Gmail schránce a přiřazuje jim
štítky (labels) pro lepší organizaci.

Požadavky:
    pip install -r requirements.txt

Nastavení Google Cloud:
    1. Přejděte na https://console.cloud.google.com
    2. Vytvořte nový projekt nebo vyberte existující
    3. Povolte Gmail API (APIs & Services > Enable APIs)
    4. Vytvořte OAuth 2.0 credentials (typ: Desktop app)
    5. Stáhněte credentials.json do stejného adresáře jako tento skript

Proměnné prostředí:
    ANTHROPIC_API_KEY  - API klíč pro Anthropic Claude (povinné)
    GMAIL_QUERY        - Gmail vyhledávací dotaz (volitelné, výchozí: "is:unread is:inbox")

Spuštění:
    python email_sorting_agent.py
    python email_sorting_agent.py "is:unread newer_than:7d"
"""

import os
import re
import sys
import json
import base64
import pickle

import anthropic
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ---------- Konfigurace ----------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILE = "gmail_token.pickle"
CREDENTIALS_FILE = "credentials.json"

MODEL = "claude-opus-4-7"
MAX_TOKENS = 4096
MAX_ITERATIONS = 60     # bezpečnostní pojistka agentic loop
MAX_EMAILS = 30         # max emailů na jedno spuštění
EMAIL_BODY_LIMIT = 1500 # max znaků těla emailu předaných modelu


# ---------- Definice nástrojů pro Claudea ----------

TOOLS = [
    {
        "name": "search_emails",
        "description": (
            "Vyhledá e-maily v Gmail schránce a vrátí seznam vláken s ID, "
            "předmětem, odesílatelem, datem a krátkým náhledem. "
            "Nepotřebuje volat get_email pro základní přehled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Gmail vyhledávací dotaz. Příklady: "
                        "'is:unread is:inbox', "
                        "'is:inbox -label:Práce', "
                        "'from:newsletter@example.com newer_than:7d'. "
                        "Výchozí: 'is:unread is:inbox'"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximální počet výsledků (1–{MAX_EMAILS}), výchozí 20",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_email",
        "description": (
            "Načte plný obsah e-mailového vlákna: předmět, odesílatel, příjemce, "
            "datum, tělo zprávy a aktuální štítky. "
            "Použij pro pochopení obsahu emailu před přiřazením štítku."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "ID vlákna emailu (získané z search_emails)",
                },
            },
            "required": ["thread_id"],
        },
    },
    {
        "name": "list_labels",
        "description": (
            "Vrátí seznam všech existujících štítků v Gmail schránce. "
            "Vždy zavolej jako první krok, abys věděl, které kategorie již existují."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "create_label",
        "description": (
            "Vytvoří nový štítek v Gmail schránce. "
            "Volej pouze pokud vhodný štítek ještě neexistuje."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Název nového štítku. Doporučené kategorie: "
                        "'Práce', 'Osobní', 'Newslettery', 'Faktury', "
                        "'Sociální sítě', 'Akce', 'Cestování', "
                        "'Bankovnictví', 'Systémová oznámení'"
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "apply_label",
        "description": "Přiřadí štítek k e-mailovému vláknu. Hlavní akce pro třídění emailů.",
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "ID vlákna emailu",
                },
                "label_id": {
                    "type": "string",
                    "description": "ID štítku (z list_labels nebo create_label)",
                },
            },
            "required": ["thread_id", "label_id"],
        },
    },
    {
        "name": "remove_label",
        "description": (
            "Odstraní štítek z e-mailového vlákna. "
            "Použij label_id='INBOX' pro archivaci (email zmizí z doručené pošty, "
            "ale zůstane dostupný ve Všech emailech)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "ID vlákna emailu",
                },
                "label_id": {
                    "type": "string",
                    "description": "ID štítku k odebrání (např. 'INBOX' pro archivaci)",
                },
            },
            "required": ["thread_id", "label_id"],
        },
    },
]


# ---------- Systémový prompt ----------

SYSTEM_PROMPT = """Jsi inteligentní asistent pro třídění e-mailů. Organizuješ Gmail schránku klienta pomocí štítků (labels).

## Postup práce

1. **Zobraz existující štítky** – zavolej `list_labels`, abys věděl, co v schránce již existuje
2. **Vyhledej emaily** – zavolej `search_emails` s dotazem ze zadání
3. **Pro každý email:**
   a. Načti obsah pomocí `get_email`
   b. Urči nejvhodnější kategorii
   c. Pokud štítek pro tuto kategorii neexistuje, vytvoř ho (`create_label`)
   d. Přiřaď štítek (`apply_label`)
   e. Newslettery a reklamní emaily volitelně archivuj (`remove_label` s label_id='INBOX')
4. **Shrnutí** – po zpracování všech emailů podej přehled: kolik emailů, jaké štítky

## Kategorie štítků

Používej konzistentní pojmenování (zachovej existující štítky):
| Štítek | Kdy použít |
|--------|------------|
| Práce | Pracovní komunikace, projekty, kolegové, klienti |
| Osobní | Přátelé, rodina, soukromá komunikace |
| Newslettery | Pravidelné zpravodaje, blog updates, info emaily |
| Faktury | Faktury, platební potvrzení, účtenky, objednávky |
| Sociální sítě | Notifikace z LinkedIn, Facebook, Twitter/X, Instagram |
| Akce | Reklamní nabídky, slevy, slevové kódy, marketing |
| Cestování | Letenky, hotely, rezervace, cestovní informace |
| Bankovnictví | Bankovní výpisy, finanční notifikace, transakce |
| Systémová oznámení | GitHub, CI/CD, monitoring, servery, automatické zprávy |

## Pravidla

- Vždy přečti obsah emailu (`get_email`) před přiřazením štítku
- Preferuj existující štítky před vytvářením nových duplicit
- Každý email zpracuj – i při nejistotě zvol nejbližší kategorii
- Buď efektivní: zpracovávej emaily jeden po druhém sekvenčně
"""


# ---------- Gmail autentizace ----------

def get_gmail_service():
    """Autentizuje se pomocí OAuth 2.0 a vrátí Gmail API service objekt."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as fh:
            creds = pickle.load(fh)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Soubor '{CREDENTIALS_FILE}' nenalezen.\n"
                    "Stáhněte OAuth 2.0 credentials z Google Cloud Console "
                    "a uložte jako 'credentials.json' do tohoto adresáře.\n"
                    "Viz: https://console.cloud.google.com/apis/credentials"
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "wb") as fh:
            pickle.dump(creds, fh)

    return build("gmail", "v1", credentials=creds)


# ---------- Gmail pomocné funkce ----------

def _extract_body(payload: dict) -> str:
    """Rekurzivně extrahuje textové tělo zprávy z MIME payload."""
    # Přímé tělo zprávy
    if payload.get("body", {}).get("data"):
        raw = base64.urlsafe_b64decode(payload["body"]["data"])
        return raw.decode("utf-8", errors="replace")

    parts = payload.get("parts", [])

    # Hledej text/plain
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Rekurze do multipart
    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            body = _extract_body(part)
            if body:
                return body

    # Fallback: HTML → odstraň tagy
    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", " ", html)
                return re.sub(r"\s+", " ", text).strip()

    return ""


def gmail_search_emails(service, query: str, max_results: int) -> dict:
    max_results = max(1, min(max_results, MAX_EMAILS))
    try:
        result = service.users().threads().list(
            userId="me", q=query, maxResults=max_results
        ).execute()

        threads = result.get("threads", [])
        summaries = []
        for t in threads:
            td = service.users().threads().get(
                userId="me",
                id=t["id"],
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            hdrs = {
                h["name"]: h["value"]
                for h in td["messages"][0].get("payload", {}).get("headers", [])
            }
            summaries.append({
                "thread_id": t["id"],
                "subject": hdrs.get("Subject", "(bez předmětu)"),
                "from": hdrs.get("From", ""),
                "date": hdrs.get("Date", ""),
                "messages": len(td["messages"]),
                "snippet": td["messages"][-1].get("snippet", "")[:200],
            })

        return {"count": len(summaries), "threads": summaries}
    except HttpError as e:
        return {"error": str(e)}


def gmail_get_thread(service, thread_id: str) -> dict:
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()

        messages = []
        for msg in thread["messages"]:
            hdrs = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            body = _extract_body(msg.get("payload", {}))
            messages.append({
                "from": hdrs.get("From", ""),
                "to": hdrs.get("To", ""),
                "subject": hdrs.get("Subject", ""),
                "date": hdrs.get("Date", ""),
                "labels": msg.get("labelIds", []),
                "body": body[:EMAIL_BODY_LIMIT],
            })

        return {"thread_id": thread_id, "messages": messages}
    except HttpError as e:
        return {"error": str(e)}


def gmail_list_labels(service) -> dict:
    try:
        r = service.users().labels().list(userId="me").execute()
        # Vrátíme uživatelské štítky + klíčové systémové
        system_keep = {"INBOX", "SENT", "TRASH", "SPAM", "STARRED", "IMPORTANT"}
        return {
            "labels": [
                {"id": lbl["id"], "name": lbl["name"]}
                for lbl in r.get("labels", [])
                if lbl.get("type") != "system" or lbl["id"] in system_keep
            ]
        }
    except HttpError as e:
        return {"error": str(e)}


def gmail_create_label(service, name: str) -> dict:
    try:
        label = service.users().labels().create(
            userId="me", body={"name": name}
        ).execute()
        return {"label_id": label["id"], "name": label["name"]}
    except HttpError as e:
        return {"error": str(e)}


def gmail_modify_thread(
    service,
    thread_id: str,
    add: list | None = None,
    remove: list | None = None,
) -> dict:
    body: dict = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    try:
        service.users().threads().modify(
            userId="me", id=thread_id, body=body
        ).execute()
        return {"success": True, "thread_id": thread_id}
    except HttpError as e:
        return {"error": str(e)}


# ---------- Dispečer nástrojů ----------

def execute_tool(service, name: str, inputs: dict) -> str:
    """Spustí nástroj a vrátí JSON výsledek jako string."""
    if name == "search_emails":
        result = gmail_search_emails(
            service,
            query=inputs.get("query", "is:unread is:inbox"),
            max_results=inputs.get("max_results", 20),
        )
    elif name == "get_email":
        result = gmail_get_thread(service, inputs["thread_id"])
    elif name == "list_labels":
        result = gmail_list_labels(service)
    elif name == "create_label":
        result = gmail_create_label(service, inputs["name"])
    elif name == "apply_label":
        result = gmail_modify_thread(service, inputs["thread_id"], add=[inputs["label_id"]])
    elif name == "remove_label":
        result = gmail_modify_thread(service, inputs["thread_id"], remove=[inputs["label_id"]])
    else:
        result = {"error": f"Neznámý nástroj: {name}"}

    return json.dumps(result, ensure_ascii=False)


# ---------- Hlavní smyčka agenta ----------

def run_agent(query: str = "is:unread is:inbox") -> None:
    """Spustí email sorting agenta pro daný Gmail dotaz."""
    print("=" * 60)
    print("  Agent pro třídění e-mailů  (Claude + Gmail API)")
    print("=" * 60)
    print(f"  Model:  {MODEL}")
    print(f"  Dotaz:  {query}")
    print()

    print("📧 Připojuji se k Gmail API...")
    service = get_gmail_service()
    print("   ✓ OK\n")

    client = anthropic.Anthropic()  # čte ANTHROPIC_API_KEY z prostředí

    messages = [
        {
            "role": "user",
            "content": (
                f"Prosím, projdi a setřiď e-maily v mé schránce. "
                f"Použij vyhledávací dotaz: '{query}'. "
                f"Analyzuj každý email a přiřaď mu vhodný štítek pro lepší organizaci."
            ),
        }
    ]

    # Agentic loop – Claude volá nástroje, dokud nedokončí třídění
    for iteration in range(1, MAX_ITERATIONS + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            final = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            print(f"\n✅ Hotovo!\n\n{final}")
            return

        if response.stop_reason == "tool_use":
            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                # Ikona dle typu operace
                read_ops = {"search_emails", "get_email", "list_labels"}
                icon = "🔍" if block.name in read_ops else "✏️"

                inputs_preview = json.dumps(block.input, ensure_ascii=False)[:120]
                print(f"{icon} [{iteration}] {block.name}({inputs_preview})")

                result_str = execute_tool(service, block.name, block.input)
                print(f"     → {result_str[:200]}\n")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        else:
            print(f"⚠️  Neočekávaný stop_reason: {response.stop_reason}")
            break

    print("⚠️  Dosažen maximální počet iterací.")


if __name__ == "__main__":
    gmail_query = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.getenv("GMAIL_QUERY", "is:unread is:inbox")
    )
    run_agent(gmail_query)
