/**
 * dsh-whale-song —— 把鲸的呼叫做成一件能弹的乐器（DSH 工具封装）。
 *
 * 两个工具：
 *   whale_song —— 弹一段：切音、移调、混音、写盘、本机播放
 *   whale_form —— 切形态（人形=说话 / 鲸形=鲸歌），状态存在 ~/.dsh/whale-form.json
 *
 * 引擎仍是 whale_song.py（纯 numpy），这里只做三件事：收参数、跑子进程、交结果。
 * 为什么引擎不改成 JS：切音/升调/循环/混响全是一维数组运算，Python 版已经调了整晚、
 * 有逐音校验兜底；重写只会把 bug 重新引一遍。
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'dsh-whale-song';
// ⚠ attachments 必须写进 inject，否则 ctx.attachments 会抛
//   `cannot get property "attachments" without inject`——不是没这个服务，是没申请。
export const inject = ['tools', 'attachments'];

const HERE = dirname(fileURLToPath(import.meta.url));
const ENGINE = join(HERE, 'whale_song.py');
const DEFAULT_SAMPLE = join(HERE, 'samples', 'commons-humpback.oga');
const OUT_DIR = join(HERE, 'out');
const FORM_FILE = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'whale-form.json');

/** 找解释器：环境变量 > bot 的 venv（有 numpy）> PATH 上的 python */
function pythonExe() {
  const cands = [
    process.env.DSH_WHALE_PYTHON,
    'C:/Users/ckmbe/Documents/deepseek-harness/default-workspace/bot/venv/Scripts/python.exe',
    'python',
  ].filter(Boolean);
  for (const c of cands) {
    if (c === 'python') return c;
    if (existsSync(c)) return c;
  }
  return 'python';
}

function runEngine(args, { timeoutMs = 300_000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonExe(), [ENGINE, ...args], {
      cwd: HERE,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    });
    let out = '';
    let err = '';
    child.stdout.on('data', (c) => { out += c.toString(); });
    child.stderr.on('data', (c) => { err += c.toString(); });
    child.on('error', (e) => reject(new Error(`跑不起 python：${e.message}`)));
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`引擎超过 ${Math.round(timeoutMs / 1000)} 秒没跑完`));
    }, timeoutMs);
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) reject(new Error(`引擎退出码 ${code}\n${(err || out).slice(-600)}`));
      else resolve({ stdout: out, stderr: err });
    });
  });
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

/* ------------------------------------------------------------------ 形态 */

const FORMS = {
  human: { label: '人形', channel: '说话', hint: '走 tts（现在是 edge-tts，将来可能换 CosyVoice）' },
  whale: { label: '鲸形', channel: '鲸歌', hint: '走这件乐器，按音阶唱' },
};

function readForm() {
  try {
    const j = JSON.parse(readFileSync(FORM_FILE, 'utf8'));
    if (j && (j.form === 'human' || j.form === 'whale')) return j;
  } catch { /* 没有文件就是默认 */ }
  return { form: 'human', since: null };
}

function writeForm(form) {
  const rec = { form, since: new Date().toISOString() };
  try {
    mkdirSync(dirname(FORM_FILE), { recursive: true });
    writeFileSync(FORM_FILE, JSON.stringify(rec, null, 2), 'utf8');
  } catch { /* 写不进去也不该让工具失败 */ }
  return rec;
}

/* ------------------------------------------------------------------ 插件 */

export async function apply(ctx) {
  try {
    return await register(ctx);
  } catch (error) {
    try { ctx?.logger?.error?.(`[dsh-whale-song] failed to load: ${error?.stack ?? error}`); } catch { /* ignore */ }
    return () => {};
  }
}

