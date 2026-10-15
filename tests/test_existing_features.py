"""新通道不能破坏原有的主人反馈、群记录和卡片行为。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import plugin_module
from jinja2 import Environment
from test_courier import Event, Host

Settings = plugin_module("core.config").Settings
Store = plugin_module("core.store").Store
card = plugin_module("core.card")
chatlog = plugin_module("core.chatlog")
onebot = plugin_module("core.onebot")
outbox = plugin_module("core.outbox")
feedback = plugin_module("core.feedback")


def test_group_history_has_independent_senders_and_images():
    lines = onebot.OneBotBridge._normalize_history(
        [
            {
                "sender": {"user_id": 10001, "card": "群友A"},
                "time": 10,
                "message": [{"type": "text", "data": {"text": "插件坏了"}}],
            },
            {
                "sender": {"user_id": 20002, "nickname": "群友B"},
                "time": 11,
                "message": [
                    {"type": "image", "data": {"url": "https://example.test/bug.png"}}
                ],
            },
            {
                "sender": {"user_id": 99999, "nickname": "爱丽丝"},
                "time": 12,
                "message": [{"type": "text", "data": {"text": "我去找狐狸"}}],
            },
        ],
        count=10,
        self_id="99999",
    )
    assert [r.sender_name for r in lines] == ["群友A", "群友B", "爱丽丝"]
    assert [r.is_self for r in lines] == [False, False, True]
    assert lines[1].images == ["https://example.test/bug.png"]


@pytest.mark.parametrize(
    "sharpness,scale", [("standard", 2), ("high", 3), ("ultra", 4)]
)
def test_card_resolution_and_lossless(sharpness, scale):
    settings = Settings({"card": {"card_sharpness": sharpness}})
    renderer = card.CardRenderer(None, settings)
    assert settings.card_scale == scale
    assert card.PAGE_WIDTH * scale == {2: 1440, 3: 2160, 4: 2880}[scale]
    assert renderer._screenshot_options(lossless=True) == {
        "full_page": True,
        "type": "png",
    }
    assert renderer._screenshot_options(lossless=False)["quality"] == 95


def test_chat_card_template_shots_layout_and_html_escape():
    renderer = card.CardRenderer(None, Settings({}))
    rows = [
        onebot.ChatLine(
            sender_name="<script>bad</script>",
            text="[图片]",
            images=["https://example.test/1.png", "https://example.test/2.png"],
        ).as_dict()
    ]
    messages = renderer.build_messages(rows, limit=10)
    assert messages[0]["text"] == ""
    template = Environment(autoescape=True).from_string(renderer._load_template())
    html = template.render(
        theme="aurora",
        messages=messages,
        page_width=720,
        scale=2,
        title="群聊",
        show_avatar=True,
    )
    assert 'class="shots many"' in html
    assert 'class="bubble shots-only"' in html
    assert html.count('class="shot"') == 2
    assert "hrShotFailed(this)" in html
    assert "<script>bad</script>" not in html


def test_card_images_switch_keeps_text_placeholder():
    renderer = card.CardRenderer(None, Settings({"card": {"card_show_images": False}}))
    message = renderer.build_messages(
        [
            onebot.ChatLine(
                text="[图片]", images=["https://example.test/1.png"]
            ).as_dict()
        ],
        limit=10,
    )[0]
    assert message["images"] == [] and message["text"] == "[图片]"


def test_chatlog_recent_images_newest_first_without_duplicates():
    log = chatlog.ChatLog(
        lines=[
            onebot.ChatLine(images=["https://example.test/a.png"]),
            onebot.ChatLine(
                images=["https://example.test/b.png", "https://example.test/a.png"]
            ),
        ]
    )
    assert log.recent_images(limit=2) == [
        "https://example.test/b.png",
        "https://example.test/a.png",
    ]


def test_owner_outbox_preserves_text_card_image_order_and_true_image_count():
    image = plugin_module("core.images")
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    box = outbox.Outbox(context, Store(Host()), Settings({}), None)
    result = asyncio.run(
        box.deliver(
            channel="feedback",
            target_session_id="qq:FriendMessage:10001",
            text="狐狸，有人找你",
            image_path="card.png",
            images=[image.ImageRef(image.KIND_URL, "https://example.test/1.png")],
        )
    )
    assert result.ok and result.images_sent == 1
    chain = context.send_message.call_args.args[1]
    assert [r.type for r in chain.chain] == ["Plain", "Image", "Image"]
    assert chain.chain[1].path.endswith("card.png")


@pytest.mark.parametrize("returned", [False, None])
def test_owner_outbox_never_claims_delivery_on_failure(returned):
    context = SimpleNamespace(send_message=AsyncMock(return_value=returned))
    box = outbox.Outbox(context, Store(Host()), Settings({}), None)
    result = asyncio.run(
        box.deliver(
            channel="feedback", target_session_id="qq:FriendMessage:10001", text="找你"
        )
    )
    assert not result.ok


def feedback_service(settings, context, log, rendered):
    return feedback.FeedbackService(
        context=context,
        settings=settings,
        store=Store(Host()),
        persona=None,
        chatlog=SimpleNamespace(
            collect=AsyncMock(return_value=log),
            group_name=AsyncMock(return_value="水库群"),
        ),
        card=SimpleNamespace(render_chatlog=AsyncMock(return_value=rendered)),
        outbox=outbox.Outbox(context, Store(Host()), settings, None),
    )


def test_owner_feedback_keeps_group_chatlog_and_current_image():
    settings = Settings({"harassment": {"report_session_id": "qq:FriendMessage:10001"}})
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    log = chatlog.ChatLog(
        lines=[onebot.ChatLine(sender_name="群友B", text="我这里也报错了")],
        group_name="水库群",
    )
    service = feedback_service(settings, context, log, "card.png")
    event = Event("20002", name="群友A", gid="12345")
    event.message_obj.message = [
        SimpleNamespace(type="Image", url="https://example.test/bug.png")
    ]
    result = asyncio.run(
        service.relay(event=event, message="狐狸，水库群的群友A说画图坏了")
    )
    assert result.ok and result.chatlog_attached and result.images_forwarded == 1
    service.chatlog.collect.assert_awaited_once()
    assert context.send_message.call_args.args[0] == "qq:FriendMessage:10001"


def test_card_failure_falls_back_to_text_not_losing_feedback():
    settings = Settings({"harassment": {"report_session_id": "qq:FriendMessage:10001"}})
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    log = chatlog.ChatLog(
        lines=[onebot.ChatLine(sender_name="群友B", text="我这里也报错了")],
        group_name="水库群",
    )
    service = feedback_service(settings, context, log, None)
    result = asyncio.run(service.relay(event=Event(), message="狐狸，有人找你"))
    assert result.ok and result.chatlog_inlined and not result.chatlog_attached
    assert (
        "群友B：我这里也报错了" in context.send_message.call_args.args[1].chain[0].text
    )


def test_feedback_cooldown_avoids_collecting_history_or_rendering():
    settings = Settings({"harassment": {"report_session_id": "qq:FriendMessage:10001"}})
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    service = feedback_service(settings, context, None, "card.png")

    async def scenario():
        await service.outbox.store.mark_cooldown("feedback", Event().unified_msg_origin)
        result = await service.relay(event=Event(), message="找你")
        assert not result.ok and result.status == outbox.STATUS_COOLDOWN
        service.chatlog.collect.assert_not_awaited()
        service.card.render_chatlog.assert_not_awaited()

    asyncio.run(scenario())


def test_real_astrbot_enum_image_and_reply_components_are_recognized():
    from astrbot.api.message_components import Image, Reply

    images = plugin_module("core.images")
    event = Event()
    event.message_obj.message = [
        Image.fromURL("https://example.test/current.png"),
        Reply(id=1, chain=[Image.fromURL("https://example.test/quoted.png")]),
    ]
    assert (
        images.from_event(event, limit=3)[0].url == "https://example.test/current.png"
    )
    assert images.from_reply(event, limit=3)[0].url == "https://example.test/quoted.png"


def test_courier_sends_actual_sdk_images_not_only_test_doubles():
    from astrbot.api.message_components import Image, Reply
    from test_courier import make, relay

    service, bridge, _ = make()
    event = Event()
    event.message_obj.message = [
        Image.fromURL("https://example.test/current.png"),
        Reply(id=1, chain=[Image.fromBase64("cG5n")]),
    ]
    assert "附带 2 张图" in relay(service, event, include_images=True)
    assert [s["data"]["file"] for s in bridge.sent[0]["message"][1:]] == [
        "https://example.test/current.png",
        "base64://cG5n",
    ]
