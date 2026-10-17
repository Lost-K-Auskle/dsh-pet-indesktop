# PR 报告：MiMo 克隆音色（参考音频）+ provider 声明面扩展（path 字段 / 显隐 / 缓存指纹）

- 日期：2026-10-15
- 基线：工作树改动（未提交），基于合并提交 `f26c44a`（采用上游 PR #267 的 provider
  架构；合并时已同步 `origin/main` 33 个提交至 v4.3.0 `b55f203`）。
- 范围：给 MiMo 后端加**克隆音色**（参考音频）；把 provider 声明面扩到能容纳
  其他 TTS 的形状（`path` kind、`visible_when`、`affects_cache`、`file_filter`、
  多条 `hidden_when`）。**不动**调度、台词、播放与免打扰语义。
- 动机：用户指令「先用 mimo（含参考音频克隆），为其他 TTS 做好占位」。MiMo 克隆的
  官方形态＝参考音频转 base64 data URI 放进 `audio.voice`，模型
  `mimo-v2.5-tts-voiceclone`（只认 mp3/wav，base64 后 ≤10MB）。

---

## 一、修改文件说明（逐文件 + `git diff --numstat`）

```
$ git diff --numstat f26c44a        # 13 个已跟踪文件；新测试文件以行数单列
1	0	.gitignore
14	7	docs/ADDING-A-TTS-PROVIDER.md
3	1	pet/config.py
2	0	pet/tts/__init__.py
42	5	pet/tts/base.py
131	10	pet/tts/mimo.py
13	2	pet/voice_chime.py
47	2	pet/voice_chime_service.py
48	10	pet/voice_chime_settings.py
4	0	tests/test_config_schema.py
1	0	tests/test_settings_page_guide.py
1	0	tests/test_voice_chime.py
32	4	tests/test_voice_chime_settings_layout.py
（新文件）341	0	tests/test_voice_chime_mimo_clone.py
13 改 + 1 新 = 14 文件 +680 / −41（另 docs/INDEX.md 登记本报告 +1 行；
本报告文件自身不计入）
```

### 改动：接口层（纯逻辑、零 Qt）

- **`pet/tts/base.py`** — 声明面扩展：
  - `FIELD_KINDS` 增 `"path"`（路径框 + 浏览按钮的声明式 kind）；
  - `TtsField` 增四个声明位：`visible_when`（白名单语义：值不等即隐）、
    `affects_cache`（文件内容影响输出 → 服务层把文件指纹并进缓存键）、
    `file_filter`（文件对话框过滤器）；`hidden_when` 从单条扩到多条
    （写成 `((name,值),(name,值))`，任一命中即隐）；
  - 新函数 `field_visible(spec, values)` + `_dep_pairs()`：显隐语义的**唯一解释器**，
    设置页与服务层共用同一份判断。
- **`pet/tts/__init__.py`** — re-export `field_visible`。

### 改动：MiMo 后端（`pet/tts/mimo.py`，主战场）

- 常量：克隆模型 `mimo-v2.5-tts-voiceclone` 进模型下拉（「克隆音色（用参考音频）」）；
  MIME 表（`.mp3`/`.wav` 两档）；原始文件上限 `7_200_000` B（对应官方「base64 后
  ≤10MB」）；默认空路径。
- 三个新函数：
  - `clean_mimo_ref_audio(value)` — 去首尾引号（用户从资源管理器复制路径常带引号）；
  - `mimo_ref_audio_problem(values)` — 可用性自检，返回人话原因（「还没选参考音频」
    「这个文件找不到了」「只支持 mp3 或 wav」「文件太大」「读不到这个文件」）或空串；
    **只看元数据不读文件内容**，GUI 线程可跑；
  - `mimo_ref_audio_uri(path_text)` — 读文件 → base64 → `data:{MIME};base64,{…}`，
    各失败分支抛可读错误。
- `mimo_payload` 三分支：内置音色＝`audio.voice` 放音色名；音色设计＝不发 `voice`；
  克隆＝`audio.voice` 收 data URI（由 `synth` 在**后台线程**注入 `values["ref_audio_uri"]`，
  `plan` 保持纯、只带路径 `ref_audio`）。
- 字段：新增 `ref_audio` 行（`kind="path"`、`visible_when=("model", 克隆)`、
  `affects_cache=True`、`file_filter="音频文件 (*.mp3 *.wav)"`）；内置音色行的
  `hidden_when` 加「克隆」一条（克隆时音色名无意义）。
- `flavor()`：克隆用固定标记 `clone` 顶替音色名——换内置音色名不换克隆缓存。
- `preflight()`：克隆模型 + 参考音频不可用（空 / 找不到 / 格式 / 过大）→ **当场回落
  内置音色**并返回 note「克隆音色还用不了（{原因}），这次先按内置音色报时。」
  （服务层 `_announce_once` 每进程只弹一次）——不硬撞一次注定失败的 API 调用。
- `clean()` 覆写：`path` 字段走 `clean_mimo_ref_audio`，让 config / 指纹 / plan
  三处看到同一个字符串。

### 改动：缓存键与服务层

- **`pet/voice_chime.py`** — `cache_key(text, cfg, provider_id=None, extra="")`：
  `extra` 非空才拼进 sha1 原料——**空串时既有缓存键逐字节不变**（老缓存全部复用，
  无冷启动）。clone 相关符号 re-export。
