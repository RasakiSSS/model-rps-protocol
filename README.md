# Model RPS

使用 Python 和 CLI（命令行界面），让两个独立的大语言模型通过交流和自主出拳完成一局猜拳博弈。

第一版提供 DeepSeek V4.1 Flash 和 GPT-6 Luna 的接口适配。模型负责策略与交流，Python 程序负责游戏规则、状态、信息可见性和结果判定。

详细规则见 [项目协议 v0.1](model-rps-protocol-v0.1.md)。

## 当前进度

裁判规则、Tick 并发调度、响应校验与重试、模拟玩家、模型适配器和 CLI 均已实现。本地 37 项自动化测试与模拟对局已通过，首局真实模型联调也已完成。

2026-10-14 联调结果：通过下方指定的兼容服务，双方在第 1 Tick 提交出拳；DeepSeek V4.1 Flash 出布，GPT-6 Luna 出石头，裁判判定 DeepSeek 获胜。对局未触发重试，程序正常退出。

## 安装与首次运行

在项目根目录打开 PowerShell，使用现有 `.venv`（Python 3.12.6）：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m model_rps
```

当前工作区已完成安装。`-e .` 表示以可编辑方式安装，修改源文件后无需重复安装；依赖声明变化时再执行安装命令。

默认是模拟模式，不需要密钥、不请求真实模型。默认场景在第 2 Tick 演示 A 改拳、B 首次出拳，最终 B 获胜。

## 模拟场景

```powershell
.\.venv\Scripts\python.exe -m model_rps --scenario deadline
```

| `--scenario` | 展示内容 |
| --- | --- |
| `change` | 默认场景：出拳后修改，按最终拳型判定 |
| `draw` | 双方一直等待，20 Tick 后平局 |
| `forfeit` | 仅 A 出拳，B 到期弃权 |
| `deadline` | 第 20 Tick 双方出拳，先处理动作再判定 |
| `retry` | WAIT 混用被拒绝，在同一 Tick 纠正后完成对局 |

## 接入真实模型

在项目根目录的 `local_secrets.py` 中填写两个字符串常量：

```python
DEEPSEEK_API_KEY = "你的 DeepSeek API key"
OPENAI_API_KEY = "你的 OpenAI API key"
```

该文件由 `.gitignore` 排除；重新克隆仓库时需要自行创建。程序只读取这两个字符串常量，不执行密钥文件里的其他 Python 语句。

```powershell
.\.venv\Scripts\python.exe -m model_rps --mode live
```

`live` 模式会向两个服务发起实际 API 请求。密钥缺失、为空或格式不正确时，程序会在发起请求前退出。

| 玩家 | 默认 API 基础地址 | 默认模型标识 |
| --- | --- | --- |
| A | `https://ai.yuhuantin.icu/v1` | `deepseek-v4.1-flash` |
| B | `https://ai.yuhuantin.icu/v1` | `gpt-6-luna` |

默认地址为本项目选用的兼容服务。2026-10-14 已通过该服务的 `/models` 接口核对两个密钥的鉴权和以上模型标识；每个玩家分别使用自己的密钥。

程序自动在基础地址后追加 `/chat/completions`。可以通过 `--deepseek-base-url`、`--openai-base-url`、`--deepseek-model`、`--openai-model` 覆盖地址或模型标识。覆盖模型名称仍使用该玩家的参数方案；适配不兼容的模型时，需要在 `players.py` 中调整 `ModelSpec` 或新增实现 `Player.respond` 的适配器。

本版 DeepSeek 使用非思考模式，GPT-6 Luna 使用 `reasoning_effort="none"`。两者都请求 JSON 输出，单次输出上限为 2048 token。调用方式依据 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 和 [OpenAI Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

## 超时、重试与退出状态

- 每次请求总时限默认 60 秒，可用 `--timeout 120` 调整。
- 每位玩家每个 Tick 最多请求 3 次，网络错误和格式纠错共用额度。
- 真实模式每次重试间隔 1 秒，模拟模式不增加等待时间。
- HTTP 408、409、429 和 5xx 可重试；其他 HTTP 错误（例如 401）直接中止，提示状态码。
- 重试时只请求出错的一方，仍使用同一份 Tick 开始状态。
- 错误详情不会打印密钥、服务器原文或结算前的拳型。

退出码：`0` 表示正常完成，`1` 表示技术异常中止，`2` 表示参数或本地配置错误，`130` 表示用户按 Ctrl+C 中止。

更多参数：

```powershell
.\.venv\Scripts\python.exe -m model_rps --help
```

## 项目结构

```text
model-rps-protocol/
├─ README.md                    项目介绍、环境与进度说明
├─ model-rps-protocol-v0.1.md    游戏规则与验收标准
├─ pyproject.toml               Python 项目信息与依赖声明
├─ .gitignore                   Git 忽略规则
├─ local_secrets.py             本地 API key，不纳入 Git
├─ .venv/                       已有的本地 Python 环境，不纳入 Git
├─ src/
│  └─ model_rps/
│     ├─ __init__.py            Python 包标记与说明
│     ├─ __main__.py            python -m model_rps 入口
│     ├─ game.py                状态、动作执行与结果判定
│     ├─ runner.py              Tick 推进、并发请求与重试协调
│     ├─ players.py             模型接入与响应处理
│     └─ cli.py                 命令行入口与事件展示
└─ tests/
   ├─ test_game.py              裁判、动作校验、可见性和到期判定
   ├─ test_runner.py            并发、重试、超时与取消
   ├─ test_players.py           模拟 HTTP 请求与模型响应处理
   └─ test_cli.py               命令行场景、密钥读取和展示
```

## 运行测试

测试使用 Python 标准库 `unittest`；模型请求通过 HTTPX 的模拟传输验证，不需要真实密钥。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

核心覆盖：九种拳型组合、跨 Tick 改拳、同一响应多个动作、结算前保密、10/18/19 Tick 提醒、到期弃权或平局、最后 Tick 行动、整份响应拒绝、同 Tick 信息隔离、有限重试、HTTP 错误、请求取消和 CLI 场景。
