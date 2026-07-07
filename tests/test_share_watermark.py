"""The share loop + PDF watermark (2026-07-05): the public /p page carries the receipts and the
decision path (the two things nothing else in-market shows), and the PDF ships free-with-watermark
on a user's own key — $7 credits / any subscription render it clean. The artifact is the funnel."""

GRAB_BAG = "I fix small business phone systems and want to sell that as a service"


def wait_status(client, sid):
    import time
    for _ in range(200):
        s = client.get(f"/api/plan/{sid}").json()
        if s["status"] != "researching":
            return s
        time.sleep(0.01)
    raise AssertionError("plan never left researching")


def _finish(client, email):
    sid = client.post("/api/plan/start", json={"idea": GRAB_BAG, "email": email}).json()["id"]
    s = wait_status(client, sid)
    while not s["done"]:
        s = client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).json()
    return sid


def test_share_page_carries_receipts_and_path(client):
    from app import store
    store.plan_create("shr1", "u@x.com", "guitar coaching idea")
    store.plan_save(
        "shr1", status="done",
        files={"1-the-setup.md": "# Setup\nThe plan."},
        research={"rows": [
            {"text": "72% of learners quit in year one", "url": "https://example.edu/study", "mark": "ok"},
            {"text": "Our app grows your audience 10x", "url": "https://vendor.com/blog", "mark": "warn"},
        ]},
        tree={"active": "n2", "nodes": {   # STORED shape: nodes = dict keyed by id
            "n1": {"id": "n1", "kind": "idea", "parent": None, "title": "Guitar coaching"},
            "n2": {"id": "n2", "kind": "section", "parent": "n1", "step": 0, "title": "The setup",
                   "feedback": "focus on adult beginners"},
        }})
    client.post("/api/plan/shr1/share", json={"shared": True})
    pub = client.get("/p/shr1")
    assert pub.status_code == 200
    # the receipts: graded rows with cited/vendor labels
    assert "The receipts" in pub.text and "cited" in pub.text and "vendor" in pub.text
    assert "72% of learners quit" in pub.text
    # the decision path with the operator's steer note
    assert "How it got here" in pub.text and "focus on adult beginners" in pub.text
    # the maker line (the growth loop) — client-safe page: filg.ai domain, no long-form profanity
    assert "filg.ai" in pub.text and "fuck" not in pub.text.lower()


def test_share_page_thin_plan_still_renders(client):
    from app import store
    store.plan_create("shr2", "u@x.com", "guitar coaching idea")
    store.plan_save("shr2", status="done", files={"1-the-setup.md": "# Setup\nThe plan."})
    client.post("/api/plan/shr2/share", json={"shared": True})
    pub = client.get("/p/shr2")   # no research rows, no tree → no receipts/path blocks, no crash
    assert pub.status_code == 200 and "Setup" in pub.text
    assert "The receipts" not in pub.text and "How it got here" not in pub.text


def test_pdf_clean_when_billing_off(client):
    sid = _finish(client, "wm0@x.com")
    r = client.get(f"/api/plan/{sid}/plan.pdf")
    assert r.status_code == 200 and r.headers["X-FILG-Watermark"] == "0"
    assert r.content[:5] == b"%PDF-"


def test_pdf_watermarked_free_copy_on_own_key(client, monkeypatch):
    # Billing on, not a subscriber, no credits — but the account has its own key: the PDF still
    # renders, watermarked (free copy), instead of 402ing. The Gamma loop.
    from app import billing, keys, main
    sid = _finish(client, "wm1@x.com")
    monkeypatch.setattr(billing, "PDF_BILLING_ENABLED", True)
    monkeypatch.setattr(main, "_is_subscriber", lambda e: False)
    monkeypatch.setattr(billing, "claim_pdf", lambda e, k: False)
    monkeypatch.setattr(keys, "enabled", lambda: True)
    monkeypatch.setattr(keys, "has_key", lambda e: True)
    r = client.get(f"/api/plan/{sid}/plan.pdf")
    assert r.status_code == 200 and r.headers["X-FILG-Watermark"] == "1"
    assert r.content[:5] == b"%PDF-" and len(r.content) > 5000


def test_pdf_paywalled_without_key_or_credits(client, monkeypatch):
    from app import billing, keys, main
    sid = _finish(client, "wm2@x.com")
    monkeypatch.setattr(billing, "PDF_BILLING_ENABLED", True)
    monkeypatch.setattr(main, "_is_subscriber", lambda e: False)
    monkeypatch.setattr(billing, "claim_pdf", lambda e, k: False)
    monkeypatch.setattr(keys, "enabled", lambda: True)
    monkeypatch.setattr(keys, "has_key", lambda e: False)
    r = client.get(f"/api/plan/{sid}/plan.pdf")
    assert r.status_code == 402
    d = r.json()
    assert d["needPurchase"] and "watermarked" in d["error"]


def test_render_watermark_param():
    # plan_pdf.render accepts watermark=True and still produces a valid PDF.
    from app import plan_pdf
    from app import planner
    r = planner.research("I play guitar and want to help people learn", mock=True)
    sess = {"idea": "guitar coaching", "research": r, "files": {}, "history": [], "step": 0,
            "cost": 0.0, "status": "building",
            "shaped": {"thesis": "guitar coaching for adults", "founder_edge": "10 years teaching"},
            "vetting": {"verdict": "pursue", "biggest_risk": "thin pipeline",
                        "first_test": "post in 3 communities"}}
    prop, _ = planner.first_proposal(sess["idea"], r, mock=True)
    sess["proposal"] = prop
    while sess.get("status") != "done":
        sess.update(planner.advance(sess, "yes_and", None, mock=True))
    plan, _ = plan_pdf.synthesize(sess, mock=True)
    data = plan_pdf.render(plan, watermark=True)
    assert bytes(data)[:5] == b"%PDF-" and len(data) > 5000
