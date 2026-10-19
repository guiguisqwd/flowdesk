"""The step README one-liner: the "> sentence" right under the "# title" of a plan.

A step's README is its plan (projects/<project>/plans/<module id>.md). The home card
shows the one sentence under the first-line "# title"; anything that breaks the rule
reads as "none" and the card stays empty. Parsing happens only here.

Self-check for agents editing plans: python -m workbench.readme <file...>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LIMIT = 30
BOM = "﻿"
_EOL = re.compile(r"\r\n|\r|\n")
_KEEP = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")
H1 = re.compile(r" {0,3}#[ \t]+\S")
SECTION = re.compile(r" {0,3}#{1,6}[ \t]+\S")
HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")
QUOTE = re.compile(r" {0,3}>[ \t]?(.*)")
_CONTROL = re.compile("[\x00-\x1f\x7f\x85  ]")
PROBLEMS = {"no_title": "开头没有「# 标题」这一行", "no_quote": "标题下第一行不是「> 一句话」",
            "empty": "「>」后面是空的", "two_lines": "一句话写成了不止一行",
            "markdown": "一句话里有 Markdown 格式（嵌套引用、粗体、代码、链接或 HTML）",
            "note": "标题下第一行是注意/提示，不是这一步的概括", "multi_sentence": "一句话里不止一句",
            "bad_end": "句末只能是「。」或不加标点", "control": "一句话里有换行符、制表符等控制字符",
            "too_long": "一句话超过 30 字"}


def _blank(line):
    return not line.strip(" \t\r\n")


def read_step_summary(markdown, heading_line=None):
    """Return (sentence, None) or (None, problem code).

    heading_line=None: a single README whose first non-blank line must be "# title".
    heading_line=n: in a multi-step document, line n (split on \\n, \\r\\n or \\r,
    counted from 1) is this step's section heading, of any level."""
    lines = _EOL.split(markdown.removeprefix(BOM))
    if heading_line is None:
        heading_line = next((n for n, line in enumerate(lines, 1) if not _blank(line)), 0)
        if not heading_line or not H1.match(lines[heading_line - 1]):
            return None, "no_title"
    elif not (1 <= heading_line <= len(lines) and SECTION.match(lines[heading_line - 1])):
        return None, "no_title"
    rest = lines[heading_line:]
    i = next((k for k, line in enumerate(rest) if not _blank(line)), None)
    quote = QUOTE.fullmatch(rest[i]) if i is not None else None
    if not quote:
        return None, "no_quote"
    after = rest[i + 1] if i + 1 < len(rest) else ""
    if not _blank(after) and not HEADING.match(after):
        return None, "two_lines"
    text = quote.group(1).strip()
    if text.endswith("。"):
        text = text[:-1].rstrip()
    if not text:
        return None, "empty"
    if _CONTROL.search(text):
        return None, "control"
    if text[0] in ">＞" or re.search(r"[*`<]|\]\(", text):
        return None, "markdown"
    if re.match(r"(注意|提示|说明|警告|示例)\s*[:：]", text):
        return None, "note"
    if re.search(r"[。！？!?；;]", text):
        return None, "multi_sentence"
    if text[-1] in "，、：…,:":
        return None, "bad_end"
    if len(text) > LIMIT:
        return None, "too_long"
    return text, None


def step_summary(markdown, heading_line=None):
    return read_step_summary(markdown, heading_line)[0]


def readme_head(markdown):
    summary, code = read_step_summary(markdown)
    return {"summary": summary, "problem": PROBLEMS[code] if code else None}


def split_head(plan):
    """(head, body). head = "# title" + blank lines + the ">" lines right after it
    (valid or not) + blank lines. Only lines that are themselves ">" lines count: the
    first line that is not one (a lazy continuation, a numbered step, 目的：…) starts the
    body. When the first non-blank line is not "# title", the head is empty and the body
    is the plan unchanged, character for character."""
    bom = BOM if plan.startswith(BOM) else ""
    parts = _KEEP.findall(plan[len(bom):])
    k = 0
    while k < len(parts) and _blank(parts[k]):
        k += 1
    if k == len(parts) or not H1.match(parts[k]):
        return "", plan
    k += 1
    while k < len(parts) and _blank(parts[k]):
        k += 1
    while k < len(parts) and QUOTE.fullmatch(parts[k].rstrip("\r\n")):
        k += 1
    while k < len(parts) and _blank(parts[k]):
        k += 1
    return bom + "".join(parts[:k]), "".join(parts[k:])


def main(argv=None):
    paths = sys.argv[1:] if argv is None else list(argv)
    if not paths:
        print("用法：python -m workbench.readme <文件…>", file=sys.stderr)
        return 2
    missing = 0
    for name in paths:
        try:
            # Decode the bytes ourselves so \r and \r\n reach the parser unchanged.
            text = Path(name).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"{name}: 读不到（unreadable）{type(error).__name__}")
            missing += 1
            continue
        summary, code = read_step_summary(text)
        if summary is None:
            print(f"{name}: 读不到（{code}）{PROBLEMS[code]}")
            missing += 1
        else:
            print(f"{name}: {summary}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
