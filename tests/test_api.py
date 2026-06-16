"""End-to-end API tests through the FastAPI app (mock mode) — the full plan flow + guards.

This is the test that, run automatically, would have caught the prod break: it
drives /api/plan/start through to a built section. (In mock mode it exercises the
wiring; the real-mode parsing is covered in test_intake_vet.)"""

from conftest import wait_status

GRAB_BAG = "I like basketball, Magic the Gathering, and food, and I'm good at sales"


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
