"""dsh-whale-song —— 把鲸的呼叫做成一件能弹的乐器。

思路（和原神「换乐器」一样）：一件乐器 = 一段原始呼叫里切出的**一个音**的采样，
按音阶移调铺满整块键盘；换乐器 = 换一段呼叫、换一套包络。

⚠ 最关键的一步不是「像鲸鱼」，是「像一个音」：
   真鲸叫是几十秒的长滑音，直接拿来弹会糊成一片。
   所以只取呼叫里最稳的 200~400ms，自己加音头、把尾巴做短。

本文件里的呼叫是**合成的**（为了先验证管线），不是真录音。
换真录音时只要替换 make_raw_call 的输入即可，后面的切音、移调、混音都不用改。
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import sys
import time
import wave

import numpy as np

try:  # 控制台是 cp950 时，中文 print 会直接抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SR = 48000
WS = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 原始呼叫

def make_raw_call(sr: int = SR, kind: str = "moan", seconds: float = 6.0,
                  f0: float = 38.0, seed: int = 7) -> np.ndarray:
    """合成一段「鲸叫」——只为跑通管线，不是真录音。

    moan：低频长滑音，基频 10~40Hz，谐波很快衰减
    pulse：短促、带调幅的脉冲串，音色更亮（高音区用它）
    """
    rng = np.random.default_rng(seed)
    t = np.arange(int(sr * seconds)) / sr
    n = len(t)

    if kind == "moan":
        # 缓慢升降 + 极慢颤音：起点低、中段平稳、末尾回落
        glide = 1.0 + 0.18 * np.sin(2 * np.pi * t / seconds - np.pi / 2)
        vib = 1.0 + 0.010 * np.sin(2 * np.pi * 0.35 * t)
        inst = f0 * glide * vib
        phase = 2 * np.pi * np.cumsum(inst) / sr
        sig = np.zeros(n)
        for k, amp in enumerate([1.0, 0.35, 0.12, 0.05], start=1):
            sig += amp * np.sin(k * phase)
        # 呼叫本身的起伏（鲸叫不是匀速的）
        env = 0.55 + 0.45 * np.sin(2 * np.pi * t / seconds - np.pi / 2) ** 2
        sig *= env
    else:  # pulse
        inst = f0 * (1.0 + 0.05 * np.sin(2 * np.pi * 0.9 * t))
        phase = 2 * np.pi * np.cumsum(inst) / sr
        carrier = np.zeros(n)
        for k, amp in enumerate([1.0, 0.6, 0.35, 0.2, 0.1], start=1):
            carrier += amp * np.sin(k * phase)
        am = 0.5 + 0.5 * np.sin(2 * np.pi * 9.0 * t) ** 2   # 脉冲感
        sig = carrier * am
        env = 0.6 + 0.4 * np.sin(2 * np.pi * t / seconds - np.pi / 2) ** 2
        sig *= env

    sig += rng.normal(0, 0.01, n)          # 一点底噪，别太干净
    return sig / (np.max(np.abs(sig)) + 1e-9)


# ---------------------------------------------------------------- 找「最稳的一段」

def steady_window(sig: np.ndarray, sr: int, f0_hint: float,
                  win: float = 0.30, stride: float = 0.05) -> tuple[int, int, float]:
    """在呼叫里找最稳的 win 秒：基频波动最小、能量不弱。

    返回 (起点样点, 终点样点, 该段的估计基频)。
    真录音没有 f0_hint，就先用 0（会在一个宽范围里搜）。
    """
    w = int(win * sr)
    step = int(stride * sr)
    best = None

    # 没有先验时：先用最响的 1 秒估个大致基频，再围着它搜。
    # 不能写死 15–60Hz —— 网上买的"鲸哭"音效大多是处理过的（这条素材主能量在
    # 300–570Hz），写死低频带只会估到次谐波，切出来就跑调或发闷。
    if not f0_hint:
        win1 = min(len(sig), sr)
        if win1 > 64:
            best_e, best_i = -1.0, 0
            for s in range(0, max(1, len(sig) - win1) + 1, max(1, win1 // 2)):
                e = float(np.mean(sig[s:s + win1] ** 2))
                if e > best_e:
                    best_e, best_i = e, s
            f0_hint = dominant_freq(sig[best_i:best_i + win1], sr, 35.0, 900.0) or 0.0

    lo = max(20.0, f0_hint * 0.8) if f0_hint else 12.0
    hi = min(sr / 4, f0_hint * 1.25) if f0_hint else 900.0

    for start in range(0, len(sig) - w, step):
        seg = sig[start:start + w]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        if rms < 0.05:
            continue
        f = estimate_f0(seg, sr, lo, hi)
        if not f:
            continue
        # 稳定性 = 段内分两半估出的基频差（越小越稳）
        half = estimate_f0(seg[:w // 2], sr, lo, hi) or f
        drift = abs(f - half) / f
        score = rms * (1.0 / (1.0 + 40 * drift))
        if best is None or score > best[0]:
            best = (score, start, f)

    if best is None:
        return 0, w, f0_hint or 30.0
    _, start, f = best
    return start, start + w, f


def estimate_f0(seg: np.ndarray, sr: int, fmin: float = 15.0, fmax: float = 60.0) -> float | None:
    """自相关估基频——只在低频段用，够用且不需要 scipy。"""
    seg = seg - seg.mean()
    if np.max(np.abs(seg)) < 1e-6:
        return None
    n = len(seg)
    # 直接自相关（段只有 300ms，O(n²) 也就 2 亿次，45k 采样有点大 → 先降采样
    target = 6000
    if n > target:
        idx = np.linspace(0, n - 1, target)
        seg = np.interp(idx, np.arange(n), seg)
        sr_eff = sr * target / n
    else:
        sr_eff = sr
    seg = seg / (np.max(np.abs(seg)) + 1e-9)
    # 自相关走 FFT（O(n log n)）。原来用 np.correlate 是 O(n²)：
    # 整段 52 秒要滑上千个窗口，一次渲染 60 秒里有 50 秒耗在这。
    m = len(seg)
    size = 1 << (2 * m - 1).bit_length()
    spec = np.fft.rfft(seg, size)
    ac = np.fft.irfft(spec * np.conj(spec), size)[:m]
    ac /= (ac[0] + 1e-9)

    lag_min = max(2, int(sr_eff / fmax))
    lag_max = min(len(ac) - 2, int(sr_eff / fmin))
    if lag_max <= lag_min:
        return None
    window = ac[lag_min:lag_max]
    if len(window) == 0:
        return None
    # 找第一个显著峰（避免取到倍频）
    peak_at = lag_min + int(np.argmax(window))
    for i in range(1, len(window) - 1):
        if window[i] > 0.5 and window[i] >= window[i - 1] and window[i] >= window[i + 1]:
            peak_at = lag_min + i
            break
    # 抛物线插值到亚采样精度：不做这一步，lag 取整会带来半个半音的音准误差
    if 0 < peak_at < len(ac) - 1:
        y0, y1, y2 = ac[peak_at - 1], ac[peak_at], ac[peak_at + 1]
        denom = (y0 - 2 * y1 + y2)
        if abs(denom) > 1e-12:
            peak_at = peak_at + 0.5 * (y0 - y2) / denom
    return sr_eff / peak_at if peak_at > 0 else None


# ---------------------------------------------------------------- 采样 → 乐器

def resample(x: np.ndarray, factor: float, sr: int) -> np.ndarray:
    """线性插值重采样。factor>1 = 升调且变短（像在键盘上弹采样）。"""
    n_out = max(2, int(round(len(x) / factor)))
    idx = np.linspace(0, len(x) - 1, n_out)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


def flatten_am(x: np.ndarray, sr: int, win_ms: float = 12.0) -> np.ndarray:
    """把缓慢的幅度起伏压平，否则循环播放会变成嗡嗡的颤音。"""
    n = max(8, int(sr * win_ms / 1000))
    if len(x) < n * 2:
        return x
    k = np.hanning(n)
    k /= k.sum()
    env = np.sqrt(np.convolve(x.astype(np.float64) ** 2, k, mode="same"))
    env = np.maximum(env, np.percentile(env, 30) + 1e-6)
    return (x / env).astype(np.float32)


def stretch_loop(x: np.ndarray, sr: int, target_s: float,
                 xfade_ms: float = 8.0) -> np.ndarray:
    """把短片段循环铺到目标长度。

    为什么是循环不是拉伸：鲸叫升调后只剩几十毫秒（升 2.8 个八度就缩到 1/7），
    直接弹就是一声「咔」。而这一段本来就是稳的，循环听着才像一个持续的音。
    循环点做等功率交叉淡化，避免咔哒声。
    """
    target = int(target_s * sr)
    if len(x) >= target:
        return x[:target]
    fade = max(8, int(xfade_ms * sr / 1000))
    tail = x[-fade:] * np.linspace(1, 0, fade)
    head = x[:fade] * np.linspace(0, 1, fade)

    out = np.zeros(target + fade, dtype=np.float32)
    pos = 0
    while pos < target:
        chunk = x.copy()
        if pos > 0:
            chunk[:fade] = chunk[:fade] * np.linspace(0, 1, fade) + out[pos:pos + fade]
        end = min(len(out), pos + len(chunk))
        out[pos:end] = chunk[: end - pos]
        pos += len(x) - fade
    return out[:target]


def dominant_freq(x: np.ndarray, sr: int, lo: float = 80.0, hi: float = 1200.0) -> float:
    """用 FFT 找主频（只认局部极大，避开频带边沿，做抛物线插值）。

    为什么不复用 estimate_f0：自相关在「带衰减包络」的信号上会系统性偏低，
    实测两件乐器一个偏 -20、一个偏 +19 音分——这种固定偏移必须靠 FFT 闭环兜住。
    """
    x = x - x.mean()
    if len(x) < 64 or np.max(np.abs(x)) < 1e-5:
        return 0.0
    n = 1 << 15
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), n))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    idx = np.where((freqs > lo) & (freqs < hi))[0]
    if len(idx) < 3:
        return 0.0
    peaks = [i for i in idx[1:-1] if spec[i] >= spec[i - 1] and spec[i] >= spec[i + 1]]
    if not peaks:
        return 0.0
    best = max(peaks, key=lambda i: spec[i])
    if 0 < best < len(spec) - 1:
        y0, y1, y2 = spec[best - 1], spec[best], spec[best + 1]
        den = y0 - 2 * y1 + y2
        if abs(den) > 1e-12:
            return float(freqs[best] + 0.5 * (y0 - y2) / den * (freqs[1] - freqs[0]))
    return float(freqs[best])


def make_instrument(raw: np.ndarray, sr: int, f0_hint: float,
                    ref_freq: float = 261.625565, note_s: float = 0.30) -> np.ndarray:
    """切一个音、升到参考音高、铺成固定长度的单音采样。"""
    a, b, f0 = steady_window(raw, sr, f0_hint)
    seg = raw[a:b].copy()

    # 整体升到参考音高（蓝鲸基频 10~40Hz，人耳放不出来）
    factor = f0 / ref_freq            # 基频要变到 ref_freq，采样率等效乘 1/factor
    seg = resample(seg, 1.0 / factor if factor else 1.0, sr)

    # 升调之后只剩几十毫秒：压平起伏后循环铺成 note_s 秒
    seg = flatten_am(seg, sr)
    seg = stretch_loop(seg, sr, note_s)

    # 去直流、归一
    seg = seg - seg.mean()
    seg = seg / (np.max(np.abs(seg)) + 1e-9)

    # 音头：6ms 起音；不要用原采样的慢起音，否则听不出节奏
    n = len(seg)
    attack = max(8, int(0.006 * sr))
    env = np.ones(n, dtype=np.float32)
    env[:attack] = np.linspace(0, 1, attack) ** 1.5
    # 尾巴：最后 60% 做衰减，避免长滑音拖成一片
    tail = int(n * 0.6)
    env[-tail:] *= np.linspace(1, 0.05, tail) ** 1.2
    seg *= env

    # 轻轻压一下共振，让它更像「一个音」而不是一段录音
    seg = np.tanh(seg * 1.6) / np.tanh(1.6)

    # 闭环校准：自相关估基频有系统性偏差（实测两件乐器分别 -20 / +19 音分），
    # 所以对成品用 FFT 再测一次、迭代拉回来。不做这一步整件乐器都「没调音」。
    for _ in range(3):
        got = dominant_freq(seg, sr, ref_freq * 0.6, ref_freq * 1.6)
        if not got:
            break
        cents = 1200 * math.log2(got / ref_freq)
        if abs(cents) <= 3:
            break
        seg = resample(seg, ref_freq / got, sr)
    return seg.astype(np.float32)


def play_note(inst: np.ndarray, sr: int, midi: int, ref_midi: int = 60,
              velocity: float = 1.0) -> np.ndarray:
    """把参考音高的采样按半音移调。

    ⚠ resample(x, k) 的 k 就是「音高倍数」：k=2 就升一个八度、长度减半。
      这里曾经写成 1/k，结果弹高音反而低八度——所以逐音校验要留着。
    """
    semis = midi - ref_midi
    factor = 2.0 ** (semis / 12.0)
    out = resample(inst, factor, sr)
    return (out * velocity).astype(np.float32)


# ---------------------------------------------------------------- 效果

def fft_convolve(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """FFT 卷积。np.convolve 是 O(n·m)：1.2M 采样 × 115k 脉冲响应要几十秒，
    而这一步占了整次渲染的大头。FFT 之后是 O(n log n)，听感完全一样。"""
    n = len(a) + len(b) - 1
    size = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a, size)
    fb = np.fft.rfft(b, size)
    return np.fft.irfft(fa * fb, size)[:n].astype(np.float32)


def add_reverb(x: np.ndarray, sr: int, decay: float = 2.2, wet: float = 0.42,
               predelay: float = 0.025) -> np.ndarray:
    """合成脉冲响应做卷积混响：指数衰减噪声 + 一点低通，够大房间用。"""
    rng = np.random.default_rng(3)
    n = int(decay * sr)
    t = np.arange(n) / sr
    ir = rng.normal(0, 1, n) * np.exp(-t * (6.9 / decay))
    # 简单一阶低通，把高频磨掉（大空间的声音不刺）
    for _ in range(3):
        ir = np.convolve(ir, np.ones(4) / 4, mode="same")
    ir[: int(predelay * sr)] = 0
    ir /= np.max(np.abs(ir)) + 1e-9

    wetsig = fft_convolve(x, ir)[: len(x)]
    wetsig /= np.max(np.abs(wetsig)) + 1e-9
    return (x * (1 - wet) + wetsig * wet).astype(np.float32)


def add_pad(mix: np.ndarray, sr: int, notes: list[tuple[float, float, int]],
            level: float = 0.18) -> np.ndarray:
    """低八度 pad：把旋律的根音拉长铺底，撑出空间。"""
    out = mix.copy()
    for start, dur, midi in notes:
        f = 440.0 * 2.0 ** ((midi - 69) / 12.0)
        n = int(dur * sr)
        if n <= 0:
            continue
        i0 = int(start * sr)
        t = np.arange(n) / sr
        env = np.minimum(1.0, t / 0.6) * np.minimum(1.0, (dur - t) / 0.9)
        env = np.clip(env, 0, 1) ** 2
        v = (np.sin(2 * np.pi * f * t) + 0.5 * np.sin(2 * np.pi * f * 1.005 * t)) * 0.5
        seg = (v * env * level).astype(np.float32)
        end = min(len(out), i0 + n)
        out[i0:end] += seg[: end - i0]
    return out


def add_delay(x: np.ndarray, sr: int, delay_s: float = 0.45,
              feedback: float = 0.35, wet: float = 0.25) -> np.ndarray:
    """句尾回响：像在开阔水面上的反射，不是房间里那种板式混响。"""
    d = max(1, int(delay_s * sr))
    out = x.copy()
    if d >= len(x):
        return out
    buf = np.zeros_like(x)
    tap = np.zeros_like(x)
    tap[d:] = x[:-d]
    buf += tap
    for _ in range(2):
        shifted = np.zeros_like(x)
        shifted[d:] = buf[:-d] * feedback
        buf = shifted
        out[d:] += buf[:-d] * wet
    return out.astype(np.float32)


def glue(x: np.ndarray, drive: float = 1.25, wet: float = 0.35) -> np.ndarray:
    """轻微失真 + 归一。失真给一点粗糙感，别做成电吉他。"""
    y = np.tanh(x * drive) / np.tanh(drive)
    y = x * (1 - wet) + y * wet
    return (y / (np.max(np.abs(y)) + 1e-9) * 0.92).astype(np.float32)


# ---------------------------------------------------------------- 鲸味：滑音与低通

def onepole_lp(x: np.ndarray, sr: int, cutoff: float, taps: int = 31) -> np.ndarray:
    """低通。鲸叫没有那么多高频，砍掉「金属感」最省事的一步。

    ⚠ 这里原来是**逐采样的一阶 IIR**（纯 Python 循环），一次渲染要 80 秒，
    实测工具调用等不起。改成等价的窗函数 FIR + np.convolve（C 实现，快两个数量级），
    听感差别可以忽略：截止频率一样，只是没有相位延迟以外的差异。
    """
    if cutoff <= 0:
        return x
    n = max(5, taps | 1)                 # 奇数抽头，对称
    fc = min(0.49, cutoff / sr)
    m = np.arange(n) - (n - 1) / 2.0
    h = np.sinc(2 * fc * m) * np.hamming(n)
    h /= np.sum(h)
    return np.convolve(x, h, mode="same").astype(np.float32)


def whale_play(inst: np.ndarray, sr: int, midi: int, ref_midi: int = 60,
               bend_cents: float = 55.0, dur: float | None = None,
               attack_ms: float = 90.0, velocity: float = 0.85) -> np.ndarray:
    """一个音，但**音高在音内滑动**——这是鲸和钢琴最大的差别。

    钢琴：音高固定 + 敲击式起音。
    鲸叫：长滑音 + 缓慢起伏 + 没有硬音头。

    于是这里做了三件事：
      1. 音内音高曲线（拱形：先升后降），用变速读采样实现；
      2. 起音缓慢上升（attack_ms，默认 90ms），去掉敲击感，但还留得住节奏；
         55ms 时主人评「像力度重的弹弦类乐器」——拨弦感就来自这里太短；
      3. 尾巴留长一点，交给混响。
    """
    base = 2.0 ** ((midi - ref_midi) / 12.0)
    n_src = len(inst)
    if dur is None:
        dur = n_src / base / sr

    n = int(dur * sr)
    t = np.linspace(0, 1, n, endpoint=False)
    # 拱形滑音：起手偏低 → 中段拱起 → 尾巴再落一点
    cents = bend_cents * np.sin(np.pi * t) - bend_cents * 0.28 * t
    rate = base * 2.0 ** (cents / 1200.0)

    pos = np.cumsum(rate)                 # 读指针（源采样单位）
    pos = np.clip(pos, 0, n_src - 1)
    out = np.interp(pos, np.arange(n_src), inst).astype(np.float32)

    atk = max(8, int(attack_ms / 1000 * sr))
    env = np.ones(n, dtype=np.float32)
    env[:atk] = np.linspace(0, 1, atk) ** 0.8
    rel = int(n * 0.45)
    env[-rel:] *= np.linspace(1, 0.08, rel) ** 1.1
    return (out * env * velocity).astype(np.float32)


# ---------------------------------------------------------------- 写盘 / 播放

def write_wav(path: str, stereo: np.ndarray, sr: int) -> None:
    data = np.clip(stereo, -1, 1)
    pcm = (data * 32767).astype("<i2")
    with wave.open(path, "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes(pcm.tobytes())


def _looks_like_wav(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    except OSError:
        return False


def _run_ffmpeg(args: list[str]) -> None:
    import subprocess
    exe = _ffmpeg_exe()
    proc = subprocess.run([exe, "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码失败：{proc.stderr.strip()[:400]}")


def _ffmpeg_exe() -> str:
    """找 ffmpeg：环境变量 > PATH > DSH 装的那份（不在 PATH 上也认）。"""
    for cand in (os.environ.get("DSH_FFMPEG"), "ffmpeg"):
        if cand and (os.path.sep in cand or cand == "ffmpeg"):
            if cand == "ffmpeg":
                from shutil import which
                hit = which("ffmpeg")
                if hit:
                    return hit
            elif os.path.exists(cand):
                return cand
    known = os.path.expanduser(r"~\.dsh\tools\ffmpeg\bin\ffmpeg.exe")
    if os.path.exists(known):
        return known
    raise FileNotFoundError("找不到 ffmpeg（非 wav 素材需要它解码）；装一个或设 DSH_FFMPEG")


def load_audio(path: str, target_sr: int = SR) -> np.ndarray:
    """读音频（wav / mp3 / flac / ogg…），混成单声道 float32、重采样到 target_sr。

    wav 走标准库，不依赖任何东西；其它格式用 ffmpeg 转一道临时 wav 再读。
    遇到不支持的位深就明确报错，别静默出错。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in ("", ".wav") or not _looks_like_wav(path):
        tmp = path + ".tmp-decode.wav"
        try:
            _run_ffmpeg(["-y", "-i", path, "-ac", "1", "-ar", str(target_sr), "-f", "wav", tmp])
            data = load_audio(tmp, target_sr)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return data

    with wave.open(path, "rb") as fh:
        ch = fh.getnchannels()
        width = fh.getsampwidth()
        sr_in = fh.getframerate()
        n = fh.getnframes()
        raw = fh.readframes(n)

    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        v = np.where(v & 0x800000, v - 0x1000000, v).astype(np.float32) / 8388608.0
        data = v
    else:
        raise ValueError(f"不支持的位深 {width*8}bit：{path}")

    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)

    if sr_in != target_sr:
        n_out = int(round(len(data) * target_sr / sr_in))
        data = np.interp(np.linspace(0, len(data) - 1, n_out), np.arange(len(data)), data).astype(np.float32)

    # 去直流并归一，真录音常常偏得很厉害
    data = data - data.mean()
    peak = np.max(np.abs(data)) or 1.0
    return (data / peak).astype(np.float32)


