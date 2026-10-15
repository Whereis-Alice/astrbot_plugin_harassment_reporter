import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import plugin_module
from test_courier import Event, make, relay

ROOT = Path(__file__).resolve().parents[1]


def test_every_production_python_file_compiles_without_writing_cache():
    for path in [ROOT / "main.py", *(ROOT / "core").glob("*.py")]:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_entrypoint_imports_against_installed_astrbot_sdk():
    assert plugin_module("main").PLUGIN_VERSION == "2.3.0"


def test_settings_schema_defaults_and_group_mapping_are_consistent():
    config = plugin_module("core.config")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    settings = config.Settings({"config_layout_version": "2"})
    for key, spec in schema["courier"]["items"].items():
        assert key in config.KEY_GROUPS
        assert config.KEY_GROUPS[key] == "courier"
        assert getattr(settings, key) == spec["default"]
    # 新增分组不重跑已有配置迁移，避免隐形旧键把用户已删除的名单灌回来。
    cfg = {
        "config_layout_version": "2",
        "watchlist": {"watchlist_entries": []},
        "watchlist_entries": ["old"],
    }
    assert config.Settings(cfg).get("watchlist_entries") == []
    assert config.CONFIG_LAYOUT_VERSION == "2"


def test_versions_and_public_docs():
    assert "version: 2.3.0" in (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    assert "version-2.3.0-" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## [2.3.0] - 2026-10-15" in (ROOT / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    assert "首个骚扰上报器插件版本" in (ROOT / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )


def test_real_tool_dispatch_and_boolean_values_are_not_truthy_strings():
    tools = plugin_module("core.tools")
    received = {}

    async def handler(**kw):
        received.update(kw)
        return "ok"

    tool = tools.PersonRelayTool(plugin=SimpleNamespace(handle_tool_courier=handler))
    wrapper = SimpleNamespace(context=SimpleNamespace(event=Event()))
    assert (
        asyncio.run(
            tool.call(
                wrapper,
                message="hi",
                target="小明",
                include_images="false",
                reply_to_recent="false",
            )
        )
        == "ok"
    )
    assert received["include_images"] is False and received["reply_to_recent"] is False


@pytest.mark.parametrize(
    "receipt",
    [
        {"message_id": True},
        {"message_id": []},
        {"message_id": {}},
        {"message_id": "garbage"},
    ],
)
def test_malformed_receipts_are_not_success(receipt):
    service, bridge, _ = make()
    bridge.receipt = receipt
    assert "不要宣称已送达" in relay(service)


def test_temp_membership_failure_never_sends():
    service, bridge, _ = make(courier_temp_group="12345")
    original = bridge.call

    async def call(client, action, **params):
        if action == "get_group_member_info":
            return {"user_id": 44444}
        return await original(client, action, **params)

    bridge.call = call
    assert "无法确认" in relay(service, target="50005")
    assert not bridge.sent


def test_invalid_state_cannot_erase_blocklist():
    service, bridge, host = make()
    host.data["courier_state_v1"] = {"blocked": "corrupt"}
    with pytest.raises(TypeError):
        relay(service)
    assert not bridge.sent


def test_timeout_is_bounded_and_no_retry(monkeypatch):
    service, bridge, _ = make()
    module = plugin_module("core.courier")
    original = module.asyncio.wait_for

    async def fast_timeout(coro, timeout):
        return await original(coro, timeout=0.01)

    monkeypatch.setattr(module.asyncio, "wait_for", fast_timeout)
    original_call = bridge.call

    async def call(client, action, **params):
        if action == "send_private_msg":
            bridge.calls.append((action, params))
            await asyncio.sleep(1)
        return await original_call(client, action, **params)

    bridge.call = call
    assert "不要宣称已送达" in relay(service)
    assert "无法确认" in relay(service)
    assert len(bridge.sent) == 1


def test_oversize_images_skip_and_still_send_text(tmp_path):
    service, bridge, _ = make(courier_image_max_mb=1)
    path = tmp_path / "large.png"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    event = Event()
    event.message_obj.message = [SimpleNamespace(type="Image", path=str(path))]
    assert "未附带图片" in relay(service, event, include_images=True)
    assert len(bridge.sent[0]["message"]) == 1


def test_postsend_persistence_failure_does_not_claim_failure_and_resend():
    service, bridge, host = make()
    original = bridge.call

    async def call(client, action, **params):
        result = await original(client, action, **params)
        if action == "send_private_msg":
            host.fail_write = True
        return result

    bridge.call = call
    assert "协议端接收" in relay(service)
    assert "没有重复发送" in relay(service)
    assert len(bridge.sent) == 1


@pytest.mark.parametrize(
    "key,raw",
    [
        ("courier_hourly_limit", -3),
        ("courier_reply_hours", "not a number"),
        ("courier_cache_seconds", None),
    ],
)
def test_settings_bad_values_have_safe_defaults(key, raw):
    settings = plugin_module("core.config").Settings({"courier": {key: raw}})
    value = getattr(settings, key)
    assert isinstance(value, int) and value >= 0


def test_legacy_feedback_image_flow_still_uses_old_tool_and_new_tool_is_separate():
    tools = plugin_module("core.tools")
    assert tools.FeedbackRelayTool().name == "relay_feedback_to_owner"
    assert tools.HarassmentReportTool().name == "report_harassment"
    assert tools.PersonRelayTool().name != tools.FeedbackRelayTool().name
    image = plugin_module("core.images")
    event = Event()
    event.message_obj.message = [
        SimpleNamespace(type="Image", url="https://example.test/a.png")
    ]
    assert image.from_event(event, limit=3)[0].url == "https://example.test/a.png"


def test_same_event_rephrasing_is_not_a_second_message():
    service, bridge, _ = make(courier_cooldown_seconds=0)
    relay(service, message="小明，狐狸找你呀")
    assert "没有重复发送" in relay(service, message="小明呀，狐狸来找你了")
    assert len(bridge.sent) == 1


def test_contact_book_pruning_does_not_reset_rate_budget():
    service, bridge, _ = make(courier_cooldown_seconds=0, courier_hourly_limit=1)
    relay(service)
    service._state["contacts"] = []
    assert "次数已用完" in relay(service, Event(mid="2"), message="又一句话")
    assert len(bridge.sent) == 1


def test_global_budget_is_per_bot_account():
    service, bridge, _ = make(courier_allow_anyone=True, courier_global_limit=1)
    relay(service)
    assert "总额度已用完" in relay(service, Event("30003", mid="2"))
    assert "协议端接收" in relay(service, Event("30003", mid="3", bot="88888"))
    assert len(bridge.sent) == 2


@pytest.mark.parametrize("value", ["false", "0", "off", 0, None, [], {}])
def test_permissions_cannot_be_accidentally_enabled_by_invalid_boolean(value):
    service, bridge, _ = make(courier_allow_anyone=value)
    assert "权限" in relay(service, Event(admin=False))
    assert not bridge.sent


def test_hint_injection_is_temporary_and_does_not_require_owner_binding():
    main = plugin_module("main")
    service, _, _ = make()
    plugin = object.__new__(main.HarassmentReporterPlugin)
    plugin.settings, plugin.courier = service.settings, service
    plugin._refresh_runtime = lambda: None
    req = SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_tool_usage_hint(Event(), req))
    assert len(req.extra_user_content_parts) == 1
    assert "relay_message_to_person" in req.extra_user_content_parts[0].text
    req = SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_tool_usage_hint(Event(admin=False), req))
    assert not req.extra_user_content_parts


