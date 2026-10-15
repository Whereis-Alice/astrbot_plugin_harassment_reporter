import asyncio
import copy
from types import SimpleNamespace

import pytest
from conftest import plugin_module

CourierService = plugin_module("core.courier").CourierService
Settings = plugin_module("core.config").Settings
Store = plugin_module("core.store").Store


class Event:
    def __init__(
        self,
        uid="10001",
        *,
        admin=True,
        gid="",
        platform="qq-main",
        bot="99999",
        name="狐狸",
        mid="1",
    ):
        self.uid, self.admin, self.gid = uid, admin, gid
        self.platform, self.bot_id, self.name = platform, bot, name
        self.bot = object()
        self.message_obj = SimpleNamespace(message=[], message_id=mid)
        self.unified_msg_origin = f"{platform}:FriendMessage:{uid}"

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.uid

    def get_sender_name(self):
        return self.name

    def get_self_id(self):
        return self.bot_id

    def get_platform_name(self):
        return "aiocqhttp"

    def get_platform_id(self):
        return self.platform

    def get_group_id(self):
        return self.gid


class Host:
    def __init__(self):
        self.data = {}
        self.fail_read = self.fail_write = False

    async def get_kv_data(self, key, default=None):
        if self.fail_read:
            raise OSError("storage unavailable")
        return copy.deepcopy(self.data.get(key, default))

    async def put_kv_data(self, key, value):
        if self.fail_write:
            raise OSError("storage full")
        self.data[key] = copy.deepcopy(value)


class Bridge:
    def __init__(self):
        self.calls = []
        self.rows = [
            {"user_id": 20002, "nickname": "小明", "remark": "卡比"},
            {"user_id": 10001, "nickname": "狐狸", "remark": ""},
            {"user_id": 30003, "nickname": "小红", "remark": ""},
        ]
        self.receipt = {"message_id": 42}
        self.friend_response = None

    async def call(self, client, action, **params):
        self.calls.append((action, params))
        await asyncio.sleep(0)
        if action == "get_friend_list":
            return self.rows if self.friend_response is None else self.friend_response
        if action == "get_group_member_info":
            return {"user_id": params["user_id"]}
        return self.receipt

    @property
    def sent(self):
        return [p for a, p in self.calls if a == "send_private_msg"]


def make(**config):
    settings = Settings({"config_layout_version": "2", "courier": config})
    host, bridge = Host(), Bridge()
    service = CourierService(settings=settings, store=Store(host), bridge=bridge)
    return service, bridge, host


def run(coro):
    return asyncio.run(coro)


def relay(service, event=None, **kwargs):
    return run(
        service.relay(
            event=event or Event(),
            target=kwargs.pop("target", "小明"),
            message=kwargs.pop("message", "小明，狐狸托我问你晚点一起玩吗？"),
            **kwargs,
        )
    )


@pytest.mark.parametrize("target", ["小明", "卡比", "20002", "  小明  "])
def test_exact_friend(target):
    service, bridge, host = make()
    assert "协议端接收" in relay(service, target=target)
    assert bridge.sent[0]["user_id"] == 20002
    assert "群聊记录" not in bridge.sent[0]["message"][0]["data"]["text"]
    assert "10001" in bridge.sent[0]["message"][0]["data"]["text"]
    assert "晚点一起玩吗" not in str(host.data)  # 不保存正文


@pytest.mark.parametrize(
    "key,value", [("courier_enabled", False), ("courier_allow_groups", False)]
)
def test_disabled_or_group(key, value):
    service, bridge, _ = make(**{key: value})
    result = relay(service, Event(gid="12345") if key.endswith("groups") else Event())
    assert "没有发送" in result and not bridge.sent


def test_no_arbitrary_user_permission():
    service, bridge, _ = make()
    assert "权限" in relay(service, Event(admin=False))
    assert not bridge.calls


@pytest.mark.parametrize(
    "config", [{"courier_allow_anyone": True}, {"courier_senders": ["10001"]}]
)
def test_explicit_permission(config):
    service, _, _ = make(**config)
    assert "协议端接收" in relay(service, Event(admin=False))


def test_other_platform():
    service, bridge, _ = make()
    event = Event()
    event.get_platform_name = lambda: "telegram"
    assert "OneBot" in relay(service, event)
    assert not bridge.calls


@pytest.mark.parametrize(
    "target", ["小", "不存在", "-12345", "123", "", "20002:GroupMessage:1"]
)
def test_no_fuzzy_or_invalid_target(target):
    service, bridge, _ = make()
    assert "没有发送" in relay(service, target=target)
    assert not bridge.sent


