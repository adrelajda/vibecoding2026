"""
Agent pro třídění e-mailů a navrhování odpovědí – webkitty webmail (IMAP).

Spouští se automaticky 1× denně v 8:00, třídí nepřečtené emaily do štítků
a pro každý relevantní email uloží návrh odpovědi jako koncept.

Pravidla třídění:
    Analemma  – slova analemma/annalemma/annalema nebo dotaz k objednávce/doručení
    kurz      – dotaz ke kurzu o vodě (moduly, videa, záznamy, přístupy)
    miniprodukt – dotaz na kurz o vodě v krizi, video o vodě v krizi, magnet, miniprodukt
    konzultace  – poptávka konzultace
    Ostatní     – nespadá do žádné výše uvedené kategorie

Požadavky:
    pip install -r requirements.txt

Proměnné prostředí:
    ANTHROPIC_API_KEY  - API klíč pro Anthropic Claude (povinné)
    EMAIL_ADDRESS      - e-mailová adresa klientky, např. jana@webkitty.cz (povinné)
    EMAIL_PASSWORD     - heslo k e-mailu (povinné)
    IMAP_HOST          - IMAP server  (výchozí: mail.webkitty.cz)
    IMAP_PORT          - IMAP port    (výchozí: 993, SSL/TLS)
    DRAFTS_FOLDER      - složka pro koncepty (výchozí: Drafts)

Spuštění:
    # Jednorázové zpracování (ručně)
    python webkitty_email_agent.py

    # Denní automatické spouštění v 8:00
    python webkitty_email_agent.py --schedule

    # Zpracování všech emailů (nejen nepřečtených)
    python webkitty_email_agent.py ALL
"""

import os
import re
import sys
import json
import time
import email
import email.utils
import imaplib
import anthropic
import schedule
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header, make_header
from datetime import datetime
from dotenv import load_dotenv

# Načteme proměnné prostředí ze souboru .env (pokud existuje)
load_dotenv()


# ---------- Konfigurace ----------

IMAP_HOST     = os.getenv("IMAP_HOST", "imap.dianasiswartonova.cz")
IMAP_PORT     = int(os.getenv("IMAP_PORT", "993"))
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
DRAFTS_FOLDER = os.getenv("DRAFTS_FOLDER", "Drafts")

MODEL          = "claude-opus-4-7"
MAX_TOKENS     = 4096
MAX_ITERATIONS = 80     # bezpečnostní pojistka agentic loop
MAX_EMAILS     = 30     # max emailů na jedno spuštění
BODY_LIMIT     = 1500   # max znaků těla emailu předaných modelu

# Předdefinované štítky – klíč = interní název, hodnota = IMAP složka
LABEL_FOLDERS = {
    "Analemma":   "Analemma",
    "kurz":       "kurz",
    "miniprodukt": "miniprodukt",
    "konzultace": "konzultace",
    "Ostatní":    "Ostatní",
}

# Štítky, pro které agent připraví návrh odpovědi
LABELS_NEEDING_REPLY = {"Analemma", "kurz", "miniprodukt", "konzultace"}


# ---------- IMAP pomocné funkce ----------

def _connect() -> imaplib.IMAP4_SSL:
    """Vytvoří a vrátí autentizované IMAP spojení."""
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        raise ValueError(
            "Nastavte proměnné prostředí EMAIL_ADDRESS a EMAIL_PASSWORD.\n"
            "  export EMAIL_ADDRESS='jana@webkitty.cz'\n"
            "  export EMAIL_PASSWORD='vase-heslo'"
        )
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    return conn


def _select(conn: imaplib.IMAP4_SSL, folder: str, readonly: bool = False) -> None:
    """Vybere složku; automaticky přidá uvozovky pokud název obsahuje mezeru."""
    name = f'"{folder}"' if (" " in folder or "/" in folder) else folder
    conn.select(name, readonly=readonly)


