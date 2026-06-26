"""End-to-end API tests through the FastAPI app (mock mode) — the full plan flow + guards.

This is the test that, run automatically, would have caught the prod break: it
drives /api/plan/start through to a built section. (In mock mode it exercises the
wiring; the real-mode parsing is covered in test_intake_vet.)"""

from conftest import wait_status

GRAB_BAG = "I like basketball, Magic the Gathering, and food, and I'm good at sales"


def test_coupon_redeem_unlocks_and_caps():
    # The free-unlock coupon: redeeming grants PDF access (record_purchase); a re-redeem on the same
    # account doesn't burn a use; a code is spent once used >= max_uses; the count is admin-only.
    from app import store
    store.init()
    code = "TESTCAP2"
    con = store._connect()
    with con:
        con.execute("INSERT OR REPLACE INTO coupons (code, max_uses, used, active, created_at) "
                    "VALUES (?,2,0,1,'t')", (code,))
    con.close()
    ok, _ = store.redeem_coupon(code, "a@x.com")
    assert ok and store.has_purchased("a@x.com")
    assert store.coupon_status(code)["used"] == 1
    ok2, reason2 = store.redeem_coupon(code, "a@x.com")          # same account → no second use burned
    assert ok2 and reason2 == "already" and store.coupon_status(code)["used"] == 1
    ok3, _ = store.redeem_coupon(code, "b@x.com")                # second distinct redeem hits the cap
    assert ok3 and store.coupon_status(code)["used"] == 2
    ok4, reason4 = store.redeem_coupon(code, "c@x.com")          # cap reached → spent
    assert not ok4 and reason4 == "spent" and not store.has_purchased("c@x.com")
    assert store.redeem_coupon("NOPE", "d@x.com") == (False, "invalid")
    assert store.coupon_status("FUCKYOUIMNOTGIVINGYOU13BUCKS")["max_uses"] == 100  # standing code seeded


def test_full_plan_flow_with_board(client):
    r = client.post("/api/plan/start",
                    json={"idea": GRAB_BAG, "email": "e2e@x.com", "directors": ["closer", "cfo"]})
    assert r.status_code == 200
    sid = r.json()["id"]

    s = wait_status(client, sid)
    assert s["status"] == "building"
    assert s["shaped"]["thesis"] and s["vetting"]["verdict"] in ("pursue", "pivot", "kill")
    assert s["directors"] == ["closer", "cfo"]
    assert s["proposal"]["section"] == "brief"

    # advance one section → the board auto-reviews it with per-director takes + a takeaway
    s = client.post(f"/api/plan/{sid}/respond", json={"choice": "yes_and", "note": ""}).json()
    assert len(s["files"]) == 1
    assert len(s["board"]) == 1 and len(s["board"][-1]["directors"]) == 2
    assert s["board"][-1]["verdict"]

    # convene the board on demand
    b = client.post(f"/api/plan/{sid}/board",
                    json={"question": "is pricing right?", "directors": ["closer", "cfo"]}).json()
    assert len(b["directors"]) == 2 and b["verdict"] and b["consensus"]

    # one-off expert
    a = client.post(f"/api/plan/{sid}/ask",
                    json={"archetype": "growth", "question": "which channel?"}).json()
    assert "composite" in a["answer"].lower()


