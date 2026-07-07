"""The diverge/converge funnel routes: /api/brainstorm -> /merge -> /commit, plus the /route spine
and lazy /node content. Mock mode (conftest sets FILG_MOCK=1), driven through the real TestClient."""

from conftest import wait_status

RAW = "I'm a baker and think I'm really good. I live in the middle of nowhere, how do I sell what I make?"


def _brainstorm(client, idea=RAW):
    r = client.post("/api/brainstorm", json={"idea": idea})
    assert r.status_code == 200, r.text
    return r.json()


# ── brainstorm: anonymous, no email, seeds the tree ──────────────────────────
def test_brainstorm_is_anonymous_and_seeds_option_nodes():
    from fastapi.testclient import TestClient
    from app import main
    c = TestClient(main.app)
    s = _brainstorm(c)
    assert s["stage"] == "brainstorm"
    # a brainstorm fork node + one option child per direction
    kinds = sorted(n["kind"] for n in s["tree"]["nodes"])
    assert "brainstorm" in kinds and kinds.count("option") >= 1
    an = s["activeNode"]
    assert an["kind"] == "brainstorm" and an["options"]
    assert an["options"][0]["direction"]["title"]     # each option carries a direction card


def test_brainstorm_rejects_too_short(client):
    r = client.post("/api/brainstorm", json={"idea": "hi"})
    assert r.status_code == 400


def test_brainstorm_roasts_gibberish(client):
    r = client.post("/api/brainstorm", json={"idea": "asdfjkl qwerty zxcvbn hjkl"})
    assert r.status_code == 200 and r.json().get("gibberish")


# ── merge: converge chosen options into one refined idea ─────────────────────
def test_merge_produces_a_refined_node_with_the_cull(client):
    s = _brainstorm(client)
    sid = s["id"]
    opt_ids = [o["id"] for o in s["activeNode"]["options"]]
    r = client.post(f"/api/plan/{sid}/merge", json={"options": opt_ids})
    assert r.status_code == 200
    s = wait_status(client, sid)
    assert s["stage"] == "refined"
    an = s["activeNode"]
    assert an["kind"] == "refined" and an["thesis"]
    assert isinstance(an["kept"], list) and isinstance(an["dropped"], list)
    assert an["research"]["prose"]["title"]           # light first-pass skim rode along
    # the tree view names the options the refined node JOINED (the graph draws it as a merge)
    refined = next(n for n in s["tree"]["nodes"] if n["kind"] == "refined")
    assert set(refined["selected"]) == set(opt_ids)


def test_merge_needs_a_selection(client):
    s = _brainstorm(client)
    r = client.post(f"/api/plan/{s['id']}/merge", json={"options": []})
    assert r.status_code == 400


# ── refine: the prelaunch gate sharpens in place, never re-spreads ────────────
def test_refined_gate_carries_questions_and_a_light_skim(client):
    from app import brainstorm
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [s["activeNode"]["options"][0]["id"]]})
    s = wait_status(client, sid)
    an = s["activeNode"]
    assert an["questions"] and len(an["questions"]) <= 3        # the gate's clarifying questions
    assert len(an["research"]["lanes"]) <= brainstorm.MERGE_RESEARCH_LANES   # the skim stays light


def test_refine_chains_a_sharper_idea_under_the_gate(client, monkeypatch):
    from app import main
    s = _brainstorm(client)
    sid = s["id"]
    opt_ids = [o["id"] for o in s["activeNode"]["options"]]
    client.post(f"/api/plan/{sid}/merge", json={"options": opt_ids})
    s = wait_status(client, sid)
    v1 = s["activeNode"]["id"]
    captured = {}
    real = main.brainstorm.merge
    def spy(idea, directions, **kw):
        captured["idea"], captured["directions"] = idea, directions
        return real(idea, directions, **kw)
    monkeypatch.setattr(main.brainstorm, "merge", spy)
    note = "the first buyer is gift senders, not homesick expats"
    r = client.post(f"/api/plan/{sid}/refine", json={"note": note})
    assert r.status_code == 200
    s = wait_status(client, sid)
    an = s["activeNode"]
    assert s["stage"] == "refined" and an["kind"] == "refined" and an["id"] != v1
    assert an["feedback"] == note                               # the clarification stays visible
    node = next(n for n in s["tree"]["nodes"] if n["id"] == an["id"])
    assert node["parent"] == v1                                 # lineage: it chains under the gate
    # the re-merge saw the prior thesis + the clarification on top, and re-ran the SAME picks
    assert "THE MERGED THESIS SO FAR" in captured["idea"] and note in captured["idea"]
    assert len(captured["directions"]) == len(opt_ids)
    # committing now builds the deep run off the sharpened node
    client.post(f"/api/plan/{sid}/commit", json={})
    s = wait_status(client, sid)
    assert s["stage"] == "building" and s["proposal"]


