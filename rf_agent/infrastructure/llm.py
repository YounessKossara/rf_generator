import os
import time
import json
import re
import json_repair
from langchain_groq import ChatGroq
from langchain.messages import SystemMessage, HumanMessage
from dotenv import load_dotenv

load_dotenv()

CURRENT_KEY_INDEX = 1
USE_CEREBRAS_FALLBACK = False

def _get_api_key():
    """Returns the current API key based on rotation index."""
    global CURRENT_KEY_INDEX
    key = os.getenv(f"GROQ_API_KEY_{CURRENT_KEY_INDEX}")
    if not key:
        for i in [1, 2, 3]:
            if os.getenv(f"GROQ_API_KEY_{i}"):
                return os.getenv(f"GROQ_API_KEY_{i}")
    return key

def rotate_api_key():
    """Switches to the next available API key."""
    global CURRENT_KEY_INDEX
    available = [i for i in [1, 2, 3] if os.getenv(f"GROQ_API_KEY_{i}")]
    if not available: return
    if CURRENT_KEY_INDEX in available:
        idx = available.index(CURRENT_KEY_INDEX)
        CURRENT_KEY_INDEX = available[(idx + 1) % len(available)]
    else:
        CURRENT_KEY_INDEX = available[0]
    print(f"🔄 Switched to Groq API Key {CURRENT_KEY_INDEX}")

def get_smart_llm():
    if USE_CEREBRAS_FALLBACK:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            api_key=os.getenv("CEREBRAS_API_KEY"),
            base_url="https://api.cerebras.ai/v1",
            model="gpt-oss-120b",
            temperature=0.1,
        )
    return ChatGroq(
        api_key=_get_api_key(),
        model="llama-3.3-70b-versatile" ,
        temperature=0.1,
    )

def get_fast_llm():
    if USE_CEREBRAS_FALLBACK:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            api_key=os.getenv("CEREBRAS_API_KEY"),
            base_url="https://api.cerebras.ai/v1",
            model="llama3.1-8b",
            temperature=0.1,
        )
    return ChatGroq(
        api_key=_get_api_key(),
        model="llama-3.1-8b-instant",
        temperature=0.1,
    )

def invoke_with_retry(model_func, messages, max_retries=1):
    """Invokes the model and rotates the API key on failure."""
    retries = 0
    while retries <= max_retries:
        try:
            model = model_func()
            return model.invoke(messages)
        except Exception as e:
            err_str = str(e).lower()
            if "api_key" in err_str or "auth" in err_str or "rate_limit" in err_str or "401" in err_str or "429" in err_str:
                retries += 1
                if retries > max_retries:
                    global USE_CEREBRAS_FALLBACK
                    if not USE_CEREBRAS_FALLBACK and os.getenv("CEREBRAS_API_KEY"):
                        print("⚠️ All Groq keys failed. Switching to Cerebras API fallback...")
                        USE_CEREBRAS_FALLBACK = True
                        retries = 0
                        continue
                    else:
                        raise Exception("All API keys failed or max retries reached.")
                print(f"⚠️ LLM Call failed with current key. Rotating... Error: {e}")
                rotate_api_key()
            else:
                raise e
    raise Exception("All API keys failed or max retries reached.")

async def ainvoke_with_retry(model_func, messages, tools=None, max_retries=3):
    """Asynchronously invokes the model and rotates the API key on failure."""
    retries = 0
    tool_retries = 0
    while True:
        try:
            model = model_func()
            if tools:
                model = model.bind_tools(tools)
            return await model.ainvoke(messages)
        except Exception as e:
            err_str = str(e).lower()
            if "tool_use_failed" in err_str or "400" in err_str:
                tool_retries += 1
                if tool_retries > 5:
                    raise e
                print("⚠️ Tool format error. Adding correction...")
                from langchain_core.messages import HumanMessage
                messages.append(HumanMessage(content=
                    "IMPORTANT: Your last tool call had a formatting error. "
                    "You MUST call tools using proper JSON arguments only. "
                    "Never use XML or <function=> format. "
                    "Try your last action again with correct format."
                ))
                import asyncio
                await asyncio.sleep(1)
                continue
            elif "429" in err_str or "rate_limit" in err_str:
                retries += 1
                if retries > max_retries:
                    global USE_CEREBRAS_FALLBACK
                    if not USE_CEREBRAS_FALLBACK and os.getenv("CEREBRAS_API_KEY"):
                        print("⚠️ All Groq keys rate limited. Switching to Cerebras API fallback...")
                        USE_CEREBRAS_FALLBACK = True
                        retries = 0
                        continue
                    else:
                        raise Exception("All API keys failed or max retries reached.")
                print(f"⚠️ LLM Rate limit hit. Rotating... Error: {e}")
                rotate_api_key()
            else:
                raise e

