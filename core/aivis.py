"""AI visibility: does ChatGPT, Perplexity, Gemini, or Claude mention this business when people ask about its services?

Each question is asked several times per engine (answers vary), every answer is read by a small model that pulls out
which businesses it named and in what order, and the scan reports mention rate, position, whether the site was cited,
what was said, wrong facts, and which sources the engines quoted instead.

These come from each engine's API with live web search turned on. They're close to what people see in the apps,
not identical (the apps also use the person's location and history), so the trend matters more than any one run.
"""
import logging
import re
from collections import Counter, defaultdict
from urllib.parse import urlparse

import httpx
from django.utils import timezone

from . import ai, conf
from .models import AIAnswer, AIPrompt, AIScan

log = logging.getLogger(__name__)
TRANSPORT = None  # tests swap in httpx.MockTransport
MAX_PROMPTS = 25
ENGINES = {"chatgpt": "ChatGPT", "perplexity": "Perplexity", "gemini": "Gemini", "claude": "Claude"}


class EngineError(Exception):
    pass


# ---------------------------------------------------------------- engines

def available_engines():
    keys = {"chatgpt": conf.get("OPENAI_API_KEY"), "perplexity": conf.get("PERPLEXITY_API_KEY"),
            "gemini": conf.get("GEMINI_API_KEY"), "claude": conf.get("ANTHROPIC_API_KEY")}
    return [e for e in ENGINES if keys[e]]


def _http():
    return httpx.Client(timeout=120, transport=TRANSPORT)


def _check(r, name):
    if r.status_code >= 400:
        try:
            msg = r.json()
            msg = (msg.get("error") or {}).get("message") if isinstance(msg.get("error"), dict) else msg.get("error") or msg
        except ValueError:
            msg = r.text[:200]
        raise EngineError(f"{name}: {str(msg)[:300]}")
    return r.json()


def ask_chatgpt(question):
    with _http() as h:
        d = _check(h.post("https://api.openai.com/v1/responses",
                          headers={"Authorization": f"Bearer {conf.get('OPENAI_API_KEY')}"},
                          json={"model": conf.get("OPENAI_MODEL"), "tools": [{"type": "web_search"}], "input": question}), "ChatGPT")
    text, cites = [], []
    for item in d.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                text.append(part.get("text", ""))
                cites += [a.get("url") for a in part.get("annotations", []) if a.get("type") == "url_citation" and a.get("url")]
    return "\n".join(text), cites


def ask_perplexity(question):
    with _http() as h:
        d = _check(h.post("https://api.perplexity.ai/chat/completions",
                          headers={"Authorization": f"Bearer {conf.get('PERPLEXITY_API_KEY')}"},
                          json={"model": conf.get("PERPLEXITY_MODEL"), "messages": [{"role": "user", "content": question}]}), "Perplexity")
    text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    cites = list(d.get("citations") or []) or [s.get("url") for s in d.get("search_results") or [] if s.get("url")]
    return text, cites


def ask_gemini(question):
    model = conf.get("GEMINI_MODEL")
    with _http() as h:
        d = _check(h.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          headers={"x-goog-api-key": conf.get("GEMINI_API_KEY")},
                          json={"contents": [{"parts": [{"text": question}]}], "tools": [{"google_search": {}}]}), "Gemini")
    cand = (d.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    # Gemini's source links are Google redirect URLs; the title is the site's domain, which is what we need.
    cites = [(c.get("web") or {}).get("title") or (c.get("web") or {}).get("uri")
             for c in (cand.get("groundingMetadata") or {}).get("groundingChunks", []) if c.get("web")]
    return text, [c for c in cites if c]


def ask_claude(question):
    resp = ai._client().messages.create(
        model=conf.get("AI_MODEL_PROFILE"), max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": question}])
    text, cites = [], []
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            text.append(block.text)
            for c in getattr(block, "citations", None) or []:
                if getattr(c, "url", None):
                    cites.append(c.url)
    return "".join(text), cites


ASK = {"chatgpt": ask_chatgpt, "perplexity": ask_perplexity, "gemini": ask_gemini, "claude": ask_claude}


# ---------------------------------------------------------------- questions

PROMPTS_TOOL = {
    "name": "save_questions",
    "description": "Save realistic questions local customers ask AI assistants.",
    "input_schema": {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}, "maxItems": 20}},
                     "required": ["questions"]},
}