def _decode_hdr(value: str | None) -> str:
    """Dekóduje MIME encoded-word hlavičku na Unicode string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _extract_body(msg: email.message.Message) -> str:
    """Rekurzivně extrahuje textové tělo zprávy z MIME payload."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


# ---------- Implementace nástrojů ----------

def tool_list_label_folders(conn: imaplib.IMAP4_SSL) -> dict:
    """Vrátí seznam existujících IMAP složek (budoucích štítků)."""
    typ, folder_list = conn.list()
    names = []
    for item in folder_list:
        decoded = item.decode("utf-8", errors="replace")
        # Formát: (\Atributy) "oddelovac" "Nazev"
        parts = decoded.rsplit(" ", 1)
        if parts:
            name = parts[-1].strip().strip('"')
            names.append(name)
    return {"folders": names}


def tool_fetch_emails(
    conn: imaplib.IMAP4_SSL,
    query: str = "UNSEEN",
    max_results: int = 20,
) -> dict:
    """Vrátí seznam emailů z INBOX (UID, předmět, odesílatel, datum)."""
    max_results = max(1, min(max_results, MAX_EMAILS))
    try:
        _select(conn, "INBOX", readonly=True)
        typ, data = conn.uid("SEARCH", None, query)
        uids = data[0].split() if data[0] else []
        uids = uids[-max_results:]  # nejnovější naposledy

        emails = []
        for uid in uids:
            typ, msg_data = conn.uid("FETCH", uid, "(RFC822.HEADER)")
            if not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            emails.append({
                "uid": uid.decode(),
                "subject": _decode_hdr(msg.get("Subject")),
                "from": _decode_hdr(msg.get("From")),
                "date": msg.get("Date", ""),
                "message_id": msg.get("Message-ID", ""),
            })

        return {"count": len(emails), "emails": emails}
    except Exception as e:
        return {"error": str(e)}


def tool_get_email(conn: imaplib.IMAP4_SSL, uid: str) -> dict:
    """Načte plný obsah e-mailu z INBOX."""
    try:
        _select(conn, "INBOX", readonly=True)
        typ, msg_data = conn.uid("FETCH", uid.encode(), "(RFC822)")
        if not msg_data or msg_data[0] is None:
            return {"error": f"Email UID {uid} nenalezen"}

        msg = email.message_from_bytes(msg_data[0][1])
        body = _extract_body(msg)

        return {
            "uid": uid,
            "subject": _decode_hdr(msg.get("Subject")),
            "from": _decode_hdr(msg.get("From")),
            "to": _decode_hdr(msg.get("To")),
            "date": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
            "body": body[:BODY_LIMIT],
        }
    except Exception as e:
        return {"error": str(e)}


def tool_create_label_folder(conn: imaplib.IMAP4_SSL, name: str) -> dict:
    """Vytvoří IMAP složku pro štítek (pokud ještě neexistuje)."""
    try:
        typ, _ = conn.create(name)
        if typ == "OK":
            return {"success": True, "folder": name}
        # Složka možná již existuje – to je OK
        return {"success": True, "folder": name, "note": "Složka pravděpodobně již existuje"}
    except Exception as e:
        return {"error": str(e)}


def tool_apply_label(conn: imaplib.IMAP4_SSL, uid: str, label: str) -> dict:
    """
    Přiřadí štítek e-mailu zkopírováním do složky štítku.
    Email zůstane v INBOX a zároveň se objeví ve složce štítku.
    """
    folder = LABEL_FOLDERS.get(label, label)
    try:
        _select(conn, "INBOX")
        typ, data = conn.uid("COPY", uid.encode(), folder)
        if typ == "OK":
            return {"success": True, "uid": uid, "label": label, "folder": folder}
        return {"error": f"COPY selhal: {data}"}
    except Exception as e:
        return {"error": str(e)}


