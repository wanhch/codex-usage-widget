# Codex Usage Widget

一个轻量的 Windows 置顶小窗，左侧显示 CPU、内存和 GPU 小仪表盘，右侧用圆形进度环显示 Codex 与 Kimi for Coding Token Plan 的剩余用量，并在圆内显示 reset 倒计时。`G7` 表示 Codex 7 天额度，`K5`、`K7` 表示 Kimi 5 小时和 7 天额度。系统仪表盘不显示数字，已占用弧段会从浅色逐步过渡到深红。Codex 和 Kimi 数据均通过本机 CLI 的只读接口获取，不需要启动浏览器：

挂件会自动采样 Windows 任务栏底色，并据此切换文字和轨道的明暗对比；GPT 使用灰黑色系，Kimi 使用蓝色系。

https://chatgpt.com/codex/cloud/settings/analytics#usage

## 使用

1. 双击 `Start-CodexUsageWidget.cmd`。
2. 小窗每 30 秒向本机 Codex CLI 查询一次额度；圆内浅色底色会按 reset 剩余时间显示成小秒表效果。CPU/内存默认每 1 秒刷新一次。
3. 需要已经安装并登录 Codex CLI 和 Kimi Code CLI。程序会自动寻找它们，也可用 `codex_cli_path`、`kimi_cli_path` 指定可执行文件。

## 操作

- 拖动小窗：左键按住空白区域拖动。
- 打开用量页：双击小窗，或右键选择 `Open usage page`。这只在默认浏览器中打开页面，不参与数据采集。
- 立即刷新：右键选择 `Refresh now`。
- 退出：右键选择 `Quit`，或点右上角 `x`。

## 配置

配置文件是 `config.json`。常用项：

- `poll_seconds`: 默认 `30`。
- `data_source`: 默认 `codex_cli`，通过 Codex CLI 直接查询。设为 `web` 才会启用旧的独立 Edge 页面采集模式。
- `codex_cli_path`: 默认自动寻找 Codex CLI；找不到时可填写 `codex.exe` 的完整路径。
- `kimi_cli_path`: 默认自动寻找 Kimi Code CLI；找不到时可填写 `kimi.exe` 的完整路径。
- `show_kimi_usage`: 默认 `true`，显示 Kimi 的 `K5`、`K7` 两个额度环。
- `system_poll_seconds`: 默认 `1`，控制 CPU、内存和 GPU 仪表盘刷新频率。
- `gauge_animation_ms`: 默认 `100`，控制 CPU/内存指针动画刷新间隔，不影响系统采样频率。
- `browser_mode`: 仅在 `data_source` 为 `web` 时使用。`visible` 使用可见的独立 Edge 采集窗口；`hidden` 会把完整 Edge 放到屏幕外并最小化；`headless` 更轻但可能被页面校验拦截。
- `refresh_page_each_poll`: 默认 `false`。如果页面不会自己更新，把它改成 `true`，程序会每轮轮询刷新页面，网络和 CPU 占用会稍高。
- `minimize_edge_after_data`: 默认 `true`。
- `close_edge_on_exit`: 默认 `true`。

调试抓取结果会写到 `last_capture.txt`，运行日志在 `widget.log`。

## 命令行查询

无需打开挂件或浏览器即可输出 JSON：

```powershell
python .\codex_usage_widget.py --json
```

## 打包版

打包后的目录在 `dist\CodexUsageWidget`，可直接运行：

```powershell
dist\CodexUsageWidget\CodexUsageWidget.exe
```

要复制到其他 Windows 电脑，复制整个 `dist\CodexUsageWidget` 目录，或解压 `dist\CodexUsageWidget.zip`。目标电脑不需要安装 Python，但默认数据源要求安装并登录 Codex CLI；只有使用 `web` 数据源时才要求 Microsoft Edge。请放在当前用户可写的位置，例如桌面或文档目录，因为程序会在同目录创建 `config.json`、`widget.log` 和 `last_capture.txt`。

重新打包运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Build-CodexUsageWidget.ps1
```

## 说明

Windows 任务栏原生嵌入需要 Explorer shell 扩展/DeskBand，复杂度和常驻成本都明显更高。这个版本采用无边框、置顶、可拖动小窗，默认贴近右下角任务栏区域，窗口尺寸约为 `334x48`。