def play_wav(path: str) -> bool:
    """复用 tts_local 的办法：Windows MCI，不新增进程。"""
    winmm = ctypes.windll.winmm
    alias = f"whale{int(time.time() * 1000) % 100000}"
    buf = ctypes.create_unicode_buffer(256)
    if winmm.mciSendStringW(f'open "{path}" type waveaudio alias {alias}', buf, 256, None) != 0:
        return False
    try:
        winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
    finally:
        winmm.mciSendStringW(f"close {alias}", None, 0, None)
    return True


# ---------------------------------------------------------------- 谱子

def lullaby() -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """勃拉姆斯摇篮曲前两句（C 大调）。返回 (高音声部, 低音根音)。

    (midi, 拍数)：1 拍 = 四分音符。
    """
    melody = [
        (64, 1), (64, 1), (67, 2),      # 睡吧 睡吧
        (64, 1), (64, 1), (67, 2),      # 我亲爱的
        (64, 1), (67, 1), (72, 1), (71, 1),   # 宝贝
        (69, 2),                        # 你
        (69, 1), (69, 1), (67, 2),      # 妈妈
    ]
    bass = [(48, 4), (48, 4), (48, 2), (43, 2), (41, 4)]
    return melody, bass


_PITCH = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}


def note_to_midi(name: str) -> int:
    """'C4' / 'C#4' / 'Db4' → MIDI 音高。"""
    s = name.strip()
    if not s:
        raise ValueError('空音名')
    letter = s[0].upper()
    if letter not in _PITCH:
        raise ValueError(f'看不懂的音名：{name}')
    i = 1
    acc = 0
    while i < len(s) and s[i] in '#b♯♭':
        acc += 1 if s[i] in '#♯' else -1
        i += 1
    try:
        octave = int(s[i:])
    except ValueError:
        raise ValueError(f'音名缺八度：{name}（要写成 C4 这样）')
    return 12 * (octave + 1) + _PITCH[letter] + acc


