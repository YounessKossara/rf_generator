"""
RF Generator — Robot Framework Code Generator

Uses LLM to generate Robot Framework (.robot) code from parsed test cases.
Includes optional page reconnaissance to provide real DOM context to the LLM.
"""

import httpx
from tools.llm import get_smart_llm, invoke_with_retry
from langchain.messages import SystemMessage, HumanMessage


SYSTEM_PROMPT = """You are an expert in Robot Framework and SeleniumLibrary.
Generate valid Robot Framework test code.

RULES:
- Always start with *** Settings ***, *** Variables ***, *** Test Cases ***
- Use SeleniumLibrary keywords: Open Browser, Input Text, Click Button, Click Element,
  Wait Until Page Contains, Wait Until Element Is Visible, Close Browser
- Use ${BASE_URL} variable for all URLs
- Add [Documentation] tag to each test case
- Add proper waits after navigation: Sleep    2s or Wait Until Element Is Visible
- Close browser at end of each test case
- Return ONLY valid Robot Framework code, no markdown, no explanations
- Do NOT wrap the code in ```robot or ``` blocks
- Use 4 spaces for indentation under test cases and keywords
- Each keyword call must be on its own line with proper indentation

═══════════════════════════════════════════════
  XPATH SELECTOR RULES — CRITICAL — READ CAREFULLY
═══════════════════════════════════════════════

NEVER USE @name SELECTORS. Modern web apps (Angular, React, Vue, OrangeHRM, etc.)
do NOT use name attributes on inputs. Using @name WILL FAIL.

BANNED — DO NOT USE THESE:
  ✗ xpath://input[@name='txtUsername']        ← WILL FAIL
  ✗ xpath://input[@name='txtPassword']        ← WILL FAIL
  ✗ xpath://input[@name='Submit']             ← WILL FAIL
  ✗ xpath://input[@id='btnSave']              ← WILL FAIL
  ✗ xpath://a[@id='welcome']                  ← WILL FAIL
  ✗ xpath://a[@href='/index.php/auth/logout'] ← WILL FAIL

MANDATORY SELECTOR PRIORITY (use in this order):
  1. @placeholder   → xpath://input[@placeholder='Username']
  2. @type          → xpath://input[@type='password']
  3. text content   → xpath://button[normalize-space()='Login']
  4. CSS class      → css:button.oxd-button--main
  5. contains text  → xpath://*[contains(text(),'Dashboard')]

CORRECT SELECTORS — USE THESE:
  ✓ xpath://input[@placeholder='Username']
  ✓ xpath://input[@placeholder='Password']
  ✓ xpath://button[@type='submit']
  ✓ xpath://button[normalize-space()='Login']
  ✓ xpath://button[normalize-space()=' Login ']
  ✓ css:input[placeholder='Username']
  ✓ css:button[type='submit']

FOR COMMON ACTIONS:
  Login form:
    Wait Until Element Is Visible    xpath://input[@placeholder='Username']    10s
    Input Text    xpath://input[@placeholder='Username']    Admin
    Input Text    xpath://input[@placeholder='Password']    admin123
    Click Button    xpath://button[@type='submit']

  Logout (modern apps use dropdown menus):
    Click Element    css:.oxd-userdropdown-tab
    OR Click Element    xpath://*[contains(@class,'userdropdown')]
    Wait Until Element Is Visible    xpath://a[normalize-space()='Logout']    5s
    Click Element    xpath://a[normalize-space()='Logout']

  Navigation links:
    Click Element    xpath://a[normalize-space()='My Info']
    OR Click Element    xpath://span[normalize-space()='My Info']

WAIT STRATEGY — MANDATORY:
  Before EVERY interaction with an element, add a wait:
    Wait Until Element Is Visible    <locator>    10s
  This prevents "element not found" errors on slow-loading pages.

  Example:
    Wait Until Element Is Visible    xpath://input[@placeholder='Username']    10s
    Input Text    xpath://input[@placeholder='Username']    Admin

═══════════════════════════════════════════════
  SYNTAX RULES — MANDATORY
═══════════════════════════════════════════════

1. Variables section format MUST be:
   ${BASE_URL}    https://example.com
   NO equal sign. Just spaces between name and value.

2. *** Settings *** section accepts ONLY:
   Library, Resource, Suite Setup, Suite Teardown,
   Test Setup, Test Teardown, Metadata, Variables

   NEVER put keywords like 'Set Selenium Speed'
   in Settings section.

3. If you need Selenium speed, add in each test case:
   Set Selenium Implicit Wait    10

4. Add after Open Browser in each test case:
   Set Window Size    1920    1080
   Sleep    3s

WINDOW RULE: NEVER use 'Maximize Browser Window'.
ALWAYS use 'Set Window Size    1920    1080' instead.

5. EMPTY FIELD TESTS — CRITICAL:
   Input Text ALWAYS requires exactly 2 arguments: locator AND value.
   For empty field tests, do NOT use Input Text with empty value.
   Instead, just click submit WITHOUT filling any fields:
     Click Button    xpath://button[@type='submit']
   Or to clear an existing field:
     Clear Element Text    xpath://input[@placeholder='Username']
   NEVER write: Input Text    xpath://...    (missing value = SYNTAX ERROR)

═══════════════════════════════════════════════
  ORANGEHRM SPECIFIC RULES
═══════════════════════════════════════════════

OrangeHRM uses a modern Vue.js frontend. Key patterns:
- Login: inputs have placeholder='Username' and placeholder='Password'
- Submit button: xpath://button[@type='submit']
- Logout is HIDDEN inside a user dropdown menu:
    1. Click Element    xpath://span[@class='oxd-userdropdown-tab']
    2. Sleep    1s
    3. Wait Until Element Is Visible    xpath://a[normalize-space()='Logout']    5s
    4. Click Element    xpath://a[normalize-space()='Logout']
- Error messages appear in: xpath://div[contains(@class,'oxd-alert')]
  or xpath://p[contains(@class,'oxd-alert-content')]
- "Required" validation: xpath://span[contains(@class,'oxd-input-field-error')]

═══════════════════════════════════════════════
  SCREENSHOT RULES
═══════════════════════════════════════════════

- At the START of each test case, add:
    Set Screenshot Directory    ${SCREENSHOT_ROOT}

- After each significant action, capture a screenshot:
    Capture Page Screenshot    {tc_id}_step_{n}.png

You MUST add Capture Page Screenshot after:
- After Open Browser + Sleep (initial page load):
    Open Browser    ${BASE_URL}    chrome
    Set Window Size    1920    1080
    Set Screenshot Directory    ${SCREENSHOT_ROOT}
    Sleep    3s
    Capture Page Screenshot    TC-001_initial.png

- After successful login or page navigation:
    Wait Until Page Contains    Dashboard    10s
    Capture Page Screenshot    TC-001_after_login.png

- After any form submission:
    Click Button    xpath://button[@type='submit']
    Sleep    2s
    Capture Page Screenshot    TC-001_after_submit.png

- Before Close Browser (final state):
    Capture Page Screenshot    TC-001_final.png
    Close Browser

Use the test case ID as prefix for screenshot filenames.

IMPORTANT: Always declare ${SCREENSHOT_ROOT} in *** Variables *** with a default:
${SCREENSHOT_ROOT}    .

═══════════════════════════════════════════════
  COMPLETE EXAMPLE (for reference)
═══════════════════════════════════════════════

*** Settings ***
Library    SeleniumLibrary

*** Variables ***
${BASE_URL}    https://opensource-demo.orangehrmlive.com
${SCREENSHOT_ROOT}    .

*** Test Cases ***
TC-001 - Login with Valid Credentials
    [Documentation]    User is redirected to Dashboard after valid login
    Set Screenshot Directory    ${SCREENSHOT_ROOT}
    Open Browser    ${BASE_URL}    chrome
    Set Window Size    1920    1080
    Sleep    3s
    Capture Page Screenshot    TC-001_initial.png
    Wait Until Element Is Visible    xpath://input[@placeholder='Username']    10s
    Input Text    xpath://input[@placeholder='Username']    Admin
    Input Text    xpath://input[@placeholder='Password']    admin123
    Capture Page Screenshot    TC-001_step_1.png
    Click Button    xpath://button[@type='submit']
    Sleep    2s
    Wait Until Page Contains    Dashboard    10s
    Capture Page Screenshot    TC-001_after_login.png
    Capture Page Screenshot    TC-001_final.png
    Close Browser"""