def suggest_prompts(site, n=15):
    """Questions a customer might ask an AI assistant, built from the confirmed profile."""
    p = site.profile or {}
    name, kind = p.get("business_name") or site.name, p.get("business_type") or "business"
    services, towns = [s for s in p.get("services") or [] if s][:8], [t for t in p.get("service_areas") or [] if t][:4]
    if ai.ai_enabled():
        out = ai._call_tool(conf.get("AI_MODEL_CLASSIFY"), "You write realistic search questions for local businesses.",
                            f"""Write {n} different questions a real customer might type into ChatGPT or Perplexity when looking for a {kind}
like this one. Mix: "best X in TOWN" style, specific problems ("car shakes when braking, where in TOWN"), price or trust questions,
and 2 questions that name the business directly ("Is {name} any good?"). Use these services and towns, plain everyday wording,
no business name except in those 2. Services: {services}. Towns: {towns}.""", PROMPTS_TOOL, max_tokens=1200)
        qs = [q.strip() for q in out.get("questions", []) if q.strip()]
    else:
        town = towns[0] if towns else ""
        qs = [f"Best {s.lower()} in {town}" for s in services] + [f"Is {name} any good?", f"Who is the best {kind} in {town}?"]
    return [q[:400] for q in qs][:n]


# ---------------------------------------------------------------- reading answers

READ_TOOL = {
    "name": "read_answer",
    "description": "Record what an AI assistant's answer says about local businesses.",
    "input_schema": {
        "type": "object",
        "properties": {
            "businesses": {"type": "array", "items": {"type": "string"},
                           "description": "Every business recommended or named, in the order the answer presents them."},
            "client_mentioned": {"type": "boolean"},
            "client_position": {"type": ["integer", "null"], "description": "1-based position of the client in that list, or null."},
            "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative", "none"]},
            "wrong_facts": {"type": "array", "items": {"type": "string"},
                            "description": "Statements about the client that contradict the profile (hours, phone, address, services). Empty if none."},
        },
        "required": ["businesses", "client_mentioned", "client_position", "sentiment", "wrong_facts"],
    },
}


def _domain(u):
    u = u if "://" in u else f"https://{u}"
    return urlparse(u).netloc.lower().removeprefix("www.")


def read_answer(site, text):
    p = site.profile or {}
    facts = {k: p.get(k) for k in ("business_name", "phone", "address", "hours", "services", "service_areas")}
    out = ai._call_tool(conf.get("AI_MODEL_CLASSIFY"), "You extract structured facts from AI assistant answers.",
                        f"""The client business: {facts} (website {site.domain}).
Read this answer and record which businesses it names, in order, and what it says about the client. Count the client as mentioned
if it's named in any recognizable way (short name, website).

Answer:
{text[:6000]}""", READ_TOOL, max_tokens=800)
    return out


# ---------------------------------------------------------------- the scan