def generate_form_data(fields: list, md_summary: str = "") -> dict:
    """
    Given a list of form fields, ask the LLM to generate realistic test data.
    Returns a dict mapping field identifier -> value.
    """
    unique_id = str(int(time.time()))[-6:]

    forced_values = {}
    context_str = ""
    if md_summary:
        context_str = f"Use these instructions for this test:\n{md_summary}\nDo NOT invent field names or values if they are provided above.\n\n"
        for line in md_summary.split('\n'):
            line = line.strip()
            if line.lower().startswith('username:'):
                forced_values['username'] = line.split(':', 1)[1].strip()
            elif line.lower().startswith('password:'):
                forced_values['password'] = line.split(':', 1)[1].strip()

    prompt = (
        f"{context_str}"
        "You are a QA test data generator. Given form fields, generate realistic fake data for each.\n"
        "Rules:\n"
        f"IMPORTANT: For the username field, you MUST use EXACTLY this value: qa_{unique_id}\n"
        "Do not change it. Do not use omar123 or any other name.\n"
        "- For email fields: use a unique fake email like qa_tester_" + unique_id + "@gmail.com\n"
        "- For phone fields: use +212612345678\n"
        "- For birthdateYear: use a year between 1970 and 2000 (User must be an ADULT).\n"
        "- For birthdateDay: use a value between 1 and 28.\n"
        "- For select dropdowns (tag: select): pick one of the 'value' strings provided in the 'options' list. If the list contains '<liferay-ui:message...', pick a simple numeric value if available.\n"
        "- SKIP checkbox/radio/hidden fields\n\n"
        "RETURN ONLY a JSON object mapping each field identifier (id or name) to its generated value.\n"
        "Do NOT add any text before or after the JSON.\n\n"
        f"Form fields:\n{json.dumps(fields, ensure_ascii=False)}\n"
    )

    response = invoke_with_retry(get_fast_llm, [HumanMessage(content=prompt)])
    raw = response.content

    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        raw = match.group(0)

    try:
        result = json_repair.loads(raw)
        
        # Apply forced values or fail-safes
        for key in list(result.keys()):
            key_lower = key.lower()
            if "username" in key_lower or "user" in key_lower or "login" in key_lower:
                if "username" in forced_values:
                    result[key] = forced_values["username"]
                else:
                    result[key] = f"qa_{unique_id}"
            elif "pass" in key_lower or "pwd" in key_lower:
                if "password" in forced_values:
                    result[key] = forced_values["password"]
                
        return result
    except Exception:
        print(f"[LLM] Failed to parse form data: {raw[:200]}")
        return {}


_page_type_cache = {}

def analyze_page_purpose(url: str, page_text_snippet: str, fields_summary: str) -> str:
    """
    Ask the LLM what type of page this is and what to do.
    Returns: 'registration', 'login', 'dashboard', 'other'
    """
    global _page_type_cache
    if url in _page_type_cache:
        return _page_type_cache[url]
        
    prompt = (
        "What type of page is this? Analyze the URL and visible text.\n"
        f"URL: {url}\n"
        f"Visible text (first 500 chars): {page_text_snippet[:500]}\n"
        f"Form fields: {fields_summary}\n\n"
        "Reply with ONLY one word: registration, login, dashboard, or other."
    )

    response = invoke_with_retry(get_fast_llm, [HumanMessage(content=prompt)])
    result = response.content.strip().lower()

    final_type = "other"
    for page_type in ["registration", "login", "dashboard"]:
        if page_type in result:
            final_type = page_type
            break
            
    _page_type_cache[url] = final_type
    return final_type