def test_branching_next_back_goto(client):
    sid = client.post("/api/plan/start",
                      json={"idea": GRAB_BAG, "email": "tree@x.com"}).json()["id"]
    s = wait_status(client, sid)
    # the tree shows from the first render (single 'setup' node) so the tool is there immediately
    assert s["proposal"]["section"] == "brief" and s["tree"] and s["tree"]["show"]
    assert len(s["tree"]["nodes"]) == 1

    # roll forward twice → two sections finalized, still a single (unbranched) line
    s = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()
    s = client.post(f"/api/plan/{sid}/next", json={"feedback": "go bolder"}).json()
    assert s["step"] == 2 and len(s["files"]) == 2

    # a forward note is flagged on the next section (how it folded into the plan)
    assert s["proposal"]["change"] and "go bolder" in s["proposal"]["change"]

    # back without a note → form error (feedback is required to go back)
    r = client.post(f"/api/plan/{sid}/back", json={"feedback": ""})
    assert r.status_code == 400

    # back WITH a note → re-drafts the previous part as a new branch; the tree now reveals itself
    s = client.post(f"/api/plan/{sid}/back", json={"feedback": "narrower niche"}).json()
    assert s["step"] == 1 and len(s["files"]) == 1          # backed up a step, that section reopened
    assert s["tree"]["show"]                                 # a real branch exists now
    assert "revised" in s["proposal"]["draft"]              # the note steered the re-draft
    nodes = s["tree"]["nodes"]
    assert sum(1 for n in nodes if n["step"] == 1) == 2     # two sibling branches at part 2

    # hop back to the original branch's node via the tree, then forward again → another branch
    active = s["tree"]["active"]
    other = next(n["id"] for n in nodes if n["step"] == 1 and n["id"] != active)
    s = client.post(f"/api/plan/{sid}/goto", json={"node": other}).json()
    assert s["tree"]["active"] == other and s["step"] == 1


def test_redraft_regenerates_current_part_as_sibling(client):
    # "Not feeling it" regenerates the CURRENT part in place (a sibling at the same step), not a step back.
    sid = client.post("/api/plan/start",
                      json={"idea": GRAB_BAG, "email": "redraft@x.com"}).json()["id"]
    s = wait_status(client, sid)
    s = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()   # advance to part 2
    assert s["step"] == 1 and len(s["files"]) == 1

    # redraft without a note → form error (a rework needs steering)
    assert client.post(f"/api/plan/{sid}/redraft", json={"feedback": ""}).status_code == 400

    # redraft WITH a note → same step, the note steers it, a sibling exists at this step, nothing finalized
    s = client.post(f"/api/plan/{sid}/redraft", json={"feedback": "go bolder"}).json()
    assert s["step"] == 1 and len(s["files"]) == 1          # still on part 2, no new file finalized
    assert "revised" in s["proposal"]["draft"]              # the note steered the regenerate
    nodes = s["tree"]["nodes"]
    assert sum(1 for n in nodes if n["step"] == 1) == 2     # original + the regenerated sibling
    assert s["tree"]["active"] != [n["id"] for n in nodes if n["step"] == 1][0]  # active moved to the new one


