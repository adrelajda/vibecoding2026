"""
Agent pro třídění e-mailů a navrhování odpovědí – webkitty webmail (IMAP).

Agent se připojí k e-mailové schránce přes IMAP, přečte e-maily,
přesune je do tematických složek a pro každý email uloží navržený
koncept odpovědi do složky Drafts.

Požadavky:
    pip install -r requirements.txt

Nastavení – proměnné prostředí:
    ANTHROPIC_API_KEY  - API klíč pro Anthropic Claude (povinné)
    EMAIL_ADDRESS      - e-mailová adresa klientky, např. jana@webkitty.cz
    EMAIL_PASSWORD     - heslo k e-mailu (povinné)
    IMAP_HOST          - IMAP server  (výchozí: mail.webkitty.cz)
    IMAP_PORT          - IMAP port    (výchozí: 993, SSL/TLS)
    DRAFTS_FOLDER      - složka pro koncepty (výchozí: Drafts)
    EMAIL_QUERY        - IMAP hledací dotaz (výchozí: UNSEEN)
    EMAIL_FOLDER       - složka ke zpracování (výchozí: INBOX)

Spuštění:
    python webkitty_email_agent.py
    python webkitty_email_agent.py "ALL"
    python webkitty_email_agent.py "SINCE 01-Apr-2026"

Nastavení IMAP pro webkitty:
    Ověřte aktuální hodnoty v administraci účtu na webkitty.cz.
    Typická konfigurace: IMAP mail.webkitty.cz:993 (SSL), SMTP mail.webkitty.cz:465 (SSL)
"""

import os
import sys
import json
import time
import email
import email.utils
import imaplib
import anthropic
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header, make_header


# ---------- Konfigurace ----------

IMAP_HOST = os.getenv("IMAP_HOST", "mail.webkitty.cz")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
DRAFTS_FOLDER = os.getenv("DRAFTS_FOLDER", "Drafts")

MODEL = "claude-opus-4-7"
MAX_TOKENS = 4096
MAX_ITERATIONS = 80     # bezpečnostní pojistka agentic loop
MAX_EMAILS = 20         # max emailů na jedno spuštění
BODY_LIMIT = 1500       # max znaků těla emailu předaných modelu


# ---------- IMAP pomocné funkce ----------

def _connect() -> imaplib.IMAP4_SSL:
    """Vytvoří a vrátí autentizované IMAP spojení."""
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        raise ValueError(
            "Nejsou nastaveny proměnné prostředí EMAIL_ADDRESS a EMAIL_PASSWORD.\n"
            "Spusťte:\n"
            "  export EMAIL_ADDRESS='jana@webkitty.cz'\n"
            "  export EMAIL_PASSWORD='vase-heslo'"
        )
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    return conn


def _select(conn: imaplib.IMAP4_SSL, folder: str, readonly: bool = False) -> None:
    """Vybere složku; automaticky přidá uvozovky pokud název obsahuje mezeru."""
    name = f'"{folder}"' if " " in folder else folder
    conn.select(name, readonly=readonly)


def _decode_header_value(value: str | None) -> str:
    """Dekóduje MIME encoded-word hlavičku na Unicode string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _extract_body(msg: email.message.Message) -> str:
    """Extrahuje textové tělo e-mailové zprávy."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    return body[:BODY_LIMIT]


# ---------- Implementace nástrojů ----------

def tool_list_folders(conn: imaplib.IMAP4_SSL) -> dict:
    """Vrátí seznam IMAP složek."""
    typ, folder_list = conn.list()
    folders = []
    for item in folder_list:
        decoded = item.decode("utf-8", errors="replace")
        # Formát: (\\Atributy) "oddelovac" "Nazev"
        parts = decoded.rsplit(" ", 1)
        if parts:
            name = parts[-1].strip().strip('"')
            folders.append(name)
    return {"folders": folders}


def tool_fetch_emails(
    conn: imaplib.IMAP4_SSL,
    folder: str = "INBOX",
    query: str = "UNSEEN",
    max_results: int = 20,
) -> dict:
    """Vrátí seznam emailů (UID, předmět, odesílatel, datum) ze zvolené složky."""
    max_results = max(1, min(max_results, MAX_EMAILS))
    try:
        _select(conn, folder, readonly=True)
        typ, data = conn.uid("SEARCH", None, query)
        uids = data[0].split() if data[0] else []
        # Vezmi posledních max_results (nejnovější)
        uids = uids[-max_results:]

        emails = []
        for uid in uids:
            typ, msg_data = conn.uid("FETCH", uid, "(RFC822.HEADER)")
            if not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            emails.append({
                "uid": uid.decode(),
                "subject": _decode_header_value(msg.get("Subject")),
                "from": _decode_header_value(msg.get("From")),
                "date": msg.get("Date", ""),
                "message_id": msg.get("Message-ID", ""),
            })

        return {"count": len(emails), "folder": folder, "emails": emails}
    except Exception as e:
        return {"error": str(e)}