def tool_mark_processed(conn: imaplib.IMAP4_SSL, uid: str) -> dict:
    """Označí email v INBOX jako přečtený (zpracovaný agentem)."""
    try:
        _select(conn, "INBOX")
        conn.uid("STORE", uid.encode(), "+FLAGS", "\\Seen")
        return {"success": True, "uid": uid}
    except Exception as e:
        return {"error": str(e)}


def tool_save_draft_reply(
    conn: imaplib.IMAP4_SSL,
    uid: str,
    reply_text: str,
) -> dict:
    """Uloží navržený text odpovědi jako koncept do složky Drafts."""
    try:
        # Načteme původní email pro citaci a adresování
        original = tool_get_email(conn, uid)
        if "error" in original:
            return {"error": f"Nelze načíst původní email: {original['error']}"}

        msg = MIMEMultipart("alternative")
        subject = original.get("subject", "")
        msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = original.get("from", "")
        msg["In-Reply-To"] = original.get("message_id", "")
        msg["References"] = original.get("message_id", "")
        msg["Date"] = email.utils.formatdate(localtime=True)

        # Citace původní zprávy
        quoted = "\n".join(f"> {line}" for line in original.get("body", "").split("\n"))
        full_body = (
            f"{reply_text}\n\n"
            f"---\n"
            f"Od: {original.get('from', '')}\n"
            f"Datum: {original.get('date', '')}\n\n"
            f"{quoted}"
        )
        msg.attach(MIMEText(full_body, "plain", "utf-8"))

        raw = msg.as_bytes()
        draft_candidates = [DRAFTS_FOLDER, "Drafts", "Koncepty", "Draft", "DRAFTS"]

        for drafts_name in draft_candidates:
            try:
                typ, _ = conn.append(
                    drafts_name,
                    "\\Draft",
                    imaplib.Time2Internaldate(time.time()),
                    raw,
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

        return {"error": "Složka Drafts nenalezena. Nastavte DRAFTS_FOLDER."}
    except Exception as e:
        return {"error": str(e)}


# ---------- Definice nástrojů pro Claudea ----------

TOOLS = [
    {
        "name": "list_label_folders",
        "description": (
            "Vrátí seznam existujících IMAP složek (= štítků). "
            "Zavolej jako první krok, abys věděla, které štítkové složky již existují."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_emails",
        "description": "Vrátí seznam nepřečtených emailů z INBOX s UID, předmětem, odesílatelem a datem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "IMAP vyhledávací kritéria. "
                        "Výchozí: 'UNSEEN' (nepřečtené). "
                        "Alternativy: 'ALL', 'SINCE 01-Apr-2026', 'FROM example@example.com'"
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
            "Načte plný obsah e-mailu z INBOX: předmět, odesílatel, příjemce, datum a tělo zprávy. "
            "Povinné před přiřazením štítku."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "UID emailu (z fetch_emails)",
                },
            },
            "required": ["uid"],
        },
    },
    {
        "name": "create_label_folder",
        "description": (
            "Vytvoří složku pro štítek, pokud ještě neexistuje. "
            "Zavolej před prvním použitím apply_label pro daný štítek."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Název složky. Povolené hodnoty: "
                        "'Analemma', 'kurz', 'miniprodukt', 'konzultace', 'Ostatní'"
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "apply_label",
        "description": (
            "Přiřadí štítek emailu (zkopíruje ho do složky štítku). "
            "Email zůstane v INBOX i ve složce štítku. "
            "Jeden email může mít více štítků – volej apply_label pro každý štítek zvlášť."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "UID emailu",
                },
                "label": {
                    "type": "string",
                    "enum": ["Analemma", "kurz", "miniprodukt", "konzultace", "Ostatní"],
                    "description": "Název štítku dle pravidel třídění",
                },
            },
            "required": ["uid", "label"],
        },
    },
    {
        "name": "mark_processed",
        "description": "Označí email jako přečtený v INBOX (signalizuje, že byl zpracován agentem).",
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "UID emailu"},
            },
            "required": ["uid"],
        },
    },
    {
        "name": "save_draft_reply",
        "description": (
            "Uloží navržený text odpovědi jako koncept do složky Drafts. "
            "Volej pro štítky: Analemma, kurz, miniprodukt, konzultace. "
            "NEPŘIPRÁVEJ odpověď pro štítek Ostatní."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "UID emailu, na který odpovídáme",
                },
                "reply_text": {
                    "type": "string",
                    "description": (
                        "Text navržené odpovědi. "
                        "Piš v jazyce původního emailu (česky/anglicky). "
                        "Buď přátelská, profesionální a věcná. "
                        "Nepřidávej 'Předmět:' ani 'Od:' – doplní se automaticky."
                    ),
                },
            },
            "required": ["uid", "reply_text"],
        },
    },
]


