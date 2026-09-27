"""AI chat demo safety: partial answers, fallback model, time budget, progress, conversation store.
Gemini is simulated at the httpx level (real status-code handling, no network)."""
import json

import httpx
import pytest

from app import config
from app.services import ai_chat
from test_routes import login
from test_sales import _paid

PRIMARY, FALLBACK = "primary-model", "fallback-model"


def _text(t):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": t}]}}]}


def _call(name="menu_performance", start="2026-09-25", end="2026-09-25"):
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": name, "args": {"start": start, "end": end}}}]}}]}


@pytest.fixture
def http(monkeypatch):
    """Scripted Gemini at the httpx level. Each script item: a dict body (200), an int status,
    or an exception instance. Records (model, payload, timeout) per request."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_MODEL", PRIMARY)
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODEL", FALLBACK)
    ai_chat.limiter.reset()
    state = {"script": [], "seen": []}

    def fake_post(url, json=None, timeout=None, headers=None):
        model = url.rsplit("/", 1)[1].split(":")[0]
        state["seen"].append({"model": model, "payload": json, "timeout": timeout})
        item = state["script"].pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = (item, {"error": {"message": "x"}}) if isinstance(item, int) else (200, item)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(ai_chat.httpx, "post", fake_post)
    return state


def _ask(db, conv="c1", q="Which dishes should I promote?"):
    return ai_chat.ask(q, conv, db["staff"]["manager"])


# ---------- 1. partial answers ----------

@pytest.mark.parametrize("failure, reason", [
    (httpx.ReadTimeout("slow"), "took too long"),
    (429, "rate limit"),
    (503, "overloaded"),
    (httpx.ConnectError("offline"), "Couldn't reach Gemini"),
])
def test_figures_survive_when_the_final_text_fails(db, clock, http, failure, reason):
    _paid(db, [("dal", 2), ("naan", 3)])
    # step 1 fetches figures; step 2 (the written answer) fails - on the fallback too for 429/503
    http["script"] = [_call(), failure] + ([failure] if isinstance(failure, int) else [])
    turn = _ask(db)
    assert turn["partial"] is True and "error" not in turn
    assert turn["note"] == "AI summary unavailable right now - here are the figures."
    assert reason in turn["reason"]
    assert [f["name"] for f in turn["figures"]] == ["menu_performance"]
    summary = " ".join(turn["summary"])
    assert "Dal Tadka (₹270)" in summary and "best seller by quantity was Butter Naan (3 sold)" in summary


def test_no_figures_means_a_plain_error(db, clock, http):
    http["script"] = [httpx.ConnectError("offline")]
    turn = _ask(db)
    assert "Couldn't reach Gemini" in turn["error"] and "partial" not in turn


def test_partial_answer_renders_figures_and_note(db, clock, http):
    _paid(db, [("dal", 1)])
    http["script"] = [_call("sales_summary"), 503, 503]
    html = login("manager").post("/insights/ask", data={"question": "How was today?"}).text
    assert "AI summary unavailable right now - here are the figures." in html
    assert "net sales ₹180 from 1 bills" in html
    assert '<details class="ai-figures" open>' in html


def test_summary_compares_two_ranges(db, clock, http):
    _paid(db, [("dal", 1)])
    http["script"] = [_call("sales_summary", "2026-09-18", "2026-09-24"), _call("sales_summary"),
                      httpx.ReadTimeout("slow")]
    turn = _ask(db, q="How did this week compare?")
    # week before: no sales; this day: ₹180 -> no % change possible from zero, so no comparison line
    assert not any(" vs " in line for line in turn["summary"])
    _paid(db, [("naan", 2)], table_index=1)  # now compare two days that both have sales
    http["script"] = [_call("sales_summary", "2026-09-25", "2026-09-25"),
                      _call("sales_summary", "2026-09-25", "2026-09-25"), httpx.ReadTimeout("slow")]
    same = _ask(db, conv="c2", q="Compare")
    assert "Net sales 2026-09-25 to 2026-09-25 were +0.0% vs 2026-09-25 to 2026-09-25." in same["summary"]


# ---------- 2. fallback model ----------

@pytest.mark.parametrize("status", [503, 429])
def test_fallback_model_retries_the_same_step(db, clock, http, status):
    _paid(db, [("dal", 1)])
    http["script"] = [status, _call(), _text("Promote Dal Tadka.")]
    turn = _ask(db)
    assert turn["answer"] == "Promote Dal Tadka." and turn["model"] == FALLBACK
    models = [s["model"] for s in http["seen"]]
    assert models == [PRIMARY, FALLBACK, FALLBACK]  # same step retried, then fallback kept
    assert http["seen"][0]["payload"] == http["seen"][1]["payload"]


def test_primary_answers_when_healthy(db, clock, http):
    http["script"] = [_text("Hello.")]
    turn = _ask(db)
    assert turn["model"] == PRIMARY and [s["model"] for s in http["seen"]] == [PRIMARY]


def test_model_shown_under_the_answer(db, clock, http):
    http["script"] = [503, _text("Short answer.")]
    html = login("manager").post("/insights/ask", data={"question": "hi"}).text
    assert "Short answer." in html and f"Answered by {FALLBACK}" in html


# ---------- 3. time budget ----------

def test_per_request_timeout_and_40s_budget(db, clock, http, monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(ai_chat, "_clock", lambda: t["now"])
    orig = ai_chat.httpx.post

    def slow(url, **kw):
        t["now"] += 18.0  # every reply takes 18 s
        return orig(url, **kw)

    monkeypatch.setattr(ai_chat.httpx, "post", slow)
    http["script"] = [_call(), _call("sales_summary"), _text("never reached")]
    turn = _ask(db)
    assert [round(s["timeout"]) for s in http["seen"]] == [20, 20]  # per-request cap
    assert len(http["seen"]) == 2  # 36 s used, 4 s left < 5 s: no third request
    assert turn["partial"] is True and "took too long" in turn["reason"]


def test_no_fallback_retry_under_five_seconds(db, clock, http, monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(ai_chat, "_clock", lambda: t["now"])
    orig = ai_chat.httpx.post

    def slow(url, **kw):
        t["now"] += 36.0
        return orig(url, **kw)

    monkeypatch.setattr(ai_chat.httpx, "post", slow)
    http["script"] = [503]
    turn = _ask(db)
    assert len(http["seen"]) == 1 and "overloaded" in turn["error"]


# ---------- 3. progress + double submit ----------

def test_progress_stages_in_order(db, clock, http, monkeypatch):
    stages = []
    real = ai_chat._set_stage
    monkeypatch.setattr(ai_chat, "_set_stage", lambda conv, st: (stages.append(st), real(conv, st)))
    http["script"] = [_call(), _text("Done.")]
    _ask(db)
    assert stages == ["Thinking...", "Fetching figures...", "Writing the answer...", None]
    assert ai_chat.progress("c1") is None


def test_progress_endpoint(db, http, monkeypatch):
    c = login("manager")
    assert c.get("/insights/progress").json() == {"stage": None}
    monkeypatch.setattr(ai_chat, "progress", lambda conv: "Fetching figures...")
    assert c.get("/insights/progress").json() == {"stage": "Fetching figures..."}
    assert login("counter").get("/insights/progress").status_code == 403


def test_second_question_while_pending_is_refused(db, clock, http):
    assert ai_chat._start("busy-conv") is True
    try:
        turn = _ask(db, conv="busy-conv")
        assert "Still working on your last question" in turn["error"]
        assert http["seen"] == []
    finally:
        ai_chat._set_stage("busy-conv", None)


def test_page_guards_double_submit():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app"
    assert 'hx-sync="this:drop"' in (root / "templates" / "insights.html").read_text()
    js = (root / "static" / "app.js").read_text()
    assert "if (aiPending) { e.preventDefault(); return; }" in js and "/insights/progress" in js


# ---------- 4. conversation store ----------

def test_store_caps_at_50_evicting_the_oldest(monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(ai_chat, "_clock", lambda: t["now"])
    store = ai_chat._Conversations()
    for i in range(55):
        t["now"] += 1
        store.add(f"conv-{i}", "q", "a")
    assert store.count() == 50
    assert store.history("conv-0") == [] and store.history("conv-4") == []   # oldest five gone
    assert store.history("conv-5") != [] and store.history("conv-54") != []


def test_store_expires_after_two_idle_hours(monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(ai_chat, "_clock", lambda: t["now"])
    store = ai_chat._Conversations()
    store.add("old", "q", "a")
    t["now"] += 3600
    store.add("recent", "q", "a")
    t["now"] += 3600 + 1  # "old" idle > 2 h, "recent" idle 1 h
    assert store.history("old") == [] and store.history("recent") != []
    assert store.count() == 1


def test_markdown_is_stripped_from_answers(db, clock, http):
    http["script"] = [_text("## Promote\n1. **Crispy Corn** (69.4% margin)\n* **Masala Chaas**\n- Fresh Lime Soda")]
    answer = _ask(db)["answer"]
    assert "**" not in answer and "##" not in answer
    assert answer == "Promote\n1. Crispy Corn (69.4% margin)\n• Masala Chaas\n• Fresh Lime Soda"