def test_refine_requires_a_refined_node(client):
    s = _brainstorm(client)
    r = client.post(f"/api/plan/{s['id']}/refine", json={"note": "the buyer is gift senders"})
    assert r.status_code == 400


def test_refine_roasts_a_mash_answer(client):
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [s["activeNode"]["options"][0]["id"]]})
    wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/refine", json={"note": "asdfjkl qwerty zxcvbn hjkl"})
    assert r.status_code == 200 and r.json().get("gibberish")


# ── commit: the deep run, attached under the refined node ────────────────────
def test_commit_runs_deep_build_and_starts_the_plan(client):
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [o["id"] for o in s["activeNode"]["options"]]})
    s = wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/commit", json={})
    assert r.status_code == 200
    s = wait_status(client, sid)
    assert s["stage"] == "building" and s["status"] == "building"
    assert s["proposal"] and s["vetting"]             # deep research + vet + first draft landed
    # a section node now hangs under the refined node — the funnel history is preserved in the tree
    kinds = sorted(n["kind"] for n in s["tree"]["nodes"])
    assert "brainstorm" in kinds and "refined" in kinds and "section" in kinds


def test_funnel_convene_survives_the_commit(client):
    # a board convene DURING the funnel (refined stage) rides the node tree into the deep build,
    # so the built plan's exports + board-notes steering still see it
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [o["id"] for o in s["activeNode"]["options"]]})
    wait_status(client, sid)
    b = client.post(f"/api/plan/{sid}/board", json={"question": "worth committing?"}).json()
    assert b["verdict"]
    client.post(f"/api/plan/{sid}/commit", json={})
    s = wait_status(client, sid)
    assert s["stage"] == "building"
    assert len(s["board"]) == 1 and s["board"][0]["section"] == "convene"


def test_build_receipts_persist_on_the_node(client):
    # backlog §v2 #10: each background op's receipts are stored server-side on the node it built,
    # so a reload / deep link restores the "how this was built" record (was a client-only stash)
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [s["activeNode"]["options"][0]["id"]]})
    s = wait_status(client, sid)
    refined_id = s["activeNode"]["id"]
    assert s["activeNode"]["log"]                        # merge's receipts rode onto the refined node
    client.post(f"/api/plan/{sid}/commit", json={})
    s = wait_status(client, sid)
    log = s["activeNode"]["log"]
    assert log and any(ln.startswith("Verdict:") for ln in log)
    assert not any(ln.startswith("§") for ln in log)     # layout sentinels never persist
    # a PAST node's receipts come back on the lazy /node fetch
    r = client.get(f"/api/plan/{sid}/node/{refined_id}")
    assert r.status_code == 200 and r.json()["log"]


def test_commit_reuses_the_skim_claims_when_thesis_unchanged(client, monkeypatch):
    # T2: committing the SAME refined idea the skim researched carries its fetched claims into the deep
    # build (prepare gets `prior`) so it re-grades instead of re-running the web fan-out. (Mock merge
    # fetches no real claims, so inject a skim claim onto the refined node to fire the reuse predicate.)
    import app.main as m
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [o["id"] for o in s["activeNode"]["options"]]})
    s = wait_status(client, sid)
    refined_id = s["activeNode"]["id"]
    full = m.store.plan_get(sid)
    tree = full["tree"]
    node = tree["nodes"][refined_id]
    node["research"] = dict(node.get("research") or {})
    node["research"]["claims"] = [{"text": "~2.5M US home-service firms", "source_url":
                                   "https://census.gov", "quantitative": True, "lane": "market?"}]
    m.store.plan_save(sid, tree=tree)

    seen = {}
    real_prepare = m.planner.prepare

    def spy(idea, mock=False, on_progress=None, prior=None):
        seen["prior"] = prior
        return real_prepare(idea, mock=mock, on_progress=on_progress, prior=prior)

    monkeypatch.setattr(m.planner, "prepare", spy)
    client.post(f"/api/plan/{sid}/commit", json={})
    wait_status(client, sid)
    assert seen["prior"] and seen["prior"]["claims"][0]["source_url"] == "https://census.gov"
    assert seen["prior"]["thesis"] == node["thesis"]      # reuse only when the thesis is unchanged


