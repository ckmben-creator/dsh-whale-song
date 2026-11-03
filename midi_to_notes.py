"""MIDI → notes 字符串（给 dsh-whale-song 用）。

为什么做这个：音频扒谱对和弦织体基本无效（试过三次），而 MIDI 本身就是音符数据，
不用猜音高；网上多数曲子都能找到 MIDI。这是「找歌」这条路上最省事的一环。

零依赖：自己读 SMF（Standard MIDI File）二进制，不装 mido。

用法：
    python midi_to_notes.py song.mid                    # 自动挑旋律轨
    python midi_to_notes.py song.mid --track 2          # 指定第 2 轨（从 0 数）
    python midi_to_notes.py song.mid --octave -12       # 整体降一个八度
    python midi_to_notes.py song.mid --list             # 只看有哪些轨
"""

from __future__ import annotations

import argparse
import struct
import sys

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def name_of(midi: int) -> str:
    return f"{NAMES[midi % 12]}{midi // 12 - 1}"


def read_varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, pos


def parse_smf(path: str) -> tuple[int, list[list[dict]], list[tuple[int, int]]]:
    """返回 (每四分音符的 tick 数, 每轨的音符列表, 速度变化列表)。"""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"MThd":
        raise ValueError("不是标准 MIDI 文件（缺 MThd）")
    hlen = struct.unpack(">I", data[4:8])[0]
    fmt, ntrks, division = struct.unpack(">HHH", data[8:14])
    pos = 8 + hlen

    tracks: list[list[dict]] = []
    tempos: list[tuple[int, int]] = []      # (tick, 每四分音符微秒)

    for _ in range(ntrks):
        if data[pos:pos + 4] != b"MTrk":
            break
        tlen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + tlen]
        pos += 8 + tlen

        tick = 0
        running = 0
        notes: list[dict] = []
        open_notes: dict[tuple[int, int], dict] = {}
        i = 0
        while i < len(body):
            delta, i = read_varlen(body, i)
            tick += delta
            status = body[i]
            if status == 0xFF:                     # meta
                i += 1
                mtype = body[i]
                i += 1
                mlen, i = read_varlen(body, i)
                payload = body[i:i + mlen]
                i += mlen
                if mtype == 0x51 and mlen == 3:
                    us = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                    tempos.append((tick, us))
                if mtype == 0x2F:
                    break
                continue
            if status in (0xF0, 0xF7):             # sysex
                i += 1
                ln, i = read_varlen(body, i)
                i += ln
                continue
            if status & 0x80:
                running = status
                i += 1
            else:
                status = running
            kind = status & 0xF0
            channel = status & 0x0F
            if kind in (0x80, 0x90):
                note = body[i]
                vel = body[i + 1]
                i += 2
                key = (channel, note)
                if kind == 0x90 and vel > 0:
                    open_notes[key] = {"note": note, "start": tick, "vel": vel}
                else:
                    info = open_notes.pop(key, None)
                    if info:
                        notes.append({**info, "end": tick})
            elif kind in (0xA0, 0xB0, 0xE0):
                i += 2
            elif kind in (0xC0, 0xD0):
                i += 1
            else:
                break
        for info in open_notes.values():
            notes.append({**info, "end": tick})
        tracks.append(notes)

    return division, tracks, tempos


