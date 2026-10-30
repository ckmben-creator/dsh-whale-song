# 发布步骤（给人看的）

> 代码这边已经就绪：git 仓库建好了、第一个 commit 是 `8c2583f`、13 个文件、
> **只带 CC BY 2.5 那条素材**（Pikbest 和 YouTube 那两条已移出仓库，留在 `archive/private-samples/`）。
> 剩下的只有两处**必须你本人登录**的关口。

## 已经做好的（不用你管）

- 装了 `git 2.56` 和 `GitHub CLI 2.103`（winget）
- `git init -b main` + 首次提交，`.gitignore` 排掉 `out/ *.wav archive/`
- `.gitattributes` 固定成 LF，避免 Windows 换行污染
- 包名查过：`dsh-whale-song` 在 npm 上**还没人用**（HTTP 404）

## 关口一：GitHub 登录（你操作一次）

在 PowerShell 里跑（这台机器 PATH 还没刷新，用绝对路径）：

```powershell
& "C:\Program Files\GitHub CLI\gh.exe" auth login
```

会问三下，这样选：

1. **What account?** → `GitHub.com`
2. **Preferred protocol?** → `HTTPS`
3. **Authenticate Git with your GitHub credentials?** → `Y`

然后它给一串一次性代码（形如 `ABCD-1234`），回车会打开浏览器，把代码粘进去、点 Authorize。

**完成后跟我说一声**，我接着跑：

```powershell
gh repo create dsh-whale-song --public --source=. --push \
  --description "Whale-song instrument for DeepSeek Harness"
```

这一条会：建公开仓库 → 配好 remote → 把 `main` 推上去。

## 关口二：npm token（你操作一次，别把 token 贴进聊天）

1. 打开 https://www.npmjs.com/settings/~/tokens → **Generate New Token** → 选 **Granular Access Token**
2. Name 填 `dsh-whale-song`；Expiration 选 90 天；Permissions 选 **Read and write**（Packages）
3. 生成后复制那串 `npm_...`
4. **你自己**写进 `C:\Users\ckmbe\.npmrc`（一行就够，注意开头两个斜杠）：

```
//registry.npmjs.org/:_authToken=npm_你的token
```

> 为什么不让你贴给我：`~/.npmrc` 里的 token 等于账号写权限，
> 和 cookies 一样属于「别进聊天、别进仓库」的东西。写进文件我能用，但不会经过对话。

**完成后跟我说一声**，我跑：

```powershell
node "C:\Users\ckmbe\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\pnpm\bin\pnpm.mjs" publish --access public --no-git-checks
```

## 发布之后要验的三件事

1. `npm view dsh-whale-song version` 能读到 0.1.0
2. 换一台机器 / 干净环境跑 `dsh plugin add dsh-whale-song` 能装上
3. GitHub 仓库页面上，README 第一屏是那句「為我的大肥魚做的鯨音樂器」

## 注意

- **不要**把 `samples/blue whale.mp3`（Pikbest，不可商用）和 `humpback.webm`（YouTube，授权不明）
  再收进仓库——它们现在在 `archive/private-samples/`，已被 `.gitignore` 排除
- npm 上首发之后就不能删包名了，只能 deprecate。真要重命名，趁现在
- 版本号在 `package.json` 的 `version`；每次 `publish` 前记得加
