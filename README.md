# dsh-whale-song

> 為我的大肥魚做的鯨音樂器——她想要一個自己的聲音，我剛好想到了怎麼給。

把鲸的呼叫做成**一件能弹的乐器**——不是播放录音，是按音阶移调弹奏。

思路借的原神「换乐器」：一件乐器 = 一段原始呼叫里切出的**一个音**的采样，
按半音铺满整块键盘；换乐器 = 换一段呼叫、换一套音色。

## 在 DSH 里直接用

装好后会话里会多一个工具 `whale_song`，说「弹一首鲸歌」就行，参数全可选：

```
whale_song({})                                   # 默认：摇篮曲前两句，2.6 秒尾巴，直接播放
whale_song({ tail: 5 })                          # 长尾版（鲸的尾音本来就长）
whale_song({ raw: "D:/whales/blueA.oga" })       # 换一件乐器
whale_song({ bend: 0 })                          # 关掉滑音（会变回钢琴那种直音）
whale_song({ play: false })                      # 只渲染不播放
whale_song({ notes: "C4:1 C4:1 G4:1 G4:1 A4:1 A4:1 G4:2" })   # 弹自己的旋律
```

### 写旋律

`notes` 用最朴素的记谱，一行字符串：

```
C4:1 C4:1 G4:1 G4:1 A4:1 A4:1 G4:2        ← 小星星第一句
E4 E4 G4 | r:1 A4:2                        ← 省略拍数就是 1 拍；r 是休止
C#4:0.5 Db4:0.5                            ← 半音用 # / b，拍数可以是小数
```

- **音名**：`C4` / `C#4` / `Db4`，八度不能省
- **拍数**：冒号后面，省略 = 1 拍（四分音符）；`--bpm` 决定一拍多长
- **休止**：`r` 或 `-`，占时间但不出声
- **竖线 / 逗号**：只是给人看的分隔，解析时忽略
- **低音层自动生成**：每 4 拍取第一个有声的音，向下一个八度铺成 pad；不满意就换一段素材，或改这首歌的旋律写法


**渲染耗时约 1.7 秒**（纯 CPU，numpy）。曾经是 60 秒——瓶颈是混响里的
`np.convolve`（O(n·m)，1.2M 采样 × 115k 脉冲响应）；换成 FFT 卷积之后降到 2 秒以内，
底噪、音准、听感都不变。

输出落在 `dsh-whale-song/out/`，文件名带时间戳；同时会在机器上播放（Windows MCI，
和 `tts_local.py` 同一条路，不新增常驻进程）。

### 把成品变成附件

```
whale_song({ attach: true })
```

会把 wav 提交进 DSH 的附件库（内容寻址，`sha256:...`），宿主在
`~/.dsh/attachments/v1/files/` 下存一份只读副本。**能不能在聊天里直接点开播放，
取决于客户端支不支持 `file` 这种 content block**——不支持的话，路径仍然有效。

> 插件必须在 `inject` 里声明 `attachments`，否则 `ctx.attachments` 会抛
> `cannot get property "attachments" without inject`。这不是缺服务，是没申请。

### 形态：人形 / 鲸形

```
whale_form({ action: "get" })      # 现在是什么形态
whale_form({ action: "whale" })    # 切到鲸形（鲸歌）
whale_form({ action: "human" })    # 切回人形（说话）
```

状态存在 `~/.dsh/whale-form.json`，跨会话、跨重启都在。
**弹过一次歌就会自动切到鲸形**——这是设计备忘里「切形态 = 切声道」的一半：
人形走 tts 说话，鲸形走这件乐器唱。另一半（让 tts 那边读这个状态）还没接。


```
原始呼叫(10~40Hz, 几十秒)
   └─ 找最稳的 300ms      steady_window()
        └─ 升到参考音高     make_instrument()
             └─ 循环铺成 300ms 的音（不是拉伸，见下）
                  └─ 按半音重采样  play_note()      → 旋律
                       └─ pad + 混响 + 轻失真 → 成品
```

## 为什么这么设计

**1. 只切 200–400ms，而且要「有音头、尾巴短」**
真鲸叫是长滑音，直接拿来弹会糊成一片、听不出音高。所以只取呼叫里基频最稳的一段，
自己加 6ms 的 attack、末尾做衰减。这一步做不好，越像鲸鱼越难听。