def parse_notes(spec: str) -> tuple[list[tuple[int | None, float]], list[tuple[int, float]]]:
    """把 'C4:1 E4:1 G4:2 r:1' 解析成 (旋律, 低音层)。

    - 音名后面跟 `:拍数`，省略就是 1 拍（四分音符）
    - `r` / `-` 是休止：不发声，但占时间
    - 竖线 `|` 和逗号都当分隔符，只是给人看的
    - 低音层自动生成：每个小节（4 拍）取第一个有声的音，向下一个八度
    """
    tokens = [t for t in spec.replace(',', ' ').replace('|', ' ').replace('/', ' ').split() if t]
    if not tokens:
        raise ValueError('旋律是空的')

    melody: list[tuple[int | None, float]] = []
    for tok in tokens:
        name, _, dur = tok.partition(':')
        beats = float(dur) if dur else 1.0
        if beats <= 0:
            beats = 1.0
        if name.lower() in ('r', 'rest', '-'):
            melody.append((None, beats))
        else:
            melody.append((note_to_midi(name), beats))

    bass: list[tuple[int, float]] = []
    bar = 0.0
    first = None
    for midi, beats in melody:
        if bar == 0.0 and midi is not None:
            first = midi
        bar += beats
        if bar >= 4.0:
            if first is not None:
                bass.append((max(24, first - 12), min(4.0, bar)))
            bar = 0.0
            first = None
    if first is not None:
        bass.append((max(24, first - 12), bar))
    return melody, bass