- **`pet/voice_chime_service.py`** — 新函数（纯逻辑，可单测）：
  - `_file_fingerprint(path)` = `sha1(f"{path}|{size}|{mtime_ns}")[:16]`——只读元数据
    不读内容；文件不在/读不到 → 空串；
  - `_attempt_cache_extra(attempt)` — 遍历 provider 声明里 `kind=="path" and
    affects_cache` 的字段算指纹，**空指纹跳过**（不贡献悬空前缀；文件丢失时合成
    届时走既有失败回退链）；
  - `_cache_path` 把 `extra` 传进 `cache_key`——换参考文件即换缓存文件。

### 改动：设置页（`pet/voice_chime_settings.py`）

- `path` kind 的控件分支：QLineEdit + 「浏览…」按钮（objectName `ttsPathBrowse`，
  供用例钉住）+ `QFileDialog.getOpenFileName(self, f"选择{spec.label}", …,
  spec.file_filter or "所有文件 (*)")`——过滤器**由 provider 声明**，设置页不写死。
- 依赖显隐（`hidden_when` / `visible_when`）统一改走 `tts.field_visible`
  （替换原先的行内联判断）。
- 别名：`mimo_ref_audio_edit`（控件）与 `mimo_ref_audio_row`（行）供测试与既有引用。

### 改动：配置（`pet/config.py`）

- `voice_chime_mimo_ref_audio`（默认 `""`）在 DEFAULTS 与 reload 白名单元组**两处
  手写登记**（白名单第 153 键；本报告原记 154，推送前审计修订于 2026-10-17）——provider 键不会自动收编（见文档修正一节）。

### 用例（5 个文件）

- **新 `tests/test_voice_chime_mimo_clone.py`**（341 行 / 14 例，四层）：
  - 纯逻辑：清洗器认克隆模型、可用性自检各原因分支（含带引号路径、用 monkeypatch
    把上限压到 2 字节）、data URI 构造与各错误、克隆 payload 的 `audio.voice`
    是 data URI、flavor 的 clone 标记忽略音色名、preflight 回落（不可用回落 /
    好文件不动 / 非克隆不做检查）、`tts.field_visible` 单条与多条语义；
  - 缓存与指纹：换参考文件换键、同文件稳定、`extra=""` 与旧键逐字节一致、没声明
    文件字段的后端无补充指纹、文件丢失为空指纹、`mtime_ns` 敏感性（用 `os.utime`
    改时间戳验证重写同名文件也换指纹）、服务层 `_cache_path` 对不同参考文件分叉；
  - 服务层 worker：假 HTTP 断言请求体里 `audio.voice` 是逐字节正确的 data URI；
    参考音频消失时**不打网络**且错误文案含「参考音频」；
  - 设置页：参考音频键写回 → 落盘 → reload（白名单门禁）；行上带浏览按钮。
- **`tests/test_voice_chime_settings_layout.py`** — 既有折叠用例适配
  `visible_when`（依赖未满足的行展开时断言「不该出现」，保持测试原意）；新增
  `test_clone_model_reveals_ref_audio_and_folds_the_voice_row`（默认隐藏 → 选克隆
  → 参考音频行现/内置音色行隐 → 选音色设计 → 两行都隐）。
- **`tests/test_config_schema.py`** — reload 白名单快照加新键（+ 历史注释）。
- **`tests/test_voice_chime.py`** — 默认配置键全集加新键。
- **`tests/test_settings_page_guide.py`** — 页面行检查清单加新键。

### 文档（`docs/ADDING-A-TTS-PROVIDER.md`）

- kind 表加 `path` 行（`file_filter`；`affects_cache=True` 的含义与范例）；
  依赖段落补多条 `hidden_when` 与 `visible_when`（统一由 `tts.field_visible` 解释）。
- **修正一处文档与实现不符**：原文称 config.py 会自动收编 provider 键——实际是
  手写字面量（`pet/config.py` 是纯逻辑层，不 import `pet/tts`）。已改为「手写登记
  两处，漏登记由 `test_config_schema` 护栏抓」。

### 杂项

- **`.gitignore`** — 加 `.venv/`（本仓虚拟环境不入库，环境收拾）。

---

## 二、性能分析（命令 + 环境 + 样本量）

**环境**：Windows 11 Home China (10.0.26200)、13th Gen Intel Core i7-13700H
（20 线程）、15.6 GB；Python 3.11.9（`.venv`）；测试日 2026-10-15。采数时同机另有
一个全量 pytest 在跑（约占 1 个线程）。

**命令**（本工作树实跑，纯离线）：

```
.venv\Scripts\python.exe .scratch\mimo-clone-bench-20261015.py
```

每项 3 轮取中位、轮内 20,000 次（读文件+base64 为轮内 100 次）；脚本归档在
`.scratch/mimo-clone-bench-20261015.py`（.scratch 不入库）。

**结果**：

| 指标 | 中位 | 发生时机 |
|---|---|---|
| `field_visible`（单字段） | **0.27 µs** | 设置页刷新整组 5 字段 ≈ 1.4 µs |
| preflight 自检（1 次 stat） | **32.2 µs** | GUI 线程；仅克隆模型、每次合成尝试一次 |
| `_file_fingerprint`（stat+sha1） | **18.7 µs** | 每次合成尝试；仅声明了 affects_cache 的字段 |
| `_attempt_cache_extra`（克隆尝试） | **45.4 µs** | 同上（含 clean + stat + sha1） |
| `cache_key`（旧路径 / 克隆路径带 extra） | **4.8 / 5.4 µs** | 与改动前同一量级 |
| 读文件+base64（0.2MB / 1MB / 5MB / 7,200,000B） | **1.2 / 4.9 / 23.6 / 31.8 ms** | 后台 worker；每次克隆合成一次 |