def test_commit_from_body_thesis_does_not_reuse(client, monkeypatch):
    # A steered commit (explicit body thesis ≠ the refined node's) must NOT reuse the skim — it forces a
    # full re-research on the new thesis. Guards the equality gate on the reuse predicate.
    import app.main as m
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [o["id"] for o in s["activeNode"]["options"]]})
    s = wait_status(client, sid)
    refined_id = s["activeNode"]["id"]
    full = m.store.plan_get(sid)
    tree = full["tree"]
    tree["nodes"][refined_id].setdefault("research", {})["claims"] = [
        {"text": "x", "source_url": "https://census.gov", "quantitative": True, "lane": "market?"}]
    m.store.plan_save(sid, tree=tree)

    seen = {}
    real_prepare = m.planner.prepare
    monkeypatch.setattr(m.planner, "prepare",
                        lambda idea, mock=False, on_progress=None, prior=None:
                        seen.update(prior=prior) or real_prepare(idea, mock=mock, on_progress=on_progress))
    client.post(f"/api/plan/{sid}/commit",
                json={"thesis": "A totally different steered thesis the skim never researched"})
    wait_status(client, sid)
    assert seen["prior"] is None                          # steered thesis → no reuse, full research


def test_commit_from_body_thesis(client):
    s = _brainstorm(client)
    sid = s["id"]
    r = client.post(f"/api/plan/{sid}/commit", json={"thesis": "Sell homemade care boxes to people far from home"})
    assert r.status_code == 200
    s = wait_status(client, sid)
    assert s["proposal"] and s["stage"] == "building"


# ── the /route spine ─────────────────────────────────────────────────────────
def test_route_classifies_commit_intent(client):
    s = _brainstorm(client)
    r = client.post(f"/api/plan/{s['id']}/route", json={"prompt": "I'm sold, build the plan", "mode": "build"})
    assert r.status_code == 200
    d = r.json()["decision"]
    assert d["intent"] == "commit" and d["confirm"] is True


def test_route_plan_steer_hard_clash_returns_a_fork(client):
    # build all the way to the plan stage, then send a contradicting steer
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/commit", json={"thesis": "A done-for-you SaaS onboarding service"})
    s = wait_status(client, sid)
    assert s["stage"] == "building"
    r = client.post(f"/api/plan/{sid}/route",
                    json={"prompt": "actually sell physical hardware instead", "mode": "build"})
    body = r.json()
    assert body["decision"]["intent"] == "steer"
    assert body.get("fork") and body["fork"]["clash"] and body["fork"]["skeptic_say"]


def test_route_plan_steer_integrable_no_fork(client):
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/commit", json={"thesis": "A done-for-you SaaS onboarding service"})
    wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/route", json={"prompt": "make the pricing simpler", "mode": "build"})
    assert "fork" not in r.json()                      # a tweak integrates, no pivot fork


def test_route_pick_selects_on_screen_directions(client):
    # at the brainstorm fork, "go with the first two" is a pick with 1-based indices — the router
    # must see the options in its context, never claim they don't exist (the 2026-07-04 bug)
    s = _brainstorm(client)
    r = client.post(f"/api/plan/{s['id']}/route",
                    json={"prompt": "let's go with the first two options", "mode": "build"})
    assert r.status_code == 200
    d = r.json()["decision"]
    assert d["intent"] == "pick" and d["picks"] == [1, 2]