# ---------- Systémový prompt ----------

SYSTEM_PROMPT = f"""Jsi asistentka pro správu e-mailové schránky. Každý den v 8:00 zpracuješ nepřečtené emaily, přiřadíš jim štítky a připravíš návrhy odpovědí.

## Postup pro každý email

1. Přečti obsah: `get_email`
2. Urči štítky podle pravidel níže (může být více štítků)
3. Vytvoř složky pokud chybí: `create_label_folder`
4. Přiřaď štítky: `apply_label` (pro každý štítek zvlášť)
5. Připrav odpověď: `save_draft_reply` (POUZE pro Analemma / kurz / miniprodukt / konzultace)
6. Označ jako přečtený: `mark_processed`

## Pravidla přiřazování štítků

### Štítek "Analemma"
Přiřaď pokud email obsahuje COKOLI z:
- Slova (bez ohledu na velikost písmen): **analemma**, **annalemma**, **annalema**
- Dotaz na **doručení zásilky nebo balíčku**
- Dotaz na **odeslání objednávky**
- Dotaz k **objednávce** (stav, číslo, reklamace, vrácení, platba)

### Štítek "kurz"
Přiřaď pokud email obsahuje dotaz nebo zájem o:
- **Kurz o vodě** (obecně – jakékoli zmínění kurzu o vodě)
- **Moduly** kurzu
- **Videa** nebo **záznamy** z kurzu
- **Přístupy** do kurzu / přihlašovací údaje ke kurzu
⚠️ VÝJIMKA: Pokud jde konkrétně o „kurz o vodě v krizi" nebo „video o vodě v krizi" → použij štítek **miniprodukt**, ne kurz

### Štítek "miniprodukt"
Přiřaď pokud email zmiňuje:
- **kurz o vodě v krizi**
- **video o vodě v krizi**
- **magnet** nebo **lead magnet**
- **miniprodukt**

### Štítek "konzultace"
Přiřaď pokud email obsahuje:
- Zájem o **konzultaci** (osobní, online, telefonní)
- **Poptávku spolupráce** nebo individuální práce

### Štítek "Ostatní"
Přiřaď pokud email **nespadá do žádné** výše uvedené kategorie.

## Priorita a kombinace
- Jeden email může dostat **více štítků** (např. Analemma + konzultace)
- **miniprodukt** a **kurz** se vzájemně **vylučují** – miniprodukt má přednost
- **Ostatní** se nepřiřazuje společně s jinými štítky

## Návrhy odpovědí – pokyny
- Piš v jazyce původního emailu (česky / anglicky / ...)
- Buď přátelská, profesionální a věcná
- **Analemma**: potvrď přijetí dotazu, informuj že se ozveš nebo předej kontakt zákaznického servisu
- **kurz**: potvrď zájem, krátce popiš možnosti a nabídni další informace nebo odkaz
- **miniprodukt**: potvrď zájem o miniprodukt/magnet, informuj o dostupnosti a dalším postupu
- **konzultace**: potvrď zájem, nabídni termín nebo instrukce k objednání konzultace
- **Ostatní**: ŽÁDNÁ odpověď

## Závěr
Po zpracování všech emailů napiš stručné shrnutí:
- Celkový počet zpracovaných emailů
- Kolik dostalo který štítek
- Kolik návrhů odpovědí bylo uloženo
"""