**怎么读这组数**：

1. GUI 线程净新增 ≈ preflight 自检 + 缓存键指纹 ≈ **80 µs / 次合成尝试**——报时类
   事件本就在分钟级节拍上，这点开销无感；两处都只 stat 元数据，不读文件内容、
   不联网。
2. 真正的数据搬运（读文件 + base64）**全部在后台合成线程**：1MB 参考音频 5 ms；
   即使用满 7.2MB 上限也只有 32 ms/次，且只在克隆模型合成时发生。
3. 缓存键与改动前同量级；`extra=""` 时不改变任何既有键（老缓存复用）。
4. 两次采集一致（另一轮：0.29 / 34.8 / 28.6 / 47.1 / 6.0 / 5.8 µs；1.1 / 5.0 /
   24.0 ms——同量级，差异在同机噪声内）。

**门禁**：

- `.venv\Scripts\python.exe -m ruff check .` → `All checks passed!`
- 全量用例（CI 主套件同款范围）见「三、实机运行记录」。
- 受影响族复跑：51 + 355 + 125 passed（见「三」第 4 节）。

---

## 三、实机运行记录（真机命令与输出）

### 1. 静态检查

```
$ .venv\Scripts\python.exe -m ruff check .
All checks passed!
```

### 2. 红 → 绿（本次改动自己引入的两处、自己抓出、自己修掉）

实现与用例落地后第一次跑受影响三文件：

```
$ .venv\Scripts\python.exe -m pytest -q --basetemp=tests/_tmp_clone_run \
      tests/test_voice_chime_mimo_clone.py tests/test_voice_chime_mimo.py \
      tests/test_voice_chime_settings_layout.py
2 failed, 49 passed in 2.06s
```

- **产品缺陷**：`_attempt_cache_extra` 对「文件丢了」的字段产出悬空前缀
  `"ref_audio:"` 而非空串（`assert 'ref_audio:' == ''`）——改为空指纹跳过；
- **用例笔误**：base64 上限用例 monkeypatch 成 4，而 `b"ID3"` 编码后恰 4 字符、
  判定是 `>`（等于不算超，语义正确）——用例改压到 3 并加注释。

修复后同一条命令：

```
51 passed in 2.09s
```

### 3. 全量用例（CI 主套件同款范围）

按 CI 主套件的既有口径（`.github/workflows/pr-test.yml` / `scripts/ci_shards.py`：
4 个时序/原生族文件从主套件隔离，webm 族在 CI 里另有独立步骤）跑：

```
$ .venv\Scripts\python.exe -m pytest -q \
    --ignore=tests/test_webm_reader_lifecycle.py \
    --ignore=tests/test_webm_clip_lifecycle.py \
    --ignore=tests/test_webm_first_frame_lock.py \
    --ignore=tests/test_low_priority_warm_interaction_yield.py
```

- **基线（合并后、本改动前）**：`7490 passed, 15 skipped, 17 warnings in
  1155.82s (0:19:15)`——确认合并基线健康（含上游 PR #267 采用 + 33 提交同步）。
  用例数账：改动前收集 7505（7490 passed + 15 skipped）→ 改动后 7520，净增 15
  （新文件 14 例 + 布局文件新用例 1）。
- **本改动后·第一轮**：`2 failed, 7503 passed, 15 skipped, 15 warnings in
  1131.16s (0:18:51)`。两条失败都在与本改动无关的时序/进程编舞文件上：
  `test_flight_frame_pacing.py::test_flight_speed_writes_do_not_starve_frame_delivery`
  （动画飞行节奏）与
  `test_webm_clip_loop.py::test_no_rearm_grace_expires_and_kills_process`
  （ffmpeg 宽限期杀进程）；两个文件都不 import 本改动触碰的任何模块
  （分别只依赖 `frameseq_*` / `pet_sprite` / `webm_clip` / `perfstats`）。两文件
  单独复跑**各 2 次全绿**（5 passed ×2 / 34 passed ×2）。该轮前段同机并行跑着
  受影响族复跑与微基准采集（负载扰动；同类先例：P1 段化报告 2026-10-07 记录
  `test_flight_frame_pacing` 「全量期 2 例红为并发实机启停负载所致，单独复跑全绿」）。
- **本改动后·第二轮（近空闲复跑）**：`2 failed, 7505 passed, 15 skipped, 16
  warnings in 856.22s (0:14:16)`——同样两条、同一边际（pacing 差 0.297 帧；
  webm `_soft_parked` False）。
- **本改动后·第三轮（空闲机）**：`1 failed, 7506 passed, 15 skipped, 15 warnings
  in 1201.37s (0:20:01)`——pacing 仍红且与第二轮逐字节同数（41 帧），**webm 转绿**。
- **基线对照（独立 worktree @ f26c44a，全量同款范围）**：`7490 passed, 15
  skipped, 17 warnings in 836.22s (0:13:56), 0 failed`。

#### 时序边际用例归因实验（前缀复现，两棵树）

两条失败都与本改动零模块依赖。取全量收集序中 pacing 用例（item 1650）之前的
**1649 条用例（85 个文件）**作前缀上下文，按收集序追加目标文件，两棵树同机依次复跑
（测新测试文件的影响时把它一并加进收集；前缀清单在 `.scratch/prefix-files.txt`，
完整日志在 `.scratch/prefix-repro-*.log`）：