def test_download_follows_active_branch_no_paywall(client):
    # Build a plan to completion, then branch part 2 and finish again. The download must zip the
    # ACTIVE branch's files (the final decision set) — and no paywall gates it.
    sid = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": "dl@x.com"}).json()["id"]
    s = wait_status(client, sid)
    while not s["done"]:
        s = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()
    r = client.get(f"/api/plan/{sid}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"  # no 402 paywall

    # hop back to an earlier node, branch with a note, finish that branch → download reflects it
    early = next(n["id"] for n in s["tree"]["nodes"] if n["step"] == 1)
    client.post(f"/api/plan/{sid}/goto", json={"node": early})
    s = client.post(f"/api/plan/{sid}/back", json={"feedback": "make the setup B2B only"}).json()
    while not s["done"]:
        s = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()
    r = client.get(f"/api/plan/{sid}/download")
    assert r.status_code == 200 and b"1-the-setup.md" in r.content


def test_chat_with_plan(client):
    # The standing advisor: grounded, persisted on the plan, owner-only.
    sid = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": "chat@x.com"}).json()["id"]
    s = wait_status(client, sid)
    while not s["done"]:
        s = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()
    assert s["chat"] == [] and s["chatStarters"]                 # state exposes thread + starter Qs

    r = client.post(f"/api/plan/{sid}/chat", json={"message": "why would they buy from me?"})
    assert r.status_code == 200
    d = r.json()
    assert d["reply"] and len(d["messages"]) == 2                # user + assistant persisted
    assert d["messages"][0]["role"] == "user" and d["messages"][1]["role"] == "assistant"

    # the thread persists on the session and a follow-up appends
    again = client.post(f"/api/plan/{sid}/chat", json={"message": "and the price?"}).json()
    assert len(again["messages"]) == 4
    assert client.get(f"/api/plan/{sid}").json()["chat"][0]["content"] == "why would they buy from me?"

    # empty message rejected
    assert client.post(f"/api/plan/{sid}/chat", json={"message": "  "}).status_code == 400


def test_plan_pdf_renders(client):
    # The core artifact: a finished plan downloads as a real, styled PDF (no paywall).
    sid = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": "pdf@x.com"}).json()["id"]
    s = wait_status(client, sid)
    while not s["done"]:
        s = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()
    r = client.get(f"/api/plan/{sid}/plan.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-" and len(r.content) > 5000      # a non-trivial PDF
    assert "filename=" in r.headers.get("content-disposition", "")

    # not downloadable until finished
    sid2 = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": "pdf2@x.com"}).json()["id"]
    wait_status(client, sid2)
    assert client.get(f"/api/plan/{sid2}/plan.pdf").status_code == 400


def test_gibberish_idea_gets_roasted_for_free(client):
    # Total nonsense → a pre-rolled roast, status 200, NO session created (no run, no LLM spend).
    junk = "asdlfk asd fa lskdjf llaskjdflkajs dflk asdfasd lf lk asdlfk sladkf lkasdf"
    r = client.post("/api/plan/start", json={"idea": junk, "email": "junk@x.com"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("gibberish") is True and d.get("title") and d.get("body") and "id" not in d
    # a real (if rough) idea is never roasted
    ok = client.post("/api/plan/start",
                     json={"idea": "i wanna help dentists with there missed calls", "email": "ok@x.com"})
    assert ok.status_code == 200 and "id" in ok.json()


def test_goto_unknown_node_404(client):
    sid = client.post("/api/plan/start",
                      json={"idea": GRAB_BAG, "email": "g@x.com"}).json()["id"]
    wait_status(client, sid)
    assert client.post(f"/api/plan/{sid}/goto", json={"node": "nope"}).status_code == 404


def test_short_idea_rejected(client):
    r = client.post("/api/plan/start", json={"idea": "hi", "email": "x@x.com"})
    assert r.status_code == 400


def test_missing_email_rejected(client):
    r = client.post("/api/plan/start", json={"idea": GRAB_BAG})
    assert r.status_code == 400


def test_bad_choice_rejected(client):
    sid = client.post("/api/plan/start",
                      json={"idea": GRAB_BAG, "email": "bc@x.com"}).json()["id"]
    wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/respond", json={"choice": "nope"})
    assert r.status_code == 400


def test_unknown_session_404(client):
    assert client.get("/api/plan/nope").status_code == 404


def test_plan_ownership_enforced_when_auth_on(client, monkeypatch):
    # With auth enabled, an owned plan is private: only the owner's verified token can load it.
    # Mint ES256 tokens like Supabase's asymmetric system and point the verifier at our public key.
    import time, types
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from app import auth, store
    priv = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "_jwk_client", types.SimpleNamespace(
        get_signing_key_from_jwt=lambda t: types.SimpleNamespace(key=priv.public_key())))

    def mint(email):
        return jwt.encode({"sub": "u", "email": email, "aud": "authenticated", "exp": time.time() + 60},
                          priv, algorithm="ES256", headers={"kid": "test"})

    store.plan_create("own1", "owner@x.com", "an idea about dentists")
    assert client.get("/api/plan/own1").status_code == 404  # no token → private
    assert client.get("/api/plan/own1",
                      headers={"Authorization": "Bearer " + mint("other@x.com")}).status_code == 404
    assert client.get("/api/plan/own1",
                      headers={"Authorization": "Bearer " + mint("owner@x.com")}).status_code == 200


def test_share_and_delete(client):
    from app import store
    store.plan_create("sh1", "u@x.com", "guitar coaching idea")
    store.plan_save("sh1", status="done", files={"1-the-setup.md": "# Setup\nThe plan."})
    assert client.get("/p/sh1").status_code == 404            # private by default
    r = client.post("/api/plan/sh1/share", json={"shared": True})
    assert r.status_code == 200 and r.json()["url"].endswith("/p/sh1")
    pub = client.get("/p/sh1")
    assert pub.status_code == 200 and "Setup" in pub.text     # public read-only render
    assert client.post("/api/plan/sh1/delete").status_code == 200
    assert client.get("/api/plan/sh1").status_code == 404
    assert client.get("/p/sh1").status_code == 404            # gone after delete


def test_tables_favicon_and_headings(client):
    html = client.get("/").text
    assert "<table><thead><tr>" in html and ".md table{" in html  # client renders + styles md tables
    assert 'rel="icon"' in html and "class=logomark" in html      # custom favicon + header mark
    assert "<h3>Ask an expert</h3>" in html and "Add-ons ·" not in html
    assert "Let’s go." in html and "You, 30 seconds ago" in html  # brand pull-quote


def test_healthz(client):
    d = client.get("/healthz").json()
    assert d["ok"] is True and d["mock"] is True


def test_advisor_uses_drawer_not_native_prompt(client):
    # Ask-an-expert / convene must use the flyout drawer, never the native prompt() dialog.
    html = client.get("/").text
    assert 'class=drawer' in html and 'id=drawer-out' in html
    assert "function openDrawer" in html and "function submitDrawer" in html
    assert "prompt('Ask the advisor" not in html and "prompt('Ask your board" not in html


def test_inline_comment_and_runner_ui_present(client):
    # #7 inline comments + the permanent main-column runner are client-side; guard their wiring stays.
    html = client.get("/").text
    assert "id=cmtpop" in html and "function saveComment" in html and "function commentsSteer" in html
    assert "function onDraftSelect" in html and "function renderComments" in html
    assert 'id=runner' in html and "classList.toggle('min')" in html   # permanent runner + collapse toggle
    assert "function _drainProgress" in html and "leafDone" in html     # leaf fan-out viz wiring
    assert "function forceNext" in html and "function talkItOut" in html  # softened kill-gate off-ramps


def test_model_stack_selection(client):
    # The user can pick a model stack; it persists, unknown values fall back, legacy names alias forward.
    sid = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": "stack@x.com"}).json()["id"]
    s = wait_status(client, sid)
    assert s["stack"] == "the-work-horse"                              # default = best Opus-free tier
    s = client.post(f"/api/plan/{sid}/stack", json={"stack": "trust-fund-baby"}).json()
    assert s["stack"] == "trust-fund-baby"
    s = client.post(f"/api/plan/{sid}/stack", json={"stack": "the-wonder-kid"}).json()
    assert s["stack"] == "the-wonder-kid"
    s = client.post(f"/api/plan/{sid}/stack", json={"stack": "damn-good"}).json()   # legacy alias
    assert s["stack"] == "the-work-horse"
    s = client.post(f"/api/plan/{sid}/stack", json={"stack": "bogus"}).json()
    assert s["stack"] == "the-work-horse"                              # unknown → default


def test_no_native_browser_dialogs(client):
    # The ux-design skill forbids native alert/confirm/prompt for product UI. The whole app must
    # use the styled toast/modal helpers instead. Match call-sites (foo(, not substrings of words).
    import re
    html = client.get("/").text
    bad = re.findall(r"(?<![\w.])(?:alert|confirm|prompt)\s*\(", html)
    assert not bad, f"native dialog call(s) leaked back in: {bad}"
    assert "function toast(" in html and "function uiConfirm(" in html and "function uiPrompt(" in html


def test_accessibility_essentials_present(client):
    # Guards the UX-pass a11y baseline (WCAG/POUR) against regression.
    html = client.get("/").text
    assert "focus-visible{outline" in html            # visible keyboard focus
    assert "prefers-reduced-motion" in html           # honors reduced motion
    assert "role=dialog aria-modal=true" in html      # drawer is a real dialog
    assert 'aria-live=polite' in html                 # screen-reader status
    for lbl in ("<label for=idea", "<label for=email", "<label for=drawerq"):
        assert lbl in html                            # inputs are labeled
    assert "aria-pressed" in html                     # toggle chips expose state
    assert "DRAWER_TRIGGER" in html                   # focus restored on drawer close


def test_kill_gate_blocks_until_resubstantiated(client):
    # A killed idea must not roll forward into a full plan. Mock vet always returns 'pursue', so we
    # force the kill verdict the gate keys off, then prove the hard gate + the /revet rescue path.
    from app import main
    sid = client.post("/api/plan/start",
                      json={"idea": "I want fame and money, help me get some", "email": "kill@x.com"}).json()["id"]
    wait_status(client, sid)
    s = main.store.plan_get(sid)
    v = {**(s.get("vetting") or {}), "verdict": "kill", "biggest_risk": "no skill or buyer named"}
    sh = {**(s.get("shaped") or {}), "clarifying_question": "What are you genuinely good at?"}
    main.store.plan_save(sid, vetting=v, shaped=sh)

    r = client.post(f"/api/plan/{sid}/next", json={"feedback": ""})   # hard gate
    assert r.status_code == 422 and r.json().get("needSubstance") is True
    assert r.json().get("question")                                   # the clarifying prompt comes back

    assert client.post(f"/api/plan/{sid}/revet", json={"more": "idk"}).status_code == 400   # too thin

    out = client.post(f"/api/plan/{sid}/revet",   # real substance → mock vet clears to pursue
                      json={"more": "I have run paid ads for SaaS for 4 years and know founders who pay for it"}).json()
    assert out["vetting"]["verdict"] != "kill"
    s2 = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()   # builder now advances


def test_kill_gate_softened_force_builds_waste_of_time(client):
    # The gate is a coaching ladder, not a wall: a non-forced advance still gets the advisement, but the
    # operator can FORCE past it with no substance → comedic "waste of time" mode (zero spend, never a
    # credible plan), and the kill verdict persists so the snark keeps escalating.
    from app import main
    sid = client.post("/api/plan/start",
                      json={"idea": "I want fame and money, help me get some", "email": "wod@x.com"}).json()["id"]
    wait_status(client, sid)
    s = main.store.plan_get(sid)
    main.store.plan_save(sid, vetting={**(s.get("vetting") or {}), "verdict": "kill",
                                       "biggest_risk": "no skill or buyer named"})

    # non-forced → still the advisement (off-ramp = /revet)
    assert client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).status_code == 422

    # forced → builds a self-aware comedic placeholder, advances a step, costs ~$0
    before = main.store.plan_get(sid).get("cost") or 0.0
    s2 = client.post(f"/api/plan/{sid}/next", json={"feedback": "", "force": True}).json()
    assert s2["step"] == 1 and len(s2["files"]) == 1
    setup = next(f["content"] for f in s2["files"] if f["path"] == "1-the-setup.md")
    assert "button" in setup.lower()                               # the comedic copy, not a real plan
    assert s2["vetting"]["verdict"] == "kill"                       # gate persists → escalation continues
    assert (main.store.plan_get(sid).get("cost") or 0.0) == before  # waste-of-time mode skips the pipeline
    assert s2.get("step", 0) >= 1


def test_session_usage_meter_fields(client):
    # The live session usage meter reads cumulative cost/tokens off each response. Assert the wiring
    # exposes them everywhere it ticks (values are 0 in mock mode; real mode fills from the ledger).
    sid = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": "meter@x.com"}).json()["id"]
    s = wait_status(client, sid)
    assert "cost" in s and "tokens" in s                      # plan-state (poll + build ops)
    nxt = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()
    assert "cost" in nxt and "tokens" in nxt
    chat = client.post(f"/api/plan/{sid}/chat", json={"message": "why would they buy from me?"}).json()
    assert "cost" in chat and "tokens" in chat                # side op echoes cumulative for the meter


def _finish_plan(client, email):
    sid = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": email}).json()["id"]
    s = wait_status(client, sid)
    while not s["done"]:
        s = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()
    return sid


def test_pdf_purchase_gate_raw_stays_free(client, monkeypatch):
    # The locked model (business_plan §16.1): the polished PDF is the one paid action ($35 one-time),
    # but the raw export is always free. When PDF billing is live and the user hasn't bought, the PDF
    # route returns 402 needPurchase; the raw .zip is untouched.
    from app import main
    sid = _finish_plan(client, "gate@x.com")

    monkeypatch.setattr(main, "_has_pdf_access", lambda *a, **k: False)   # locked (hasn't bought)
    r = client.get(f"/api/plan/{sid}/plan.pdf")
    assert r.status_code == 402 and r.json().get("needPurchase") is True
    assert client.get(f"/api/plan/{sid}/download").status_code == 200    # raw export still free

    monkeypatch.setattr(main, "_has_pdf_access", lambda *a, **k: True)    # bought → unlocked
    r = client.get(f"/api/plan/{sid}/plan.pdf")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


def test_pdf_open_when_billing_unconfigured(client):
    # Dev/local (no Stripe key → PDF_BILLING_ENABLED False): the PDF is open so the app still runs.
    from app import main
    assert main.billing.PDF_BILLING_ENABLED is False
    sid = _finish_plan(client, "dev@x.com")
    assert client.get(f"/api/plan/{sid}/plan.pdf").status_code == 200


def test_free_taste_dedup_normalizes_email():
    # Anti-abuse (§16.2 #2): the free-taste counter dedupes on a normalized email, so +suffix and
    # gmail-dot aliases of the same person count as one taste, not infinite.
    import auth
    import usage
    a = auth.normalize_email("Taste.Dedup+one@gmail.com")
    b = auth.normalize_email("tastededup+two@googlemail.com")
    assert a == b == "tastededup@gmail.com"
    usage.record_run(a, 0.1)
    assert usage.free_used(b) is True               # the alias is already counted as used
    assert usage.free_used("someone-else@x.com") is False


def test_clean_plan_url_serves_spa(client):
    # History-API routing: /plan/{id} serves the SPA shell (not a 404), so deep-links/refresh work
    # and there's no '#' in the path. Distinct from /p/{id} (public share) and /r/{id} (teardown).
    r = client.get("/plan/abc123def")
    assert r.status_code == 200 and "window.FILG" in r.text
    home = client.get("/").text
    assert "routeFromPath" in home and "popstate" in home   # path router + back/fwd wired
    assert "location.hash" not in home                       # hash routing fully removed


def test_humanize_error_translates_openrouter_401():
    from app import main
    msg, need_key = main._humanize_error(
        Exception("Error code: 401 - {'error': {'message': 'User not found.', 'code': 401}}"))
    assert need_key is True
    assert "key" in msg.lower() and "401" not in msg and "User not found" not in msg


def test_humanize_error_credits_and_generic():
    from app import main
    msg_c, nk_c = main._humanize_error(Exception("Error code: 402 - insufficient credits openrouter"))
    assert nk_c is False and "credit" in msg_c.lower()
    msg_g, nk_g = main._humanize_error(ValueError("could not parse JSON from model"))
    assert nk_g is False and "went wrong" in msg_g.lower() and "JSON" not in msg_g


def test_engine_error_sets_needkey_flag():
    from app import main
    r = main._engine_error(Exception("Error code: 401 - User not found."))
    import json
    body = json.loads(bytes(r.body))
    assert body.get("needKey") is True and "key" in body["error"].lower()