# ---------- Dispečer nástrojů ----------

def execute_tool(conn: imaplib.IMAP4_SSL, name: str, inputs: dict) -> str:
    """Spustí příslušný nástroj a vrátí JSON výsledek jako string."""
    if name == "list_label_folders":
        result = tool_list_label_folders(conn)
    elif name == "fetch_emails":
        result = tool_fetch_emails(
            conn,
            query=inputs.get("query", "UNSEEN"),
            max_results=inputs.get("max_results", 20),
        )
    elif name == "get_email":
        result = tool_get_email(conn, uid=inputs["uid"])
    elif name == "create_label_folder":
        result = tool_create_label_folder(conn, name=inputs["name"])
    elif name == "apply_label":
        result = tool_apply_label(conn, uid=inputs["uid"], label=inputs["label"])
    elif name == "mark_processed":
        result = tool_mark_processed(conn, uid=inputs["uid"])
    elif name == "save_draft_reply":
        result = tool_save_draft_reply(conn, uid=inputs["uid"], reply_text=inputs["reply_text"])
    else:
        result = {"error": f"Neznámý nástroj: {name}"}

    return json.dumps(result, ensure_ascii=False)


# ---------- Hlavní smyčka agenta ----------

def run_agent(query: str = "UNSEEN") -> None:
    """Spustí e-mail agenta pro zadaný IMAP dotaz."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("=" * 60)
    print(f"  Agent pro třídění e-mailů  –  {now}")
    print("=" * 60)
    print(f"  Model:   {MODEL}")
    print(f"  Server:  {IMAP_HOST}:{IMAP_PORT}")
    print(f"  Účet:    {EMAIL_ADDRESS or '(nenastaveno)'}")
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
                f"Zpracuj prosím dnešní nepřečtené emaily (IMAP dotaz: '{query}'). "
                f"Pro každý email urči štítek(y) dle pravidel a připrav návrh odpovědi "
                f"u emailů, které to vyžadují."
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

                # Vizuální ikona
                if block.name in {"list_label_folders", "fetch_emails", "get_email"}:
                    icon = "🔍"
                elif block.name == "save_draft_reply":
                    icon = "💬"
                elif block.name == "apply_label":
                    icon = "🏷️"
                elif block.name == "mark_processed":
                    icon = "✓"
                else:
                    icon = "📁"

                inputs_preview = json.dumps(block.input, ensure_ascii=False)[:130]
                print(f"{icon} [{iteration}] {block.name}({inputs_preview})")

                result_str = execute_tool(conn, block.name, block.input)
                print(f"     → {result_str[:230]}\n")

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


# ---------- Scheduler (denní běh v 8:00) ----------

def _scheduled_run() -> None:
    """Obálka pro scheduler – spustí agenta a zachytí případné výjimky."""
    try:
        run_agent(query="UNSEEN")
    except Exception as e:
        print(f"❌ Chyba při zpracování emailů: {e}")


def run_scheduler() -> None:
    """Spustí denní scheduler – agent poběží každý den v 8:00."""
    print("⏰ Scheduler spuštěn.")
    print(f"   Agent bude spouštět každý den v 08:00 ({IMAP_HOST})")
    print("   Pro zastavení stiskněte Ctrl+C\n")

    schedule.every().day.at("08:00").do(_scheduled_run)

    # Spustit okamžitě při startu (zpracuje dnešní emaily hned)
    print("▶  Spouštím první zpracování hned teď...\n")
    _scheduled_run()

    while True:
        schedule.run_pending()
        time.sleep(30)


# ---------- Vstupní bod ----------

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--schedule" in args:
        run_scheduler()
    else:
        # Jednorázové spuštění (volitelně s vlastním IMAP dotazem)
        query = next((a for a in args if not a.startswith("--")), "UNSEEN")
        run_agent(query=query)
