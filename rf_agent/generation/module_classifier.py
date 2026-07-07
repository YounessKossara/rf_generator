"""
Module classifier — nav-link extraction and per-test URL classification.
"""

import re as _re
import httpx


_LOGIN_PAGE_INDICATORS = [
    "login-button", "login_button", "login_credentials", "login_credentials_wrap",
    "login_logo", "login_container", "login_wrapper", "login-box", "login_box",
    'data-test="username"', 'data-test="password"', 'data-test="login-button"',
    'data-test="login-container"',
    'name="login-button"', 'id="login-button"',
]


def _looks_like_login_page(html: str) -> bool:
    if not html:
        return True
    lowered = html.lower()
    hits = sum(1 for ind in _LOGIN_PAGE_INDICATORS if ind.lower() in lowered)
    return hits >= 2


def _fetch_page_html(url: str) -> str:
    """Fetch login page HTML, returning elements relevant to form discovery."""
    try:
        with httpx.Client(timeout=10, follow_redirects=True, verify=False) as client:
            resp = client.get(url)
            html = resp.text

        elements = _re.findall(
            r'<(?:input|button|a|select|textarea|form|label|div[^>]*class="[^"]*'
            r'(?:login|form|nav|menu|dropdown|sidebar)[^"]*")[^>]*'
            r'(?:/>|>[^<]*</(?:input|button|a|select|textarea|form|label|div)>|>)',
            html,
            _re.IGNORECASE | _re.DOTALL,
        )
        if elements:
            return "\n".join(elements[:60])[:4000]
        return html[:3000]
    except Exception as e:
        print(f"   ⚠️  Could not fetch page HTML: {e}")
        return ""


def _extract_nav_links(html: str) -> list:
    """
    Pull <a href="/..."> entries out of dashboard-DOM HTML, preserving any
    visible label text near the anchor. Returns [(href, label), ...] deduped.
    """
    nav_links = []
    if not html:
        return nav_links
    for m in _re.finditer(
        r'<a\b[^>]*href=["\'](/[^"\']{3,120})["\'][^>]*>(?:[^<]{0,80})?'
        r'(?:<[^>]+>([^<]{0,60})</[^>]+>)?',
        html, _re.IGNORECASE | _re.DOTALL,
    ):
        href = m.group(1)
        if any(href.endswith(x) for x in (".png", ".jpg", ".css", ".js")):
            continue
        inner = (m.group(2) or "").strip()
        near_text = m.group(0)
        for tm in _re.finditer(r'>([A-Za-z][^<]{2,40})<', near_text):
            inner = tm.group(1).strip()
            break
        for tm in _re.finditer(r'(?:title|aria-label|data-text)=["\']([^"\']{2,60})["\']', m.group(0)):
            if not inner:
                inner = tm.group(1).strip()
        nav_links.append((href, inner))
    seen_h = set()
    uniq = []
    for href, label in nav_links:
        if href not in seen_h:
            seen_h.add(href)
            uniq.append((href, label))
    return uniq


def _label_from_url(href: str) -> str:
    """Best-effort human label from a URL path."""
    return _re.sub(r'[/_\-]+', ' ', href).strip()


def _tc_text(tc: dict) -> str:
    """Concatenate title + preconditions + steps into one searchable string."""
    parts = [str(tc.get("title", "")), str(tc.get("expected", ""))]
    pcs = tc.get("preconditions", [])
    if isinstance(pcs, list):
        parts.extend(str(p) for p in pcs)
    else:
        parts.append(str(pcs))
    for step in tc.get("steps", []):
        if isinstance(step, dict):
            parts.append(str(step.get("action", "")))
            parts.append(str(step.get("expected", "")))
        else:
            parts.append(str(step))
    return " ".join(parts).lower()


_MODULE_KEYWORDS = [
    (r'\b(admin(?:istrator)?|user\s+management|user[s]?\b|gestion\s+(?:des?\s+)?utilisateur|role)', r'admin|user'),
    (r'\b(p\.?i\.?m\.?|employ(?:e|ee|é)|personnel|hr\b|human\s+resource)', r'pim|employee|personnel'),
    (r'\b(leave|cong[ée]|absence|holiday|vacation)', r'leave|absence'),
    (r'\b(time(?:sheet)?|attendance|punch|feuille\s+de\s+temps|pointage)', r'time|attendance|timesheet'),
    (r'\b(recruit(?:ment)?|candidate|candidat|vacanc(?:y|ies)|application)', r'recruit|candidate|vacancy'),
    (r'\b(performance|review|appraisal|kpi|objectif)', r'performance|review'),
    (r'\b(directory|annuaire|org[\s-]?chart)', r'directory|org'),
    (r'\b(buzz|feed|news\s*feed|social|post|publication)', r'buzz|feed|social'),
    (r'\b(dashboard|tableau\s+de\s+bord|home\s*page)', r'dashboard|home'),
    (r'\b(claim|frais|expense|reimburs)', r'claim|expense'),
    (r'\b(maintenance|nettoyage|purge)', r'maintenance'),
    (r'\b(report|rapport|statistic|analytics)', r'report|analytic'),
    (r'\b(my\s+info|profile|profil|mes\s+infos)', r'myinfo|profile|mydetails'),
    (r'\b(setting|config|param[èe]tre)', r'setting|config'),
]