def test_ambiguous_friend_no_directory_leak():
    service, bridge, _ = make()
    bridge.rows.append({"user_id": 40004, "nickname": "小明", "remark": ""})
    result = relay(service)
    assert "多个好友" in result
    assert "20002" not in result and "40004" not in result
    assert not bridge.sent


def test_alias_priority_and_collision():
    service, bridge, _ = make(courier_aliases=["小明=30003"])
    assert "协议端接收" in relay(service)
    assert bridge.sent[-1]["user_id"] == 30003
    service, bridge, _ = make(courier_aliases=["小明=30003", "小明=20002"])
    assert "多个 QQ" in relay(service)
    assert not bridge.sent


def test_explicit_qq_cannot_be_redirected_by_alias():
    service, bridge, _ = make(courier_aliases=["20002=30003"])
    assert "协议端接收" in relay(service, target="20002")
    assert bridge.sent[-1]["user_id"] == 20002


@pytest.mark.parametrize("target", ["10001", "99999"])
def test_self_or_sender_target(target):
    service, bridge, _ = make()
    assert "没有发送" in relay(service, target=target)
    assert not bridge.sent


def test_friend_wrapped_and_failure():
    service, bridge, _ = make()
    bridge.friend_response = {"status": "ok", "retcode": 0, "data": bridge.rows}
    assert "协议端接收" in relay(service)
    service, bridge, _ = make()
    bridge.friend_response = {"status": "failed", "retcode": 1, "data": bridge.rows}
    assert "取不到好友列表" in relay(service)
    assert not bridge.sent


def test_cache_is_account_scoped_and_ttl_zero():
    async def scenario():
        service, bridge, _ = make()
        await service.friends(Event())
        await service.friends(Event())
        await service.friends(Event(platform="qq-other"))
        await service.friends(Event(bot="88888"))
        assert len(bridge.calls) == 3
        service.settings.raw["courier"]["courier_cache_seconds"] = 0
        await service.friends(Event())
        await service.friends(Event())
        assert len(bridge.calls) == 5

    run(scenario())


def test_failed_refresh_discards_stale_cache():
    service, bridge, _ = make()
    run(service.friends(Event()))
    bridge.friend_response = {"status": "failed", "retcode": 1}
    assert "失败" in run(service.refresh(Event()))
    assert not service._friends


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {},
        {"message_id": 0},
        {"status": "failed", "retcode": 1},
        {"status": "ok", "data": {}},
        False,
    ],
)
def test_unknown_send_result_is_not_success_or_retried(receipt):
    service, bridge, host = make()
    bridge.receipt = receipt
    assert "不要宣称已送达" in relay(service)
    assert "不重复发送" in relay(service)
    assert len(bridge.sent) == 1
    # 即使重启也保留不明状态，不能变成「刚才成功了」。
    restarted = CourierService(
        settings=service.settings, store=Store(host), bridge=bridge
    )
    assert "无法确认" in relay(restarted)
    assert len(bridge.sent) == 1


def test_wrapped_receipt():
    service, bridge, _ = make()
    bridge.receipt = {"status": "ok", "retcode": 0, "data": {"message_id": "111"}}
    assert "协议端接收" in relay(service)


def test_optout_persists_and_cannot_be_bypassed_by_alias():
    service, bridge, host = make(courier_aliases=["卡比老师=20002"])
    assert "已拒收" in run(service.optout(Event("20002", admin=False), enabled=False))
    service = CourierService(
        settings=service.settings, store=Store(host), bridge=bridge
    )
    assert "不接收" in relay(service, target="卡比老师")
    assert not bridge.sent
    assert "已恢复" in run(service.optout(Event("20002", admin=False), enabled=True))
    assert "协议端接收" in relay(service)


def test_config_blocklist():
    service, bridge, _ = make(courier_blocked=["20002"])
    assert "不接收" in relay(service)
    assert not bridge.sent


def test_unprivileged_recipient_can_reply_only_to_recent_sender():
    service, bridge, _ = make()
    assert "协议端接收" in relay(service)
    recipient = Event("20002", admin=False, name="小明", mid="2")
    assert "带过话的人" in run(service.hint(recipient))
    assert "权限" in relay(service, recipient, target="小红")
    assert "协议端接收" in relay(
        service,
        recipient,
        target="",
        reply_to_recent=True,
        message="狐狸，小明说晚点见！",
    )
    assert bridge.sent[-1]["user_id"] == 10001


@pytest.mark.parametrize(
    "change", [{"platform": "other"}, {"bot": "88888"}, {"uid": "30003"}]
)
def test_reply_cannot_cross_account_or_recipient(change):
    service, bridge, _ = make()
    relay(service)
    recipient = Event(**({"uid": "20002", "admin": False} | change))
    assert "没有唯一" in relay(service, recipient, target="", reply_to_recent=True)
    assert len(bridge.sent) == 1


