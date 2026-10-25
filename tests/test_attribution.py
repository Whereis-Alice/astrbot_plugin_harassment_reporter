"""截图中的 A/B/C 群聊复现：B 当前发言不能背上 A 的历史。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import At, Image, Plain, Reply
from conftest import plugin_module
from test_courier import Event, Host

attr = plugin_module("core.attribution")
chatlog = plugin_module("core.chatlog")
onebot = plugin_module("core.onebot")
Settings = plugin_module("core.config").Settings
Store = plugin_module("core.store").Store
outbox = plugin_module("core.outbox")
HarassmentService = plugin_module("core.harassment").HarassmentService


def trigger(*, text="艾草", uid="2630660291", mid="100", timestamp=1792888559):
    event = Event(uid, gid="1061036726", name="羊羊羊", mid=mid, bot="3138617996")
    event.unified_msg_origin = "default:GroupMessage:1061036726"
    event.message_str = text
    event.message_obj.timestamp = timestamp
    event.message_obj.message = [Plain(text)]
    event.message_obj.raw_message = {
        "time": timestamp,
        "message_id": mid,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return event


def group_record():
    return chatlog.ChatLog(
        group_id="1061036726",
        group_name="星吧大水库",
        title="星吧大水库",
        source=chatlog.SOURCE_GROUP,
        lines=[
            onebot.ChatLine(
                sender_name="爱丽丝和艾拉的SuperStar",
                sender_id="10001",
                text="看看你",
                timestamp=1792886050,
                message_id="90",
            ),
            onebot.ChatLine(
                sender_name="艾拉不理人",
                sender_id="3138617996",
                text="蹲着呢 膝盖也很正常哦",
                timestamp=1792886051,
                is_self=True,
                message_id="91",
            ),
            onebot.ChatLine(
                sender_name="爱丽丝和艾拉的SuperStar",
                sender_id="10001",
                text="[回复] 单推，结婚",
                timestamp=1792886100,
                message_id="92",
            ),
            onebot.ChatLine(
                sender_name="艾拉不理人",
                sender_id="3138617996",
                text="Er、Er、Error",
                timestamp=1792886101,
                is_self=True,
                message_id="93",
            ),
            onebot.ChatLine(
                sender_name="咖啡瓦豆鲁迪",
                sender_id="20002",
                text="早啊",
                timestamp=1792887860,
                message_id="95",
            ),
            onebot.ChatLine(
                sender_name="羊羊羊",
                sender_id="2630660291",
                text="艾草",
                timestamp=1792888559,
                message_id="100",
            ),
        ],
    )


def service(
    *,
    rewritten="狐狸，羊羊羊这次发了艾草，我觉得不太舒服，想请你看看。",
    config=None,
    log=None,
):
    settings = Settings(
        {
            "config_layout_version": "2",
            "harassment": {
                "report_session_id": "default:FriendMessage:10001",
                "owner_report_style": "persona_natural",
                **(config or {}),
            },
        }
    )
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    recorder = SimpleNamespace(collect=AsyncMock(return_value=log or group_record()))
    persona = SimpleNamespace(rewrite_for_event=AsyncMock(return_value=rewritten))
    history = SimpleNamespace(
        summary=AsyncMock(
            return_value="用户: 看看你\n助手: 蹲着呢 膝盖也很正常哦\n用户: 单推，结婚"
        )
    )
    card = SimpleNamespace(
        enabled_for=lambda kind: True, render_chatlog=AsyncMock(return_value="card.png")
    )
    watchlist = SimpleNamespace(
        get=AsyncMock(return_value=None), add_report=AsyncMock()
    )
    store = Store(Host())
    result = HarassmentService(
        context=context,
        settings=settings,
        store=store,
        watchlist=watchlist,
        persona=persona,
        history=history,
        chatlog=recorder,
        card=card,
        outbox=outbox.Outbox(context, store, settings, card),
    )
    return result, context, persona, recorder, history, card


def send(subject, event=None, **kwargs):
    return asyncio.run(
        subject.send_report(
            event=event or trigger(),
            reason=kwargs.get("reason", "谐音开黄腔，言语骚扰与挑衅"),
            severity="medium",
            evidence=kwargs.get("evidence", "用户发送“艾草”，我觉得不舒服。"),
        )
    )


def test_rewrite_never_receives_foreign_user_or_bot_history():
    subject, context, persona, recorder, history, card = service()
    assert send(subject).ok
    material = persona.rewrite_for_event.call_args.kwargs["material"]
    assert "羊羊羊" in material and "2630660291" in material and "艾草" in material
    for foreign in ("看看你", "单推，结婚", "膝盖", "Error", "早啊"):
        assert foreign not in material
    history.summary.assert_not_awaited()
    recorder.collect.assert_awaited_once()
    assert card.render_chatlog.call_args.args[0] is recorder.collect.return_value
    body = context.send_message.call_args.args[1].chain[0].text
    # 收件人仍能看到完整的群聊，但其作者由代码原样拼接，模型不能篡改。
    assert "爱丽丝和艾拉的SuperStar（10001）：看看你" in body
    assert "羊羊羊（2630660291）：艾草" in body


def test_bad_natural_rewrite_is_replaced_even_when_it_parrots_the_user_quote():
    bad = "羊羊羊先说了“看看你”，我回复“蹲着呢 膝盖也很正常哦”，后来他发了“单推，结婚”，现在又发“艾草”。"
    subject, context, _, _, _, _ = service(rewritten=bad)
    assert send(subject).ok
    body = context.send_message.call_args.args[1].chain[0].text
    spoken = body.split("最近聊天原文")[0]
    assert "看看你" not in spoken and "单推" not in spoken and "膝盖" not in spoken
    assert "他这次发的是：艾草" in spoken


def test_misattributed_model_evidence_and_reason_do_not_reach_rewriter_or_card():
    subject, _, persona, _, _, card = service()
    assert send(
        subject,
        reason="他先说“看看你”又说“单推，结婚”",
        evidence="之前他让我蹲着呢膝盖也很正常哦，还说单推，结婚",
    ).ok
    material = persona.rewrite_for_event.call_args.kwargs["material"]
    note = card.render_chatlog.call_args.kwargs["note"]
    assert "看看你" not in material + note
    assert "单推" not in material + note
    assert "已省略" in material


def test_same_nickname_different_ids_never_combined():
    log = group_record()
    log.lines[0].sender_name = "羊羊羊"
    assert attr.subject_messages(trigger(), log) == ["艾草"]


def test_unknown_group_context_gets_no_current_sender_id_name_or_avatar():
    settings = Settings({})
    history = SimpleNamespace(
        chat_rows=AsyncMock(
            return_value=[
                {"role": "user", "name": "羊羊羊", "text": "看看你"},
                {"role": "assistant", "name": "爱丽丝", "text": "膝盖也很正常哦"},
                {"role": "other", "name": "系统", "text": "secret system prompt"},
            ]
        )
    )
    collector = chatlog.ChatLogCollector(
        settings=settings, bridge=SimpleNamespace(), history=history
    )
    lines = asyncio.run(collector._from_context(trigger(), 6))
    assert len(lines) == 2
    assert lines[0].sender_id == "" and lines[0].sender_name == "群聊发言者（未核实）"
    log = chatlog.ChatLog(
        lines=lines, group_id="1061036726", source=chatlog.SOURCE_CONTEXT
    )
    collector._append_current(trigger(), log)
    collector._attach_avatars(trigger(), log)
    collector._label(trigger(), log)
    assert lines[0].avatar == ""
    assert log.lines[-1].sender_id == "2630660291"
    assert "未核实" in log.subtitle
    assert attr.subject_messages(trigger(), log) == ["艾草"]


def test_private_context_still_has_one_known_partner():
    settings = Settings({})
    history = SimpleNamespace(
        chat_rows=AsyncMock(
            return_value=[{"role": "user", "name": "小明", "text": "hi"}]
        )
    )
    collector = chatlog.ChatLogCollector(
        settings=settings, bridge=None, history=history
    )
    event = Event("20002", name="小明")
    row = asyncio.run(collector._from_context(event, 3))[0]
    assert row.sender_id == "20002" and row.sender_name == "小明"


def test_no_model_summary_is_used_when_onebot_history_fails():
    log = chatlog.ChatLog(
        source=chatlog.SOURCE_CONTEXT,
        lines=[
            onebot.ChatLine(sender_name="群聊发言者（未核实）", text="单推，结婚"),
        ],
    )
    subject, context, persona, _, _, _ = service(log=log)
    assert send(subject).ok
    assert "单推，结婚" not in persona.rewrite_for_event.call_args.kwargs["material"]
    body = context.send_message.call_args.args[1].chain[0].text
    assert "群聊发言者（未核实）：单推，结婚" in body


def test_identical_text_from_different_people_is_not_deduplicated_or_gets_their_image():
    event = trigger(text="艾草")
    event.message_obj.message.append(Image.fromURL("https://example.test/new.png"))
    log = chatlog.ChatLog(
        source=chatlog.SOURCE_GROUP,
        lines=[
            onebot.ChatLine(
                sender_id="10001",
                sender_name="群友A",
                text="艾草",
                timestamp=1792888559,
                message_id="99",
            )
        ],
    )
    chatlog.ChatLogCollector._append_current(event, log)
    assert len(log.lines) == 2
    assert not log.lines[0].images
    assert log.lines[1].images == ["https://example.test/new.png"]


def test_same_sender_two_message_ids_with_same_text_are_not_merged():
    log = chatlog.ChatLog(
        lines=[
            onebot.ChatLine(
                sender_id="2630660291",
                text="艾草",
                timestamp=1792888559,
                message_id="99",
            )
        ]
    )
    chatlog.ChatLogCollector._append_current(trigger(), log)
    assert len(log.lines) == 2


@pytest.mark.parametrize("previous", ["另一句话", "艾草"])
def test_without_ids_same_second_only_identical_authored_text_is_deduplicated(previous):
    log = chatlog.ChatLog(
        lines=[
            onebot.ChatLine(sender_id="2630660291", text=previous, timestamp=1792888559)
        ]
    )
    chatlog.ChatLogCollector._append_current(trigger(mid=""), log)
    assert len(log.lines) == (1 if previous == "艾草" else 2)
    assert log.lines[0].text == previous


def test_missing_sender_identity_cannot_confirm_any_history():
    event = trigger(uid="unknown")
    log = chatlog.ChatLog(
        source=chatlog.SOURCE_GROUP,
        lines=[onebot.ChatLine(sender_id="unknown", text="别人的话")],
    )
    assert attr.subject_messages(event, log) == ["艾草"]
    assert attr.foreign_messages(event, log) == ["别人的话"]


def test_current_message_same_id_refreshes_image_without_duplicate():
    log = group_record()
    event = trigger()
    event.message_obj.message.append(Image.fromURL("https://example.test/new.png"))
    chatlog.ChatLogCollector._append_current(event, log)
    assert len(log.lines) == 6
    assert log.lines[-1].images == ["https://example.test/new.png"]


def test_same_message_id_replaces_expanded_reply_text_with_authored_text():
    log = group_record()
    log.lines[-1].text = "引用别人的原文 艾草"
    chatlog.ChatLogCollector._append_current(trigger(), log)
    assert log.lines[-1].text == "艾草"
    assert len(log.lines) == 6


def test_reply_original_author_not_added_to_subjects_own_text():
    event = trigger(text="这是他发的，我觉得不好")
    event.message_obj.raw_message = None
    event.message_obj.message = [
        Reply(
            id=1, sender_id="10001", sender_nickname="群友A", message_str="单推，结婚"
        ),
        At(qq="3138617996"),
        Plain("这是他发的，我觉得不好"),
    ]
    assert "单推" not in attr.authored_text(event)
    assert "群友A（10001）" in attr.quoted_message(event)
    subject, context, persona, _, _, _ = service()
    assert send(subject, event, evidence="").ok
    assert "单推，结婚" not in persona.rewrite_for_event.call_args.kwargs["material"]
    assert (
        "引用消息 · 群友A（10001）：单推，结婚"
        in context.send_message.call_args.args[1].chain[0].text
    )


def test_history_and_card_collection_failure_still_delivers_current_event():
    subject, context, persona, recorder, _, card = service()
    recorder.collect.side_effect = OSError("history unavailable")
    card.render_chatlog.side_effect = OSError("renderer unavailable")
    assert send(subject).ok
    assert "艾草" in persona.rewrite_for_event.call_args.kwargs["material"]
    assert context.send_message.await_count == 1


def test_persona_exception_still_delivers_current_authored_message_and_chatlog():
    subject, context, persona, _, _, _ = service()
    persona.rewrite_for_event.side_effect = RuntimeError("provider unavailable")
    assert send(subject).ok
    body = context.send_message.call_args.args[1].chain[0].text
    assert "他这次发的是：艾草" in body
    assert "爱丽丝和艾拉的SuperStar（10001）：看看你" in body
    assert context.send_message.await_count == 1


def test_cooldown_stops_before_attribution_collection_or_rewrite():
    subject, _, persona, recorder, history, _ = service()
    event = trigger()

    async def check():
        await subject.store.mark_cooldown("harassment", event.unified_msg_origin)
        assert not (
            await subject.send_report(event=event, reason="test", severity="medium")
        ).ok

    asyncio.run(check())
    persona.rewrite_for_event.assert_not_awaited()
    recorder.collect.assert_not_awaited()
    history.summary.assert_not_awaited()


def test_context_disabled_does_not_append_other_peoples_text():
    subject, context, _, _, _, _ = service(
        config={"attach_recent_context_summary": False}
    )
    assert send(subject).ok
    assert "看看你" not in context.send_message.call_args.args[1].chain[0].text


def test_current_message_disabled_hides_quote_and_current_text_from_owner_body():
    subject, context, _, _, _, _ = service(
        config={"include_message_text": False, "attach_recent_context_summary": False},
        rewritten="狐狸，羊羊羊让我不舒服，想请你看看。",
    )
    assert send(subject, evidence="").ok
    assert "当前本人消息" not in context.send_message.call_args.args[1].chain[0].text


def test_unverified_model_reason_is_marked_as_judgement():
    subject, context, _, _, _, _ = service(config={"owner_report_style": "structured"})
    assert send(subject).ok
    assert (
        "模型判断（不是已核实事实）"
        in context.send_message.call_args.args[1].chain[0].text
    )


def test_snapshot_summary_does_not_truncate_away_speaker_identity():
    log = group_record()
    summary = attr.attributed_summary(log, lines=6, max_chars=80)
    assert "羊羊羊（2630660291）：艾草" in summary
    assert len(summary) <= 80


def test_real_onebot_message_ids_preserved_and_platform_time_prioritized():
    lines = onebot.OneBotBridge._normalize_history(
        [
            {
                "message_id": 123,
                "time": 10,
                "sender": {"user_id": 2630660291},
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        ],
        count=1,
    )
    assert lines[0].message_id == "123"
    event = trigger(timestamp=10)
    event.message_obj.timestamp = 20
    assert plugin_module("core.eventinfo").message_timestamp(event) == 10


@pytest.mark.parametrize("stamp", [None, "not-time", "nan", "inf", -1])
def test_unknown_platform_time_is_not_fabricated_or_used_to_merge(stamp):
    lines = onebot.OneBotBridge._normalize_history(
        [{"sender": {"user_id": 2630660291}, "message": "艾草", "time": stamp}],
        count=1,
    )
    assert lines[0].timestamp == 0
    log = chatlog.ChatLog(lines=lines, source=chatlog.SOURCE_GROUP)
    chatlog.ChatLogCollector._append_current(trigger(mid=""), log)
    assert len(log.lines) == 2


@pytest.mark.parametrize(
    "natural", ["他先说看看你，然后又说单推，结婚", "他发了“未出现的原话”"]
)
def test_quote_guard_rejects_unverified_or_foreign_originals(natural):
    assert not attr.grounded(natural, own=["艾草"], foreign=["看看你", "单推，结婚"])


def test_hint_binds_real_current_user_and_excludes_quoted_original():
    hint = attr.current_identity_hint(trigger())
    assert "2630660291" in hint and "羊羊羊" in hint and "艾草" in hint
    assert "User/用户 不代表都是这个人" in hint
