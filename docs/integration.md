# 录制与投稿接口

本仓库提供算法和无凭据的投稿接口适配。录制器、上传 worker、Cookie 文件、HTTP 会话和任务队列由部署方管理；公开仓库的测试不会连接 B 站。

## biliup 1.2.6

录制器保存视频与 B 站弹幕 XML，配置 `uploader = "Noop"`，使录制监控不等待本地压制和上传。外部 worker 在录制文件封口后进行预合并、弹幕压制、剪辑和媒体校验，再使用 biliup 1.2.6 命令行投稿。`biliup_integration.BiliupCLI` 构造 `list`、`show`、`upload` 和 `append` 的参数数组，调用方可直接交给 `subprocess.run`。新建与追加均使用 `--submit web`；新建还带 `--charging-pay 0`。分类和标签由部署方提供，不写入仓库。

```python
from pathlib import Path
from biliup_integration import BiliupCLI

cli = BiliupCLI(Path("biliup.exe"), Path("cookies.json"))
command = cli.upload(
    title="Example recording",
    description="",
    tags="game,stream",
    category_id=171,
    video_paths=[Path("recording.flv")],
)
```

命令数组只用于展示接口；Cookie 文件必须由部署方单独保管。仓库不包含可用账号或上传配置。`parse_list_output()` 会去除颜色码并提取 BV 号与标题；`parse_show_output()` 从诊断文本后的 JSON 读取稿件信息。调用方应先检查进程退出码，再解析输出。

## 本地 P 号与直播日

`broadcast_day()` 以 07:00 为分界。`ordered_source_inputs()` 使用录制日期和文件名中的场次时间排序，给各场次赋源 P 号。节目单与剪辑均使用这个本地 P 号，而不是读取线上分 P 当前顺序来反推。

`desired_remote_parts()` 将已上传视频固定排列为：

```text
无弹幕 19 时场、无弹幕 23 时场、有弹幕 19 时场、有弹幕 23 时场、下饭片段
```

有弹幕分 P 尚未出现时允许暂缺；无弹幕原片缺失、同名或出现本地未登记的原片时停止编辑。其他非回放分 P 保留在下饭片段之前。`archive_edit_payload()` 使用当前稿件的 `archive` 信息和顺序构造 `videos` 列表，提交后还需重新读取线上 CID 顺序验证。

## 汇总与替换

同一直播日的新场次上传后，重新汇总当日原片、节目单与已剪片段。新下饭片段先追加，再等待 `status=0` 且 `xcode_state=6`；`replacement_payload()` 此时才可生成删除旧片段、保留一个新片段的编辑载荷。原片不可读时，节目单按现有本地 P 分列，不以线上顺序推断缺失原片。

稿件编辑请求使用 `POST https://member.bilibili.com/x/vu/web/edit`，提交包含 `videos` 的 JSON `archive`，并携带投稿账号的 CSRF。待转码分 P 可以先尝试重排；若平台拒绝，等待其转码就绪后重试。无论接口返回什么，都应核对线上最终 CID 顺序。节目单评论在发布前查询当前稿件，仅删除本账号以前发布的节目单评论；评论提交和删除由部署方处理。

这些接口受平台控制，字段和权限可能变化。不要把 Cookie、CSRF、直播间 ID、稿件 ID 或原始观众数据提交到公开仓库。