def run_scan(scan):
    site = scan.site
    prompts = list(site.ai_prompts.filter(active=True)[:MAX_PROMPTS])
    engines = scan.engines or available_engines()
    runs = max(1, int(conf.get("AI_VISIBILITY_RUNS") or 1))
    scan.status, scan.engines = AIScan.RUNNING, engines
    scan.save(update_fields=["status", "engines"])
    total, done, failures = len(prompts) * len(engines) * runs, 0, Counter()
    try:
        if not prompts:
            raise EngineError("Add some questions first.")
        if not engines:
            raise EngineError("Add at least one AI engine key in Settings.")
        for prompt in prompts:
            for engine in engines:
                for run in range(1, runs + 1):
                    if failures[engine] >= 3:
                        break  # this engine keeps failing (bad key, no credit): don't spend the whole scan on it
                    a = AIAnswer(scan=scan, prompt=prompt, prompt_text=prompt.text, engine=engine, run=run)
                    try:
                        a.text, cites = ASK[engine](prompt.text)
                        a.citations = list(dict.fromkeys(cites))[:30]
                        a.cited = any(_domain(c) == site.domain for c in a.citations)
                        read = read_answer(site, a.text) if a.text else {}
                        a.businesses = [b[:200] for b in read.get("businesses", [])][:20]
                        a.mentioned = bool(read.get("client_mentioned")) or a.cited
                        a.position = read.get("client_position") if a.mentioned else None
                        a.sentiment = read.get("sentiment", "") if a.mentioned else ""
                        a.wrong_facts = [w[:300] for w in read.get("wrong_facts", [])][:5]
                    except Exception as exc:
                        failures[engine] += 1
                        a.error = (ai.explain_error(exc) if engine == "claude" else str(exc))[:500]
                        if engine == "claude" and ai.is_fatal(exc):
                            failures[engine] = 99
                    a.save()
                    done += 1
                    if done % 5 == 0:
                        AIScan.objects.filter(id=scan.id).update(progress=f"{done} of {total} answers")
        scan.summary = summarize(scan)
        scan.status = AIScan.DONE
        failed = Counter(scan.answers.exclude(error="").values_list("engine", flat=True))
        scan.error = "; ".join(f"{ENGINES[e]} failed {n} time{'s' if n != 1 else ''} and was skipped after that" if failures[e] >= 3
                               else f"{ENGINES[e]} failed {n} time{'s' if n != 1 else ''}" for e, n in failed.items())
    except Exception as exc:
        log.exception("AI visibility scan %s failed", scan.id)
        scan.status, scan.error = AIScan.FAILED, str(exc)[:1000]
    scan.finished_at = timezone.now()
    scan.progress = ""
    scan.save()
    return scan


def _norm_name(s):
    s = re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("&", " and "))
    s = re.sub(r"\b(llc|inc|co|the|and)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_client(business, client_name):
    """Same business, allowing short forms ("Joe's Auto" for "Joe's Auto Repair") but not a rival
    that merely shares a first word ("Joe's Garage")."""
    b, c = _norm_name(business), _norm_name(client_name)
    if not b or not c:
        return False
    return b == c or (len(b) >= 6 and b in c) or (len(c) >= 6 and c in b)


def _pct(a, b):
    return round(a / b * 100) if b else None


def summarize(scan):
    site = scan.site
    answers = [a for a in scan.answers.all() if not a.error]
    name = ((site.profile or {}).get("business_name") or site.name).lower()
    by_engine, by_prompt = defaultdict(list), defaultdict(list)
    for a in answers:
        by_engine[a.engine].append(a)
        by_prompt[a.prompt_text].append(a)
    competitors, sources = Counter(), Counter()
    for a in answers:
        for b in a.businesses:
            if not is_client(b, name):
                competitors[b.strip()] += 1
        if not a.mentioned:
            for c in a.citations:
                d = _domain(c)
                if d and d != site.domain:
                    sources[d] += 1
    positions = [a.position for a in answers if a.position]
    return {
        "answers": len(answers),
        "mention_pct": _pct(sum(a.mentioned for a in answers), len(answers)),
        "cited_pct": _pct(sum(a.cited for a in answers), len(answers)),
        "avg_position": round(sum(positions) / len(positions), 1) if positions else None,
        "positive_pct": _pct(sum(a.sentiment == "positive" for a in answers if a.mentioned), sum(a.mentioned for a in answers)),
        "engines": [{"engine": e, "label": ENGINES[e], "answers": len(v), "mention_pct": _pct(sum(a.mentioned for a in v), len(v)),
                     "cited_pct": _pct(sum(a.cited for a in v), len(v))} for e, v in by_engine.items()],
        "prompts": sorted([{"text": t, "mentioned": sum(a.mentioned for a in v), "of": len(v)} for t, v in by_prompt.items()],
                          key=lambda x: (x["mentioned"] / x["of"] if x["of"] else 0, x["text"])),
        "competitors": competitors.most_common(10),
        "sources": sources.most_common(10),
        "wrong_facts": list(dict.fromkeys(w for a in answers for w in a.wrong_facts))[:10],
    }


def start_scan(site):
    scan = AIScan.objects.create(site=site, engines=available_engines())
    from django.db import transaction
    from .tasks_local import scan_ai
    transaction.on_commit(lambda: scan_ai.delay(scan.id))
    return scan


def add_suggested_prompts(site):
    have = {p.text.lower() for p in site.ai_prompts.all()}
    n = 0
    for q in suggest_prompts(site):
        if q.lower() not in have:
            AIPrompt.objects.create(site=site, text=q, added_by="ai")
            n += 1
    return n