def pick_track(tracks: list[list[dict]]) -> int:
    """默认旋律轨。

    规则（踩过坑）：
      - 跳过「单音轨」：整轨只有一个音高 = 鼓/打击乐，音高再高也不是旋律
        （29 轨的文件里就有一堆这样的轨，旧规则会挑中只有 14 个音的稀疏轨）
      - 跳过太短的轨（< 20 个音）
      - 剩下的取「中位音高最高」的那轨（旋律一般压在最上面）
    """
    best, best_score = -1, -1e9
    for idx, notes in enumerate(tracks):
        if len(notes) < 20:
            continue
        pitches = sorted(n["note"] for n in notes)
        if len(set(pitches)) < 5:          # 单音轨 = 鼓
            continue
        median = pitches[len(pitches) // 2]
        # 太高（中位 > 78，约 F#5）的轨多半是装饰音/副旋律，不是主旋律——
        # 25.mid 就是这么栽的：轨 6 中位 A#5 被选中，换到轨 1（D5）才是歌
        if median > 78:
            continue
        score = median
        if score > best_score:
            best, best_score = idx, score
    if best >= 0:
        return best
    # 全都不合格时退回旧规则
    for idx, notes in enumerate(tracks):
        if len(notes) >= 8:
            return idx
    return 0


def to_melody(notes: list[dict], division: int, quant: float = 0.25) -> list[tuple[int, float, float]]:
    """音符 → (midi, 拍数, 起点拍)，按起点排序；重叠时保留高的那个（伴奏丢掉）。"""
    def beats(ticks: int) -> float:
        return ticks / division

    events = []
    for n in notes:
        start = beats(n["start"])
        dur = max(quant, beats(n["end"] - n["start"]))
        events.append((start, dur, n["note"]))
    events.sort()

    out: list[tuple[int, float, float]] = []
    last_start, last_pitch = -1e9, -1
    for start, dur, pitch in events:
        if start < last_start + quant and pitch <= last_pitch:
            continue                      # 同一时刻更低的音，丢掉（当伴奏）
        q = max(quant, round(dur / quant) * quant)
        out.append((pitch, q, start))
        last_start, last_pitch = start, pitch
    return out


def breathe_bar(melody: list[tuple[int, float, float]], bar_len: float = 16.0,
                rest: float = 1.0) -> list[tuple[str, int, float]]:
    """按小节切句：每 bar_len 拍换一次气。

    为什么需要这个：很多 MIDI 是连奏（钢琴踩着踏板），音符之间没有空档，
    auto 模式切不出来；而流行曲多数是 4 小节一句，按时长切反而接近人的呼吸。
    """
    out: list[tuple[str, int, float]] = []
    boundary = bar_len
    for midi, dur, start in melody:
        out.append(("note", midi, dur))
        end = start + dur
        if end >= boundary:
            out.append(("rest", 0, rest))
            while boundary <= end:
                boundary += bar_len
    return out


def breathe_auto(melody: list[tuple[int, float, float]],
                 min_gap: float = 0.5, min_rest: float = 1.0) -> list[tuple[str, float]]:
    """按 MIDI 自己的间隙换气——比「音高跳进」准，因为间隙是编曲者写的。

    melody 里带起点，所以能算出两个音之间真正的空档：
      gap = 下一个音的起点 − (本音起点 + 本音时值)
    空档 ≥ min_gap 就当作换气，休止取 max(空档, min_rest)。
    """
    out: list[tuple[str, float]] = []
    for i, (midi, dur, start) in enumerate(melody):
        out.append(("note", midi, dur))
        if i + 1 < len(melody):
            nxt_start = melody[i + 1][2]
            gap = nxt_start - (start + dur)
            if gap >= min_gap:
                out.append(("rest", 0, max(min_rest, round(gap * 2) / 2)))
    return out


def _切句(melody: list[tuple[int, float, float]], long_note: float, leap: int,
          min_notes: int, max_notes: int) -> list[list[tuple[int, float, float]]]:
    """把旋律切成「句」——只看谱面能给的东西，不假装懂音乐。

    边界判据（按任务书）：
      · 长音后：本音时值 ≥ long_note 拍 → 强边界
      · 休止后：谱面本来就有空档 → 强边界
      · 音高反转且跨度 ≥ leap 半音 → 候选边界
    切完再做两件修补：太短的并到前句、太长的在最弱音程处再切一刀。
    """
    n = len(melody)
    if n == 0:
        return []

    边界 = [False] * n          # 边界[i] = 第 i 个音之后断句
    方向 = 0                    # 上一个非零方向：+1 上行 / -1 下行
    for i in range(n - 1):
        midi, dur, start = melody[i]
        nxt_midi, _, nxt_start = melody[i + 1]
        强 = False
        if dur >= long_note:
            强 = True
        gap = nxt_start - (start + dur)
        if gap > 0.05:
            强 = True
        step = nxt_midi - midi
        if step != 0:
            if 方向 and (step > 0) != (方向 > 0) and abs(step) >= leap:
                强 = True
            方向 = 1 if step > 0 else -1
        边界[i] = 强

    # 1) 先切
    段们: list[list[tuple[int, float, float]]] = []
    当前: list[tuple[int, float, float]] = []
    for i, item in enumerate(melody):
        当前.append(item)
        if 边界[i] or i == n - 1:
            段们.append(当前)
            当前 = []

    # 2) 太短 → 并到前一句（第一句太小就并到后一句）
    #    ⚠ 判的是「累加后够不够 min_notes」，不是「这一段够不够」——
    #      卡農这种每个音都是长音、每处都断的谱，按后者会一路并成一大句（实测并出 11+5）。
    并好: list[list[tuple[int, float, float]]] = []
    for seg in 段们:
        if 并好 and len(seg) < min_notes and len(并好[-1]) < min_notes:
            并好[-1] = 并好[-1] + seg
        else:
            并好.append(list(seg))
    if len(并好) >= 2 and len(并好[0]) < min_notes:
        并好[1] = 并好[0] + 并好[1]
        并好.pop(0)

    # 2b) 上一步会漏下「前一句已经够长、后面跟着一个孤零零的短句」——
    #     实测天空之城出了 1 音、2 音的句子，那种就是「唱一个音然后停」，比不切还难听。
    #     所以再扫一遍：短句一律往后并进前一句，只要并完不超过 max_notes。
    修好: list[list[tuple[int, float, float]]] = []
    for seg in 并好:
        if 修好 and len(seg) < min_notes and len(修好[-1]) + len(seg) <= max_notes:
            修好[-1] = 修好[-1] + seg
        else:
            修好.append(list(seg))
    并好 = 修好

    # 3) 太长 → 在最弱音程处再切（|音高差| 最小的地方最不像「乐句的转折」）
    out: list[list[tuple[int, float, float]]] = []
    for seg in 并好:
        while len(seg) > max_notes:
            lo = max(min_notes - 1, 0)
            hi = min(len(seg) - min_notes, max_notes - 1)
            候选 = range(lo, hi + 1) if hi >= lo else range(1, len(seg) - 1)
            中 = (len(seg) - 1) / 2.0
            档 = min(候选, key=lambda i: (abs(seg[i][0] - seg[i + 1][0]), abs(i - 中)))
            out.append(seg[: 档 + 1])
            seg = seg[档 + 1:]
        if seg:
            out.append(seg)
    return out


def breathe_phrase(melody: list[tuple[int, float, float]], rest: float = 0.3,
                   long_note: float = 2.0, leap: int = 5,
                   min_notes: int = 4, max_notes: int = 12,
                   log: bool = False) -> list[tuple[str, int, float]]:
    """按「鲸的句」换气（任务书主打的切法）。

    为什么不是按小节：小节线是**人的排版**。照小节切，乐句会在半口气上被切断，
    听起来像喘不上来。鲸歌的结构是 音 → 句 → 段 → 整首，句是按声音走的。

    收尾两件事：句末最后一个音 **+15% 时值**；句间留白 **rest 拍**（默认 0.3，
    和 `tail` 的混响尾巴分开算 —— tail 是空间，rest 是呼吸）。

    rest 建议落在 0.2–0.35 拍：再长就又变成「喘气」了。
    """
    rest = max(0.05, float(rest))
    段们 = _切句(melody, long_note, leap, min_notes, max_notes)
    out: list[tuple[str, int, float]] = []
    for k, seg in enumerate(段们):
        for j, (midi, dur, _start) in enumerate(seg):
            末音 = j == len(seg) - 1
            out.append(("note", midi, round(dur * 1.15, 4) if 末音 else dur))
        if k < len(段们) - 1:
            out.append(("rest", 0, rest))
    if log:
        简报 = "  ".join(f"{len(s)}音/{sum(d for _, d, _ in s):g}拍" for s in 段们)
        print(f"# 按句切：{len(段们)} 句（{简报}）", file=sys.stderr)
    return out


def breathe(notes: list[tuple[int, float]]) -> list[tuple[int, float, bool]]:
    """按「向上跳 ≥4 半音且本句已 ≥4 音」切句，句末拖长、句间休止。

    和天空之城那版用的是同一条规则（见 鲸译规则.md）。谱面的换行不算乐句边界。
    """
    out: list[tuple[int, float, bool]] = []
    count = 0
    for i, (midi, dur) in enumerate(notes):
        nxt = notes[i + 1][0] if i + 1 < len(notes) else None
        count += 1
        phrase_end = nxt is None or (count >= 4 and nxt - midi >= 4) or count >= 12
        if phrase_end:
            out.append((midi, max(dur, 2.0), True))     # 句末拖两拍
            count = 0
        else:
            out.append((midi, dur, False))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--track", type=int, default=None, help="指定轨号（从 0 数）")
    ap.add_argument("--list", action="store_true", help="只列轨")
    ap.add_argument("--octave", type=int, default=0, help="整体移调：-12 = 降一个八度")
    ap.add_argument("--quant", type=float, default=0.25, help="量化到几分之四拍，默认 0.25")
    ap.add_argument("--max", type=int, default=0, help="只取前 N 个音，0 = 全部")
    ap.add_argument("--breath", choices=["off", "leap", "auto", "bar", "phrase"], default="off",
                    help="换气方式：phrase=按句（长音/休止/音高反转切句，推荐），"
                         "bar=每 N 拍（按排版，不推荐），auto=按 MIDI 自身间隙，leap=按音高跳进")
    ap.add_argument("--gap", type=float, default=0.5, help="auto 模式下，空档 ≥ 几拍算换气")
    ap.add_argument("--bar-len", type=float, default=16.0, dest="bar_len", help="bar 模式下，几句拍换一次气（16=4 小节）")
    ap.add_argument("--rest", type=float, default=1.5, help="换气休止几拍，默认 1.5")
    args = ap.parse_args()

    division, tracks, tempos = parse_smf(args.path)
    bpm = 60000000 / tempos[0][1] if tempos else 120.0

    if args.list:
        print(f"共 {len(tracks)} 轨，{division} ticks/四分音符，首段 tempo {bpm:.0f} BPM")
        for i, notes in enumerate(tracks):
            if not notes:
                print(f"  轨 {i}: 空")
                continue
            pitches = sorted(n["note"] for n in notes)
            print(f"  轨 {i}: {len(notes):4d} 音  音域 {name_of(pitches[0])}–{name_of(pitches[-1])}  "
                  f"中位 {name_of(pitches[len(pitches)//2])}")
        return

    idx = args.track if args.track is not None else pick_track(tracks)
    if idx >= len(tracks):
        raise SystemExit(f"没有第 {idx} 轨")
    melody = to_melody(tracks[idx], division, args.quant)
    if args.max > 0:
        melody = melody[: args.max]

    lo = min(m for m, _, _ in melody)
    hi = max(m for m, _, _ in melody)
    total = sum(d for _, d, _ in melody)

    print(f"# 轨 {idx}  原速 {bpm:.0f} BPM  音符 {len(melody)} 个  "
          f"时长 {total * 60 / bpm:.0f}s  音域 {name_of(lo)}–{name_of(hi)}", file=sys.stderr)

    if args.breath == "bar":
        parts = []
        for kind, value, dur in breathe_bar(melody, args.bar_len, args.rest):
            if kind == "note":
                parts.append(f"{name_of(value + args.octave)}:{dur:g}")
            else:
                parts.append(f"r:{dur:g}")
        print(" ".join(parts))
        return

    if args.breath == "phrase":
        parts = []
        for kind, value, dur in breathe_phrase(melody, args.rest, log=True):
            if kind == "note":
                parts.append(f"{name_of(value + args.octave)}:{dur:g}")
            else:
                parts.append(f"r:{dur:g}")
        print(" ".join(parts))
        return

    if args.breath == "auto":
        parts: list[str] = []
        for kind, value, dur in breathe_auto(melody, args.gap, args.rest):
            if kind == "note":
                parts.append(f"{name_of(value + args.octave)}:{dur:g}")
            else:
                parts.append(f"r:{dur:g}")
        print(" ".join(parts))
        return

    shifted = [(m + args.octave, d) for m, d, _ in melody]
    if args.breath == "leap":
        parts = []
        for midi, dur, phrase_end in breathe(shifted):
            parts.append(f"{name_of(midi)}:{dur:g}")
            if phrase_end:
                parts.append(f"r:{args.rest:g}")
        print(" ".join(parts))
    else:
        print(" ".join(f"{name_of(m)}:{d:g}" for m, d in shifted))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