def tool_get_email(
    conn: imaplib.IMAP4_SSL,
    uid: str,
    folder: str = "INBOX",
) -> dict:
    """Načte plný obsah e-mailu (předmět, odesílatel, příjemce, datum, tělo)."""
    try:
        _select(conn, folder, readonly=True)
        typ, msg_data = conn.uid("FETCH", uid.encode(), "(RFC822)")
        if not msg_data or msg_data[0] is None:
            return {"error": f"Email UID {uid} nenalezen ve složce {folder}"}

        msg = email.message_from_bytes(msg_data[0][1])
        return {
            "uid": uid,
            "folder": folder,
            "subject": _decode_header_value(msg.get("Subject")),
            "from": _decode_header_value(msg.get("From")),
            "to": _decode_header_value(msg.get("To")),
            "date": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
            "body": _extract_body(msg),
        }
    except Exception as e:
        return {"error": str(e)}


def tool_create_folder(conn: imaplib.IMAP4_SSL, name: str) -> dict:
    """Vytvoří novou IMAP složku (kategorii)."""
    try:
        typ, data = conn.create(name)
        if typ == "OK":
            return {"success": True, "folder": name}
        return {"error": data[0].decode() if data else "Neznámá chyba"}
    except Exception as e:
        return {"error": str(e)}


def tool_move_to_folder(
    conn: imaplib.IMAP4_SSL,
    uid: str,
    src_folder: str,
    dst_folder: str,
) -> dict:
    """Přesune email do jiné složky (třídění do kategorie)."""
    try:
        _select(conn, src_folder)
        # Pokus o RFC 6851 MOVE příkaz (většina moderních serverů ho podporuje)
        typ, data = conn.uid("MOVE", uid.encode(), dst_folder)
        if typ == "OK":
            return {"success": True, "uid": uid, "destination": dst_folder}

        # Fallback: COPY → DELETE → EXPUNGE
        typ, data = conn.uid("COPY", uid.encode(), dst_folder)
        if typ != "OK":
            return {"error": f"Nelze kopírovat: {data}"}
        conn.uid("STORE", uid.encode(), "+FLAGS", "\\Deleted")
        conn.expunge()
        return {"success": True, "uid": uid, "destination": dst_folder}
    except Exception as e:
        return {"error": str(e)}


def tool_save_draft_reply(
    conn: imaplib.IMAP4_SSL,
    uid: str,
    folder: str,
    reply_text: str,
) -> dict:
    """
    Uloží navržený text odpovědi jako koncept do složky Drafts.
    Automaticky přidá citaci původní zprávy.
    """
    try:
        # Načteme původní email pro citaci
        original = tool_get_email(conn, uid, folder)
        if "error" in original:
            return {"error": f"Nelze načíst původní email: {original['error']}"}

        # Sestavíme MIME zprávu
        msg = MIMEMultipart("alternative")
        subject = original.get("subject", "")
        msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = original.get("from", "")
        msg["In-Reply-To"] = original.get("message_id", "")
        msg["References"] = original.get("message_id", "")
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["X-Draft-Info"] = "type=reply"

        # Text s citací původní zprávy
        quoted = "\n".join(f"> {line}" for line in original.get("body", "").split("\n"))
        full_body = (
            f"{reply_text}\n\n"
            f"--- Původní zpráva ---\n"
            f"Od: {original.get('from', '')}\n"
            f"Datum: {original.get('date', '')}\n"
            f"Předmět: {original.get('subject', '')}\n\n"
            f"{quoted}"
        )
        msg.attach(MIMEText(full_body, "plain", "utf-8"))

        # Zkus různé kandidáty pro složku Drafts
        draft_candidates = [DRAFTS_FOLDER, "Drafts", "Koncepty", "Draft", "DRAFTS"]
        raw_msg = msg.as_bytes()

        for drafts_name in draft_candidates:
            try:
                typ, data = conn.append(
                    drafts_name,
                    "\\Draft",
                    imaplib.Time2Internaldate(time.time()),
                    raw_msg,
                )
                if typ == "OK":
                    return {
                        "success": True,
                        "drafts_folder": drafts_name,
                        "subject": msg["Subject"],
                        "to": msg["To"],
                    }
            except Exception:
                continue

        return {"error": "Složka Drafts nenalezena – zkuste nastavit DRAFTS_FOLDER"}
    except Exception as e:
        return {"error": str(e)}


