"""原神琴「键盘谱」→ 我们的 notes 字符串。

原神琴是 21 键、C 大调、三排：
    Z X C V B N M  →  C3 D3 E3 F3 G3 A3 B3
    A S D F G H J  →  C4 D4 E4 F4 G4 A4 B4
    Q W E R T Y U  →  C5 D5 E5 F5 G5 A5 B5

谱面格式（UP 原话）：`/` 是小节线，括号里是一起按的。

⚠ 我们的乐器是单声部，所以每个和弦只取一个音。默认取**最高的那个**——
旋律通常在最高声部；如果出来的东西不像旋律，改 --pick lowest 或 --pick melody。
"""

from __future__ import annotations

import argparse
import re
import sys

LOW = "ZXCVBNM"
MID = "ASDFGHJ"
HIGH = "QWERTYU"
# ⚠ 三排都是**七声音阶**，不是半音阶：每个八度只有 C D E F G A B。
# 一开始按 48+i 铺，结果 Z=C3、X=C#3、C=D3 —— 整首会跑调，还听不出错在哪。
DEGREE = [0, 2, 4, 5, 7, 9, 11]

KEY_TO_MIDI: dict[str, int] = {}
for i, ch in enumerate(LOW):
    KEY_TO_MIDI[ch] = 48 + DEGREE[i]      # C3 起
for i, ch in enumerate(MID):
    KEY_TO_MIDI[ch] = 60 + DEGREE[i]      # C4 起
for i, ch in enumerate(HIGH):
    KEY_TO_MIDI[ch] = 72 + DEGREE[i]      # C5 起

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def name_of(midi: int) -> str:
    return f"{NAMES[midi % 12]}{midi // 12 - 1}"


def parse(text: str) -> list[list[int]]:
    """把一个小节解析成一串「事件」，**保持书写顺序**。

    一个事件 = 一个和弦（取最高音）或一个单音。
    ⚠ 早期版本把整个小节压成一个音，64 小节只剩 64 个音、每个拖 4 拍——
      听起来像骨架不像旋律。这里必须保留小节内所有事件。
    """
    bars: list[list[int]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not re.search(r"[A-Z]", line):
            continue
        for chunk in line.split("/"):
            chunk = chunk.strip()
            if not chunk:
                continue
            events: list[int] = []
            i = 0
            while i < len(chunk):
                ch = chunk[i]
                if ch == "(":
                    j = chunk.find(")", i + 1)
                    if j < 0:
                        j = len(chunk)
                    group = chunk[i + 1:j]
                    picked = [KEY_TO_MIDI[c] for c in group if c in KEY_TO_MIDI]
                    if picked:
                        events.append(max(picked))
                    i = j + 1
                    continue
                if ch in KEY_TO_MIDI:
                    events.append(KEY_TO_MIDI[ch])
                i += 1
            if events:
                bars.append(events)
    return bars


def to_notes(bars: list[list[int]], beats_per_bar: float = 4.0,
             pick: str = "highest") -> str:
    """一小节平均分成 beats_per_bar 拍。"""
    out: list[str] = []
    for bar in bars:
        events = bar
        if pick == "lowest":
            events = [min(bar)]
        elif pick == "first":
            events = [bar[0]]
        beat = beats_per_bar / max(1, len(events))
        out.append(" ".join(f"{name_of(m)}:{beat:g}" for m in events))
    return " / ".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="谱面 txt（评论里复制下来的那种）")
    ap.add_argument("--bars", type=int, default=0, help="只取前几小节，0 = 全部")
    ap.add_argument("--beats", type=float, default=4.0, help="每小节几拍")
    ap.add_argument("--pick", default="highest", choices=["highest", "lowest", "first"])
    args = ap.parse_args()

    with open(args.path, "r", encoding="utf-8") as fh:
        text = fh.read()
    bars = parse(text)
    if args.bars > 0:
        bars = bars[: args.bars]
    print(f"小节数 {len(bars)}")
    for i, bar in enumerate(bars[:8], 1):
        print(f"  第{i}小节: " + " ".join(name_of(m) for m in bar))
    print("\nnotes:")
    print(to_notes(bars, args.beats, args.pick))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()


# ---------------------------------------------------------------- 数字谱

def parse_numbered(text: str) -> list[list[int]]:
    """数字谱：`-1` = 低音排的 1，`+5` = 高音排的 5，不带符号是中间排。

    ⚠ `(-33)` 是两个音（-3 和 3），不是三十三；`(-257)` 是三个音。
      所以括号里也要逐个数字解析，不能当整数。
    """
    bars: list[list[int]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not re.search(r"[1-7]", line):
            continue
        for chunk in line.split("/"):
            chunk = chunk.strip()
            if not chunk:
                continue
            notes: list[int] = []
            for sign, digit in re.findall(r"([+-]?)([1-7])", chunk):
                base = 48 if sign == "-" else (72 if sign == "+" else 60)
                notes.append(base + DEGREE[int(digit) - 1])
            if notes:
                bars.append(notes)
    return bars