```
$ .venv/Scripts/python.exe -m pytest -q --basetemp=tests/_tmp_prefix2 \
      $(cat .scratch/prefix-files.txt) \
      tests/test_flight_frame_pacing.py tests/test_voice_chime_mimo_clone.py
# 基线 worktree 同命令（去掉末项、换 --basetemp）
```

| 运行 | 树 | 上下文 | pacing 结果 |
|---|---|---|---|
| 全量 3 轮 | 本改动 | 7520 例 | 红 3/3（40–41 帧，需 ≥42） |
| 全量 1 轮 | 基线 worktree | 7505 例 | 绿 |
| 前缀 R1 | 本改动 | 1649 例 + 目标 | 红 |
| 前缀 R1 | 基线 worktree | 1649 例 | 绿 |
| 前缀 R2 | 本改动 | 1649 例 + 目标 | 红（40 帧） |
| 前缀 R2 | 基线 worktree | 1649 例 | **红（41 帧 vs 阈值 41.813）** |

- 小上下文（本改动树）**6/6 绿**：pacing 单跑 ×2、前驱文件（15 例）+目标 ×1、
  新测试文件（含「只引入 import、测试全部 deselect」探针）+目标 ×2。
- **判定：既有边际时序用例，非本改动引入**——pristine 基线在相同前缀上下文下同样
  复现红（R2）；该用例要求 1.2s 真窗口交付 ≥42 帧（「无截断应交付」两次实测
  44.297 / 44.813，容差仅 3 帧），大上下文下实测 40–41 帧，差 0.3–1.3 帧。两棵树
  在该用例上都是「大上下文易红、单独全绿」。
- webm 用例同族（0.2s 软停宽限被主线程停顿吃掉 → 走硬停）：本改动树全量 2/3 红
  （第三轮转绿）、单独 2/2 绿；基线全量 1/1 绿；未做前缀归因（其收集位次近尾部，
  前缀≈全量）。同类先例：P1 段化报告 2026-10-07 记录 `test_flight_frame_pacing`
  「全量期 2 例红为并发实机启停负载所致，单独复跑全绿」。

### 4. 受影响族复跑（今日复跑，命令可复现）

```
$ .venv\Scripts\python.exe -m pytest -q --basetemp=tests/_tmp_clone_run \
      tests/test_voice_chime_mimo_clone.py tests/test_voice_chime_mimo.py \
      tests/test_voice_chime_settings_layout.py
51 passed in 2.09s

$ .venv\Scripts\python.exe -m pytest -q --basetemp=tests/_tmp_clone_run2 \
      tests/test_voice_chime.py tests/test_config_schema.py \
      tests/test_voice_chime_service.py tests/test_settings_page_guide.py
355 passed in 31.44s

$ .venv\Scripts\python.exe -m pytest -q --basetemp=tests/_tmp_clone_run3 \
      tests/test_architecture.py tests/test_tts_doubao.py tests/test_tts_edge.py \
      tests/test_tts_secrets.py tests/test_verify_bundle_tts.py \
      tests/test_settings_default_marks.py tests/test_settings_copy_lint.py \
      tests/test_settings_reset_defaults.py tests/test_settings_search_aliases.py \
      tests/test_festival_settings_layout.py
125 passed in 36.90s
```

覆盖面向：新克隆链路全层、配置快照与白名单门禁、provider 契约与分层红线
（`test_architecture` 的零 Qt 守卫）、设置页文案红线（`test_settings_copy_lint` /
`test_settings_page_guide`）、打包清单（`test_verify_bundle_tts`）。

### 5. 真实参考音频上的离线验证（2026-10-15，真实文件 `D:\dsh-pet-develop\danjinvoice.wav`）

用户把真实参考音频放到仓库**之外**的 `D:\dsh-pet-develop\danjinvoice.wav` 后，在其上
补做一轮**全离线**验证（不联网、不调 TTS API、不读钥匙串；只写 `.scratch/`，未改任何
已跟踪文件）：

```
$ cd /d/dsh-pet-develop/dsh-pet-indesktop && .venv/Scripts/python.exe .scratch/verify_mimo.py
```

