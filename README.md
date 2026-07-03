# Codex Usage Widget

一个轻量的 Windows 置顶小窗，左侧显示 CPU/内存小仪表盘，右侧用两个圆形进度环显示 5 小时和 7 天 Codex 用量，并在圆内显示 reset 倒计时。Codex 数据通过独立 Edge 配置读取：

https://chatgpt.com/codex/cloud/settings/analytics#usage

## 使用

1. 双击 `Start-CodexUsageWidget.cmd`。
2. 默认会打开一个独立 Edge 采集窗口。首次登录或登录失效时，请在这个窗口里登录 ChatGPT；也可以双击小窗或右键选择 `Open collector page` 重新打开采集页。
3. 小窗每 30 秒读取一次页面文本，显示 5h 和 7d 百分比；圆内浅色底色会按 reset 剩余时间显示成小秒表效果。CPU/内存默认每 1 秒刷新一次。
4. 成功读到数据后，采集用的 Edge 窗口会按配置自动最小化。

## 操作

- 拖动小窗：左键按住空白区域拖动。
- 打开采集页：双击小窗，或右键选择 `Open collector page`。
- 立即刷新：右键选择 `Refresh now`。
- 退出：右键选择 `Quit`，或点右上角 `x`。

## 配置

配置文件是 `config.json`。常用项：

- `poll_seconds`: 默认 `30`。
- `system_poll_seconds`: 默认 `1`，控制 CPU/内存仪表盘刷新频率。
- `browser_mode`: 默认 `visible`，使用可见的独立 Edge 采集窗口。`hidden` 会把完整 Edge 放到屏幕外并最小化；`headless` 更轻但可能被页面校验拦截。
- `refresh_page_each_poll`: 默认 `false`。如果页面不会自己更新，把它改成 `true`，程序会每轮轮询刷新页面，网络和 CPU 占用会稍高。
- `minimize_edge_after_data`: 默认 `true`。
- `close_edge_on_exit`: 默认 `true`。

调试抓取结果会写到 `last_capture.txt`，运行日志在 `widget.log`。

## 打包版

打包后的目录在 `dist\CodexUsageWidget`，可直接运行：

```powershell
dist\CodexUsageWidget\CodexUsageWidget.exe
```

要复制到其他 Windows 电脑，复制整个 `dist\CodexUsageWidget` 目录，或解压 `dist\CodexUsageWidget.zip`。目标电脑需要安装 Microsoft Edge；不需要安装 Python。请放在当前用户可写的位置，例如桌面或文档目录，因为程序会在同目录创建 `config.json`、`edge-profile`、`widget.log` 和 `last_capture.txt`。

重新打包运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Build-CodexUsageWidget.ps1
```

## 说明

Windows 任务栏原生嵌入需要 Explorer shell 扩展/DeskBand，复杂度和常驻成本都明显更高。这个版本采用无边框、置顶、可拖动小窗，默认贴近右下角任务栏区域，窗口尺寸约为 `150x72`。
