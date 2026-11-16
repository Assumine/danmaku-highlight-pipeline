# Danmaku Highlight Pipeline

**游戏实况直播自动切片：用弹幕定位精彩片段，生成节目单与有弹幕 / 无弹幕双版本。**

[![Tests](https://github.com/Assumine/danmaku-highlight-pipeline/actions/workflows/tests.yml/badge.svg)](https://github.com/Assumine/danmaku-highlight-pipeline/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

把已录完的游戏实况长录像和弹幕，整理成便于回看、检查和投稿的精彩片段合集。适合从数小时直播中寻找翻车、反转、搞笑互动、精彩操作等观众反应集中的内容，减少人工拖动进度条找片段的工作。

项目提供可执行的 Python 处理链，以及无需真实直播数据、无需账号即可跑通的合成样例。片段发现依赖弹幕规则，可选 Whisper 用于语音边界精修，不依赖 AI 看懂游戏画面。

## 主要用途

- **游戏实况直播回放切片**：根据多人弹幕反应定位候选，保留必要铺垫，在反应回落后尽快收尾。
- **精彩片段双版本出片**：同一套剪辑时间轴生成无弹幕版和有弹幕版，适配不同观看偏好；示例中称为“下饭片段”。
- **回放节目单与人工复核**：输出源录像时间点、剪辑边界和判定证据，方便跳转原片、检查误判及调整频道规则。
- **接入现有录制和投稿流程**：读取外部录制工具产生的视频/XML；校验通过后，可显式调用 biliup 向指定稿件追加成片。

更适合有持续观众互动、弹幕能反映节目效果的频道。弹幕稀疏、固定口号刷屏或反应与画面明显错位时，仍需人工抽检和规则校准。项目本身不录制直播，也不会默认连接账号或投稿。

## 默认剪辑节奏与配置

普通片段的节奏是：**触发前约 40 秒铺垫 → 保留精彩反应 → 首次稳定回落后快速收尾 → 可选语音校对补齐句尾**。以下是当前公开代码的默认值，不是对每段视频长度的保证。

| 配置项 | 当前默认值 | 用途与边界 |
|---|---|---|
| 普通片段前置铺垫 | 触发点前约 **40 秒** | 保留事件起因和反差铺垫；连续节目点成组、爽局等候选另有范围规则。 |
| 爽局候选前置 | 首个受支持主题点前 **120 秒** | 单独的主题候选规则，用于预留较长对照，不套用到所有精彩片段。 |
| 头部语音检查 | 入点前 **10 秒**、后 **3 秒** | 启用 Whisper 后，若入点落在一句话中间，可向前补齐该话轮。 |
| 精彩区间反应跟踪 | **20 秒**反应统计窗口 | 用于反应波分析，不表示每段只剪 20 秒，也不是笑点后固定再留 20 秒。 |
| 快速收尾 | 首次稳定回落；**4 秒**回落确认 | 以反应回落确定结束参考，避免把后续普通聊天、复读无限接在尾部；不是额外固定保留 4 秒。 |
| 尾部语音检查 | 结束参考前 **20 秒**、后 **15 秒** | 启用 Whisper 后寻找句子边界，不等于把整个检查窗口加入成片。 |
| 邻近节目点分组 | 相邻触发点间隔不超过 **100 秒** | 形成连续候选组，减少紧邻节目点被拆得过碎；不代表语义上必然是同一事件。 |
| 常规叠化转场 | 画面 **0.6 秒**、音频 **0.2 秒** | 按片段边缘响度选择转场，强反应相接等情况可采用不同重叠时长。 |
| 整片结尾 | 最多补 **0.6 秒**，最长 **0.8 秒**淡出 | 只处理最后一段，并受下一句语音和源录像剩余时长约束。 |
| 语音精修 | 默认关闭；可选 `--speech whisper`，模型 `small` | 只转写头尾局部窗口，不做全片转写，也不负责理解游戏机制或编造事件前因后果。 |
| 正式出片 | 原分辨率、原帧率；输出两版 | 可用 `--preview-height 360` 先做低分辨率试跑。 |

**“约 20 秒”是分析窗口，不是固定片长或固定尾巴。** 实际精彩区间可以更短或更长；收尾由候选结束参考、反应回落和可选语音边界共同决定。

候选发现另使用 **24 秒**滑动窗口和 **180 秒**历史基线；同一账号在 **15 秒**内刷屏时限制其重复贡献，避免把单人复读当成多人节目效果。

多数节奏参数目前是代码常量，不是独立的配置文件或设置面板：

- 反应窗口、语音检查范围与转场时长：[highlight_editor.py](highlight_editor.py)。
- 普通候选前置时间 `PRE_ROLL_SECONDS`、分组间隔、发现窗口、历史基线与笑声规则：[program_rule_engine_v2.py](program_rule_engine_v2.py)。
- 稳定回落、双平台融合与主题候选：[dual_platform_program.py](dual_platform_program.py)。
- 更换游戏、直播间表情和栏目词典：[个性化配置指南](docs/customization.md)。

## 可运行的处理链

```mermaid
flowchart TD
    A[已封口录像 + B 站 XML] --> C[时钟对齐与 UID 刷屏过滤]
    B[可选抖音弹幕] --> C
    C --> D[密度、独立人数、反应词与历史基线]
    D --> E[候选时间线与平台证据]
    E --> F[反应回落、可选语音断句、音频弱谷]
    F --> G[剪辑、转场与来源时间映射]
    G --> H[无弹幕下饭片段]
    G --> I[弹幕重映射与 ASS 烧录]
    I --> J[有弹幕下饭片段]
    H --> K[全包、时长与音视频边界校验]
    J --> K
    K --> L[本地成片 + 持久化回执]
    L -. 指定 BV 并显式执行 .-> M[biliup 追加上传与 CID 核对]
```

每个本地任务处理一份录像/XML。跨日归属、多 P 重排与替换另有纯函数适配层；本地入口不会自动执行这些编辑操作，也不包含生产环境的后台调度服务。

## 快速跑通

需要 **Python 3.9+、FFmpeg 和 ffprobe**。FFmpeg 需包含 `libx264`、AAC、`lavfi` 和 `libass`；Whisper 为可选依赖。

```powershell
git clone https://github.com/Assumine/danmaku-highlight-pipeline.git
cd danmaku-highlight-pipeline
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

# 生成测试视频、虚构用户弹幕，并执行双版本出片
danmaku-pipeline --demo --workspace ./outputs/demo --preview-height 360

python -m unittest discover -s tests -q
```

Linux/macOS 激活虚拟环境使用 `source .venv/bin/activate`。

合成样例是一段 **120 秒的动态图案和正弦音轨**，中间加入虚构账号的集中笑声反应。输出是按候选边界剪好的两版成片，而不是简单复制原视频。CI 在 Python 3.9 和 3.13 上运行单元测试及 FFmpeg 样例，并要求产生两份成片。

FFmpeg 不在 `PATH` 时可指定：

```powershell
$env:FFMPEG_BIN = 'C:\ffmpeg\bin\ffmpeg.exe'
$env:FFPROBE_BIN = 'C:\ffmpeg\bin\ffprobe.exe'
```

## 处理自己的素材

```powershell
danmaku-pipeline --video ./input/session.mp4 --xml ./input/session.xml --workspace ./outputs/session
```

- `--douyin <目录> --room-url <本地直播间地址>`：按时间戳关联旁路弹幕。
- `--speech whisper`：语音边界精修，先安装 `pip install -e ".[speech]"`。
- `--preview-height 360`：低分辨率试跑；正式出片不传此参数。

只需要某个阶段，也可以使用：

```powershell
danmaku-program ./input/session.xml --no-clipboard --no-pause
danmaku-highlights --help
danmaku-audit --help
```

## 结果可以追溯

```text
workspace/
  records/<content-key>/
    job.json                  # 状态、输入/代码身份、输出哈希
    analysis/                 # 候选时间线、对齐报告、拒绝证据
    highlight-*/manifest.json # 最终边界、转场、来源时间映射
    montage.xml               # 映射到成片时钟的弹幕
    montage.ass               # 实际烧录的弹幕轨
  outputs/<content-key>/
    highlight-*.mp4           # 无弹幕版与有弹幕版
```

- **不是按总弹幕数机械选片**：独立账号、刷屏限制、反应类型与历史密度共同影响候选。
- **不是固定截取一段就结束**：边界结合群体反应、可选语音断句和音频变化，具体依据进入 manifest。
- **两版共用剪辑时间轴**：按转场偏移重映射源弹幕，不重新选择片段。ASS 使用顶部五条滚动轨，溢出丢弃并记录数量。
- **失败记录不被覆盖**：相同输入命令可接续，已完成输出检查哈希和媒体完整性，失败的剪辑尝试保留独立目录。
- **变更不静默复用**：输入、参数或代码改变会得到新的任务标识。已有成片被改动时停止，而不是直接当作可信缓存。

## 投稿需要显式同意

`danmaku-submit` 使用调用方本地安装的 biliup 1.2.6 和独立 Cookie 文件，只向**明确指定的 BV** 追加两版下饭片段。

```powershell
# 默认只展示计划，不访问账号、不发送请求
danmaku-submit ./outputs/session/records/<content-key>/job.json --target <BV> --biliup ./tools/biliup.exe --cookies ./private/cookies.json
```

检查目标后，加 `--execute` 才实际执行；`--wait-seconds 120` 可等待平台就绪。

追加前保存已存在 CID，追加后重新查询稿件。CLI 报错但新 CID 已出现时，不盲目重传；中断结果无法确认时停止等待人工核对。同名分 P 已存在且无对应回执，也不会自动覆盖。

**该入口不删除、重排或替换远端分 P。** 载荷构造见 [接口文档](docs/integration.md)，由部署方审核和执行。公开测试不会连接真实账号。

## 代码入口

| 模块 | 实际职责 |
|---|---|
| [pipeline.py](pipeline.py) | 本地端到端执行、双版本出片、合成样例与接续 |
| [pipeline_runtime.py](pipeline_runtime.py) | 原子 JSON 回执、SHA-256、跨平台进程锁 |
| [montage_danmaku.py](montage_danmaku.py) | 转场时钟映射、ASS 生成、轨道溢出控制 |
| [submission_worker.py](submission_worker.py) | 显式追加、CID 核对、中断去重和转码状态 |
| [program_rule_engine_v2.py](program_rule_engine_v2.py) | 滑动窗口、基线、多人反应和候选评分 |
| [dual_platform_program.py](dual_platform_program.py) | 录像时钟、双平台证据、稳定区间与候选融合 |
| [program_source_analysis.py](program_source_analysis.py) | 展示节目单主题及来源证据 |
| [highlight_editor.py](highlight_editor.py) | 边界精修、音频边缘、转场、来源角标与渲染 |
| [media_integrity.py](media_integrity.py) | 全包扫描、时间戳、时长与音视频边界检查 |
| [biliup_integration.py](biliup_integration.py) | biliup 命令、直播日、多 P 编辑载荷纯函数 |
| [program_extractor.py](program_extractor.py) / [program_audit.py](program_audit.py) | 节目单与只读审计命令行 |
| [danmaku_uid.py](danmaku_uid.py) | 账号身份有效性及短窗刷屏过滤 |

## 文档与边界

- [运行、接续与投稿](docs/running.md)
- [算法架构](docs/architecture.md)
- [录制与投稿接口](docs/integration.md)
- [换直播间前的校准](docs/customization.md)
- [误判、延迟与能力限制](docs/limitations.md)
- [安全与数据处理](SECURITY.md)

本仓库不包含真实弹幕、录像、生产账号、主播配置或历史稿件记录。生成的任务、日志和回执可能含本地路径、观众信息、BV/CID，必须留在本地。默认忽略规则不能替代发布前审查。

合成样例验证执行链，不证明算法理解了“好笑”；自动校验不能替代内容抽检、版权核对和平台规则审核。当前公开实现没有后台定时调度、自动录制、坏素材修复或跨日自动合并。

许可证：[MIT](LICENSE)。biliup、FFmpeg、faster-whisper 等外部工具使用各自许可证，需单独安装；本项目不包含这些工具代码，也不是其官方组件，见 [NOTICE.md](NOTICE.md)。