| 项 | 实测值 |
|---|---|
| 文件 | 302,332 B（≈295.25 KB）；sha256 `2941021f…895a`；magic `RIFF\xf4\x9c\x04\x00WAVEfmt `（certutil / PowerShell Get-FileHash / hashlib 三法同值） |
| 容器/编码 | RIFF/WAVE，PCM（未压缩，comptype=NONE），单声道，16-bit，44100 Hz，151,144 帧，**3.427 s** |
| `clean_mimo_ref_audio` | 原始 / 带双引号 / 首尾空白 三种输入归一为同一裸路径 |
| `mimo_ref_audio_problem` | 真实文件 → `""`（可用）；不存在 → 「这个文件找不到了」；同内容复制成 `.txt` → 「只支持 mp3 或 wav」（**只看扩展名与元数据、不读内容**，属预期门禁） |
| `mimo_ref_audio_uri` | 前缀 `data:audio/wav;base64,`；base64 段 403,112 字符；**解码后与文件逐字节相同**（sha256 相等）；raw 上限 7,200,000 与 base64 上限 10×1024×1024 两条均通过（余量 6.9 MB / 10.1 MB） |
| provider plan | 键 `[api_key, model, ref_audio, style, voice]`；字符串搜索无 `base64`/`data:`、无 `ref_audio_uri` 键（**纯**）；`preflight` 对真实文件**不回落**（`fell_back=False`、note 空）；`flavor` 形如 `mimo\|mimo-v2.5-tts-voiceclone\|clone\|{style}` |
| 端到端（假 HTTP） | monkeypatch `pet.http_util.urlopen` 跑 `provider.synth`（注入点 `pet/tts/mimo.py:300-304`）：请求体 `audio.voice` 以 `data:audio/wav;base64,UklGRvSc` 开头、与 `mimo_ref_audio_uri` 输出**逐字节相等**；此时 plan/synth_plan 仍纯 |
| 缓存 | `_file_fingerprint` = `7185da707680c918`（连续两次一致）；`os.utime` 改 `mtime_ns` 后变化，测毕按原值 `1746296723000000000` 复原且指纹复原（文件 sha256 未变） |
| `cache_key` | `extra=""` 与不传 `extra` 逐字节一致、且等于改动前旧公式 `sha1(f"{text}\|{flavor}")[:16]`；`extra`=文件指纹时换键。**注**：本轮复算向量取默认（非克隆）配置（`flavor="mimo\|mimo-v2.5-tts\|冰糖\|"`、`text="现在是上午九点整"`），常量 `3368efa58a0e6049`/`1afa0df24f511693` 由该向量产出、非克隆 flavor 产出；「空串不改旧键」是键的**结构性**性质，与具体向量无关 |
| 配置回环 | 临时 base 目录 set/save/reload：`voice_chime_mimo_model` 与 `voice_chime_mimo_ref_audio` 两键原样取回（白名单 `config.py:1545`、DEFAULTS `config.py:1276`） |

**官方约束对照**（依据官方文档
`https://mimo.mi.com/static/docs/quick-start/usage-guide/audio/speech-synthesis-v2.5.md`
与 API schema `https://mimo.mi.com/static/docs/api/audio/tts.md`）：硬约束三条——格式仅
mp3/wav、MIME 仅 audio/mpeg（或 audio/mp3）/audio/wav、base64 后 ≤10 MB——真实文件
**全部满足**；仓库另设更严的原始文件 ≤7.2 MB 闸（`MIMO_REF_AUDIO_MAX_BYTES`，
`pet/tts/mimo.py:61-65`）也满足（等价于官方文档口径 raw ≤7.5 MB）。**官方未写明的**：
参考音频时长数字区间（只有定性「几秒 / a few seconds」）、采样率、声道、语种——一律
记为未知、不编造；本文件 3.427 s / 44.1 kHz / 单声道，在「几秒」语义内。另注一处口径
差异：官方文档说 base64 ≤10 MB，官方参考脚本比对的是**原始字节** ≤10 MB（更宽松），
本文件远低于两者。

**独立复核**：另一路用 certutil / Get-FileHash / 自写解码脚本重做关键点（文件哈希、
URI 逐字节一致、plan 纯性、preflight 不回落**并构造反例证明回落分支可达**、大小门禁），
10/10 与上表一致。

**git 卫生**：该音频位于仓库 toplevel 上一级（`../danjinvoice.wav`），父目录**不是**
git 仓库；`git add -A --dry-run` 全量输出无任何音频扩展名、无 >1 MB 新文件，**不会被
提交**。仓库既有的 `assets/sounds/**` 音频由 `!assets/sounds/` 白名单有意跟踪，故
**不新增** `*.wav`/`*.mp3` 全局忽略（会与白名单冲突、可能 un-track 既有音频）。

**结论**：离线链路在真实文件上全绿（脚本归档 `.scratch/verify_mimo.py`）。带 Key 的
实网合成随后补做，见下节。

### 6. 实网克隆合成（真机、真 Key，2026-10-15）

用户把 MiMo **按量付费** Key 放进环境变量 `MIMO_API_KEY`（len=51）后，在其上做真机
实网验证。三个脚本归档在 `.scratch/`（不入库）：`mimo_e2e_clone_20261015.py`（首轮）、
`mimo_latency_sweep_20261015.py`（时延扫描）、`mimo_ttfb_20261015.py`（首字节分解）。

**Key 纪律**（三个脚本一致）：只从环境变量读；脚本不打印 Key 值、不写盘、不落日志；
所有打印文本过 `scrub()` 擦除；请求只记「有没有」不记头值。日志归档
`.scratch/mimo_e2e_20261015/{run1.log,sweep.log,ttfb.log}`（`sweep.log` / `ttfb.log`
为脚本自身 `tee` 落盘；`run1.log` 的临时任务输出文件已被会话清理，现由**会话转录中
取回的同一次运行原始 stdout** 补齐，无人工转录）。

```
$ cd /d/dsh-pet-develop/dsh-pet-indesktop && .venv/Scripts/python.exe .scratch/mimo_e2e_clone_20261015.py
```

**首轮三次调用**（模型 / 时延 / 产物）：

| 调用 | 模型 | 耗时 | 产物 |
|---|---|---|---|
| clone_short（9 字） | `mimo-v2.5-tts-voiceclone` | 3.08 s | 99,884 B；24 kHz 单声道 16-bit；49,920 帧 = 2.08 s；rms 2303.4；peak 20256 |
| clone_long（29 字） | `mimo-v2.5-tts-voiceclone` | 49.14 s | 345,644 B；172,800 帧 = 7.20 s；rms 2369.0 |
| builtin_short（9 字） | `mimo-v2.5-tts`（内置冰糖） | 0.83 s | 84,524 B；42,240 帧 = 1.76 s；rms 3185.1 |

