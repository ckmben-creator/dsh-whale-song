"""一条命令：MIDI / 谱面 → 鲸译成品。

    python whale_pipeline.py song.mid
    python whale_pipeline.py song.mid --preset bright
    python whale_pipeline.py score.txt --score genshin --preset calm

它把之前手工做的四步串起来：
    1. 读输入（MIDI 或原神琴谱）
    2. 只留旋律，降一个八度
    3. 换气（按小节 / 按 MIDI 间隙 / 按音高跳进）
    4. 用鲸译参数渲染成 wav

两个预设（--preset）：
    calm    慢、闷、长尾巴 —— 沉在水里（默认）
    bright  快一点、亮一点、短尾巴 —— 还能听出旋律的轻快
"""

from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "dsh-whale-song"))

import genshin_score as gs
import midi_to_notes as mn

# 两个预设 = 两种取向，不只是两套数字：
#   calm   —— 鲸译（沉在水里）：降八度、慢、长尾巴、**每 4 小节换气**（bar）
#   bright —— 听得清：原调、稍快、短尾巴、不换气（先确认是不是那首歌）
#
# ⚠ 2026-11-02：`calm` 曾经把默认改成 `phrase`（按句切），被主人否掉了 ——
#   「第 1 版没换气更好，更流畅」。天空之城 52 句的切法反而把歌切碎了。
#   phrase 留着当选项（手工挑句子时有用），但**不当默认**。
PRESETS = {
    "calm":   {"bpm_scale": 0.63, "tail": 6.0, "lp": 2000.0, "bend": 70.0,
               "rest": 1.5, "octave": -12, "breath": "bar", "attack": 90.0},
    "bright": {"bpm_scale": 0.85, "tail": 4.0, "lp": 2600.0, "bend": 55.0,
               "rest": 1.0, "octave": 0, "breath": "off", "attack": 70.0},
}

def find_sample() -> str | None:
    """找那座鲸的录音：本目录（装成插件时）> 同级插件目录 > desktop profile。"""
    candidates = [
        os.path.join(HERE, "samples", "commons-humpback.oga"),
        os.path.join(os.path.dirname(HERE), "dsh-whale-song", "samples", "commons-humpback.oga"),
        os.path.expanduser("~/.dsh/profiles/desktop/node_modules/dsh-whale-song/samples/commons-humpback.oga"),
    ]
    for c in candidates:
        p = os.path.normpath(c)
        if os.path.isfile(p):
            return p
    return None


def load_melody(path: str, score_kind: str = "auto", track: int | None = None,
                octave: int = -12) -> tuple[list[tuple[int, float, float]], float]:
    """返回 (melody, 原速 BPM)。melody 元素是 (midi, 拍数, 起点拍)。"""
    lower = path.lower()
    if lower.endswith((".mid", ".midi")):
        division, tracks, tempos = mn.parse_smf(path)
        bpm = 60000000 / tempos[0][1] if tempos else 120.0
        idx = track if track is not None else mn.pick_track(tracks)
        melody = mn.to_melody(tracks[idx], division)
        return [(m + octave, d, s) for m, d, s in melody], bpm

    text = open(path, encoding="utf-8-sig").read()
    kind = score_kind
    if kind == "auto":
        kind = "numbered" if re.search(r"[+-]\d", text) else "keyboard"
    bars = gs.parse_numbered(text) if kind == "numbered" else gs.parse(text)
    src = int(kind == "numbered")
    if src:
        events = [m for bar in bars for m in bar]
    else:
        events = [max(bar) for bar in bars] if bars and isinstance(bars[0], list) else []
        events = [m for bar in bars for m in ([max(bar)] if isinstance(bar, list) else [bar])]
    melody = [(m + octave, 1.0, float(i)) for i, m in enumerate(events)]
    return melody, 100.0