def test_path_snippets_carry_fork_steering():
    # a brainstorm fork's snippet must include the instruction that created it — dropping it is
    # how "drop the DTC piece" evaporated from a later pivot's context (2026-07-04)
    from app import main as m
    fork = {"id": "b2", "kind": "brainstorm", "parent": None,
            "feedback": "keep the story angle, drop the branded cookie delivery"}
    snip = m._node_snippet(fork)
    assert "steered by" in snip and "drop the branded cookie delivery" in snip
    assert m._node_snippet({"id": "b1", "kind": "brainstorm"}) == "the fork where directions were spread"


def test_lookup_returns_graded_claims(client):
    # research-mode chat lookup: one lane + the gate, claims come back labeled (mock canned)
    s = _brainstorm(client)
    r = client.post(f"/api/plan/{s['id']}/lookup", json={"message": "office snack spend per month?"})
    assert r.status_code == 200
    cs = r.json()["claims"]
    assert cs and all({"text", "url", "tier", "flagged"} <= set(c) for c in cs)
    assert any(c["flagged"] for c in cs) and any(not c["flagged"] for c in cs)


def test_lookup_requires_a_question(client):
    s = _brainstorm(client)
    assert client.post(f"/api/plan/{s['id']}/lookup", json={"message": ""}).status_code == 400


def test_tripped_kill_switch_degrades_funnel_to_key_prompt(client, monkeypatch):
    # invariant #3: the funnel feeds the daily meter, so it must READ it too. A tripped kill switch
    # on FILG's key → 402 + needKey (degrade to the key prompt), never an uncapped run.
    from app import access, ops
    from engine import usage

    class _HostedProv:
        bills_filg = True
    monkeypatch.setattr(ops, "MOCK", False)
    monkeypatch.setattr(access, "_provider_for", lambda user: _HostedProv())
    monkeypatch.setattr(usage, "kill_switch_tripped", lambda: True)
    r = client.post("/api/brainstorm", json={"idea": "cookies with ex cons on tiktok"})
    assert r.status_code == 402 and r.json().get("needKey") is True


def test_info_requests_never_pivot(client):
    # 'tell me which node i'm on' routed as a steer and spread a garbage fork (2026-07-04, twice).
    # An imperative info request is an ask — at every stage, browsing or not.
    s = _brainstorm(client)
    sid = s["id"]
    opt = s["activeNode"]["options"][0]
    for prompt in ("tell me which node i'm currently looking at",
                   "sorry i just want you to tell me which node i've focused on (last click)"):
        r = client.post(f"/api/plan/{sid}/route",
                        json={"prompt": prompt, "mode": "build", "node": opt["id"]})
        assert r.json()["decision"]["intent"] == "ask", prompt


def test_scaffold_never_renders_as_a_direction():
    from app import brainstorm
    echo = [{"title": "THE OPERATOR IS PIVOTING. Their pivot instruction OUTWEIGHS",
             "one_liner": "THE OPERATOR IS PIVOTING. Their pivot instruction OUTWEIGHS everything"}]
    assert brainstorm._clean_directions(echo) == []
    real = [{"title": "Wholesale gift boxes", "one_liner": "Sell to cafes."}]
    assert len(brainstorm._clean_directions(real)) == 1


def test_keep_going_routes_to_next_not_commit(client):
    # 'keep going' means the ONE next step at every stage — never the whole-build commit (2026-07-04)
    s = _brainstorm(client)
    sid = s["id"]
    r = client.post(f"/api/plan/{sid}/route", json={"prompt": "keep going", "mode": "build"})
    assert r.json()["decision"]["intent"] == "next"
    client.post(f"/api/plan/{sid}/commit", json={"thesis": "Wholesale baked goods co-op"})
    wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/route", json={"prompt": "ok next step", "mode": "build"})
    assert r.json()["decision"]["intent"] == "next"
    # an explicit whole-build ask still commits
    r = client.post(f"/api/plan/{sid}/route", json={"prompt": "I'm sold, build the plan", "mode": "build"})
    assert r.json()["decision"]["intent"] == "commit"