async function register(ctx) {
  const { defineTool } = await import('@deepseek-ai/dsh-tools');

  const song = defineTool({
    name: 'whale_song',
    description: [
      '用鲸的呼叫做成的乐器，弹一段旋律并在本机播放（或只渲染）。',
      '',
      '一件乐器 = 一段原始呼叫里切出的「一个音」的采样，按半音移调铺满音域；',
      '换素材 = 换一件乐器。默认用 CC BY 2.5 的座头鲸录音（可商用、需署名）。',
      '',
      '可以弹内置摇篮曲，也可以自己写旋律：notes: "C4:1 E4:1 G4:2"。',
      '渲染一次约 2 秒。返回里带输出路径和用了哪份素材。',
    ].join('\n'),
    parameters: {
      raw: { type: 'string', description: '素材路径（wav/mp3/oga/webm 都行）；不传用内置的座头鲸录音' },
      tune: {
        type: 'string',
        enum: ['lullaby'],
        description: '曲目；只有 lullaby（勃拉姆斯摇篮曲前两句）。要弹别的用 notes 自己写',
      },
      notes: {
        type: 'string',
        description: '自定义旋律："C4:1 E4:1 G4:2 | r:1 A4:2"——音名 + 冒号 + 拍数（省略=1 拍），' +
          'r 是休止，竖线和逗号只当分隔符。低音层自动按每 4 拍取第一个音向下一个八度生成。',
      },
      bpm: { type: 'number', description: '速度，默认 66' },
      tail: { type: 'number', description: '结尾留多少秒混响，默认 2.6；鲸的长尾音用 4–6' },
      bend: { type: 'number', description: '音内滑音幅度（音分），默认 55；0 = 直音（会像钢琴）' },
      play: { type: 'boolean', description: '渲染完是否直接播放，默认 true' },
      attach: { type: 'boolean', description: '把 wav 提交成附件（能在聊天里点开），默认 false' },
      debug: { type: 'boolean', description: '内部用：返回可用的附件接口，不渲染' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          text: { type: 'string', required: true },
          file: { type: 'json', description: '可选：提交到附件库的 wav 引用' },
        },
      },
      render(args, value) {
        const blocks = [{ type: 'text', text: value.text }];
        if (value.file) blocks.push({ type: 'file', attachment: value.file });
        return blocks;
      },
    },
    timeoutMs: 300_000,
    isConcurrencySafe() {
      return false; // 播放是独占的，两个同时跑会抢 MCI
    },
    async execute(args) {
      if (args?.debug) {
        const c = ctx && ctx.attachments;
        return { text: 'attachments: ' + (c ? Object.keys(c).join(', ') : '(没有这个服务)') };
      }

      const raw = args?.raw || DEFAULT_SAMPLE;
      if (!existsSync(raw)) throw new Error(`找不到素材：${raw}`);
      if (!existsSync(ENGINE)) throw new Error(`找不到引擎：${ENGINE}`);

      mkdirSync(OUT_DIR, { recursive: true });
      const out = join(OUT_DIR, `whale-${stamp()}.wav`);

      const argv = ['--raw-low', raw, '--out', out];
      if (typeof args?.notes === 'string' && args.notes.trim()) argv.push('--notes', args.notes.trim());
      if (Number.isFinite(args?.bpm)) argv.push('--bpm', String(args.bpm));
      if (Number.isFinite(args?.tail)) argv.push('--tail', String(args.tail));
      if (Number.isFinite(args?.bend)) argv.push('--bend', String(args.bend));
      if (args?.play !== false) argv.push('--play');

      const t0 = Date.now();
      const { stdout } = await runEngine(argv);
      const took = ((Date.now() - t0) / 1000).toFixed(1);

      const size = existsSync(out) ? statSync(out).size : 0;
      const lastLine = stdout.trim().split('\n').filter(Boolean).pop() ?? '';

      // 唱过 = 当前是鲸形。这是「切形态」的自动那一半。
      const prev = readForm();
      if (prev.form !== 'whale') writeForm('whale');

      let fileRef = null;
      let attachNote = '';
      if (args?.attach === true) {
        try {
          if (typeof ctx?.attachments?.saveFile === 'function') {
            fileRef = await ctx.attachments.saveFile({
              data: readFileSync(out),
              name: basename(out),
            });
            attachNote = `已提交成附件：${fileRef.attachmentId ?? '(已存)'}`;
          } else {
            attachNote = '宿主没有 saveFile，附件没成——文件还在上面的路径。';
          }
        } catch (error) {
          attachNote = `附件没成：${error?.message ?? error}`;
        }
      }

      return {
        text: [
          `弹好了：${out}`,
          `用时 ${took}s，文件 ${(size / 1024 / 1024).toFixed(1)} MB`,
          `素材：${raw}`,
          lastLine,
          ...(attachNote ? ['', attachNote] : []),
          '',
          '素材是 CC BY 2.5（PLOS ONE / Wikimedia Commons），公开发布时要署名——',
          '署名文字在 CREDITS.md，别漏。',
        ].join('\n'),
        ...(fileRef ? { file: fileRef } : {}),
      };
    },
  });

  const form = defineTool({
    name: 'whale_form',
    description: [
      '切换「形态」——人形说话 / 鲸形唱歌。',
      '',
      '这是这件乐器的接口约定：切形态就是切声道。她一开口，你听得出现在是哪一态。',
      '状态存在 ~/.dsh/whale-form.json，跨会话、跨重启都在；弹过 whale_song 之后会自动变成鲸形。',
      '',
      'action=get 看现在是什么形态；action=human / whale 切过去。',
    ].join('\n'),
    parameters: {
      action: {
        type: 'string',
        required: true,
        enum: ['get', 'human', 'whale'],
        description: 'get 查询 / human 人形（说话） / whale 鲸形（鲸歌）',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: { text: { type: 'string', required: true } },
      },
      render(args, value) { return [{ type: 'text', text: value.text }]; },
    },
    timeoutMs: 10_000,
    isConcurrencySafe() { return true; },
    async execute(args) {
      const action = String(args?.action ?? 'get').toLowerCase();
      if (action === 'get') {
        const cur = readForm();
        const info = FORMS[cur.form];
        const since = cur.since ? new Date(cur.since).toLocaleString('zh-CN', { hour12: false }) : '不知道从什么时候起';
        return { text: `现在是${info.label}（${info.channel}）—— ${info.hint}。从 ${since} 起。` };
      }
      if (action === 'human' || action === 'whale') {
        const rec = writeForm(action);
        const info = FORMS[action];
        return { text: `切到${info.label}了：${info.channel}—— ${info.hint}。` };
      }
      throw new Error(`unknown action: ${args?.action}`);
    },
  });

  const disposers = [ctx.tools.register(song), ctx.tools.register(form)];
  return () => {
    for (const dispose of disposers) { try { dispose?.(); } catch { /* ignore */ } }
  };
}