def render_demo(out_path: str, bpm: float = 66.0, seed: int = 7,
                effects: bool = True, low_raw: np.ndarray | None = None,
                high_raw: np.ndarray | None = None,
                low_f0: float = 0.0, high_f0: float = 0.0,
                bend: float = 55.0, lp_hz: float = 3200.0, tail_s: float = 2.6,
                attack_ms: float = 90.0,
                notes: str | None = None) -> str:
    sr = SR
    beat = 60.0 / bpm

    # 两件乐器：低音区用 moan，高音区用 pulse——像换乐器一样切
    # 传了真录音就用真录音；没传就合成一段占位（只为验证管线）
    #
    # ⚠ 只给一份素材时，高音区也必须用它。否则高音区会悄悄退回合成的 pulse，
    #   听感就是「前半段像人声、后面像钢琴」——这是实测踩出来的坑。
    if high_raw is None and low_raw is not None:
        high_raw = low_raw
    if low_raw is None and high_raw is not None:
        low_raw = high_raw

    moan_raw = low_raw if low_raw is not None else make_raw_call(sr, "moan", seconds=6.0, f0=32.0, seed=seed)
    pulse_raw = high_raw if high_raw is not None else make_raw_call(sr, "pulse", seconds=5.0, f0=45.0, seed=seed + 1)
    inst_low = make_instrument(moan_raw, sr, low_f0)
    inst_high = make_instrument(pulse_raw, sr, high_f0)

    if notes:
        melody, bass = parse_notes(notes)
    else:
        melody, bass = lullaby()
    total_beats = sum(d for _, d in melody)
    total = total_beats * beat + tail_s                   # 尾巴：留长一点本来就是鲸的特点，可调
    mix = np.zeros(int(total * sr) + sr, dtype=np.float32)

    t = 0.0
    n_notes = max(1, sum(1 for m, _ in melody if m is not None))
    sounding = 0
    for midi, dur in melody:
        if midi is None:                       # 休止：占时间，不出声
            t += dur * beat
            continue
        inst = inst_high if midi >= 67 else inst_low
        note_len = max(dur * beat * 0.92, 0.34)
        # 力度：几乎是平的，只在中段轻轻抬一点。
        # 之前用 0.64→0.86 的大拱形，听感是「前面起伏大、后面像没了」——
        # 起伏超过 6dB 人耳记不住旋律，只记住「后面小声了」。
        vel = 0.76 + 0.06 * math.sin(math.pi * sounding / max(1, n_notes - 1))
        if n_notes - sounding <= 3:
            vel = max(vel, 0.82)          # 收句别掉下去，不然整段像没唱完
        lead = whale_play(inst, sr, midi, ref_midi=60, bend_cents=bend, dur=note_len,
                          attack_ms=attack_ms, velocity=vel)
        if lp_hz > 0:
            lead = onepole_lp(lead, sr, lp_hz)
        # 低八度一层：鲸的「体」在低频，单靠高音区只会像口哨/钢琴
        sub = whale_play(inst, sr, midi - 12, ref_midi=60, bend_cents=bend * 0.6,
                         dur=note_len * 1.2, attack_ms=attack_ms, velocity=vel * 0.55)
        i0 = int(t * sr)
        for buf in (lead, sub):
            end = min(len(mix), i0 + len(buf))
            if end > i0:
                mix[i0:end] += buf[: end - i0]
        sounding += 1
        t += dur * beat

    # pad：每个低音根音下到两个八度，铺满整小节
    pad_notes = []
    tb = 0.0
    for midi, beats in bass:
        pad_notes.append((tb, beats * beat + 0.8, midi - 12))
        tb += beats * beat

    if effects:
        mix = add_pad(mix, sr, pad_notes, level=0.12)
        mix = add_reverb(mix, sr, decay=2.4, wet=0.45)
        # 句尾回响：1.5 拍一次的延迟，像水面反射，不像房间混响
        mix = add_delay(mix, sr, delay_s=beat * 1.5, feedback=0.3, wet=0.22)
        mix = glue(mix, drive=1.2, wet=0.3)

    # 轻微宽度：左右差 7ms 的延迟，别做成立体声抖动
    delay = int(0.007 * sr)
    left = mix
    right = np.concatenate([np.zeros(delay, dtype=np.float32), mix[:-delay]])
    stereo = np.stack([left, right * 0.96], axis=1)

    write_wav(out_path, stereo, sr)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(WS, "whale-demo.wav"))
    ap.add_argument("--bpm", type=float, default=66.0)
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--dry", action="store_true", help="不加 pad / 混响 / 失真，只验旋律")
    ap.add_argument("--inspect", action="store_true", help="打印每件乐器的基频，确认音高对不对")
    ap.add_argument("--raw-low", help="低音区乐器的原始呼叫 wav（真录音）；不给就用合成的")
    ap.add_argument("--raw-high", help="高音区乐器的原始呼叫 wav")
    ap.add_argument("--f0-low", type=float, default=0.0, help="低音区呼叫的基频提示；0 = 自动搜 15–60Hz")
    ap.add_argument("--f0-high", type=float, default=0.0, help="高音区呼叫的基频提示；0 = 自动搜")
    ap.add_argument("--bend", type=float, default=55.0, help="音内滑音幅度（音分）；0 = 不用滑音，回到钢琴式直音")
    ap.add_argument("--lp", type=float, default=3200.0, help="低通截止频率 Hz；0 = 不低通")
    ap.add_argument("--tail", type=float, default=2.6, help="结尾留多少秒纯混响；鲸的长尾音就用 4–6")
    ap.add_argument("--attack", type=float, default=90.0,
                    help="起音上升时间 ms；55 = 像拨弦，90–110 = 更接近鲸的软起音")
    ap.add_argument("--notes", help="自定义旋律，例如 'C4:1 E4:1 G4:2 | r:1 A4:2'；不填用内置摇篮曲")
    args = ap.parse_args()

    if args.inspect:
        sr = SR
        for kind, f0 in (("moan", 32.0), ("pulse", 45.0)):
            raw = make_raw_call(sr, kind, seconds=6.0, f0=f0)
            a, b, est = steady_window(raw, sr, f0)
            print(f"{kind:6s} 原基频={f0:5.1f}Hz  估出={est:5.1f}Hz  窗口={a/sr:.2f}-{b/sr:.2f}s")
            inst = make_instrument(raw, sr, f0)
            spec = np.abs(np.fft.rfft(inst * np.hanning(len(inst)), 1 << 16))
            freqs = np.fft.rfftfreq(1 << 16, 1 / sr)
            peak = freqs[int(np.argmax(spec[1:])) + 1]
            print(f"       采样长度={len(inst)/sr*1000:.0f}ms  峰值={peak:.1f}Hz（应接近 261.6）")

    low_raw = load_audio(args.raw_low) if args.raw_low else None
    high_raw = load_audio(args.raw_high) if args.raw_high else None
    if low_raw is not None:
        print(f"低音区素材：{args.raw_low}  {len(low_raw)/SR:.1f}s  f0 提示 {args.f0_low or '自动'}")
    if high_raw is not None:
        print(f"高音区素材：{args.raw_high}  {len(high_raw)/SR:.1f}s  f0 提示 {args.f0_high or '自动'}")

    path = render_demo(args.out, args.bpm, effects=not args.dry,
                       low_raw=low_raw, high_raw=high_raw,
                       low_f0=args.f0_low, high_f0=args.f0_high,
                       bend=args.bend, lp_hz=args.lp, tail_s=args.tail,
                       attack_ms=args.attack, notes=args.notes)
    size = os.path.getsize(path)
    with wave.open(path, "rb") as fh:
        dur = fh.getnframes() / fh.getframerate()
    print(f"写好了：{path}  {dur:.1f}s  {size/1024:.0f}KB")
    if args.play:
        print("播放中…")
        play_wav(path)


if __name__ == "__main__":
    main()
