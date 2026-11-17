from trail.scenes.cw import experience

AFFIX = {"affixes": [{"name": "成长的烦恼"}], "affixes_status": "partial"}


def obs(level=8, exp="4/72", coins=76, valid=True):
    return {"valid": valid, "level": level, "exp": exp, "coins": coins, "price_currency": "gold", "displayed_price": 4}


def arrange(monkeypatch, observations):
    values = iter(observations)
    calls = []
    monkeypatch.setattr(experience, "_observe", lambda runtime: (next(values), "frame"))
    monkeypatch.setattr(experience, "build_cw_shop_exp_buyer", lambda runtime: lambda: calls.append("click"))
    return calls


def test_quote_affix_and_insufficient_gold(monkeypatch):
    calls = arrange(monkeypatch, [obs()])
    result, frame = experience.quote_cw_experience(None, opening=AFFIX, target_level=9)
    assert (result["status"], result["purchases_required"], result["gold_required"], frame) == ("insufficient_gold", 17, 85, "frame")
    assert calls == []


def test_quote_level_nine(monkeypatch):
    arrange(monkeypatch, [obs(9, "20/84", 80)])
    result, _ = experience.quote_cw_experience(None, opening=AFFIX, target_level=10)
    assert (result["status"], result["purchases_required"], result["gold_required"]) == ("ready", 16, 80)


def test_unknown_and_budget_never_click(monkeypatch):
    calls = arrange(monkeypatch, [obs(coins=100), obs(coins=100), obs(coins=100, valid=False)])
    first, _ = experience.buy_cw_experience_to_level(None, opening={}, target_level=9, max_gold=100, check_cancelled=lambda: None)
    second, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=84, check_cancelled=lambda: None)
    third, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=100, check_cancelled=lambda: None)
    assert [first["status"], second["status"], third["status"]] == ["unknown", "budget_exceeded", "unknown"]
    assert calls == []


def test_mismatch_stops_and_reports_verified_partial(monkeypatch):
    calls = arrange(monkeypatch, [obs(exp="64/72", coins=20), obs(exp="68/72", coins=15), obs(9, "0/84", 9)])
    result, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=10, check_cancelled=lambda: None)
    assert result["status"] == "unconfirmed"
    assert (result["purchases_verified"], result["gold_spent"], len(calls)) == (1, 5, 2)


def test_each_purchase_verified_until_reached(monkeypatch):
    calls = arrange(monkeypatch, [obs(exp="64/72", coins=10), obs(exp="68/72", coins=5), obs(9, "0/84", 0)])
    result, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=10, check_cancelled=lambda: None)
    assert (result["status"], result["purchases_verified"], result["gold_spent"], len(calls)) == ("reached", 2, 10, 2)


def test_affix_absence_requires_complete_evidence():
    assert experience._price({}, 8) is None
    assert experience._price({"affixes_status": "observed", "affixes": []}, 8) is None
    assert experience._price({"affixes_status": "observed", "affixes": [], "affixes_empty_verified": True}, 8) == 4
    assert experience._price({"affixes_status": "observed", "affixes": [{"name": "永久创伤"}]}, 8) == 4
    assert experience._price({}, 7) == 4


def test_missing_experience_or_gold_never_clicks(monkeypatch):
    calls = arrange(monkeypatch, [obs(exp=None, coins=100), obs(exp="64/72", coins=None)])
    a, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=None, check_cancelled=lambda: None)
    b, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=None, check_cancelled=lambda: None)
    assert (a["reason"], b["reason"], calls) == ("experience_unavailable", "coins_unavailable", [])


def test_stale_frame_is_polled_without_repeat_click(monkeypatch):
    calls = arrange(monkeypatch, [obs(exp="68/72", coins=5), obs(exp="68/72", coins=5), obs(9, "0/84", 0)])
    monkeypatch.setattr(experience, "sleep", lambda _: None)
    result, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=None, check_cancelled=lambda: None)
    assert result["status"] == "reached" and calls == ["click"]


