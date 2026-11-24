# Antigravity 状态栏（antigravity-status-bar）

**简体中文** | [English](README.en.md)

---

一款专为 **Antigravity 2.0 桌面端**打造的实时侧边状态栏插件——直接只读解析会话本地数据库（SQLite + Protobuf），**每次刷新消耗 0 Token**。

<p align="center">
  <img src="docs/img/preview.png" alt="Antigravity 状态栏效果预览" width="340">
</p>

## 核心功能

- **Claude Code 风格分段上下文条**：按 `System / User / Agent / Tools / Custom / SysMsg` 分色展示上下文占用比例，内置**裁剪/压缩阈值刻度线**，悬停任一色段可实时查看数值并联动高亮明细行。
- **合并式 Token Usage（含缓存命中率 & 输出思考占比）**：汇总消息、系统提示词、工具、Rules、Skills、Subagents、Plugins、MCP 等各维度 Token；底部紧凑单行直接展示 `Cache hit`（单次 + 会话累计命中率）与 `Output`（累计输出 + 思考 Token 占比）。
- **会话等值 API 费用估算（Estimated Cost）**：按 Google Gemini / Anthropic Claude 官方公开 API 牌价折算等值美元消耗（支持按生效日期切价与长上下文阶梯定价），`Total` 与 `Subagents` 各自提供独立折叠栏，点击即可分别展开查看各模型费用明细（参考估算，非订阅实际扣费）。
- **多模型用量分布（Models）**：适配 Orchestrator 混用模型场景，自动按基础模型归并不同思考强度，展示当前会话各模型的累计 Token 与占比（单模型会话自动隐藏，详细调用次数与缓存命中率悬停可见）。
- **后台任务 & Subagent 实时监控**：自动捕捉当前会话的后台命令（`run_command`）、守护进程（`Daemon`）、定时器（`Timer`）及子 Agent（`Subagent`）；点击即可展开查看命令 / Prompt（超长时截断至 32K 字符）、执行状态与实时日志尾部（末尾 32 KB）。
- **可折叠 Checkpoints 时间线**：自动提炼最近用户请求作为 Checkpoint 语义标题，当前生效项以 `Active` 标识，点击即可展开历史归档记录与时间戳。
- **零额外消耗 & 自适应主题**：通过 Antigravity Sidecar 每 3 秒只读轮询，模型上下文上限自动同步应用内实时目录，原生自适应明暗主题。

## 快捷安装

### 方式一：直接复制给 Agent 安装（推荐）

把下面这段提示词直接发给 Antigravity（需要本机已安装 `git` 与 `Python 3.9+`）：

```text
请帮我安装「Antigravity 状态栏」插件：
1. 将 https://github.com/ballShell/antigravity-status-bar.git 克隆到 ~/.gemini/config/plugins/antigravity-status-bar（若目录已存在则拉取最新代码）。
2. 完成后检查插件结构是否完整，并提醒我前往 Antigravity 的 Customizations → Installed 开启「Antigravity 状态栏」，然后在右侧面板打开使用。
```

### 方式二：手动一行命令安装

**macOS / Linux：**

```bash
git clone https://github.com/ballShell/antigravity-status-bar.git ~/.gemini/config/plugins/antigravity-status-bar
```

**Windows (PowerShell)：**

```powershell
git clone https://github.com/ballShell/antigravity-status-bar.git "$HOME\.gemini\config\plugins\antigravity-status-bar"
```

安装后重启 Antigravity（或进入 **Customizations → Installed** 开启 **Antigravity 状态栏**），即可在右侧辅助栏 `+` 菜单中打开。

## 致谢

本项目 Fork 自 [dimitr-io/antigravity-desktop-context](https://github.com/dimitr-io/antigravity-desktop-context)，由衷感谢原作者在 Antigravity 会话库 Protobuf 解析、实时模型目录同步与 Sidecar 面板架构上的扎实工作。

基于 [MIT](LICENSE) 协议开源。
