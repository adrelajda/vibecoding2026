"""
LLM API skript s pouzitim nastroju (tool use).
Vola Anthropic Claude API, definuje vypocetni nastroj (kalkulacku),
a kdyz ho LLM pouzije, spocita vysledek a posle ho zpet.

Pouziti:
    pip install anthropic
    export ANTHROPIC_API_KEY="tvuj-api-klic"
    python llm_tool_use.py
"""

import json
import anthropic

# ---------- definice nastroje (kalkulacka) ----------
CALCULATOR_TOOL = {
    "name": "calculator",
    "description": "Vypocita matematicky vyraz. Podporuje +, -, *, /, ** (mocnina) a zakladni funkce.",
    "input_schema": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Matematicky vyraz k vypocitani, napr. '2 + 2' nebo '12 ** 0.5'",
            }
        },
        "required": ["expression"],
    },
}

ALLOWED_NAMES = {"__builtins__": {}}  # omezeni pro eval


def calculate(expression: str) -> str:
    """Bezpecne vyhodnoti jednoduchy matematicky vyraz."""
    import math

    safe_ns = {"__builtins__": {}, "math": math}
    try:
        result = eval(expression, safe_ns)  # noqa: S307
        return str(result)
    except Exception as e:
        return f"Chyba: {e}"


# ---------- hlavni smycka ----------
def main():
    client = anthropic.Anthropic()          # cte ANTHROPIC_API_KEY z prostredi
    model = "claude-sonnet-4-20250514"
    tools = [CALCULATOR_TOOL]

    user_message = "Kolik je (1234 * 5678) + 9 a odmocnina z 144? Pouzij kalkulacku."

    print(f">>> Uzivatel: {user_message}\n")

    # 1) Prvni volani – LLM se muze rozhodnout pouzit nastroj
    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        tools=tools,
        messages=messages,
    )

    print(f"<<< LLM stop_reason: {response.stop_reason}")

    # 2) Smycka: dokud LLM chce pouzivat nastroje, zpracovavame je
    while response.stop_reason == "tool_use":
        # Najdi vsechny tool_use bloky
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_name = block.name
                tool_input = block.input
                print(f"    Nastroj: {tool_name}({json.dumps(tool_input)})")

                # Spocitej vysledek
                if tool_name == "calculator":
                    result = calculate(tool_input["expression"])
                else:
                    result = f"Neznamy nastroj: {tool_name}"

                print(f"    Vysledek: {result}")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

        # Pridej odpoved asistenta a vysledky nastroju do konverzace
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        # 3) Dalsi volani – LLM dostane vysledky a bud odpovi, nebo pouzije dalsi nastroj
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        print(f"<<< LLM stop_reason: {response.stop_reason}")

    # 4) Finalni textova odpoved
    final_text = "".join(
        block.text for block in response.content if hasattr(block, "text")
    )
    print(f"\n<<< LLM: {final_text}")


if __name__ == "__main__":
    main()