def test_main_exception_guard_never_claims_delivery():
    main = plugin_module("main")
    plugin = object.__new__(main.HarassmentReporterPlugin)

    async def fail(**kwargs):
        raise OSError("storage unavailable")

    plugin.courier = SimpleNamespace(relay=fail)
    plugin._refresh_runtime = lambda: None
    result = asyncio.run(
        plugin.handle_tool_courier(event=Event(), target="小明", message="hi")
    )
    assert "不要宣称已送达" in result


def test_private_contact_relations_never_injected_into_group_chat():
    service, bridge, _ = make(courier_allow_groups=True)
    relay(service)
    event = Event("20002", admin=False, gid="12345")
    assert not asyncio.run(service.hint(event))
    assert "私聊" in relay(service, event, target="", reply_to_recent=True)
    assert len(bridge.sent) == 1


def test_cleanup_is_persisted_without_new_messages():
    service, _, host = make()
    relay(service)
    service._state["contacts"][0]["at"] -= 73 * 3600
    service._state["attempts"][0]["at"] -= 73 * 3600
    asyncio.run(service.hint(Event()))
    assert host.data["courier_state_v1"]["contacts"] == []
    assert host.data["courier_state_v1"]["attempts"] == []


def test_failed_preflight_persistence_can_recover_without_false_duplicate():
    service, bridge, host = make()
    host.fail_write = True
    assert "没有发送" in relay(service)
    host.fail_write = False
    assert "协议端接收" in relay(service)
    assert len(bridge.sent) == 1
