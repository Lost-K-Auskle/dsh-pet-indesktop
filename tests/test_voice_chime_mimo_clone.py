# -*- coding: utf-8 -*-
"""MiMo 克隆音色（``mimo-v2.5-tts-voiceclone``，参考音频）回归测试。

三层覆盖：
1. 纯逻辑：参考音频清洗 / 可用性自检（preflight 回落内置音色）、data URI 构造、
   请求体克隆分支的 ``audio.voice``、flavor 的 clone 标记、字段依赖解释器；
2. 缓存与指纹：参考文件指纹并进缓存键（换文件换键、同文件复用）、服务层 ``_cache_path``；
3. 服务层 worker 与设置页：克隆请求在后台线程把参考音频读成 data URI 打进 HTTP 请求体；
   参考音频键写回 → 落盘 → 重载（白名单门禁），行上带「浏览…」按钮。

全部用例不打网络（``http_util`` 一律 monkeypatch）。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pet import tts  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.tts import mimo as mimo_provider  # noqa: E402
from pet.voice_chime import (  # noqa: E402
    MIMO_MODEL,
    MIMO_MODEL_VOICE_CLONE,
    MIMO_MODEL_VOICE_DESIGN,
    cache_key,
    clean_mimo_model,
    mimo_payload,
    mimo_ref_audio_problem,
    mimo_ref_audio_uri,
    normalize_chime_config,
)


def _attempt(provider_id, **overrides):
    """按注册表里的 provider 造一条合成尝试（值来自字段默认值 + 覆盖）。"""
    provider = tts.get(provider_id)
    values = provider.values({})
    values.update(overrides)
    return tts.TtsAttempt(provider=provider_id, values=values, plan=provider.plan(values))


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _mimo_ok_payload(audio: bytes) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"audio": {"data": base64.b64encode(audio).decode()}}}]}
    ).encode("utf-8")


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ============================================================ 纯逻辑


def test_clone_model_is_an_option_and_cleans_like_a_model():
    """克隆模型进得了下拉清单（清洗器认它），且三种模型并存。"""
    assert MIMO_MODEL_VOICE_CLONE in {model for model, _ in mimo_provider.MIMO_MODELS}
    assert clean_mimo_model(MIMO_MODEL_VOICE_CLONE) == MIMO_MODEL_VOICE_CLONE
    assert {model for model, _ in mimo_provider.MIMO_MODELS} == {
        MIMO_MODEL, MIMO_MODEL_VOICE_DESIGN, MIMO_MODEL_VOICE_CLONE,
    }


def test_ref_audio_problem_walks_through_each_reason(tmp_path, monkeypatch):
    """可用性自检只看元数据：空 / 找不到 / 格式 / 太大各给一句人话，能用时空串。"""
    good = tmp_path / "voice.wav"
    good.write_bytes(b"RIFF")
    assert mimo_ref_audio_problem({}) == "还没选参考音频"
    assert mimo_ref_audio_problem({"ref_audio": ""}) == "还没选参考音频"
    assert mimo_ref_audio_problem({"ref_audio": f'"{good}"'}) == "", "带引号的路径照样认"
    assert "找不到" in mimo_ref_audio_problem({"ref_audio": str(tmp_path / "nope.mp3")})

    text = tmp_path / "notes.txt"
    text.write_bytes(b"x")
    assert "mp3" in mimo_ref_audio_problem({"ref_audio": str(text)}), "只支持 mp3/wav"

    monkeypatch.setattr(mimo_provider, "MIMO_REF_AUDIO_MAX_BYTES", 2)
    assert "太大" in mimo_ref_audio_problem({"ref_audio": str(good)})


def test_ref_audio_uri_builds_data_uri_and_rejects_bad_input(tmp_path, monkeypatch):
    """data URI 构造：MIME 随扩展名；缺文件 / 空文件 / 超上限都抛可读错误。"""
    wav = tmp_path / "me.wav"
    wav.write_bytes(b"RIFF-DATA")
    assert mimo_ref_audio_uri(str(wav)) == (
        "data:audio/wav;base64," + base64.b64encode(b"RIFF-DATA").decode("ascii")
    )
    mp3 = tmp_path / "me.mp3"
    mp3.write_bytes(b"ID3")
    assert mimo_ref_audio_uri(str(mp3)).startswith("data:audio/mpeg;base64,")

    with pytest.raises(ValueError, match="mp3 或 wav"):
        mimo_ref_audio_uri(str(tmp_path / "me.ogg"))
    with pytest.raises(ValueError, match="读不出来"):
        mimo_ref_audio_uri(str(tmp_path / "missing.wav"))
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="空文件"):
        mimo_ref_audio_uri(str(empty))

    monkeypatch.setattr(mimo_provider, "MIMO_REF_AUDIO_BASE64_MAX", 3)  # b"ID3" 编码后 4 字符
    with pytest.raises(ValueError, match="太大"):
        mimo_ref_audio_uri(str(mp3))


def test_clone_payload_sends_the_ref_uri_as_voice():
    """克隆模型：audio.voice 收参考音频的 data URI（不是音色名、不是路径）。"""
    uri = "data:audio/wav;base64,QUJD"
    payload = mimo_payload("现在是上午九点整", {
        "model": MIMO_MODEL_VOICE_CLONE,
        "voice": "冰糖",  # 克隆时内置音色名无意义，必须被忽略
        "ref_audio_uri": uri,
        "style": "",
    })
    assert payload["model"] == MIMO_MODEL_VOICE_CLONE
    assert payload["audio"]["voice"] == uri
    assert payload["audio"]["format"] == "wav"


def test_clone_flavor_marks_clone_and_ignores_builtin_voice_names():
    """克隆音色的声音由参考音频决定：内置音色名不进缓存键，标记 clone 顶替。"""
    provider = tts.get("mimo")
    clone_a = provider.flavor({"model": MIMO_MODEL_VOICE_CLONE, "voice": "冰糖", "style": ""})
    clone_b = provider.flavor({"model": MIMO_MODEL_VOICE_CLONE, "voice": "苏打", "style": ""})
    assert clone_a == clone_b, "换内置音色名不该换克隆缓存"
    assert "clone" in clone_a
    assert provider.flavor({"model": MIMO_MODEL, "voice": "冰糖", "style": ""}) != clone_a


def test_preflight_falls_back_to_builtin_voice_when_ref_audio_unusable(tmp_path):
    """克隆音色配了不可用的参考音频：不硬撞一次 API，preflight 当场回落内置音色。"""
    provider = tts.get("mimo")
    values = {**provider.values({}), "model": MIMO_MODEL_VOICE_CLONE}

    fixed, note = provider.preflight(dict(values))  # 没选参考音频
    assert fixed["model"] == MIMO_MODEL
    assert "还没选参考音频" in note and "内置音色" in note

    good = tmp_path / "me.wav"
    good.write_bytes(b"RIFF")
    unchanged, ok_note = provider.preflight({**values, "ref_audio": str(good)})
    assert unchanged["model"] == MIMO_MODEL_VOICE_CLONE and ok_note == ""

    other, quiet = provider.preflight({**values, "model": MIMO_MODEL})
    assert other["model"] == MIMO_MODEL and quiet == "", "非克隆模型不做这套检查"


def test_field_visible_interprets_single_and_multi_conditions():
    """字段依赖解释器：hidden_when 支持多条（任一命中即隐），visible_when 是白名单。"""
    spec = tts.TtsField(
        "x", "voice_chime_x", "X",
        hidden_when=(("model", "a"), ("model", "b")),
        visible_when=("model", "m"),
    )
    assert tts.field_visible(spec, {"model": "m"})
    assert not tts.field_visible(spec, {"model": "a"})
    assert not tts.field_visible(spec, {"model": "b"})
    assert not tts.field_visible(spec, {"model": "other"}), "visible_when 没命中即隐"

    single = tts.TtsField("y", "voice_chime_y", "Y", hidden_when=("model", "a"))
    assert tts.field_visible(single, {"model": "m"})
    assert not tts.field_visible(single, {"model": "a"})


# ============================================================ 缓存与指纹


def test_ref_audio_fingerprint_drives_the_cache_key(tmp_path):
    """同一套参数下换参考文件必须换缓存键；extra 缺省（""）与旧键逐字节一致。"""
    from pet.voice_chime_service import _attempt_cache_extra

    text = "现在是上午九点整。加油"
    cfg = normalize_chime_config({"voice_chime_tts_backend": "mimo"})
    ref1 = tmp_path / "a.wav"
    ref1.write_bytes(b"one")
    ref2 = tmp_path / "b.wav"
    ref2.write_bytes(b"two")

    def extra_for(path):
        return _attempt_cache_extra(
            _attempt("mimo", model=MIMO_MODEL_VOICE_CLONE, ref_audio=str(path))
        )

    extra1, extra2 = extra_for(ref1), extra_for(ref2)
    assert extra1 and extra2 and extra1 != extra2
    assert extra_for(ref1) == extra1, "同一个文件指纹稳定（缓存可复用）"
    assert cache_key(text, cfg, "mimo", extra=extra1) != cache_key(text, cfg, "mimo", extra=extra2)
    assert cache_key(text, cfg, "mimo") == cache_key(text, cfg, "mimo", extra=""), (
        "extra 默认空串不改变既有缓存键"
    )
    assert _attempt_cache_extra(_attempt("edge")) == "", "没声明文件字段的后端没有补充指纹"
    assert extra_for(tmp_path / "gone.wav") == "", "文件丢了 → 空指纹（合成届时走回退）"


def test_file_fingerprint_tracks_rewrites_and_missing(tmp_path):
    """重写同名文件（即使大小不变）也必须换指纹：mtime_ns 是主抓手。"""
    import os

    from pet.voice_chime_service import _file_fingerprint

    ref = tmp_path / "me.wav"
    ref.write_bytes(b"AAAA")
    os.utime(ref, (1000, 1000))
    first = _file_fingerprint(str(ref))
    assert first
    os.utime(ref, (2000, 2000))
    assert _file_fingerprint(str(ref)) != first
    assert _file_fingerprint(str(tmp_path / "missing.wav")) == ""


def test_service_cache_path_separates_different_ref_files(tmp_path):
    """服务层缓存路径：同文本同参数、不同参考文件 → 两个不同的缓存文件。"""
    import pet.voice_chime_service as svc_mod

    service = svc_mod.VoiceChimeService.__new__(svc_mod.VoiceChimeService)
    service._cfg = normalize_chime_config({"voice_chime_tts_backend": "mimo"})
    service._cache_dir = tmp_path
    ref1 = tmp_path / "a.wav"
    ref1.write_bytes(b"one")
    ref2 = tmp_path / "b.wav"
    ref2.write_bytes(b"two")

    def path_for(ref):
        return service._cache_path(
            "文本", _attempt("mimo", model=MIMO_MODEL_VOICE_CLONE, ref_audio=str(ref))
        )

    path1, path2 = path_for(ref1), path_for(ref2)
    assert path1 != path2
    assert path1.suffix == ".wav" and path2.suffix == ".wav"


# ============================================================ 服务层 worker


def test_clone_worker_reads_ref_audio_and_posts_the_data_uri(tmp_path, monkeypatch):
    """服务层 worker：克隆请求在后台线程把参考音频读成 data URI 打进请求体。"""
    import pet.voice_chime_service as svc_mod

    seen: list[dict] = []

    def fake_urlopen(request, timeout=None, context=None):
        seen.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse(_mimo_ok_payload(b"WAV-BYTES"))

    monkeypatch.setattr("pet.http_util.urlopen", fake_urlopen)

    ref = tmp_path / "my_voice.wav"
    ref.write_bytes(b"RIFF-REF")
    out = tmp_path / "chime.wav"
    results: list[tuple] = []
    svc_mod._TTSWorker(
        "现在是上午九点整。",
        (_attempt("mimo", api_key="k-123", model=MIMO_MODEL_VOICE_CLONE, ref_audio=str(ref)),),
        (out,),
        lambda path, text, error: results.append((path, text, error)),
    ).run()

    assert results == [(str(out), "现在是上午九点整。", "")]
    assert out.read_bytes() == b"WAV-BYTES"
    assert len(seen) == 1
    assert seen[0]["audio"]["voice"] == (
        "data:audio/wav;base64," + base64.b64encode(b"RIFF-REF").decode("ascii")
    )


def test_worker_reports_readable_error_when_ref_audio_disappears(tmp_path, monkeypatch):
    """合成前参考音频没了：错误里说清原因（走既有回退链），不是一句「合成失败」。"""
    import pet.voice_chime_service as svc_mod

    monkeypatch.setattr("pet.http_util.urlopen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("参考音频都读不到，不该打网络")
    ))
    out = tmp_path / "chime.wav"
    errors: list[str] = []
    svc_mod._TTSWorker(
        "现在是上午九点整。",
        (_attempt("mimo", api_key="k", model=MIMO_MODEL_VOICE_CLONE,
                  ref_audio=str(tmp_path / "gone.wav")),),
        (out,),
        lambda path, text, error: errors.append(error),
    ).run()

    assert len(errors) == 1 and "参考音频" in errors[0]
    assert not out.exists()


# ============================================================ 设置页


def test_settings_page_round_trips_ref_audio_key(tmp_path):
    """参考音频键走设置页写回 → 落盘 → 重载：值必须活下来（白名单门禁）。"""
    from pet.voice_chime_settings import VoiceChimeSettingsPage

    _qapp()
    cfg = Config(base=tmp_path)
    page = VoiceChimeSettingsPage(cfg)
    page.mimo_model_select.setCurrentData(MIMO_MODEL_VOICE_CLONE)
    page.mimo_ref_audio_edit.setText("D:/voices/me.wav")
    page.apply_to_config()
    cfg.save()

    reloaded = Config(base=tmp_path)
    assert reloaded.get("voice_chime_mimo_model") == MIMO_MODEL_VOICE_CLONE
    assert reloaded.get("voice_chime_mimo_ref_audio") == "D:/voices/me.wav"


def test_ref_audio_row_has_a_browse_button(tmp_path):
    """参考音频行是「路径框 + 浏览…」：包里带着可点的浏览按钮（对象名稳定）。"""
    from PySide6.QtWidgets import QPushButton

    from pet.voice_chime_settings import VoiceChimeSettingsPage

    _qapp()
    page = VoiceChimeSettingsPage(Config(base=tmp_path))
    browse = page.mimo_ref_audio_row.findChild(QPushButton, "ttsPathBrowse")
    assert browse is not None, "参考音频行必须带浏览按钮"
    assert page.mimo_ref_audio_edit.text() == "", "默认没选参考音频"
