"""
Extracts login credentials (username / password) from parsed test cases and raw markdown.
"""

import re


_LABEL_WORDS = {
    "username", "user", "userid", "user_name", "users",
    "login", "logon", "signin", "sign-in",
    "email", "e-mail", "mail",
    "identifiant", "identifiants", "utilisateur",
    "password", "passwd", "pass", "pwd", "secret", "motdepasse", "mdp",
    "first", "firstname", "last", "lastname", "name",
    "submit", "button", "field", "value", "input",
}


def _is_label_word(s: str) -> bool:
    return s.strip().lower() in _LABEL_WORDS


def _score_candidate(u: str, p: str, position: int, full_text: str) -> int:
    score = 0
    freq = len(re.findall(rf'\b{re.escape(u)}\b', full_text, re.IGNORECASE))
    if freq >= 3:
        score += 5
    elif freq >= 2:
        score += 2
    ctx = full_text[max(0, position - 200): position + 50].lower()
    if any(w in ctx for w in ("login", "connexion", "authentif", "log in")):
        score += 3
    if any(w in ctx for w in ("ajout", "création", "creation", "create",
                              "add ", "+ add", "new ", "nouveau", "nouvelle")):
        score -= 4
    return score


def _extract_default_credentials(test_cases: list, raw_md: str = "") -> tuple:
    """
    Scan test case steps/titles AND the raw MD for the first valid credential pair.
    Returns (username, password) or ("", "") if none found.
    """
    full_text = raw_md + "\n"
    for tc in test_cases:
        full_text += " " + tc.get("title", "") + " " + tc.get("expected", "")
        for step in tc.get("steps", []):
            if isinstance(step, dict):
                full_text += " " + step.get("action", "") + " " + step.get("expected", "")
            else:
                full_text += " " + str(step)

    bad_user_substrings = ["locked", "invalid", "wrong", "bad", "incorrect", "invalide"]

    def _accept(u: str, p: str) -> bool:
        u = u.strip().rstrip(".,;:'\"`")
        p = p.strip().rstrip(".,;:'\"`")
        if not u or not p or u == p:
            return False
        if _is_label_word(u) or _is_label_word(p):
            return False
        if any(x in u.lower() for x in bad_user_substrings):
            return False
        if not re.match(r'^[\w.@+\-]+$', u) or not re.match(r'^[\w.@+\-]+$', p):
            return False
        return True

    # Pattern A: "Username: Admin … Password: admin123"
    pat_a = (r'(?:username|user|email|login|identifiant|utilisateur|user\s*name)'
             r'\s*[=:]\s*[`"\']?(\S{3,40}?)[`"\']?[\s,;]+'
             r'(?:.{0,60}?)(?:password|pass(?:word)?|pwd|mot\s*de\s*passe|mdp)'
             r'\s*[=:]\s*[`"\']?(\S{4,40}?)[`"\']?(?:[\s,;.`"\']|$)')
    candidates = []
    for m in re.finditer(pat_a, full_text, re.IGNORECASE | re.DOTALL):
        u_raw = m.group(1).strip().rstrip(".,;:'\"`")
        p_raw = m.group(2).strip().rstrip(".,;:'\"`")
        if _accept(u_raw, p_raw):
            candidates.append((
                _score_candidate(u_raw, p_raw, m.start(), full_text),
                m.start(), u_raw, p_raw,
            ))
    if candidates:
        candidates.sort(key=lambda c: (-c[0], c[1]))
        best = candidates[0]
        try:
            print(f"   [CREDS] Picked best of {len(candidates)} candidate(s): "
                  f"{best[2]} (score={best[0]})")
        except Exception:
            pass
        return best[2], best[3]

    # Pattern B: French/English action-quoted
    fr_en_action = r'(?:saisir|entrer|taper|enter|type|input|fill(?:\s+in)?|set)'
    user_label = r'(?:username|user\s*name|user|email|login|identifiant|utilisateur)'
    pass_label = r'(?:password|pass(?:word)?|pwd|mot\s*de\s*passe|mdp)'
    bridge = r"[^`'\"\n.]{0,60}?"

    user_value = ""
    pass_value = ""
    for m in re.finditer(
        rf"{fr_en_action}\s+[`'\"]([^`'\"\n]{{2,40}})[`'\"]{bridge}{user_label}\b",
        full_text, re.IGNORECASE,
    ):
        cand = m.group(1).strip()
        if cand and not _is_label_word(cand):
            user_value = cand
            break
    for m in re.finditer(
        rf"{fr_en_action}\s+[`'\"]([^`'\"\n]{{2,40}})[`'\"]{bridge}{pass_label}\b",
        full_text, re.IGNORECASE,
    ):
        cand = m.group(1).strip()
        if cand and not _is_label_word(cand):
            pass_value = cand
            break
    if user_value and pass_value and _accept(user_value, pass_value):
        print(f"   \U0001f465 [CREDS] Found credentials (action-quoted): {user_value}")
        return user_value, pass_value

    # Pattern C: reverse-quoted "Username: 'Admin'"
    rev_user = re.search(rf"{user_label}\s*[:=]?\s*[`'\"]([^`'\"\n]{{3,40}})[`'\"]",
                         full_text, re.IGNORECASE)
    rev_pass = re.search(rf"{pass_label}\s*[:=]?\s*[`'\"]([^`'\"\n]{{4,40}})[`'\"]",
                         full_text, re.IGNORECASE)
    if rev_user and rev_pass:
        u, p = rev_user.group(1).strip(), rev_pass.group(1).strip()
        if _accept(u, p):
            print(f"   \U0001f465 [CREDS] Found credentials (label-quoted): {u}")
            return u, p

    # Pattern D: "user / pass" with credential keyword nearby
    cred_context = (r'(?:username|user|email|login|identifiant|utilisateur|'
                    r'password|pwd|cred|account|compte)')
    for line in full_text.split("\n"):
        if not re.search(cred_context, line, re.IGNORECASE):
            continue
        m = re.search(r'\b([\w.@+\-]{3,})\s*/\s*([\w.@+\-]{5,})\b', line)
        if m and _accept(m.group(1), m.group(2)):
            print(f"   \U0001f465 [CREDS] Found credentials (slash, with context): {m.group(1)}")
            return m.group(1), m.group(2)

    # Pattern E: last-resort "\w+_user near a 6+ char word"
    m = re.search(r'\b(\w+_user)\b[^.\n]{0,80}?\b(\w{6,})\b', full_text, re.IGNORECASE)
    if m and _accept(m.group(1), m.group(2)):
        print(f"   \U0001f465 [CREDS] Found credentials (fallback _user): {m.group(1)}")
        return m.group(1), m.group(2)

    print("   ⚠️  [CREDS] No credentials found in test cases")
    return "", ""