def test_chat_answers_in_place_when_browsing_an_old_node(client):
    # the advisor gets WHERE the user is: reading a passed-over node → no next-step coaching
    # (mock replies echo the awareness + drop their 'Next step:' tail when a situation is set)
    s = _brainstorm(client)
    sid = s["id"]
    opts = s["activeNode"]["options"]
    client.post(f"/api/plan/{sid}/merge", json={"options": [opts[0]["id"]]})
    wait_status(client, sid)
    r = client.post(f"/api/plan/{sid}/chat",
                    json={"message": "why did we pass on this?", "node": opts[1]["id"]})
    assert r.status_code == 200
    reply = r.json()["reply"]
    assert "reading an earlier node" in reply and "Next step:" not in reply


def test_finished_plan_lands_on_stage_done(client):
    # the off-ramp: when the terminal node lands, stage must flip to done (not stay 'building'
    # forever, which rendered 'Part 8 of 7' + Keep going — 2026-07-04)
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/commit", json={"thesis": "Wholesale baked goods co-op"})
    s = wait_status(client, sid)
    for _ in range(20):
        if s["status"] == "done":
            break
        r = client.post(f"/api/plan/{sid}/next", json={"feedback": ""})
        assert r.status_code == 200, r.text
        s = r.json()
    assert s["status"] == "done" and s["stage"] == "done" and s["proposal"] is None
    # and the server refuses to roll past the end
    assert client.post(f"/api/plan/{sid}/next", json={"feedback": ""}).status_code == 409


def test_journey_digest_names_picked_options(client):
    # the advisor's grounding must carry the tree: options offered + which were picked — without it
    # 'which option did I pick?' gets 'I don't see which option you picked' (the 2026-07-04 bug)
    from app import main as m
    s = _brainstorm(client)
    sid = s["id"]
    opt = s["activeNode"]["options"][0]
    client.post(f"/api/plan/{sid}/merge", json={"options": [opt["id"]]})
    wait_status(client, sid)
    digest = m._journey_digest(m.store.plan_get(sid))
    assert "✓ PICKED" in digest and "passed over" in digest
    assert (opt["direction"]["title"] or "")[:30] in digest


def test_route_context_lists_brainstorm_options():
    from app import main as m
    s = {"tree": {"active": "b1", "nodes": {
        "b1": {"id": "b1", "kind": "brainstorm", "children": ["o1", "o2"]},
        "o1": {"id": "o1", "kind": "option", "parent": "b1",
               "direction": {"title": "Cookie brand with reentry jobs"}},
        "o2": {"id": "o2", "kind": "option", "parent": "b1",
               "direction": {"title": "Baking workshop series"}}}},
         "idea": "cookies with ex cons on tiktok"}
    ctx = m._route_context(s)
    assert "1) 'Cookie brand with reentry jobs'" in ctx and "2) 'Baking workshop series'" in ctx


# ── lazy node content for the graph zoom ─────────────────────────────────────
def test_node_content_returns_the_option_card(client):
    s = _brainstorm(client)
    opt = s["activeNode"]["options"][0]["id"]
    r = client.get(f"/api/plan/{s['id']}/node/{opt}")
    assert r.status_code == 200 and r.json()["kind"] == "option"
    assert r.json()["direction"]["title"]


def test_fork_node_lists_offered_options_and_picks(client):
    s = _brainstorm(client)
    sid = s["id"]
    opt_ids = [o["id"] for o in s["activeNode"]["options"]]
    client.post(f"/api/plan/{sid}/merge", json={"options": opt_ids[:1]})
    wait_status(client, sid)
    fork = next(n for n in s["tree"]["nodes"] if n["kind"] == "brainstorm")
    r = client.get(f"/api/plan/{sid}/node/{fork['id']}").json()
    assert len(r["options"]) == len(opt_ids)                       # every direction offered
    assert all(o["direction"]["title"] for o in r["options"])
    picked = {o["id"] for o in r["options"] if o["picked"]}
    assert picked == set(opt_ids[:1])                              # the merge's pick is marked


def test_node_content_unknown_node_404(client):
    s = _brainstorm(client)
    r = client.get(f"/api/plan/{s['id']}/node/nope")
    assert r.status_code == 404


