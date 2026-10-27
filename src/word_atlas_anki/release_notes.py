import html
from datetime import datetime, timezone

from .constants import LEVELS, PROJECT_REPO, UPSTREAM_REPO

FIELD_LABELS = {
    "AtlasID": "身份", "Word": "词头", "IPAUK": "英式音标", "IPAUS": "美式音标", "CEFR": "等级", "Topic": "话题",
    "Image": "插画", "Senses": "词性及全部释义", "Example1": "第一条例句", "Translation1": "第一句译文",
    "Example2": "第二条例句", "Translation2": "第二句译文", "AudioUK": "英音", "AudioUS": "美音",
    "AudioExample1": "第一句录音", "AudioExample2": "第二句录音", "Collocations": "搭配", "Family": "词族",
    "Synonyms": "近义词", "Antonyms": "反义词", "Tip": "提示", "UsageNote": "用法提醒", "Source": "来源说明",
}
MEDIA_LABELS = {"img": "插画", "uk": "英音单词", "us": "美音单词", "ex": "第一句录音", "ex2": "第二句录音"}


def md(value):
    value = html.escape(str(value).replace("\n", " ").replace("\r", " "))
    for char in ("\\", "`", "*", "_", "[", "]", "|", "#"):
        value = value.replace(char, "\\" + char)
    return value[:180]


def size(value):
    return f"{value / 1024**2:,.2f} MiB"


