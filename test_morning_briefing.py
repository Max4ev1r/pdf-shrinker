import json
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


def _fresh_item(section: str, index: int, source: str) -> dict:
    item = _item(section, index, source, 120)
    item["published_at"] = briefing.now().isoformat()
    item["feed_source"] = source
    return item


def _rankable_item(section: str, index: int, source: str) -> dict:
    item = _fresh_item(section, index, source)
    titles = {
        "domestic": (
            "国务院发布经济政策支持企业发展"
            if index == 1
            else "人民银行公布最新货币政策安排"
        ),
        "world": (
            "美国宣布新的国际安全措施"
            if index == 1
            else "伊朗宣布新的停火协议安排"
        ),
        "tech": (
            f"OpenAI 发布第{index}个大模型，扩大人工智能应用"
            if index == 1
            else f"英伟达推出第{index}代 AI 芯片，提升数据中心算力"
        ),
    }
    item["title"] = titles[section]
    item["original_title"] = item["title"]
    return item


def _collect_news_with_sources(monkeypatch, tmp_path, source_items, search_results):
    sources = [source for source, _items in source_items]
    items_by_name = {source.name: items for source, items in source_items}
    monkeypatch.setattr(briefing, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(briefing, "STATE_FILE", tmp_path / "source_health.json")
    monkeypatch.setattr(briefing, "SOURCES", sources)

    def fake_fetch(source):
        items = items_by_name[source.name]
        return source, bool(items), items, None if items else "unavailable"

    monkeypatch.setattr(briefing, "fetch_source", fake_fetch)
    calls = []

    def fake_search(query, category, limit):
        calls.append((query, category, limit))
        return search_results.get(category, (False, [], "not requested"))

    monkeypatch.setattr(briefing, "fetch_canonical_web_search", fake_search)
    return briefing.collect_news(), calls


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


def test_focus_uses_highest_value_candidate_across_sections():
    domestic = _item("domestic", 1, "新华社", 100)
    world = _item("world", 1, "Reuters", 140)
    tech = _item("tech", 1, "IT之家", 110)

    assert briefing.choose_focus({
        "domestic": [domestic],
        "world": [world],
        "tech": [tech],
    }) is world


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


def test_healthy_primary_sections_do_not_gap_fill(monkeypatch, tmp_path):
    source_items = []
    for section, source_names in {
        "domestic": ("新华社", "人民网"),
        "world": ("Reuters", "AP"),
        "tech": ("openai.com", "机器之心"),
    }.items():
        for index, source_name in enumerate(source_names, 1):
            category = "ai" if section == "tech" else section
            source_items.append((
                briefing.Source(f"{section}-{index}", category, f"https://{section}-{index}"),
                [_rankable_item(section, index, source_name)],
            ))

    (ranked, _state, gap_fill), calls = _collect_news_with_sources(
        monkeypatch, tmp_path, source_items, {}
    )

    assert calls == []
    assert gap_fill["calls"] == []
    assert {section: briefing.selected_count_from_ranked(ranked, section) for section in (
        "domestic", "world", "tech"
    )} == {"domestic": 2, "world": 2, "tech": 2}


def test_domestic_gap_fill_searches_domestic_only(monkeypatch, tmp_path):
    source_items = [
        (briefing.Source("domestic-1", "domestic", "https://domestic-1"), [_rankable_item("domestic", 1, "新华社")]),
        (briefing.Source("world-1", "world", "https://world-1"), [_rankable_item("world", 1, "Reuters")]),
        (briefing.Source("world-2", "world", "https://world-2"), [_rankable_item("world", 2, "AP")]),
        (briefing.Source("tech-1", "ai", "https://tech-1"), [_rankable_item("tech", 1, "openai.com")]),
        (briefing.Source("tech-2", "ai", "https://tech-2"), [_rankable_item("tech", 2, "机器之心")]),
    ]
    search_item = _rankable_item("domestic", 2, "人民网")

    (ranked, _state, gap_fill), calls = _collect_news_with_sources(
        monkeypatch,
        tmp_path,
        source_items,
        {"domestic": (True, [search_item], None)},
    )

    assert calls == [(briefing.GAP_FILL_QUERIES["domestic"], "domestic", 8)]
    assert set(call[1] for call in calls) == {"domestic"}
    assert gap_fill["sections"]["domestic"]["accepted_count"] == 2
    assert briefing.selected_count_from_ranked(ranked, "domestic") == 2


def test_international_gap_fill_searches_international_only(monkeypatch, tmp_path):
    source_items = [
        (briefing.Source("domestic-1", "domestic", "https://domestic-1"), [_rankable_item("domestic", 1, "新华社")]),
        (briefing.Source("domestic-2", "domestic", "https://domestic-2"), [_rankable_item("domestic", 2, "人民网")]),
        (briefing.Source("world-1", "world", "https://world-1"), [_rankable_item("world", 1, "Reuters")]),
        (briefing.Source("tech-1", "ai", "https://tech-1"), [_rankable_item("tech", 1, "openai.com")]),
        (briefing.Source("tech-2", "ai", "https://tech-2"), [_rankable_item("tech", 2, "机器之心")]),
    ]
    search_item = _rankable_item("world", 2, "AP")

    (_ranked, _state, gap_fill), calls = _collect_news_with_sources(
        monkeypatch,
        tmp_path,
        source_items,
        {"world": (True, [search_item], None)},
    )

    assert calls == [(briefing.GAP_FILL_QUERIES["world"], "world", 8)]
    assert gap_fill["sections"]["world"]["accepted_count"] == 2


def test_low_quality_gap_fill_stays_insufficient(monkeypatch, tmp_path):
    source_items = [
        (briefing.Source("world-1", "world", "https://world-1"), [_rankable_item("world", 1, "Reuters")]),
        (briefing.Source("world-2", "world", "https://world-2"), [_rankable_item("world", 2, "AP")]),
        (briefing.Source("tech-1", "ai", "https://tech-1"), [_rankable_item("tech", 1, "openai.com")]),
        (briefing.Source("tech-2", "ai", "https://tech-2"), [_rankable_item("tech", 2, "机器之心")]),
    ]
    low_quality = {
        "title": "今日新闻汇总页面",
        "url": "https://random.example/news",
        "summary": "没有明确来源和时间的聚合内容。",
        "source": "随机聚合站",
        "feed_source": "canonical web_search",
        "category": "domestic",
        "published_at": briefing.now().isoformat(),
    }

    (ranked, _state, gap_fill), calls = _collect_news_with_sources(
        monkeypatch,
        tmp_path,
        source_items,
        {"domestic": (True, [low_quality], None)},
    )

    assert calls == [(briefing.GAP_FILL_QUERIES["domestic"], "domestic", 8)]
    assert ranked["domestic"] == []
    assert gap_fill["sections"]["domestic"]["accepted_count"] == 0
    assert gap_fill["sections"]["domestic"]["remaining_triggers"]


def test_weather_primary_failure_uses_one_authoritative_fallback(monkeypatch):
    calls = []

    def fail_primary(*_args, **_kwargs):
        raise OSError("primary unavailable")

    def fallback(query, category, limit):
        calls.append((query, category, limit))
        return True, [{
            "title": "无锡天气预报 今日",
            "url": "https://www.weather.com.cn/weather1d/101190101.shtml",
            "summary": "无锡今天晴，实时天气参考。",
            "source": "weather.com.cn",
            "display_source": "weather.com.cn",
            "category": category,
        }], None

    monkeypatch.setattr(briefing, "fetch_url", fail_primary)
    monkeypatch.setattr(briefing, "fetch_canonical_web_search", fallback)

    weather = briefing.collect_weather()

    assert weather["ok"] is True
    assert weather["fallback"] is True
    assert calls == [(briefing.GAP_FILL_QUERIES["weather"], "weather", 5)]
    assert "无锡天气预报 今日" in briefing.weather_line(weather)


def test_weather_primary_success_skips_fallback(monkeypatch):
    primary = {
        "current": {
            "temperature_2m": 24,
            "apparent_temperature": 24,
            "weather_code": 0,
            "relative_humidity_2m": 50,
            "wind_speed_10m": 8,
        },
        "hourly": {
            "time": ["2026-09-03T08:00"],
            "temperature_2m": [24],
            "precipitation_probability": [5],
            "wind_speed_10m": [8],
        },
        "daily": {
            "weather_code": [0],
            "temperature_2m_max": [28],
            "temperature_2m_min": [20],
            "precipitation_probability_max": [10],
        },
    }
    monkeypatch.setattr(
        briefing,
        "fetch_url",
        lambda *_args, **_kwargs: json.dumps(primary).encode(),
    )
    monkeypatch.setattr(
        briefing,
        "fetch_canonical_web_search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fallback called")),
    )

    weather = briefing.collect_weather()

    assert weather["ok"] is True
    assert weather["source"] == "Open-Meteo"
    assert "fallback" not in weather