# ---------- Definice nástrojů pro Claudea ----------

TOOLS = [
    {
        "name": "list_folders",
        "description": (
            "Vrátí seznam všech IMAP složek v e-mailové schránce. "
            "Zavolej jako první krok, abys věděl, jaké kategorie již existují."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_emails",
        "description": (
            "Vypíše emaily v dané složce. Vrátí UID, předmět, odesílatele a datum. "
            "Pro přečtení těla zprávy použij get_email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Název IMAP složky (výchozí: INBOX)",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "IMAP vyhledávací kritéria. Příklady: "
                        "'UNSEEN' (nepřečtené), "
                        "'ALL' (vše), "
                        "'SINCE 01-Apr-2026', "
                        "'FROM newsletter@example.com'. "
                        "Výchozí: UNSEEN"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximální počet emailů (1–{MAX_EMAILS}), výchozí 20",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_email",
        "description": (
            "Načte plný obsah e-mailu: předmět, odesílatel, příjemce, datum a tělo zprávy. "
            "Nutné zavolat před uložením návrhu odpovědi."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "UID emailu (z fetch_emails)",
                },
                "folder": {
                    "type": "string",
                    "description": "Složka, ve které email leží (výchozí: INBOX)",
                },
            },
            "required": ["uid"],
        },
    },
    {
        "name": "create_folder",
        "description": (
            "Vytvoří novou složku (kategorii) v IMAP schránce. "
            "Volej pouze pokud vhodná složka ještě neexistuje."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Název složky. Doporučené kategorie: "
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
        "name": "move_to_folder",
        "description": "Přesune email do tematické složky (třídění).",
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "UID emailu",
                },
                "src_folder": {
                    "type": "string",
                    "description": "Zdrojová složka (výchozí: INBOX)",
                },
                "dst_folder": {
                    "type": "string",
                    "description": "Cílová složka (kategorie)",
                },
            },
            "required": ["uid", "src_folder", "dst_folder"],
        },
    },
    {
        "name": "save_draft_reply",
        "description": (
            "Uloží navržený text odpovědi jako koncept do složky Drafts. "
            "Automaticky cituje původní zprávu. "
            "Volej po přečtení emailu (get_email), pokud je vhodné odpovědět."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "UID emailu, na který odpovídáme",
                },
                "folder": {
                    "type": "string",
                    "description": "Složka, kde se email nachází",
                },
                "reply_text": {
                    "type": "string",
                    "description": (
                        "Text navržené odpovědi. Piš přirozeně a profesionálně "
                        "v jazyce, ve kterém byl email napsán. "
                        "Nepřidávej 'Předmět:' ani 'Od:' – ty se doplní automaticky."
                    ),
                },
            },
            "required": ["uid", "folder", "reply_text"],
        },
    },
]


# ---------- Systémový prompt ----------

SYSTEM_PROMPT = """Jsi inteligentní asistentka pro správu e-mailové schránky. Pomáháš klientce organizovat její e-maily a připravuješ návrhy odpovědí.

## Postup práce

1. **Zobraz složky** – zavolej `list_folders`, abys věděla, jaké kategorie již existují
2. **Načti emaily** – zavolej `fetch_emails` se zadaným dotazem
3. **Pro každý email sekvenčně:**
   a. Přečti obsah: `get_email`
   b. Urči kategorii a přesuň do složky: `move_to_folder`
      (Pokud složka neexistuje, vytvoř ji nejdříve: `create_folder`)
   c. Navrhni a ulož odpověď: `save_draft_reply`
      (Přeskočit pokud jde o newsletter, reklamu nebo automatickou notifikaci)
4. **Závěrečné shrnutí** – kolik emailů zpracováno, do jakých kategorií, kolik návrhů odpovědí

## Kategorie složek

| Složka | Kdy použít |
|--------|------------|
| Práce | Pracovní komunikace, projekty, kolegové, klienti, obchodní partneři |
| Osobní | Přátelé, rodina, soukromá komunikace |
| Newslettery | Pravidelné zpravodaje, blog updates, info emaily |
| Faktury | Faktury, platební potvrzení, účtenky, objednávky |
| Sociální sítě | Notifikace z LinkedIn, Facebook, Twitter/X, Instagram |
| Akce | Reklamní nabídky, slevy, slevové kódy, marketingové emaily |
| Cestování | Letenky, hotely, rezervace, cestovní informace |
| Bankovnictví | Bankovní výpisy, finanční notifikace, transakce |
| Systémová oznámení | GitHub, CI/CD, monitoring, automatické zprávy systémů |

## Pravidla pro návrh odpovědí

- Piš v jazyce původního emailu (česky, anglicky, ...)
- Buď profesionální, přátelská a věcná – navrhuj realistické odpovědi
- Pro pracovní emaily: potvrď přijetí, nastín dalšího postupu nebo odpověz na otázku
- Pro osobní emaily: reaguj přirozeně a lidsky
- Newslettery, reklamy a automatická oznámení: NEPŘIPRAVUJ odpověď
- Faktury: navrhni potvrzení přijetí nebo dotaz na upřesnění (dle kontextu)

## Pravidla obecná

- Vždy přečti email (get_email) před přesunutím nebo odpovědí
- Preferuj existující složky před vytvářením duplicit
- Zpracuj všechny emaily ze seznamu
"""