def release_notes(manifest, changes):
    date = datetime.fromtimestamp(manifest["created_at"], timezone.utc).strftime("%Y-%m-%d")
    title = "全量初版" if changes["initial"] else "增量更新"
    if not manifest["full"]:
        title = "不完整测试小样，禁止作为正式全量发布"
    lines = [f"# Word Atlas Anki | {date} | {title}", "",
             f'输入版本：`{manifest["input_fingerprint"]}`。一词一条笔记、一张正向卡，无作者复习记录。', ""]
    if manifest["channel"] != "stable":
        lines += ["**预发布：桌面、Android、iOS 的实机音频互斥与升级验收尚未全部完成。浏览器和后端测试不等于三端实机通过。**", ""]
    lines += ["## 本次变化", "",
              f'- 新增 {len(changes["added"]):,} 词，修改 {len(changes["modified"]):,} 词，经确认删除 {len(changes["confirmed_deleted"]):,} 词。',
              f'- 卡片模板变化：{"是" if changes["template_changed"] else "否"}；本地构建/资源输入变化：{"是" if changes["builder_changed"] else "否"}。']
    if changes["field_changes"]:
        lines.append("- 修改字段：" + "；".join(f"{FIELD_LABELS[k]} {v}" for k, v in sorted(changes["field_changes"].items())) + "。")
    else:
        lines.append("- 初版包含全部字段。" if changes["initial"] else "- 没有已有词条的字段修改。")
    lines.append("- 已有词条媒体替换/增减：" + "；".join(f"{label} {changes['media_changes'].get(kind, 0)}" for kind, label in MEDIA_LABELS.items()) + "。新增词条的媒体另计入下方覆盖数。")
    for key, label in (("added", "新增示例"), ("modified", "修改示例"), ("confirmed_deleted", "确认删除示例")):
        if changes[key]:
            lines.append(f"- {label}：" + "、".join(md(row["word"]) for row in changes[key][:12]) + "。")
    lines.append("- 完整逐词差异、字段和分类变化见附件 `changes.json`。")
    if changes["previous_upstream_sha"]:
        lines.append(f'- [上游比较]({UPSTREAM_REPO}/compare/{changes["previous_upstream_sha"]}...{manifest["upstream_sha"]})。')
    else:
        lines.append(f'- [上游固定提交]({UPSTREAM_REPO}/tree/{manifest["upstream_sha"]})。')
    lines += ["", "## 内容与下载", "",
              f'- 本包 {manifest["note_count"]:,} 条笔记，来源全集 {manifest["upstream_count"]:,} 词；{"全量" if manifest["full"] else "不完整小样"}。',
              "- 等级：" + "；".join(f'{level} {manifest["level_counts"][level]:,}' for level in LEVELS) + "。",
              "- 覆盖：" + "；".join(f'{label} {manifest["coverage"][kind]:,}' for kind, label in MEDIA_LABELS.items()) + "。",
              f'- 去重后共 {manifest["media_count"]:,} 个离线媒体/字体/控制/许可文件，媒体字节总计 {size(manifest["media_bytes"])}。',
              "- 下列 `.apkg` 都可独立导入。若提供分包，请全部导入；不是字节切片，仍汇入同一套六个子牌组。", "",
              "| 附件 | 笔记 | 体积 | SHA-256 |", "| --- | ---: | ---: | --- |"]
    for part in manifest["packages"]:
        lines.append(f'| `{part["name"]}` | {len(part["notes"]):,} | {size(part["bytes"])} | `{part["sha256"]}` |')
    lines += ["", "所有附件的校验值见 `SHA256SUMS`；来源、每条笔记修改时间和媒体摘要见 `manifest.json`。", "",
              "## 导入与升级", "",
              "1. 先备份 Anki 集合和媒体，然后直接导入新版 `.apkg`，不要先删除旧牌组。",
              "2. 不导入作者学习进度。现代导入器使用“笔记和笔记类型：如较新则更新”；本仓库使用固定 GUID 和固定笔记类型身份。",
              "3. 若你在本地修改过源字段，较新的本地编辑可能阻止默认更新。需要强制采用词库版本时，可备份后选“始终更新笔记”，这会覆盖你对相应字段的编辑。稳定 GUID 不保证保留被覆盖字段。",
              "4. 已有卡的复习、挂起状态和用户移动的牌组位置不强制覆盖。等级或话题变动以 CEFR/Topic 字段为准。Anki 26.9.3 后端实测更新笔记会替换标签，包括移除自定义标签；请先备份重要标签。其他客户端/版本可能合并标签，不保证自动删除旧分类标签。",
              "5. 需要整理旧分类标签时，在浏览器按当前 CEFR/Topic 字段筛选，核对后再手动整理标签或移牌组。不要为同步分类而删卡重建。",
              "6. `.apkg` 不会自动删除上游已删除的旧卡。确认删除列表见差异附件，是否保留由你决定。",
              "7. 媒体替换使用新的内容寻址文件名。旧媒体清理前先备份，再使用 Anki 的“检查媒体”；不要按文件日期盲删。",
              "8. 仅保证本仓库版本之间的升级契约，不迁移任意旧 CSV 牌组；不同设备导入/同步完成后再离线学习。", "",
              "许可原文保留在笔记类型的样式注释和许可附件中，不占用复习卡面。", "",
              "## 音频与兼容性", "",
              "- 正反面始终分别显示 UK、US 音标；缺失侧明确标识，不复制另一侧冒充。没有对应录音时不显示无效按钮。",
              "- 正面仅自动播放一次英音；反面自动顺序为英音单词、第一句。美音单词和第二句只在手动点击时播放。第二句是不同例句，不是第一句的另一口音。",
              "- 需在 Anki 中开启牌组自动播放、显示重播按钮，并启用反面重播时跳过正面音频的选项。模板不会修改你的全局 FSRS、学习步长或牌组预设。",
              "- 隐藏的短静音 WAV 是中断原生队列的控制项，不是额外可听内容。手动播放与原生队列互斥依赖客户端的原生重播行为，尚未实测的客户端不作保证。",
              "- 跟随 Anki 自身的明暗模式：浅色暖纸，深色保留星光主题配色并使用纯色背景。难度标签和播放按钮按 A1/A2 绿、B1/B2 蓝、C1/C2 红区分。英文使用系统无衬线字体，字形可能随设备略有不同。长词条保持可读字号并允许纵向滚动；不承诺 AnkiWeb 兼容。"]
    for client, evidence in manifest["client_evidence"].items():
        lines.append(f'- {client} 实机证据：' + (f'{md(evidence["version"])}，{md(evidence["tested_at"])}，{md(evidence["record"])}。' if evidence else "未验证，未记录已测试版本。"))
    lines += ["", "## 来源与授权", "",
              f'- [Word Atlas contributors]({UPSTREAM_REPO})：词库、插画和成品录音为 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，源码和界面设计为 MIT。',
              f'- [{PROJECT_REPO.split("/")[-1]}]({PROJECT_REPO}) 是非官方适配，修改包括 Anki 排版、英美双音标显示、离线媒体命名与播放策略；未重生成文本、插画或语音。',
              "- 随包保留的原始 Fraunces 字体资源为 OFL 1.1，当前卡面不选用该衬线字体；Lucide 图标保留其 ISC/MIT 许可。许可原文随仓库和牌组媒体提供。",
              "- 上游内容由 AI 生成、未经逐条人工审校，按现状提供。录音授权不包含声音模型或声音身份，不代表声音提供方背书；本项目也非官方 IELTS 词表。", "",
              "## 自动更新边界", "",
              "GitHub Actions 每日检查有效输入变化，未变化不发布空版本；许可变化、异常删除/重命名和校验失败会阻止发布。稳定版需要三端实机证据，当前预发布不会冒充稳定版或更新稳定版 latest。",
              "GitHub 可能在公共仓库长期无活动后停用定时工作流；维护者可在 Actions 页面重新启用工作流并使用 Run workflow 手动触发。不承诺无限期无人维护。", ""]
    return "\n".join(lines)