**请求取证**（包装 `pet.http_util.urlopen` 记录，只记布尔与长度）：

- URL `https://api.xiaomimimo.com/v1/chat/completions`，`timeout=30.0`（原样透传到
  opener，见 `pet/http_util.py:57-70`）；
- 克隆请求体 403,308 / 403,368 B（首轮两条；差值是文本长度，扫描里另见 403,335），`audio.voice` 恒为
  **403,134 字符**、前缀 `data:audio/wav;base64,UklGRvScBA`（= 参考文件自身 RIFF 头）
  ——**参考音频确实以 data URI 随每次请求发出**；`api-key` 与 `authorization` 两个头
  **都在**（只记存在性）；
- 内置音色请求体仅 **169 B**，`audio.voice` = `冰糖`，无 data URI。

**独立复核（workflow 两路：产物校验 + 对抗复核）**：首轮三项产物的字节数 / 帧数 /
时长 / RMS / peak / sha256 **逐项吻合**（无一处不符；对抗一路改用手工 RIFF chunk 解析
+ numpy int64 与子采样双路 RMS 重算：`riff_declared+8 == 文件实际长度`、无截断/损坏、
非静音、非纯噪声、仓库内无其它 `.wav` 的 sha256 与之相同）。**旁证「确实是实网调用」**：
三份文件的 **mtime 间隔** clone_short→clone_long = **49.138 s**、clone_long→builtin =
**0.859 s**，与日志记录的调用耗时 49.14 s / 0.83 s 吻合（差 <0.03 s）；参考文件
302,332 B 的 base64 长度算术（`4·⌈n/3⌉ = 403,112`，+22 前缀 = **403,134**）恰等于
日志里的 `voice_len`。**音色代理指标**：受控同文本对比下克隆版与内置版确实不同
（中位谱质心 2414 vs 1661 Hz、RMS 2303 vs 3185、中位 F0 269.7 vs 247.4 Hz），且克隆版
在倍频带谱形与 F0 上**更接近参考音频**（`clone_long` 中位 F0 338 Hz，参考 321.9 Hz，
内置 247.4 Hz）。但两路都指出**这些指标方法依赖强**：对抗一路换窗口参数
（2048/hop1024）得中位谱质心 2658.5 vs 1737.2 Hz——定性结论不变、**数字不逐位可
复现**；整段平均谱余弦距离甚至给出相反排序；`clone_short` 的整段 ZCR 被 t=0.80 s 处
一个摩擦音突发抬高。故代理指标**只能当旁证**，不能证明「音色确实像参考音频」——
那要人耳（见四.2）。

#### 6.1 克隆调用时延远超 45 s 合成看门狗（本次实网暴露，见四.9）

八次克隆调用耗时（同一参考音频、同一模型、`preflight_note` 全为空 = 无回落）：

| 调用 | 文本 | 耗时 | 调用 | 文本 | 耗时 |
|---|---|---|---|---|---|
| clone_short | 9 字 | 3.08 s | t19 | 18 字 | 13.44 s |
| t09_b | 9 字 | 4.81 s | clone_long | 29 字 | 49.14 s |
| t31 | 29 字 | 6.92 s | t09_a | 9 字 | 82.14 s |
| | | | t09（扫描） | 9 字 | 84.28 s |
| | | | t31b | 29 字 | 105.55 s |

- **与文本长度无关**：9 字既出现 3.08 s 也出现 84.28 s；29 字既 6.92 s 也 105.55 s；
  同一个 29 字文本连续两次 6.92 s / 105.55 s（**15 倍差**）。
- **8 次里 4 次 > 45 s**（中位 31.29 s，范围 3.08–105.55 s）；同机同刻的内置音色对照
  2 次分别 **0.83 / 1.11 s**（请求体 169 B）。
- 同一文本多次合成**时长不恒定**（9 字：1.76 / 1.92 / 2.08 s），采样非确定性。

**首字节分解**（`mimo_ttfb_20261015.py`：把 `urlopen` 返回（响应头到货）与读响应体
分开计时）：

| 调用 | 响应头到货 | 响应体读完 | 合计 | 响应形态 |
|---|---|---|---|---|
| clone 9 字（a） | 8.30 s | **+73.82 s** | 82.14 s | `Transfer-Encoding: chunked` |
| clone 9 字（b） | 4.73 s | +0.08 s | 4.81 s | `Content-Length: 123434` |
| builtin 9 字 | 0.98 s | +0.13 s | 1.11 s | `Content-Length: 102947` |

→ 403 KB 参考音频的**上传 + 建连约 5–8 s**（对照内置的 1.0 s：是体积代价，但不是量级
原因）；**方差全部在服务端生成**——同一个请求体，响应头到货后一次 **0.08 s** 就拿到
完整响应体，另一次等了 **73.8 s**（chunked 流式）。**结论：慢在 MiMo 克隆推理 / 排队，
不在本机、不在上传。**

**与 45 s 看门狗的关系**：`pet/voice_chime_service.py:115` 的 `_SYNTH_TIMEOUT_S = 45.0`
（注释依据是「正常合成 3-8s 完成，45s 余量充足」——克隆路径打破了该前提）。真正生效的
复位时刻是 `_busy_since + 46 s`（`_UNLOCK_INTERVAL_S = 45+1`）或首个 elapsed ≥45 s 的
20 s tick；到点由 `_release_stale_busy` 复位合成锁并 `_next_synthesis_generation()`
递增代次，迟到回调在 `_on_synthesized` 按代次整条丢弃——**上表 4 次 >45 s 的克隆合成
在真机上会走到这条作废路径**（49.14 s 与 46 s 只差 3 s 余量）。

