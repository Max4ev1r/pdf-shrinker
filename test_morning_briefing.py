from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import morning_briefing_lib as briefing


def _item(section: str, index: int, source: str, score: float) -> dict:
    category = {
        "domestic": "domestic",
        "world": "world",
        "tech": "ai",
    }[section]
    article_type = {
        "domestic": "policy",
        "world": "hard_news",
        "tech": "tech_core",
    }[section]
    return {
        "title": f"{section} 有效新闻 {index}",
        "original_title": f"{section} 有效新闻 {index}",
        "url": f"https://example.com/{section}/{index}",
        "source": source,
        "display_source": source,
        "category": category,
        "tier": "A",
        "article_type": article_type,
        "topic_key": f"{section}-{index}",
        "score": score,
        "brief_score": score,
        "score_reasons": [],
        "summary": (
            "这是一段足够长的事实摘要，用来证明候选新闻具有可以约束重点解读的材料，"
            "其中包含事件主体、已经发生的动作和可以核对的实际影响，而不是只有标题。"
        ),
    }


def test_sections_do_not_fill_to_quota(monkeypatch):
    news = {
        "domestic": [_item("domestic", 1, "domestic-source", 120)],
        "world": [],
        "tech": [],
    }
    monkeypatch.setattr(briefing, "rank_items", lambda items, _section: items)

    selected = briefing.select_sections({"news": news})

    counts = {section: len(items) for section, items in selected.items()}
    assert counts == {"domestic": 1, "world": 0, "tech": 0}
    assert sum(counts.values()) < 9


def test_decision_value_rewards_relevance_and_penalizes_generic_local_event():
    relevant, reasons = briefing.decision_value({
        "title": "江苏无锡发布小微企业税收支持政策与人工智能产业措施",
    })
    generic, generic_reasons = briefing.decision_value({
        "title": "包头市发布人工智能行动方案和首批机会场景清单",
    })

    assert relevant > generic
    assert "relevance:local" in reasons
    assert "relevance:policy" in reasons
    assert "relevance:unrelated_local" in generic_reasons


def test_weather_advice_only_appears_when_actionable():
    calm = {
        "ok": True,
        "condition": "晴",
        "current_c": 24,
        "min_c": 20,
        "max_c": 28,
        "rain_probability": 10,
        "commute_rain_probability": 5,
        "wind_kmh": 8,
        "commute_wind_kmh": 10,
    }
    rain = dict(calm, commute_rain_probability=70)

    assert briefing.weather_action(calm) is None
    assert "早晚温差" not in briefing.weather_line(calm)
    assert "带伞" in briefing.weather_action(rain)


def test_expired_source_quarantine_gets_half_open_probe():
    source = briefing.Source(
        "half-open",
        "world",
        "https://example.com/feed.xml",
        priority=50,
    )
    state = {
        source.name: {
            "failures": 20,
            "successes": 0,
            "quarantined_until": (
                briefing.now() - timedelta(minutes=1)
            ).isoformat(),
        },
    }

    assert briefing.score_source(source, state) > 0


def test_render_has_no_repeated_generic_footer_or_reminder():
    selected = {
        "domestic": [_item("domestic", 1, "国内源", 120)],
        "world": [_item("world", 1, "国际源", 119)],
        "tech": [_item("tech", 1, "科技源", 118)],
    }
    payload = {
        "weather": {
            "ok": True,
            "condition": "晴",
            "current_c": 24,
            "min_c": 20,
            "max_c": 28,
            "rain_probability": 10,
            "commute_rain_probability": 5,
            "wind_kmh": 8,
            "commute_wind_kmh": 10,
        },
    }

    rendered = briefing.render_message(payload, selected=selected)

    assert "信息噪音偏多" not in rendered
    assert "稳稳推进" not in rendered
    assert "📌 今日提醒" not in rendered


def test_focus_requires_substantive_evidence():
    weak = _item("domestic", 1, "国内源", 130)
    weak["summary"] = ""
    strong = _item("tech", 1, "科技源", 125)
    selected = {"domestic": [weak], "world": [], "tech": [strong]}

    assert briefing.choose_focus(selected) is strong


def test_short_summary_can_be_focus():
    short = _item("world", 1, "财联社", 130)
    short["title"] = "伊朗外长说无意延长停火协议"
    short["original_title"] = short["title"]
    short["summary"] = "伊朗外长说无意延长停火协议。"
    selected = {"domestic": [], "world": [short], "tech": []}

    assert briefing.choose_focus(selected) is short


def test_focus_not_repeated_in_sections():
    focus = _item("tech", 1, "科技源", 130)
    focus["title"] = "Anthropic 发布新模型"
    focus["original_title"] = focus["title"]
    other = _item("domestic", 1, "国内源", 120)
    selected = {"domestic": [other], "world": [], "tech": [focus]}
    payload = {
        "weather": {
            "ok": True,
            "condition": "晴",
            "current_c": 24,
            "min_c": 20,
            "max_c": 28,
            "rain_probability": 10,
            "commute_rain_probability": 5,
            "wind_kmh": 8,
            "commute_wind_kmh": 10,
        },
    }
    enhancement = {
        "focus": {
            "index": 1,
            "what": "Anthropic 发布新模型已经发布",
            "why": "值得关注",
        },
    }

    rendered = briefing.render_message(
        payload,
        selected=selected,
        enhancement=enhancement,
    )

    assert rendered.count(focus["title"]) == 1


def test_domestic_story_not_world():
    item = _item("world", 1, "新华网", 130)
    item["title"] = "中国央行将开展1万亿元买断式逆回购操作"
    item["original_title"] = item["title"]
    item["category"] = "world"
    item["article_type"] = "policy"

    assert briefing.item_is_eligible(item, "world") is False


def test_ai_story_not_world_when_obvious():
    item = _item("world", 1, "IT之家", 130)
    item["title"] = "前谷歌员工打造AI聊天机器人"
    item["original_title"] = item["title"]
    item["category"] = "world"
    item["article_type"] = "tech_core"

    assert briefing.item_is_eligible(item, "world") is False


def test_model_failure_returns_deterministic_fallback(monkeypatch):
    from agent import auxiliary_client

    strong = _item("tech", 1, "科技源", 125)
    selected = {"domestic": [], "world": [], "tech": [strong]}

    def fail_call(**_kwargs):
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(auxiliary_client, "call_llm", fail_call)
    enhancement = briefing.enhance_selected_with_llm(selected)

    assert enhancement["ok"] is False
    assert enhancement["used_model"] is False
    assert "TimeoutError" in enhancement["error"]


def test_delivery_reads_pre_rendered_file_and_is_idempotent(
    monkeypatch, tmp_path, capsys,
):
    fixed_now = datetime(2026, 8, 1, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(briefing, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(briefing, "now", lambda: fixed_now)
    day = tmp_path / "2026-08-01"
    day.mkdir()
    message = "☀️ 早，Max · 2026年08月01日\n\n🌤 无锡天气\n晴，正常出门。"
    (day / "final.md").write_text(message, encoding="utf-8")

    briefing.deliver()
    first = capsys.readouterr().out
    briefing.deliver()
    second = capsys.readouterr().out

    assert first.strip() == message
    assert second == ""