# ── the base node + in-tree re-spread (pivots never orphan the old branch) ───
def test_brainstorm_roots_at_a_base_idea_node(client):
    s = _brainstorm(client)
    kinds = {n["kind"] for n in s["tree"]["nodes"]}
    assert "idea" in kinds
    base = next(n for n in s["tree"]["nodes"] if n["kind"] == "idea")
    fork = next(n for n in s["tree"]["nodes"] if n["kind"] == "brainstorm")
    assert base["parent"] is None and fork["parent"] == base["id"]


def test_rebrainstorm_branches_off_the_pivot_point_keeping_the_old_tree(client):
    s = _brainstorm(client)
    sid = s["id"]
    client.post(f"/api/plan/{sid}/commit", json={"thesis": "A done-for-you SaaS onboarding service"})
    s = wait_status(client, sid)
    old_ids = {n["id"] for n in s["tree"]["nodes"]}
    old_sections = [n for n in s["tree"]["nodes"] if n["kind"] == "section"]
    assert old_sections
    pivot_point = s["tree"]["active"]
    r = client.post(f"/api/plan/{sid}/rebrainstorm", json={"idea": "keep the onboarding angle but sell to agencies instead"})
    assert r.status_code == 200
    s2 = r.json()
    new_ids = {n["id"] for n in s2["tree"]["nodes"]}
    assert old_ids <= new_ids                                # nothing orphaned — the old branch survives
    assert s2["stage"] == "brainstorm"
    forks = [n for n in s2["tree"]["nodes"] if n["kind"] == "brainstorm"]
    assert len(forks) == 2                                   # the original + the pivot's new spread
    new_fork = next(n for n in forks if n["id"] not in old_ids)
    assert new_fork["parent"] == pivot_point                 # it grows out of where you pivoted
    assert s2["tree"]["active"] == new_fork["id"]


def test_rebrainstorm_from_a_fork_branches_off_the_fork_itself(client):
    # the pivot contract: EVERY node is branchable, and the branch is a CHILD of the pivot node
    s = _brainstorm(client)
    sid = s["id"]
    fork = next(n for n in s["tree"]["nodes"] if n["kind"] == "brainstorm")
    r = client.post(f"/api/plan/{sid}/rebrainstorm", json={"idea": "totally different direction please"})
    s2 = r.json()
    new_fork = next(n for n in s2["tree"]["nodes"] if n["kind"] == "brainstorm" and n["id"] != fork["id"])
    assert new_fork["parent"] == fork["id"]


def test_pivot_from_an_option_builds_off_that_option_with_path_context(client, monkeypatch):
    # picking a pivot on a selected option = the question re-answered with ONLY that option, plus the
    # feedback weighted above everything; the new tree grows off the option node itself
    from app import main
    s = _brainstorm(client)
    sid = s["id"]
    opt = s["activeNode"]["options"][0]["id"]
    captured = {}
    real = main.brainstorm.diverge
    def spy(idea, mock=False):
        captured["in"] = idea
        return real(idea, mock=mock)
    monkeypatch.setattr(main.brainstorm, "diverge", spy)
    r = client.post(f"/api/plan/{sid}/rebrainstorm",
                    json={"feedback": "make it sexy, maybe an onlyfans?", "node": opt})
    assert r.status_code == 200
    s2 = r.json()
    new_fork = next(n for n in s2["tree"]["nodes"] if n["kind"] == "brainstorm" and n["parent"] == opt)
    assert s2["tree"]["active"] == new_fork["id"]            # the new tree grows OFF the option
    assert "make it sexy" in captured["in"]                  # feedback present…
    assert captured["in"].index("make it sexy") < captured["in"].index("COMMITTED PATH")   # …and weighted on top
    assert "the original idea" in captured["in"]             # ancestors ride along
    assert "the direction" in captured["in"]                 # the option itself is the chosen endpoint


# ── run epochs: a pivot mid-run abandons the running query's RESULT ──────────
def test_bg_run_abandoned_when_user_pivots_midflight(client):
    from app import main
    s = _brainstorm(client)
    sid = s["id"]
    opts = [o["id"] for o in s["activeNode"]["options"]]
    sess = main.store.plan_get(sid)
    tree = sess["tree"]; tree["_run"] = "newer-epoch"          # the user pivoted while it ran
    main.store.plan_save(sid, tree=tree)
    main._run_merge(sid, opts, sess.get("user"), tok="stale-epoch")   # the old run finishes late
    s2 = main.store.plan_get(sid)
    kinds = [n.get("kind") for n in s2["tree"]["nodes"].values()]
    assert "refined" not in kinds                              # its result was discarded
    assert any("abandoned" in ln for ln in (s2.get("progress") or []))