后果**不是无条件的「听不到」**（独立审计实测，见 6.2）：被丢弃的只是**本次播放**，
worker 仍会 `_publish()` 落盘，而报时路径有预合成窗口（`PRECACHE_WINDOW_S = 60`，由
20 s tick 触发，实际提前 40–60 s）——**只要合成在报时点前落盘，`_fire` 的缓存命中
分支照样把它播出来**；但 40–60 s 的窗口装不下 80–105 s 的那几次（8 次里 3 次），这些
报时会迟到或不出声，且每次尝试都计费。**没有预合成的路径（手动 say_now / 设置页试听
/ 节日提醒 / 点击自言自语）则直接静默。**本改动未处理**（属调度 / 自愈语义，本 PR
声明「不动调度」）；风险与可选处置登记在四.9。

#### 6.2 独立审计（workflow：Key 泄漏审计 + 看门狗可达性审计）

**Key 泄漏审计**（另一路独立扫描；先做**正向对照**——在 .scratch 造一个含 Key 的临时
文件、扫描器命中 line=3、随即删除——证明扫描器有效，再扫全量）：

| 区域 | 扫描量 | 命中 |
|---|---|---|
| 仓库工作树（含 .venv/.scratch/二进制） | 50,359 文件 / 1.55 GB | 0 |
| git 历史（`git log -S` + `rev-list --objects --all` 全对象） | 12,754 对象 / 7,585 blob | 0 |
| `.git` 非 objects 文件 + objects 目录原始字节 | 142 文件 | 0 |
| 本项目会话记录（`D--dsh-pet-develop-dsh-pet-indesktop` + `d--dsh-pet-develop`） | 177 文件 / 58.8 MB | 0 |
| 任务输出（`Temp/claude`，含 36 个 `*.output`） | 75 文件 / 5.3 MB | 0 |
| 仓库内 `*.log` 与 .scratch 日志 | 逐份实测 | 0 |

**0 命中**（plain / base64 / base64url 三种编码口径），且**结构上不可能泄漏到缓存键**：
`cache_key` = `sha1(f"{text}|{flavor}")[:16]`（`pet/voice_chime.py:601-625`），而
`flavor()` 只拼 model/voice/style（`pet/tts/mimo.py:260-273`）；`api_key` 是
`kind="secret"` 字段，被 `_attempt_cache_extra` 的 `kind=="path"` 过滤排除；Key 只走
`api-key` / `Authorization` **请求头、不进请求体**（`mimo_payload` 不带它）。持久化位置
是 OS 钥匙串（`pet/chat/models.py:131-146`，Windows 凭据管理器，ref `tts/mimo`）。

**⚠️ 审计的附带发现（与本改动无关、不在本仓库，但必须如实登记）**：对抗一路把口径
放宽到**整机全部 `.claude/projects/`**（3,432 文件 / 1.44 GB）后，**Key 逐字出现在
另一个项目 `chaotic-maid-chan` 的 6 个会话转录里（共 16 处）**，上下文是 OpenAI client
的 `api_key="…"` 字面量（`base_url=https://api.xiaomimimo.com/v1`）——即该 Key 在
**那个项目**里被硬编码进代码并被转录留存。我已独立复核（`os.walk` 全量 + 精确子串，
只打印布尔与路径）：6 个文件、16 处，Key 的 `sha256[:12] = 20c94ecc44df` 与本次审计
一致。**不是本次改动造成、也不在本仓库内**，但说明这个 Key 已经落在磁盘上（很可能
同时在那份源码里），**建议轮换**。本项目自身（仓库、本项目会话记录、任务输出、日志）
仍是 0 命中。

**看门狗可达性审计**（另一路，用**假时钟仿真 + 真 `_TTSWorker` / 真 `_publish` / 真
queued 信号**实测，脚本 `.scratch/sim_watchdog_49s.py`、`.scratch/sim_precache_rescue.py`）：

- **场景 A**（单次 49 s 合成）：46 s 看门狗 → `_busy=False`、代次 1→2；worker 在
  49.14 s 落盘并回调 → 命中「丢弃已作废旧合成的迟到结果（代次 1，当前 2）」→
  **播放 0 次，缓存里留着 4,099 字节音频**。
- **场景 B**（复位那一拍队列里还有一条待播）：自愈立刻再起一次合成 → 它同样在 46 s
  后作废 → **合成 2 次、播放 0 次**（重复计费且一次也没出声）。
- **预合成救回路径**（两路独立复现）：46 s 作废后 worker 于 49.14 s 落盘，到点
  `_on_tick` 后 **`plays=1`**——机制是 `_consume_precache` 要求 `_precache_path` 非空
  （超时丢弃回调后它恒为 `None`）→ 回退到 `_fire`，而 `_fire` 的缓存命中分支只看文件
  是否存在、不看预合成占位。**故「超时作废 ⇒ 用户听不到」只在无预合成路径成立。**
- **无 provider 级超时覆盖机制**：服务只消费 provider 的 fields/plan/flavor/
  availability/preflight/synth，全仓无「合成超时」配置键；要只给 MiMo 放宽，只能改
  `pet/voice_chime_service.py:115` 常量或另加机制。
- **文本侧不切句**：整句交给单个 `_TTSWorker`、每个后端一次 `provider.synth()`；报时句
  最坏约 131 字（报时文本 ~10 字 + `_MAX_CUSTOM_QUOTE_LEN = 120`）。

