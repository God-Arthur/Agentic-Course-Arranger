r"""把 docs/*.md 转成适合 A4 打印的 HTML（仅标准库，不引入新依赖）。

出 PDF 的下一步（本机有 xelatex 但无 pandoc，md→tex 得手写转换器，且 xeCJK 会丢 ①④⑥ ≤ 这类字符；
因此用 Edge 无头打印）：
  1) .venv/bin/python tools/md_to_pdf.py docs/技术路线.md /tmp/x.html 技术路线
  2) 在 Windows 侧执行：
     "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless --disable-gpu \n       --no-pdf-header-footer --user-data-dir=%TEMP%\edge_pdf_profile \n       --print-to-pdf=<输出.pdf> file://<x.html 的绝对路径>
  3) 用 pdftotext 反查文本与页数（本机有 poppler），不要只看文件存在。

用法：python md2html.py 输入.md 输出.html [标题]
支持范围就是本项目文档用到的语法：标题、段落、引用块、有序/无序列表、表格、
围栏代码块、行内 `code`、**粗体**、[链接](url)、分隔线。
"""
import html
import re
import sys
from pathlib import Path

CODE_TOKEN = "\x00{}\x00"


def inline(text):
    out = html.escape(text, quote=False)
    spans = []

    def stash(match):
        spans.append(match.group(1))
        return CODE_TOKEN.format(len(spans) - 1)

    out = re.sub(r"`([^`]+)`", stash, out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', out)
    for index, span in enumerate(spans):
        out = out.replace(CODE_TOKEN.format(index), f"<code>{span}</code>")
    return out


def split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(line):
    return bool(re.fullmatch(r"\|?[\s:|-]+\|?", line)) and "-" in line


def render(lines):
    parts = []
    index = 0
    n = len(lines)
    while index < n:
        raw = lines[index]
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            index += 1
            body = []
            while index < n and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            parts.append("<pre>" + html.escape("\n".join(body), quote=False) + "</pre>")
            continue
        if not stripped:
            index += 1
            continue
        if re.fullmatch(r"-{3,}", stripped):
            parts.append("<hr>")
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            parts.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < n and is_separator(lines[index + 1]):
            header = split_row(stripped)
            index += 2
            rows = []
            while index < n and lines[index].strip().startswith("|"):
                rows.append(split_row(lines[index].strip()))
                index += 1
            thead = "".join(f"<th>{inline(cell)}</th>" for cell in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            parts.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>")
            continue
        if stripped.startswith(">"):
            block = []
            while index < n and lines[index].strip().startswith(">"):
                block.append(lines[index].strip().lstrip(">").strip())
                index += 1
            paras = []
            current = []
            for item in block:
                if item:
                    current.append(item)
                elif current:
                    paras.append(" ".join(current))
                    current = []
            if current:
                paras.append(" ".join(current))
            body = "".join(f"<p>{inline(par)}</p>" for par in paras)
            parts.append(f"<blockquote>{body}</blockquote>")
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet or number:
            ordered = number is not None
            items = []
            pattern = r"^\d+[.)]\s+(.*)$" if ordered else r"^[-*]\s+(.*)$"
            while index < n:
                current = lines[index].strip()
                match = re.match(pattern, current)
                if not match:
                    if (current and items and not current.startswith(("|", ">", "#", "```", "- ", "* "))
                            and not re.match(r"^\d+[.)]\s", current)):
                        items[-1] += " " + current
                        index += 1
                        continue
                    break
                items.append(match.group(1))
                index += 1
            tag = "ol" if ordered else "ul"
            parts.append(f"<{tag}>" + "".join(f"<li>{inline(item)}</li>" for item in items) + f"</{tag}>")
            continue
        para = []
        while index < n:
            current = lines[index].strip()
            if not current or current.startswith(("|", ">", "#", "```", "- ", "* ")) or re.match(r"^\d+[.)]\s", current):
                break
            para.append(current)
            index += 1
        if para:
            parts.append("<p>" + inline(" ".join(para)) + "</p>")
        else:
            index += 1
    return "\n".join(parts)


CSS = """
@page { size: A4; margin: 14mm 12mm; }
body { font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
       font-size: 10pt; line-height: 1.48; color: #111; max-width: none; margin: 0; }
h1 { font-size: 16pt; margin: 0 0 8px; }
h2 { font-size: 12.5pt; margin: 13px 0 4px; padding-bottom: 2px; border-bottom: 1px solid #bbb; }
h3 { font-size: 11pt; margin: 10px 0 3px; }
h2, h3 { page-break-after: avoid; }
p { margin: 4px 0; }
blockquote { margin: 6px 0; padding: 4px 9px; border-left: 3px solid #888;
             background: #f6f6f6; color: #333; }
blockquote p { margin: 2px 0; }
ul, ol { margin: 4px 0 4px 20px; padding: 0; }
li { margin: 1px 0; }
hr { border: none; border-top: 1px solid #ccc; margin: 10px 0; }
code { font-family: Consolas, "Courier New", monospace; font-size: 9pt;
       background: #f0f0f0; padding: 0 3px; border-radius: 2px; }
pre { font-family: Consolas, "Courier New", monospace; font-size: 6.6pt; line-height: 1.25;
      white-space: pre; background: #fafafa; border: 1px solid #ddd; padding: 7px 9px;
      overflow: visible; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 8.4pt; margin: 6px 0; }
th, td { border: 1px solid #999; padding: 2px 5px; text-align: left; vertical-align: top; }
th { background: #efefef; }
td:empty, th:empty { border: none; background: none; }
tr { page-break-inside: avoid; }
a { color: #14497d; text-decoration: none; }
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title><style>{css}</style></head>
<body>{body}</body></html>
"""


def main(argv):
    source = Path(argv[0])
    target = Path(argv[1])
    title = argv[2] if len(argv) > 2 else source.stem
    body = render(source.read_text(encoding="utf-8").splitlines())
    target.write_text(PAGE.format(title=html.escape(title), css=CSS, body=body), encoding="utf-8")
    print(f"OK {source} -> {target} ({len(body)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
