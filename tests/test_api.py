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


def test_healthz(client):
    d = client.get("/healthz").json()
    assert d["ok"] is True and d["mock"] is True


def test_advisor_uses_drawer_not_native_prompt(client):
    # Ask-an-expert / convene must use the flyout drawer, never the native prompt() dialog.
    html = client.get("/").text
    assert 'class=drawer' in html and 'id=drawer-out' in html
    assert "function openDrawer" in html and "function submitDrawer" in html
    assert "prompt('Ask the advisor" not in html and "prompt('Ask your board" not in html


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