def test_weather_low_quality_fallback_keeps_failure(monkeypatch):
    calls = []

    monkeypatch.setattr(briefing, "fetch_url", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("primary unavailable")))

    def low_quality(query, category, limit):
        calls.append((query, category, limit))
        return True, [{
            "title": "无锡天气信息汇总",
            "url": "https://random.example/weather",
            "summary": "来源不明的天气内容。",
            "source": "随机聚合站",
            "category": category,
        }], None

    monkeypatch.setattr(briefing, "fetch_canonical_web_search", low_quality)
    weather = briefing.collect_weather()

    assert weather["ok"] is False
    assert weather["fallback_search"]["accepted"] is False
    assert calls == [(briefing.GAP_FILL_QUERIES["weather"], "weather", 5)]


def test_weather_failure_is_rendered_once_and_not_as_reminder():
    selected = {"domestic": [_item("domestic", 1, "国内源", 120)], "world": [], "tech": []}
    rendered = briefing.render_message(
        {"weather": {"ok": False}},
        selected=selected,
    )

    failure = "天气源暂不可用，出门前再确认一下实时天气。"
    assert rendered.count(failure) == 1
    assert "📌 今日提醒" not in rendered


def test_stale_artifact_is_rejected_before_delivery(monkeypatch, tmp_path):
    fixed_now = datetime(2026, 9, 3, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(briefing, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(briefing, "now", lambda: fixed_now)
    day = tmp_path / "2026-09-03"
    day.mkdir()
    (day / "final.md").write_text("旧日期简报", encoding="utf-8")
    briefing.write_json(day / "input.json", {
        "date": "2026-09-02",
        "generated_at": "2026-09-02T07:15:00+08:00",
        "weather": {},
        "news": {},
        "source_health": {},
    })
    briefing.write_json(day / "quality.json", {"source_health_summary": {}})

    def fresh_collect():
        briefing.write_json(day / "input.json", {
            "date": "2026-09-03",
            "generated_at": "2026-09-03T07:20:00+08:00",
            "weather": {},
            "news": {},
            "source_health": {},
        })
        briefing.write_json(day / "quality.json", {"source_health_summary": {}})

    def fresh_render():
        (day / "final.md").write_text("新日期简报", encoding="utf-8")

    monkeypatch.setattr(briefing, "collect", fresh_collect)
    monkeypatch.setattr(briefing, "render", fresh_render)

    path = briefing.ensure_ready()

    assert path == day / "final.md"
    assert path.read_text(encoding="utf-8") == "新日期简报"


def test_rumor_focus_preserves_uncertainty_label():
    rumor = _item("tech", 1, "IT之家", 110)
    rumor["title"] = "爆料：苹果 A20 Pro 或采用 7 核 GPU"
    rumor["original_title"] = rumor["title"]
    rumor["score_reasons"] = ["unconfirmed"]
    selected = {"domestic": [], "world": [], "tech": [rumor]}

    assert briefing.choose_focus(selected) is rumor
    rendered = briefing.render_message(
        {"weather": {"ok": True, "condition": "晴"}},
        selected=selected,
        enhancement={
            "focus": {"index": 0, "what": "苹果 A20 Pro 芯片结构出现新的公开推测", "why": "仍需等待官方确认"},
        },
    )

    assert "信息性质：爆料/推测，尚未官方确认" in rendered


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
    briefing.write_json(day / "input.json", {
        "date": "2026-08-01",
        "generated_at": "2026-08-01T07:15:00+08:00",
        "weather": {"ok": True},
        "news": {},
        "source_health": {"source": {"last_checked_at": "2026-08-01T07:15:00+08:00"}},
    })
    briefing.write_json(day / "quality.json", {"source_health_summary": {}})

    briefing.deliver()
    first = capsys.readouterr().out
    briefing.deliver()
    second = capsys.readouterr().out

    assert first.strip() == message
    assert second == ""
