import html

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
    lines = ["## 本次变化", "",
             f'- 词条：新增 {len(changes["added"]):,}，修改 {len(changes["modified"]):,}，确认删除 {len(changes["confirmed_deleted"]):,}。']
    updated = [label for key, label in (("template_changed", "卡片模板"), ("builder_changed", "构建流程")) if changes[key]]
    if updated:
        lines.append("- 更新：" + "、".join(updated) + "。")
    if changes["field_changes"]:
        lines.append("- 修改字段：" + "；".join(f"{FIELD_LABELS[k]} {v}" for k, v in sorted(changes["field_changes"].items())) + "。")
    media = [f"{label} {changes['media_changes'][kind]}" for kind, label in MEDIA_LABELS.items() if changes["media_changes"].get(kind)]
    if media:
        lines.append("- 已有词条媒体更新：" + "；".join(media) + "。")
    elif not changes["initial"]:
        lines.append("- 已有词条媒体无变化。")
    for key, label in (("added", "新增示例"), ("modified", "修改示例"), ("confirmed_deleted", "确认删除示例")):
        if changes[key]:
            lines.append(f"- {label}：" + "、".join(md(row["word"]) for row in changes[key][:3]) + "。")
    if changes["previous_upstream_sha"] and changes["previous_upstream_sha"] != manifest["upstream_sha"]:
        lines.append(f'- [上游变更]({UPSTREAM_REPO}/compare/{changes["previous_upstream_sha"]}...{manifest["upstream_sha"]})。')
    elif not changes["previous_upstream_sha"]:
        lines.append(f'- [上游快照]({UPSTREAM_REPO}/tree/{manifest["upstream_sha"]})。')
    lines += ["", "## 内容与下载", "",
              f'- {"全量" if manifest["full"] else "不完整小样"}：{manifest["note_count"]:,} 条笔记，来源全集 {manifest["upstream_count"]:,} 词，含离线插画与音频。',
              "- 等级：" + "；".join(f'{level} {manifest["level_counts"][level]:,}' for level in LEVELS) + "。"]
    if not manifest["full"]:
        lines.append("- 仅供测试，不可作为全量更新。")
    if manifest["channel"] != "stable":
        lines.append("- 预发布：尚未完成全部客户端实机验收。")
    if len(manifest["packages"]) > 1:
        lines.append("- 请导入全部分包，它们会汇入同一套牌组。")
    lines += ["", "| 文件 | 词条 | 大小 |", "| --- | ---: | ---: |"]
    for part in manifest["packages"]:
        lines.append(f'| `{part["name"]}` | {len(part["notes"]):,} | {size(part["bytes"])} |')
    if changes["initial"] and manifest["full"]:
        lines += ["", "## 导入与兼容", "",
                  "- 先备份集合与媒体，再导入 `.apkg`。更新时直接导入新版，不要先删除旧牌组；本地字段和标签的更新注意事项见 README。",
                  "- 开启自动播放和重播按钮，并设置反面重播时跳过正面音频。默认正面播放英音，反面播放英音与第一句；美音和第二句手动播放。",
                  "- 兼容 Anki Desktop、AnkiDroid、AnkiMobile 和 AnkiWeb。", "",
                  "## 来源与许可", "",
                  f"- 词库、插画和成品录音来自 [Word Atlas]({UPSTREAM_REPO})，采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)；本项目是非官方 Anki 适配。",
                  "- 转换代码采用 MIT，字体和图标保留各自许可。上游内容由 AI 生成，建议核对；完整许可随牌组保留。"]
    lines += ["", f"完整使用说明见 [README]({PROJECT_REPO})；校验值见 `SHA256SUMS`。", ""]
    return "\n".join(lines)
