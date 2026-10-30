"""确定性报告与条目计数 SVG：不编造性能数据。"""

from collections import Counter
from html import escape

LABELS = {
    "model_serving": "模型服务",
    "manufacturing_embodied_training": "制造业具身训练",
    "other_ai_infra": "其他直接相关基础设施",
}


def md(value):
    return escape(value).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("*", "\\*").replace("_", "\\_").replace("`", "\\`").replace("\n", " ")


def report(brief):
    lines = [f"# AI Infra 晨报 · {brief['date']}（模拟）", "", f"> {brief['warning']}", "",
             f"截至：{brief['as_of']}；时区：Asia/Shanghai；最终筛选窗口：{brief['window_hours']} 小时。", "",
             "![模拟筛选计数，不代表新闻全量](overview.svg)", ""]
    if not brief["items"]:
        lines += ["## 零项结果", "本次模拟输入没有合格新项。未做真实检索，不能据此判断今天没有新闻。", ""]
    for index, item in enumerate(brief["items"], 1):
        # 链接使用尖括号包裹，并转义分隔符，防止来源文本突破 Markdown 边界。
        url = item["source_url"].replace("<", "%3C").replace(">", "%3E").replace("\n", "%0A")
        lines += [f"## {index}. {md(item['title'])}",
                  f"**方向：** {LABELS[item['topic']]} · **状态：** 未核验模拟 fixture", "",
                  f"- **发生了什么：** {md(item['summary'])}",
                  f"- **与当前工作的关系：** {md(item['relevance'])}"]
        if item["limitations"].strip():
            lines.append(f"- **选型限制：** {md(item['limitations'])}")
        lines += [f"- **证据摘述：** {md(item['evidence'])}",
                  f"- **来源：** [{md(item['source_name'])}](<{url}>)（{item['source_type']}；发布 {item['published_at']}；读取 {item['retrieved_at']}）", ""]
    lines += ["## 运行说明", *[f"- {md(note)}" for note in brief["notes"]], "",
              "真实网络研究、模型研究/编辑、Slack 图文投递均待接入。本地模拟回执不代表真实送达。", ""]
    return "\n".join(lines)


def graphic(brief):
    counts = Counter(x["topic"] for x in brief["items"])
    rows = []
    for i, (topic, label) in enumerate(LABELS.items()):
        y = 132 + i * 70
        rows.append(f'<text x="32" y="{y}" font-size="18">{escape(label)}</text>')
        rows.append(f'<rect x="300" y="{y-21}" width="{counts[topic]*40}" height="28" rx="5" fill="#66d9c6"/>')
        rows.append(f'<text x="{315 + counts[topic]*40}" y="{y}" font-size="18">{counts[topic]} 项</text>')
    return '\n'.join([
        '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="390" viewBox="0 0 720 390" role="img" aria-labelledby="title desc">',
        '<title id="title">AI Infra 模拟筛选结果</title>',
        '<desc id="desc">仅显示模拟输入的条目计数，不是实测性能图，也不代表真实新闻数量。</desc>',
        '<rect width="720" height="390" fill="#122332"/>',
        '<g fill="#edf7fa" font-family="sans-serif">',
        f'<text x="32" y="44" font-size="25">AI Infra · {escape(brief["date"])} · 模拟</text>',
        f'<text x="32" y="77" font-size="15">筛选窗口 {brief["window_hours"]} 小时 · 来源未联网核验</text>',
        *rows,
        '<text x="32" y="332" font-size="15">模型服务优先；不假设跨模型共享权重。</text>',
        '<text x="32" y="359" font-size="15">本图仅为流程验证；不代表真实晨报或性能结论。</text>',
        '</g></svg>\n',
    ])
