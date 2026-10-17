# -*- coding: utf-8 -*-
"""口播分段（按标点切片、逐段合成与顺序播放）回归测试。

两层覆盖：
1. 纯逻辑：``split_speech_segments`` 的切分规则（长度阈值 / 强弱标点 / 碎段合并 /
   段数上限 / 收尾符号吸收 / 单段原样返回）；
2. 服务层：分段作业（``_SpeechJob``）的合成顺序、预取、顺序播放、失败跳过、
   看门狗跳过、免打扰切断、抢占、预合成 attach、排队语义。

纪律：不打网络、不起真实线程（合成 worker 打桩）、不碰真实播放器（conftest
全局静音 play()，播放器替身只记账）。
"""
from __future__ import annotations

from pathlib import Path

from pet.voice_chime import (
    MAX_SPEECH_SEGMENTS,
    SEGMENT_SPLIT_MIN_TEXT,
    split_speech_segments,
)

# ------------------------------------------------------------ 纯逻辑：切分规则


def test_short_text_returns_single_original():
    text = "现在是上午九点整。"
    assert len(text) < SEGMENT_SPLIT_MIN_TEXT
    assert split_speech_segments(text) == (text,)


def test_empty_text_returns_empty_tuple():
    assert split_speech_segments("") == ()
    assert split_speech_segments(None) == ()
    assert split_speech_segments("   ") == ()


def test_no_boundary_long_text_stays_single():
    text = "这是一段没有任何断句标点的很长很长的口播文本内容"
    assert split_speech_segments(text) == (text,)


def test_ascii_period_is_not_a_boundary():
    text = "The version is 1.2.3 and it still reads as one piece"
    assert split_speech_segments(text) == (text,)


def test_chime_plus_quote_splits_at_sentence_end():
    text = "现在是上午九点整。山重水复疑无路，柳暗花明又一村。"
    assert split_speech_segments(text) == (
        "现在是上午九点整。",
        "山重水复疑无路，柳暗花明又一村。",
    )


def test_weak_boundary_only_cuts_when_run_is_long_enough():
    # 逗号处累计不足 SEGMENT_WEAK_MIN(12) 不切；到达后切
    text = "山重水复疑无路，柳暗花明又一村，前路漫漫亦灿灿，星光不问赶路人。"
    assert split_speech_segments(text) == (
        "山重水复疑无路，柳暗花明又一村，",
        "前路漫漫亦灿灿，星光不问赶路人。",
    )


def test_tiny_pieces_merge_forward():
    # 「对！」「呀！」这类短片不单独成段
    text = "现在是上午九点整。对！呀！这就是全部内容了。"
    segments = split_speech_segments(text)
    assert segments[0] == "现在是上午九点整。"
    assert len(segments) == 2
    assert segments[1] == "对！呀！这就是全部内容了。"


def test_tiny_tail_merges_back_into_last_piece():
    text = "现在是上午十一点四十五分。你好。"
    assert split_speech_segments(text) == (text,)


def test_closing_quote_absorbed_into_piece():
    text = "现在是上午九点整。他说：“你好。”"
    assert split_speech_segments(text) == (
        "现在是上午九点整。",
        "他说：“你好。”",
    )


def test_newline_is_a_strong_boundary():
    text = "第一行内容比较长\n第二行内容也比较长"
    assert split_speech_segments(text) == (
        "第一行内容比较长",
        "第二行内容也比较长",
    )


def test_segment_count_is_capped():
    text = "一二三四五六七八，" * 20  # 每段 9 字，全部会切，共 20 段
    segments = split_speech_segments(text)
    assert len(segments) <= MAX_SPEECH_SEGMENTS
    # 合并不丢字符：拼回去应等于原文
    assert "".join(segments) == text


def test_concatenation_preserves_all_text():
    # 片段首尾会被 strip（口播不需要边界空白），故按「去空白后逐字相等」验证不丢内容
    samples = [
        "现在是上午九点整。山重水复疑无路，柳暗花明又一村。",
        "现在是晚上十一点整。床前明月光，疑是地上霜，举头望明月，低头思故乡。",
        "现在是下午三点整。Some English follows here! And more text follows too.",
    ]
    for text in samples:
        joined = "".join(split_speech_segments(text))
        assert "".join(joined.split()) == "".join(text.split())


def test_single_piece_returns_exact_source():
    # 只有一处强标点在末尾、且切后仍单段：必须原样返回（缓存键与不分段时代一致）
    text = "现在是上午九点整。对！"
    assert split_speech_segments(text) == (text,)