def test_bg_run_attaches_to_the_fresh_tree_not_a_stale_copy(client):
    from app import main
    s = _brainstorm(client)
    sid = s["id"]
    opts = [o["id"] for o in s["activeNode"]["options"]]
    sess = main.store.plan_get(sid)
    tree = sess["tree"]; tree["_run"] = "tok1"
    # something else wrote a node mid-run (same epoch) — it must survive the run's save
    tree["nodes"]["extra1234"] = {"id": "extra1234", "parent": None, "children": [],
                                  "kind": "idea", "step": 0, "title": "survives",
                                  "files": {}, "history": [], "board": []}
    main.store.plan_save(sid, tree=tree)
    main._run_merge(sid, opts, sess.get("user"), tok="tok1")
    s2 = main.store.plan_get(sid)
    assert "extra1234" in s2["tree"]["nodes"]                  # no clobber: fresh-tree attach
    assert any(n.get("kind") == "refined" for n in s2["tree"]["nodes"].values())


# ── the root shell (the former v2 surface — v1 retired 2026-07-06) + assets serve ──
def test_root_shell_and_assets_serve(client):
    page = client.get("/")
    assert page.status_code == 200 and "window.FILG=" in page.text   # the config head
    assert '/static/app.js' in page.text and '/static/styles.css' in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    # deep-link paths serve the same shell (History-API routing survives refresh)
    assert "window.FILG=" in client.get("/plan/abc123").text
    assert "window.FILG=" in client.get("/account/plans").text
    # old /v2 links (shares, bookmarks, pre-move Stripe returns) redirect to the clean paths
    r = client.get("/v2", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/"
    r = client.get("/v2/plan/abc123", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/plan/abc123"
    r = client.get("/v2/account/settings", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/account/settings"
    # research / board / help / summary are DISPLAYS over the one chat: panes + drawers all present
    assert 'id=rpane' in page.text and 'id=rdrawer' in page.text and 'id=rexpand' in page.text
    assert 'id=bpane' in page.text and 'id=bdrawer' in page.text and 'id=bseats' in page.text
    assert 'id=hpane' in page.text and 'id=spane' in page.text and 'tooldrawer' not in page.text
    js = client.get("/static/app.js").text
    for needle in ("enterResearch", "exitResearch", "expandResearch", "collapseResearch",
                   "renderResearch", "offerExitMode", "RMODE='split'",
                   "enterBoard", "exitBoard", "expandBoard", "collapseBoard",
                   "renderBoard", "BMODE='split'", "forgeModal", "stressGo", "boardNotesHtml",
                   "enterHelp", "renderHelp", "faqPush", "HELP_BLURB",
                   "enterSummary", "exitSummary", "summaryHtml",
                   "openAccount", "acctExport", "acctPrompt"):
        assert needle in js, needle
    # the 2026-07-07 UX wave: gate-question answers + confirm gate, click-a-line comments, chat-send
    # debounce, drawer width-resize/collapse, and the landing help chat
    for needle in ("gateProceed", "foldGateAnswers", "toggleGateQ", "setGateAns", "atRefinedGate",
                   "modalConfirm", "openCmtPop", "setChatBusy", "CHAT_BUSY",
                   "initDrawerResize", "setDrawerWidth", "landingHelpSend", "landingHelpExit",
                   # alertable concerns: unified pre-advance gate for questions + board objections
                   "advanceGuard", "stepConcerns", "boardSeverity", "CONCERN_ACK", "boardTopSeverity"):
        assert needle in js, needle
    # the drawer resize grips render in the shell; the landing help chat has its reveal class
    assert page.text.count("class=hgrip") >= 3 and "data-drawer=left" in page.text
    css = client.get("/static/styles.css").text
    assert ".hgrip" in css and "helpchat" in css and "chatbusy" in css
