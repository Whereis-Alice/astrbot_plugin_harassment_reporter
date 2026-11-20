"""QQ 官方 WS/Webhook：原生 OpenID、实际回执、分图发送与可见记录。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import At, Plain
from conftest import plugin_module
from test_courier import Event, Host
from test_feedback_reply import SAMPLE

official = plugin_module("core.qqofficial")
Courier = plugin_module("core.courier").CourierService
Settings = plugin_module("core.config").Settings
Store = plugin_module("core.store").Store
Outbox = plugin_module("core.outbox").Outbox
Feedback = plugin_module("core.feedback").FeedbackService
ChatLogCollector = plugin_module("core.chatlog").ChatLogCollector
Images = plugin_module("core.images")
PID = "official-main"
FOX = "F" * 32
PEER = "A" * 32
GROUP = "G" * 32


class OfficialEvent(Event):
    def __init__(
        self,
        uid=FOX,
        *,
        family="qq_official",
        group="",
        admin=True,
        mid="incoming-1",
        text="帮我传个话",
    ):
        super().__init__(
            uid, admin=admin, gid=group, platform=PID, bot="qq_official", mid=mid
        )
        self.family = family
        self.message_str = text
        self.unified_msg_origin = (
            f"{PID}:{'GroupMessage' if group else 'FriendMessage'}:{group or uid}"
        )
        self.message_obj.timestamp = time.time()
        self.message_obj.message = [At(qq="qq_official"), Plain(text)]
        self.message_obj.raw_message = SimpleNamespace(
            id=mid,
            group_openid=group,
            author=SimpleNamespace(member_openid=uid)
            if group
            else SimpleNamespace(user_openid=uid),
        )

    def get_platform_name(self):
        return self.family


def run(coro):
    return asyncio.run(coro)


def setup(*, family="qq_official", **config):
    settings = Settings(
        {
            "config_layout_version": "2",
            "identity": {"report_receiver_name": "狐狸", "bot_self_name": "爱丽丝"},
            "courier": {
                "courier_aliases": [f"小明={PID}:FriendMessage:{PEER}"],
                **config,
            },
            "feedback": {
                "feedback_session_id": f"{PID}:FriendMessage:{FOX}",
                "feedback_attach_chatlog": False,
            },
        }
    )
    api = SimpleNamespace(
        post_c2c_message=AsyncMock(return_value={"id": "native-c2c-id"}),
        post_group_message=AsyncMock(return_value={"id": "native-group-id"}),
        post_message=AsyncMock(return_value={"id": "native-channel-id"}),
        post_dms=AsyncMock(return_value={"id": "native-dm-id"}),
        _http=SimpleNamespace(
            request=AsyncMock(return_value={"file_info": "official-media-info"})
        ),
    )
    inst = SimpleNamespace(
        meta=lambda: SimpleNamespace(name=family, id=PID),
        appid="11111",
        get_client=lambda: SimpleNamespace(api=api),
    )
    context = SimpleNamespace(
        get_platform_inst=lambda pid: inst if pid == PID else None,
        send_message=AsyncMock(return_value=True),
    )
    bridge = official.QQOfficialBridge(context, settings)
    store = Store(Host())
    onebot = SimpleNamespace(
        call=AsyncMock(side_effect=AssertionError("OneBot must not be called"))
    )
    courier = Courier(settings=settings, store=store, bridge=onebot, official=bridge)
    return bridge, courier, api, context, settings, store, inst


@pytest.mark.parametrize("family", ["qq_official", "qq_official_webhook"])
def test_native_private_alias_send_uses_real_receipt_and_preserves_persona_text(family):
    bridge, courier, api, context, _, _, _ = setup(family=family)
    event = OfficialEvent(family=family)
    result = run(courier.relay(event=event, target="小明", message=SAMPLE))
    assert "官方接口确认发送" in result
    kwargs = api.post_c2c_message.call_args.kwargs
    assert kwargs["openid"] == PEER and kwargs["content"] == SAMPLE
    assert "msg_id" not in kwargs
    context.send_message.assert_not_awaited()
    assert "官方" in run(courier.hint(event))


@pytest.mark.parametrize(
    "target",
    ["2127074778", PEER, f"wrong:FriendMessage:{PEER}", f"{PID}:GroupMessage:{GROUP}"],
)
def test_qq_number_group_member_id_or_foreign_platform_cannot_be_used_as_c2c_target(
    target,
):
    _, courier, api, _, _, _, _ = setup()
    assert "没有发送" in run(
        courier.relay(event=OfficialEvent(), target=target, message=SAMPLE)
    )
    api.post_c2c_message.assert_not_awaited()


def test_explicit_verified_private_session_id_is_accepted():
    _, courier, api, _, _, _, _ = setup()
    assert "确认发送" in run(
        courier.relay(
            event=OfficialEvent(), target=f"{PID}:FriendMessage:{PEER}", message=SAMPLE
        )
    )
    assert api.post_c2c_message.call_args.kwargs["openid"] == PEER


def test_group_member_openid_is_not_treated_as_private_user_openid():
    _, courier, api, _, _, _, _ = setup(courier_allow_groups=True)
    assert "member_openid" in run(
        courier.relay(event=OfficialEvent(group=GROUP), target="小明", message=SAMPLE)
    )
    api.post_c2c_message.assert_not_awaited()


def test_real_c2c_recipient_can_reply_but_not_contact_arbitrary_people():
    _, courier, api, _, _, _, _ = setup()
    run(courier.relay(event=OfficialEvent(), target="小明", message=SAMPLE))
    recipient = OfficialEvent(PEER, admin=False, mid="incoming-2")
    assert "带过话的人" in run(courier.hint(recipient))
    assert "权限" in run(
        courier.relay(
            event=recipient,
            target=f"{PID}:FriendMessage:{'B' * 32}",
            message="去找别人",
        )
    )
    assert "确认发送" in run(
        courier.relay(
            event=recipient,
            target="",
            reply_to_recent=True,
            message="狐狸，小明说知道了。",
        )
    )
    assert api.post_c2c_message.call_args.kwargs["openid"] == FOX


def test_official_optout_and_same_event_dedup_are_enforced():
    _, courier, api, _, _, _, _ = setup()
    recipient = OfficialEvent(PEER, admin=False)
    assert "拒收" in run(courier.optout(recipient, enabled=False))
    assert "不接收" in run(
        courier.relay(event=OfficialEvent(), target="小明", message=SAMPLE)
    )
    api.post_c2c_message.assert_not_awaited()
    run(courier.optout(recipient, enabled=True))
    assert "确认发送" in run(
        courier.relay(event=OfficialEvent(), target="小明", message=SAMPLE)
    )
    assert "没有重复发送" in run(
        courier.relay(event=OfficialEvent(), target="小明", message="换种说法")
    )
    assert api.post_c2c_message.await_count == 1


@pytest.mark.parametrize(
    "reply", [None, False, {"id": False}, {"id": []}, {"id": ""}, {"id": 0}]
)
def test_missing_receipt_never_claims_delivery_or_calls_framework_bool_send(reply):
    bridge, _, api, context, _, _, _ = setup()
    api.post_c2c_message.return_value = reply
    result = run(bridge.send(f"{PID}:FriendMessage:{FOX}", SAMPLE))
    assert not result.ok and "回执" in result.detail
    context.send_message.assert_not_awaited()


def test_group_reply_uses_group_openid_and_same_group_incoming_message():
    bridge, _, api, _, _, _, _ = setup()
    bridge.observe(OfficialEvent(PEER, group=GROUP, mid="real-group-message"))
    assert run(bridge.send(f"{PID}:GroupMessage:{GROUP}", SAMPLE)).ok
    assert api.post_group_message.call_args.kwargs["group_openid"] == GROUP
    assert api.post_group_message.call_args.kwargs["msg_id"] == "real-group-message"
    api.post_c2c_message.assert_not_awaited()


def test_old_group_receipt_is_not_reused_for_active_push():
    bridge, _, api, _, _, _, _ = setup()
    event = OfficialEvent(PEER, group=GROUP)
    bridge.observe(event)
    bridge._routes[event.unified_msg_origin]["at"] -= 301
    assert run(bridge.send(event.unified_msg_origin, SAMPLE)).ok
    assert "msg_id" not in api.post_group_message.call_args.kwargs


def test_unknown_group_is_not_guessed_as_a_private_session():
    bridge, _, api, context, _, _, _ = setup()
    assert not run(bridge.send(f"{PID}:GroupMessage:{GROUP}", SAMPLE)).ok
    api.post_group_message.assert_not_awaited()
    api.post_c2c_message.assert_not_awaited()
    context.send_message.assert_not_awaited()


def test_qq_channel_and_channel_dm_keep_original_scene():
    bridge, _, api, _, _, _, _ = setup()
    channel = OfficialEvent(group="123456789012345678")
    channel.message_obj.raw_message = SimpleNamespace(
        channel_id=channel.gid, author=SimpleNamespace(id=PEER)
    )
    bridge.observe(channel)
    assert run(bridge.send(channel.unified_msg_origin, SAMPLE)).ok
    assert api.post_message.call_args.kwargs["channel_id"] == channel.gid
    dm = OfficialEvent()
    dm.message_obj.raw_message = SimpleNamespace(
        guild_id="dm-session-id", author=SimpleNamespace(id=FOX)
    )
    bridge.observe(dm)
    assert run(bridge.send(dm.unified_msg_origin, SAMPLE)).ok
    assert api.post_dms.call_args.kwargs["guild_id"] == "dm-session-id"
    api.post_c2c_message.assert_not_awaited()


def test_card_and_each_original_picture_are_sent_separately(tmp_path):
    bridge, _, api, _, settings, store, _ = setup()
    card = tmp_path / "card.png"
    card.write_bytes(b"card")
    pictures = [
        Images.ImageRef(Images.KIND_URL, "https://example.test/one.png"),
        Images.ImageRef(Images.KIND_BASE64, "cG5n"),
    ]
    box = Outbox(bridge.context, store, settings, None, official=bridge)
    result = run(
        box.deliver(
            channel="feedback",
            target_session_id=f"{PID}:FriendMessage:{FOX}",
            text=SAMPLE,
            image_path=str(card),
            images=pictures,
        )
    )
    assert result.ok and result.card_sent and result.images_sent == 2
    assert api.post_c2c_message.await_count == 4
    assert api.post_c2c_message.call_args_list[0].kwargs["content"] == SAMPLE
    assert all(
        item.kwargs["msg_type"] == 7 for item in api.post_c2c_message.call_args_list[1:]
    )
    assert api._http.request.call_args_list[0].kwargs["json"]["file_data"] == "Y2FyZA=="
    assert api._http.request.call_args_list[1].kwargs["json"]["url"].endswith("one.png")


def test_card_upload_failure_preserves_text_without_claiming_card_attached(tmp_path):
    bridge, _, api, _, settings, store, _ = setup()
    api._http.request.side_effect = OSError("image upload unavailable")
    card = tmp_path / "card.png"
    card.write_bytes(b"card")
    box = Outbox(bridge.context, store, settings, None, official=bridge)
    result = run(
        box.deliver(
            channel="feedback",
            target_session_id=f"{PID}:FriendMessage:{FOX}",
            text=SAMPLE,
            image_path=str(card),
        )
    )
    assert result.ok and not result.card_sent and result.images_sent == 0
    assert api.post_c2c_message.await_count == 1


def test_visible_group_history_keeps_actual_authors_and_not_synthetic_mentions():
    bridge, _, _, _, settings, _, _ = setup()
    first = OfficialEvent("A" * 32, group=GROUP, mid="a", text="看看你")
    second = OfficialEvent("B" * 32, group=GROUP, mid="b", text="艾草")
    bridge.observe(first)
    bridge.observe(second)
    collector = ChatLogCollector(
        settings=settings, bridge=None, history=None, official=bridge
    )
    log = run(collector.collect(second, count=14))
    assert log.real and log.source == "official_received"
    assert [line.sender_id for line in log.lines] == ["A" * 32, "B" * 32]
    assert [line.text for line in log.lines] == ["看看你", "艾草"]
    assert all(not line.avatar for line in log.lines)
    assert "实际收到" in log.subtitle
    assert plugin_module("core.attribution").subject_messages(second, log) == ["艾草"]


def test_official_feedback_to_owner_then_persona_reply_goes_back_to_original_group():
    bridge, _, api, context, settings, store, _ = setup()
    source = OfficialEvent(PEER, group=GROUP, text="爱丽丝，帮我找狐狸")
    bridge.observe(source)
    box = Outbox(context, store, settings, None, official=bridge)
    feedback = Feedback(
        context=context,
        settings=settings,
        store=store,
        persona=None,
        chatlog=SimpleNamespace(group_name=AsyncMock(return_value="")),
        card=None,
        outbox=box,
    )
    outcome = run(feedback.relay(event=source, message="狐狸，茶馆有人找你呀。"))
    assert outcome.ok
    identifier = Store.contact_id(run(store.get_contacts())[0])
    assert "已经把回话" in run(
        feedback.handle_reply_tool(
            event=OfficialEvent(), contact_id=identifier, message=SAMPLE
        )
    )
    assert api.post_group_message.call_args.kwargs["group_openid"] == GROUP
    assert api.post_group_message.call_args.kwargs["content"] == SAMPLE
    context.send_message.assert_not_awaited()


def test_actual_sdk_event_classes_and_public_api_signatures_supported():
    import inspect

    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
    from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_event import (
        QQOfficialWebhookMessageEvent,
    )
    from botpy.api import BotAPI

    assert issubclass(QQOfficialWebhookMessageEvent, QQOfficialMessageEvent)
    assert "openid" in inspect.signature(BotAPI.post_c2c_message).parameters
    assert "group_openid" in inspect.signature(BotAPI.post_group_message).parameters
    assert "file_image" in inspect.signature(BotAPI.post_dms).parameters