def test_reply_disambiguation():
    service, bridge, _ = make(courier_allow_anyone=True)
    relay(service)
    relay(service, Event("30003", name="小红", mid="2"))
    recipient = Event("20002", admin=False)
    assert "没有唯一" in relay(service, recipient, target="", reply_to_recent=True)
    assert len(bridge.sent) == 2
    assert "协议端接收" in relay(
        service, recipient, target="10001", reply_to_recent=True
    )


def test_reply_expiry_and_hint_not_available_to_strangers():
    service, _bridge, _ = make()
    relay(service)
    service._state["contacts"][0]["at"] -= 73 * 3600
    assert not run(service.hint(Event("20002", admin=False)))
    assert "没有唯一" in relay(
        service, Event("20002", admin=False), target="", reply_to_recent=True
    )


def test_limits_and_concurrency():
    async def scenario():
        service, bridge, _ = make(courier_cooldown_seconds=0, courier_hourly_limit=1)
        results = await asyncio.gather(
            *[
                service.relay(
                    event=Event(mid=str(i)), target="小明", message=f"消息{i}"
                )
                for i in range(5)
            ]
        )
        assert sum("协议端接收" in x for x in results) == 1
        assert len(bridge.sent) == 1

    run(scenario())


def test_duplicate_not_resent_after_restart():
    service, bridge, host = make()
    relay(service)
    restarted = CourierService(
        settings=service.settings, store=Store(host), bridge=bridge
    )
    assert "没有重复发送" in relay(restarted)
    assert len(bridge.sent) == 1


def test_recipient_budget_shared_between_senders():
    service, bridge, _ = make(courier_allow_anyone=True, courier_recipient_limit=1)
    relay(service)
    assert "对方这一小时" in relay(service, Event("30003", mid="2"))
    assert len(bridge.sent) == 1


def test_storage_read_failure_fails_closed():
    service, bridge, host = make()
    host.fail_read = True
    with pytest.raises(OSError):
        relay(service)
    assert not bridge.sent


def test_storage_write_failure_prevents_send():
    service, bridge, host = make()
    host.fail_write = True
    assert "没有发送" in relay(service)
    assert not bridge.sent


def test_temp_session_optin_and_membership_checked():
    service, bridge, _ = make()
    assert "不是 Bot 好友" in relay(service, target="50005")
    assert not bridge.sent
    service, bridge, _ = make(courier_temp_group="12345")
    assert "协议端接收" in relay(service, target="50005")
    assert bridge.sent[0]["group_id"] == 12345
    assert sum(a == "get_group_member_info" for a, _ in bridge.calls) == 2


def test_cq_code_is_plain_text_not_parsed():
    service, bridge, _ = make()
    relay(service, message="[CQ:at,qq=all] [CQ:image,file=file:///secret]")
    assert len(bridge.sent[0]["message"]) == 1
    assert bridge.sent[0]["message"][0]["type"] == "text"


def test_images_require_explicit_request_and_only_current_or_reply(tmp_path):
    service, bridge, _ = make()
    event = Event()
    event.message_obj.message = [
        SimpleNamespace(type="Image", url="https://example.test/a.png")
    ]
    relay(service, event)
    assert len(bridge.sent[-1]["message"]) == 1
    service, bridge, _ = make()
    path = tmp_path / "test.png"
    path.write_bytes(b"png-data")
    event.message_obj.message += [
        SimpleNamespace(
            type="Reply",
            chain=[
                SimpleNamespace(type="Image", url="https://example.test/a.png"),
                SimpleNamespace(type="Image", url="", file="", path=str(path)),
            ],
        )
    ]
    assert "附带 2 张图" in relay(service, event, include_images=True)
    shots = [s["data"]["file"] for s in bridge.sent[-1]["message"][1:]]
    assert shots[0] == "https://example.test/a.png"
    assert shots[1].startswith("base64://")  # 支持分离部署


def test_images_off_does_not_claim_sent():
    service, bridge, _ = make(courier_forward_images=False)
    event = Event()
    event.message_obj.message = [
        SimpleNamespace(type="Image", url="https://example.test/a.png")
    ]
    assert "未附带图片" in relay(service, event, include_images=True)
    assert len(bridge.sent[0]["message"]) == 1


def test_tool_class_loads_in_real_sdk():
    tool = plugin_module("core.tools").PersonRelayTool()
    assert tool.name == "relay_message_to_person"
    assert tool.parameters["required"] == ["message"]
    assert run(
        tool.call(SimpleNamespace(context=SimpleNamespace(event=Event())), message="hi")
    ).endswith("没有发送。")