def _classify_test_to_module(tc: dict, nav_links: list, base_root: str) -> str:
    """
    Decide which nav link a test most likely targets. Returns absolute URL or "".
    """
    if not nav_links:
        return ""
    text = _tc_text(tc)
    if not text.strip():
        return ""

    auth_veto = _re.compile(
        r'\b(d[ée]connexion|deconnexion|logout|log\s*out|sign\s*out|'
        r'connexion\s+(?:r[ée]ussie|[ée]chou[ée]e|valide|invalide)|'
        r'change\s+password|modifi(?:cation|er)\s+(?:du\s+|de\s+)?mot\s+de\s+passe|'
        r'authentif|sign\s*in)\b',
        _re.IGNORECASE,
    )
    if auth_veto.search(text):
        return ""

    best_score = 0
    best_url = ""

    for href, label in nav_links:
        href_l = href.lower()
        label_l = (label or "").lower()
        url_text = (label_l + " " + _label_from_url(href_l)).strip()
        score = 0

        if label_l and len(label_l) >= 3 and label_l in text:
            score += 4
        for kw_pat, url_hint_pat in _MODULE_KEYWORDS:
            if _re.search(kw_pat, text, _re.IGNORECASE) and _re.search(url_hint_pat, url_text, _re.IGNORECASE):
                score += 3
        for seg in _re.findall(r'[a-z]{4,}', _label_from_url(href_l)):
            if _re.search(rf'\b{_re.escape(seg)}', text, _re.IGNORECASE):
                score += 1

        if score > best_score:
            best_score = score
            best_url = href

    if best_url and best_score >= 3:
        return base_root + best_url if best_url.startswith("/") else best_url
    return ""


def _inject_go_to(test_body: str, target_url: str) -> str:
    """
    Ensure a `Go To <target_url>` step is present right after `Open App And Login`.
    Idempotent — does nothing if a Go To with the same URL is already present.
    """
    if not target_url:
        return test_body
    if _re.search(rf'(?im)^\s*Go To\s+{_re.escape(target_url)}\s*$', test_body):
        return test_body
    if _re.search(r'(?im)^\s*Go To\s+\S+', test_body):
        return test_body

    lines = test_body.split("\n")
    new_lines = []
    injected = False
    for line in lines:
        new_lines.append(line)
        if not injected and _re.match(r'^\s*Open App And Login\b', line, _re.IGNORECASE):
            indent = _re.match(r'^(\s*)', line).group(1)
            new_lines.append(f"{indent}Go To    {target_url}")
            new_lines.append(f"{indent}Sleep    2s")
            injected = True
    return "\n".join(new_lines)


def _force_go_to(block: str, target: str) -> str:
    """
    Ensure the FIRST `Go To` in `block` points to `target`. Conservative
    override rules — see function body for details.
    """
    if not target:
        return block
    m = _re.search(r'(?im)^(\s*)Go To\s+(\S+)\s*$', block)
    if not m:
        return _inject_go_to(block, target)
    existing = m.group(2)
    if existing == target:
        return block
    if existing.startswith("${BASE_URL}"):
        new_line = f"{m.group(1)}Go To    {target}"
        return block[:m.start()] + new_line + block[m.end():]

    if "://" in existing and "://" in target:
        try:
            from urllib.parse import urlparse
            ex_p, tg_p = urlparse(existing), urlparse(target)
            if ex_p.netloc and ex_p.netloc == tg_p.netloc:
                def _module_key(path: str) -> str:
                    segs = [s for s in path.split('/')
                            if s and s not in ('web', 'index.php', 'app', 'spa')]
                    return segs[0] if segs else ''
                if _module_key(ex_p.path) != _module_key(tg_p.path):
                    new_line = f"{m.group(1)}Go To    {target}"
                    return block[:m.start()] + new_line + block[m.end():]
        except Exception:
            pass
    return block