def test_max_level_marker_and_balance_verify_last_purchase(monkeypatch):
    end = {**obs(10, None, 19), "max_level": True}
    calls = arrange(monkeypatch, [obs(9, "80/84", 24), end])
    result, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=10, max_gold=None, check_cancelled=lambda: None)
    assert result["status"] == "reached" and result["gold_spent"] == 5 and calls == ["click"]
    calls = arrange(monkeypatch, [end])
    result, _ = experience.quote_cw_experience(None, opening=AFFIX, target_level=10)
    assert result["status"] == "reached" and calls == []


def test_gold_icon_template_rejects_blank_shifted_or_unrelated_icon():
    from pathlib import Path
    from PIL import Image
    from trail.scenes.cw import experience
    icon = Image.open(Path(experience.__file__).with_name("assets") / "experience_gold.png").convert("RGB")
    frame = Image.new("RGB", (1920, 1080), "black")
    frame.paste(icon, (268, 985))
    assert experience._has_gold_price_icon(frame)
    for replacement, position in [(Image.new("RGB", icon.size, "black"), (268, 985)),
                                  (Image.new("RGB", icon.size, "red"), (268, 985)),
                                  (icon, (277, 985))]:
        bad = Image.new("RGB", (1920, 1080), "black")
        bad.paste(replacement, position)
        assert not experience._has_gold_price_icon(bad)


def test_non_gold_currency_and_ambiguous_affix_do_not_buy(monkeypatch):
    a = {**obs(coins=100), "price_currency": None}
    b = obs(coins=100)
    calls = arrange(monkeypatch, [a, b])
    first, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=None, check_cancelled=lambda: None)
    second, _ = experience.buy_cw_experience_to_level(None, opening={"affixes_status": "observed", "affixes": [{"name": "成长的瀮恼"}]}, target_level=9, max_gold=None, check_cancelled=lambda: None)
    assert (first["status"], second["status"], calls) == ("unknown", "unknown", [])


def test_displayed_discount_changes_effective_price(monkeypatch):
    calls = arrange(monkeypatch, [{**obs(coins=100), "displayed_price": 3}])
    result, _ = experience.quote_cw_experience(None, opening=AFFIX, target_level=9)
    assert result["gold_required"] == 68 and result["cost_per_purchase"] == 4 and calls == []


def test_missing_price_after_verified_step_returns_partial_without_retry(monkeypatch):
    after = {**obs(exp="68/72", coins=5), "displayed_price": None}
    calls = arrange(monkeypatch, [obs(exp="64/72", coins=10), after])
    result, _ = experience.buy_cw_experience_to_level(None, opening=AFFIX, target_level=9, max_gold=None, check_cancelled=lambda: None)
    assert (result["status"], result["purchases_attempted"], result["purchases_verified"], result["gold_spent"], calls) == ("partial", 1, 1, 5, ["click"])


def test_gold_icon_accepts_observed_small_shift():
    from pathlib import Path
    from PIL import Image
    icon = Image.open(Path(experience.__file__).with_name("assets") / "experience_gold.png").convert("RGB")
    frame = Image.new("RGB", (1920, 1080), "black")
    frame.paste(icon, (271, 989))
    assert experience._has_gold_price_icon(frame)


def test_missing_fields_are_individually_reported():
    result = experience._plan({**obs(), "exp": None, "coins": None, "price_currency": None, "displayed_price": None}, AFFIX, 9)
    assert result["missing_fields"] == ["exp", "coins", "price_currency", "displayed_price"]


def test_early_level_real_quotes():
    a = experience._plan(obs(5, "6/20", 17), {}, 6)
    b = experience._plan(obs(6, "20/40", 20), {}, 7)
    assert (a["purchases_required"], a["gold_required"]) == (4, 16)
    assert (b["purchases_required"], b["gold_required"]) == (5, 20)