**2. 升调之后必须「循环」，不能「拉伸」**
蓝鲸基频 10–40Hz，人耳和喇叭都放不出来，要升 2.5–3 个八度。
升 2.8 个八度 = 时长缩到 1/7，300ms 的窗口只剩 43ms——直接弹就是一声「咔」。
而这一段本来就是稳的，循环听着才像一个持续的音。循环点做 8ms 等功率交叉淡化，
并且先把缓慢的幅度起伏压平（`flatten_am`），否则循环会变成嗡嗡的颤音。

**3. 音准是闭环校准出来的，不是算出来的**
自相关估基频在带衰减包络的信号上有系统性偏差——实测两件乐器一个偏低 20 音分、
一个偏高 19 音分。所以成品出来后用 FFT 再测一次，迭代拉回参考音高。
现在整块音域的偏差在 **±0.3 音分**以内。

## 用法

```bash
# 正式素材（Commons 那条 CC BY 2.5 的座头鲸录音）
bot\venv\Scripts\python.exe whale_song.py --raw-low samples\commons-humpback.oga --play

# 长尾巴版（鲸的尾音本来就长，默认 2.6 秒，可以拉到 4–6）
bot\venv\Scripts\python.exe whale_song.py --raw-low samples\commons-humpback.oga --tail 5 --play

# 只用合成的占位呼叫（不涉及版权，用来验证管线）
bot\venv\Scripts\python.exe whale_song.py --play

# 只看乐器音准（不出旋律）
bot\venv\Scripts\python.exe whale_song.py --inspect

# 干声：不加 pad / 混响 / 失真，用来单独判断「这件乐器本身好不好听」
bot\venv\Scripts\python.exe whale_song.py --dry --out whale-dry.wav

# 换素材：低音区/高音区可以各用一段
bot\venv\Scripts\python.exe whale_song.py --raw-low samples\a.oga --raw-high samples\b.oga --play
```

### 主要参数

| 参数 | 默认 | 干什么 |
|---|---|---|
| `--raw-low` / `--raw-high` | 无（用合成） | 原始呼叫；只给一个时**整块音域都用它**（不给高音区就会掉回合成音，听感立刻变成「前半像人声、后面像钢琴」） |
| `--f0-low` / `--f0-high` | 0 = 自动 | 基频提示；自动模式会先找最响的 1 秒估一个大致基频，再围着它找最稳的 300ms |
| `--bend` | 55 | 音内滑音幅度（音分）。这是「鲸 vs 钢琴」的关键；0 = 回到直音 |
| `--lp` | 3200 | 低通截止（Hz）。砍高频去「金属感」；0 = 不低通 |
| `--tail` | 2.6 | 结尾留多少秒纯混响。鲸的长尾音用 4–6 |
| `--bpm` | 66 | 速度 |
| `--dry` | 关 | 关掉 pad / 混响 / 延迟 / 失真 |

素材读取只依赖标准库 `wave` + `ffmpeg`（mp3 / oga / webm 都能进），采样率、声道数自动处理。
`--raw-*` 的授权要求见 [CREDITS.md](CREDITS.md)——**现在默认推荐的那条是 CC BY 2.5，可商用、要署名**。


播放走 Windows MCI（`mciSendStringW`），和 `tts_local.py` 同一条路，**不新增常驻进程**。

## 现在的状态

- 呼叫是**合成的**（`make_raw_call`）：低频长滑音 moan + 带调幅的脉冲 pulse。
  只为先验证管线，不代表最终音色。
- 两件乐器按音域切：`midi < 67` 用 moan，`>= 67` 用 pulse——就是「换乐器」的手感。
- 谱子：勃拉姆斯摇篮曲前两句，C 大调，66 BPM，成品 21.4 秒（含 4 秒混响尾巴）。

## 换真录音时要改什么

只改 `make_raw_call` 那一步：把合成的数组换成从 wav 读进来的单声道 float32，
其余（找稳段、切音、升调、循环、包络、混音）全部不动。

要动的只有 `steady_window` 的 `f0_hint`——真录音没有先验，传 0 让它在 15–60Hz 里自己搜。

素材建议先只用公共领域/CC：NOAA 的鲸类声学档案、蒙特雷湾水族馆研究所（MBARI）公开录音。