# ---------- Dispečer nástrojů ----------

def execute_tool(conn: imaplib.IMAP4_SSL, name: str, inputs: dict) -> str:
    """Spustí příslušný nástroj a vrátí JSON výsledek."""
    if name == "list_folders":
        result = tool_list_folders(conn)
    elif name == "fetch_emails":
        result = tool_fetch_emails(
            conn,
            folder=inputs.get("folder", "INBOX"),
            query=inputs.get("query", "UNSEEN"),
            max_results=inputs.get("max_results", 20),
        )
    elif name == "get_email":
        result = tool_get_email(
            conn,
            uid=inputs["uid"],
            folder=inputs.get("folder", "INBOX"),
        )
    elif name == "create_folder":
        result = tool_create_folder(conn, inputs["name"])
    elif name == "move_to_folder":
        result = tool_move_to_folder(
            conn,
            uid=inputs["uid"],
            src_folder=inputs.get("src_folder", "INBOX"),
            dst_folder=inputs["dst_folder"],
        )
    elif name == "save_draft_reply":
        result = tool_save_draft_reply(
            conn,
            uid=inputs["uid"],
            folder=inputs.get("folder", "INBOX"),
            reply_text=inputs["reply_text"],
        )
    else:
        result = {"error": f"Neznámý nástroj: {name}"}

    return json.dumps(result, ensure_ascii=False)


# ---------- Hlavní smyčka agenta ----------

def run_agent(folder: str = "INBOX", query: str = "UNSEEN") -> None:
    """Spustí e-mail agenta pro danou složku a IMAP dotaz."""
    print("=" * 60)
    print("  Agent pro třídění e-mailů a návrhy odpovědí")
    print("  webkitty webmail (IMAP)")
    print("=" * 60)
    print(f"  Model:   {MODEL}")
    print(f"  Server:  {IMAP_HOST}:{IMAP_PORT}")
    print(f"  Účet:    {EMAIL_ADDRESS or '(nenastaveno)'}")
    print(f"  Složka:  {folder}")
    print(f"  Dotaz:   {query}")
    print()

    print("📧 Připojuji se k IMAP serveru...")
    conn = _connect()
    print(f"   ✓ Přihlášen jako {EMAIL_ADDRESS}\n")

    client = anthropic.Anthropic()

    messages = [
        {
            "role": "user",
            "content": (
                f"Prosím, projdi e-maily ve složce '{folder}' "
                f"(vyhledávací dotaz: '{query}'). "
                f"Pro každý email: urči kategorii a přesuň do příslušné složky, "
                f"a pokud jde o zprávu vyžadující odpověď, ulož návrh odpovědi jako koncept."
            ),
        }
    ]

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
            conn.logout()
            return

        if response.stop_reason == "tool_use":
            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                # Vizuální ikona dle typu operace
                read_ops = {"list_folders", "fetch_emails", "get_email"}
                write_ops = {"save_draft_reply"}
                if block.name in read_ops:
                    icon = "🔍"
                elif block.name in write_ops:
                    icon = "💬"
                else:
                    icon = "📁"

                inputs_preview = json.dumps(block.input, ensure_ascii=False)[:120]
                print(f"{icon} [{iteration}] {block.name}({inputs_preview})")

                result_str = execute_tool(conn, block.name, block.input)
                print(f"     → {result_str[:220]}\n")

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
    conn.logout()


if __name__ == "__main__":
    src_folder = os.getenv("EMAIL_FOLDER", "INBOX")
    imap_query = sys.argv[1] if len(sys.argv) > 1 else os.getenv("EMAIL_QUERY", "UNSEEN")
    run_agent(folder=src_folder, query=imap_query)