---

## 四、未验到 / 如实登记

1. **真机克隆合成已测（实网、真 Key）**：参考音频 `D:\dsh-pet-develop\danjinvoice.wav`
   （仓库之外，302,332 B / 3.427 s / 单声道 44.1 kHz，sha256 `2941021f…895a`）的离线
   链路已验（「三」第 5 节），实网克隆合成随后补做（「三」第 6 节）：8 次调用
   `preflight_note` 全为空（无回落）、请求体带 403,134 字符 data URI、`api-key` 与
   `authorization` 两个头都在，产物为有效 24 kHz 单声道 16-bit WAV。**仍未做**：
   ① 主观听感（见下条）；② **服务层整链**的实网跑——Key 在环境变量里，而服务层从
   钥匙串取 Key（`secret_ref="tts/mimo"`），故实网只跑到 **provider 层**
   （`provider.synth()`）；provider 与服务层之间的落盘 / 缓存 / 播放由既有用例
   （假 HTTP）覆盖。官方未写明参考音频的时长/采样率/声道/语种要求（未知），本文件
   属「几秒、单声道」的安全一侧。
2. **克隆音色的主观听感仍待实听（人耳）**：产物已落盘可直接试听——
   `.scratch/mimo_e2e_20261015/` 下的 `clone_short.wav` / `clone_long.wav`（克隆）与
   `builtin_short.wav`（内置冰糖、同文本对照），另有扫描与首字节分解的 8 份
   （`lat_*.wav` / `ttfb_*.wav`）。代理指标（中位谱质心、F0、倍频带谱形）只能说明
   「克隆版与内置版不同、且更接近参考音频」，**不能证明音色确实像**——那要人耳。
3. **设置页「浏览…」的真文件对话框交互**未机器验证：offscreen 环境弹不出原生
   对话框；用例只钉住「按钮存在、对象名稳定、过滤器由声明提供」。实机手点未做。
4. **7.2MB 边界文件的整链合成**未跑（无 Key）：单测用 monkeypatch 常量验证判定
   逻辑；性能表里 7.2MB 只测了「读文件 + base64」这一段。
5. **webm 生命周期族与 low-priority 预热族**未在本地复跑：与本改动无关
   （动画读取 / 线程编舞域），CI 里本就是独立步骤；主套件范围已含除这 4 个文件
   外的全部用例。
6. **preflight 回落 note 的真机可见性**未验：走既有 `_announce_once` 冒泡通路，
   机制由既有用例覆盖，本改动只改 note 文案与触发条件。
7. **是否回贡上游（MerZlin）待定**：上游 PR #267 已在 provider 方向铺路，克隆
   能力是否以同形回贡待维护者口径。
8. **边际用例红率的样本量限制**：pacing 用例观察到「本树全量 3/3 红、基线全量
   1/1 绿；前缀 2/2 红、基线 1/2 红（R2 已复现）」。已足以判定非本改动引起
   （基线同前缀可复现红、单独两树皆绿、零模块依赖），但「新增模块是否轻微扰动
   进程状态、略提高该用例红率」要两棵树各 ~5 次全量才能给出红率区间，本次未跑；
   如需可在后续空窗补跑。
9. **⚠️ 克隆调用时延远超 45 s 合成看门狗（本次实网暴露，未处理）**：见「三」6.1 / 6.2——
   8 次克隆调用中 **4 次 >45 s**（3.08–105.55 s，中位 31.29 s），首字节分解证明慢在
   **服务端生成**（响应头到货后 0.08 s 拿到响应体 vs 另一次 73.8 s），不在本机、不在
   上传；而 `pet/voice_chime_service.py:115` 的 `_SYNTH_TIMEOUT_S = 45.0` 在
   `_busy_since + 46 s` 到点复位合成锁并递增代次，迟到回调被 `_on_synthesized` 丢弃。
   后果（独立审计用**假时钟仿真实测**，非纯推理）：被丢弃的只是**本次播放**，音频仍
   落盘——① 报时路径有 60 s 预合成窗口（实际提前 40–60 s），**合成若在报时点前落盘
   会被 `_fire` 的缓存命中分支救回、照常出声**；② 但 80–105 s 的那 3 次装不进窗口 →
   报时迟到或不出声；③ **没有预合成的路径（手动 say_now / 设置页试听 / 节日提醒 /
   点击自言自语）直接静默**；④ 最坏情形实测到「复位那一拍队列里还有待播 → 自愈再起
   一次合成 → 又一次超时作废」的**循环：合成 2 次、播放 0 次、计费 2 次**。
   **触发条件是「用户选了克隆模型」，与文本长短无关**（9 字同样会中）。
   **本改动未处理**——属调度 / 自愈语义，本 PR 声明「不动调度」。可选处置（供拍板）：
   ① provider 声明面加 `synth_timeout_s`（服务层读 provider 声明的超时，只给 MiMo
   放宽到 ≥180 s）——最贴合本 PR 的声明面主题，但会改到服务层自愈语义；
   ② 维持现状，仅在设置页给克隆模型加一句时延提示（不改语义）；
   ③ 不用克隆模型报时，把克隆留给别的用途。
   另注：`MIMO_TIMEOUT_S = 30.0` 是 **socket 级（单次读写）**超时、不是整请求时限——
   105.55 s 的调用照样成功返回；但服务端若某次静默 >30 s，该次请求仍会判超时失败。