def _fetch_page_html(url: str) -> str:
    """
    Fetch the login/landing page HTML to give the LLM real DOM context.
    Returns a truncated version focusing on form elements.
    """
    try:
        with httpx.Client(timeout=10, follow_redirects=True, verify=False) as client:
            resp = client.get(url)
            html = resp.text

            # Extract relevant parts: forms, inputs, buttons, links
            import re
            # Get all input, button, a, select, textarea tags
            elements = re.findall(
                r'<(?:input|button|a|select|textarea|form|label|div[^>]*class="[^"]*(?:login|form|nav|menu|dropdown|sidebar)[^"]*")[^>]*(?:/>|>[^<]*</(?:input|button|a|select|textarea|form|label|div)>|>)',
                html,
                re.IGNORECASE | re.DOTALL
            )

            if elements:
                relevant_html = "\n".join(elements[:60])  # Cap at 60 elements
                return relevant_html[:4000]  # Cap at 4000 chars
            else:
                # Return a chunk of the page
                return html[:3000]
    except Exception as e:
        print(f"   ⚠️  Could not fetch page HTML for recon: {e}")
        return ""


def generate_rf_code(test_cases: list[dict], base_url: str) -> str:
    """
    Build a prompt that includes ALL test cases at once and ask the LLM
    to generate a complete .robot file.

    Performs page reconnaissance first to provide real DOM context.

    Args:
        test_cases: List of parsed test case dicts from md_parser.
        base_url: The base URL of the application under test.

    Returns:
        The generated .robot code as a string.
    """
    # Step 0: Fetch real page HTML for selector accuracy
    print("   🔍 Fetching page HTML for selector reconnaissance...")
    page_html = _fetch_page_html(base_url)
    recon_context = ""
    if page_html:
        recon_context = f"""

═══════════════════════════════════════════════
  REAL PAGE HTML (from {base_url})
═══════════════════════════════════════════════
Below is the ACTUAL HTML from the target page. Use these REAL elements
to build your XPath/CSS selectors. Do NOT guess — use what you see here:

{page_html}

Use the EXACT attributes you see above (placeholder, type, class, text content).
DO NOT invent selectors. If you see placeholder='Username', use that.
"""
        print(f"   ✅ Got {len(page_html)} chars of page HTML for context")
    else:
        print("   ⚠️  No page HTML available, using generic selectors")

    # Format test cases into structured text for the prompt
    tc_text_parts = []
    for tc in test_cases:
        part = f"--- Test Case: {tc['id']} ---\n"
        part += f"Title: {tc['title']}\n"

        if tc.get("preconditions"):
            part += "Preconditions:\n"
            for pre in tc["preconditions"]:
                part += f"  - {pre}\n"

        if tc.get("steps"):
            part += "Steps:\n"
            for i, step in enumerate(tc["steps"], 1):
                if isinstance(step, dict):
                    # Table-format step with sub-expected
                    part += f"  {i}. {step['action']}"
                    if step.get("expected"):
                        part += f"  → Expected: {step['expected']}"
                    part += "\n"
                else:
                    part += f"  {i}. {step}\n"

        if tc.get("expected"):
            part += f"Expected Result: {tc['expected']}\n"

        tc_text_parts.append(part)

    all_tcs_text = "\n".join(tc_text_parts)

    human_prompt = f"""Generate a complete Robot Framework test file for the following test cases.

Base URL: {base_url}
{recon_context}
Test Cases:
{all_tcs_text}

CRITICAL INSTRUCTIONS:
- Generate ONE *** Settings *** section at the top with Library SeleniumLibrary
- Add a *** Variables *** section with ${{BASE_URL}}    {base_url}  (NO equal sign, use spaces)
- Add ${{SCREENSHOT_ROOT}}    .  in Variables section
- Generate one Robot Framework test case for each TC above
- Use the TC id and title as the test case name (e.g., "TC-001 - Login with Valid Credentials")
- Each test case must open a browser, perform the steps, verify the expected result, and close the browser
- Use appropriate SeleniumLibrary keywords for each step
- Add [Documentation] to each test case with the expected result
- IMPORTANT: Add Capture Page Screenshot after each significant action
- MANDATORY: Use Wait Until Element Is Visible before EVERY Input Text and Click action
- NEVER use @name selectors. Use @placeholder, @type, normalize-space(), or CSS class selectors
- If page HTML was provided above, use the EXACT attributes from that HTML
- Return ONLY the .robot code, nothing else"""

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=human_prompt),
    ]

    response = invoke_with_retry(get_smart_llm, messages)
    rf_code = response.content.strip()

    # Clean up: remove markdown code fences if LLM wrapped them
    if rf_code.startswith("```"):
        lines = rf_code.split("\n")
        # Remove first line (```robot or ```)
        lines = lines[1:]
        # Remove last line if it's ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        rf_code = "\n".join(lines)

    return rf_code