def breathe(melody: list[tuple[int, float, float]], mode: str,
            bar_len: float, rest: float) -> list[tuple[str, int, float]]:
    if mode == "phrase":
        # 句间留白：按预设 rest 的 0.2 折算，夹在 0.2–0.35 拍
        # （任务书的风险线 —— 再长就又变成「喘气」了）
        r = min(0.35, max(0.2, rest * 0.2))
        return mn.breathe_phrase(melody, r, log=True)
    if mode == "bar":
        return mn.breathe_bar(melody, bar_len, rest)
    if mode == "auto":
        return mn.breathe_auto(melody, 0.5, rest)
    if mode == "leap":
        shifted = [(m, d) for m, d, _ in melody]
        out = []
        for midi, dur, end in mn.breathe(shifted):
            out.append(("note", midi, dur))
            if end:
                out.append(("rest", 0, rest))
        return out
    return [("note", m, d) for m, d, _ in melody]


def main() -> None:
    ap = argparse.ArgumentParser(description="MIDI/谱面 → 鲸译成品")
    ap.add_argument("input")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="calm")
    ap.add_argument("--track", type=int, default=None)
    ap.add_argument("--octave", type=int, default=None, help="默认按预设")
    ap.add_argument("--breath", choices=["phrase", "bar", "auto", "leap", "off"], default=None,
                    help="默认按预设（calm 用 phrase=按句切；bright 不换气，用来确认是不是那首歌）")
    ap.add_argument("--bar-len", type=float, default=16.0, dest="bar_len")
    ap.add_argument("--score", choices=["auto", "numbered", "keyboard"], default="auto")
    ap.add_argument("--bpm", type=float, default=None, help="直接指定最终速度（跳过预设换算）")
    ap.add_argument("--attack", type=float, default=None,
                    help="起音上升时间 ms；默认按预设（calm 90 / bright 70）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--play", action="store_true")
    args = ap.parse_args()

    preset = PRESETS[args.preset]
    octave = args.octave if args.octave is not None else preset["octave"]
    mode = args.breath if args.breath is not None else preset["breath"]
    attack = args.attack if args.attack is not None else preset.get("attack", 90.0)
    melody, src_bpm = load_melody(args.input, args.score, args.track, octave)
    if not melody:
        raise SystemExit("没读到音符——检查文件或加 --track")

    bpm = args.bpm if args.bpm else max(40.0, round(src_bpm * preset["bpm_scale"]))
    voiced = breathe(melody, mode, args.bar_len, preset["rest"])

    parts = []
    for kind, value, dur in voiced:
        parts.append(f"{mn.name_of(value)}:{dur:g}" if kind == "note" else f"r:{dur:g}")
    notes = " ".join(parts)

    n_notes = sum(1 for k, _, _ in voiced if k == "note")
    n_rests = sum(1 for k, _, _ in voiced if k == "rest")
    lo = min(m for m, _, _ in melody)
    hi = max(m for m, _, _ in melody)
    total_beats = sum(d for _, _, d in voiced)
    print(f"# {os.path.basename(args.input)}  预设 {args.preset}（音高 {octave:+d} / 换气 {mode} / 起音 {attack:.0f}ms）", file=sys.stderr)
    print(f"# 音符 {n_notes}  换气 {n_rests}  音域 {mn.name_of(lo)}–{mn.name_of(hi)}  "
          f"{bpm:.0f} BPM  时长 {total_beats*60/bpm:.0f}s", file=sys.stderr)

    import whale_song as ws
    sample = find_sample()
    if sample is None:
        raise SystemExit("找不到鲸的录音素材（samples/commons-humpback.oga）")
    raw = ws.load_audio(sample)
    stem = os.path.splitext(os.path.basename(args.input))[0]
    out = args.out or os.path.join(
        os.path.expanduser("~/.dsh/profiles/desktop/node_modules/dsh-whale-song/out"),
        f"{stem}-{args.preset}.wav")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    path = ws.render_demo(out, bpm=bpm, low_raw=raw, high_raw=raw,
                          bend=preset["bend"], lp_hz=preset["lp"],
                          tail_s=preset["tail"], attack_ms=attack, notes=notes)
    print(path)
    print(f"# 写好：{path}  {os.path.getsize(path)/1024/1024:.1f} MB", file=sys.stderr)
    if args.play:
        ws.play_wav(path)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
