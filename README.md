# 风物之诗琴自动化工具

把歌曲伴奏（MP3/WAV）自动转成**浏览器模拟器**可用的按键谱，并提供可视化微调、四点校准与模拟点击播放。

> ⚠️ 安全边界：本工具**仅针对浏览器版风物之诗琴模拟器**，不注入、不读内存、不修改游戏文件，不连接《原神》游戏本体。把同类脚本用于游戏本体风险自负。

## 流程

```
音频 MP3/WAV → ① 音频转 MIDI → ② MIDI 映射 21 白键 → ③ 浏览器编辑器微调
            → ④ 四点定位校准 → ⑤ 模拟点击播放 → ⑥ 端到端测试
```

## 目录

```
piano_tool_project/
├── 启动.bat            # 用户入口：启动桌面版
├── desktop_app.py      # 桌面版主程序（Tkinter）
├── app.py              # 网页控制台（Flask）
├── run.py              # 一键启动菜单
├── piano_tool/         # Python 模块（转写/分离/映射/定位/播放/CLI）
├── editor/editor.html  # 单文件可视化编辑器（零依赖，双击即开）
├── webui/              # 网页控制台模板
├── tests/              # pytest 单元测试
├── data/               # 输入/输出/校准数据
├── skills/             # 运维知识（更新后清理流程）
├── requirements.txt
├── 更新日志.md          # 版本变更记录
├── 代码索引.md          # 代码地图（改代码前先看）
└── README.md
```

## 安装

### 自动（推荐，给没有 Python 的电脑用）

直接双击 **`启动.bat`**，环境引导会自动完成全部准备，无需手动装任何东西：

1. 检测本机真实 Python（自动排除微软商店的 python 占位符），要求 3.9 ≤ 版本 < 3.12
2. 没有兼容 Python 时，自动从国内镜像下载并静默安装 Python 3.10.11（用户级，不需要管理员）
3. 自动创建 `.venv` 并通过清华/阿里云/中科大/华为云镜像依次安装依赖（断网或 SSL 劫持会自动换源重试）
4. 关键库做真实导入冒烟，损坏会自动补装

引导相关文件：`启动.bat` → `launcher.ps1`（检测/Python/venv）→ `install_python.ps1`（装 Python）→ `bootstrap.py`（装依赖）。菜单模式：`启动.bat menu`。

### 手动

```bash
cd piano_tool_project
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

> basic-pitch 0.4.x 使用 onnxruntime 推理，无需 TensorFlow。人声分离为可选功能：`pip install demucs`（会附带 CPU 版 PyTorch，约 200 MB）。

## 用法

### 桌面版（推荐）

双击 `启动.bat`，或：

```bash
python desktop_app.py
```

全部流程（转写、可选人声分离、映射、编辑器、四点校准、模拟播放）在一个 Tkinter 窗口内完成。

### 网页控制台

本地起一个 Flask 服务，所有操作在浏览器里点完，无需命令行：

```bash
pip install flask          # 已加入 requirements.txt
python app.py              # 自动打开 http://127.0.0.1:8000
```

控制台包含四个步骤卡片：
1. **音频 → MIDI**：上传 MP3/WAV 转写（需 basic-pitch）。
2. **MIDI → 琴谱**：映射为 events.json，并可一键在「音符编辑器」中微调。
3. **四点校准**：上传模拟器截图，在网页上点 左上→右上→右下→左下 四角，自动算 21 键坐标（比 OpenCV 弹窗更方便）。
4. **模拟播放**：按琴谱+坐标模拟点击；支持 dry-run 试跑与真实播放，急停把鼠标移到屏幕左上角。

顶部「打开音符编辑器」按钮即原 `editor/editor.html`，并支持 `?events=` 自动载入琴谱。

### 一键菜单

```bash
python run.py              # 选 1 启动网页控制台
```

### CLI（typer）

```bash
python -m piano_tool convert  data/input/song.mp3 -o data/output/song.mid
python -m piano_tool map      data/output/song.mid -o data/output/events.json
python -m piano_tool calibrate
python -m piano_tool play     data/output/events.json --speed 1.0
python -m piano_tool test-simulator --midi data/output/song.mid
```

可视化编辑器：浏览器打开 `editor/editor.html`，导入 `events.json`，拖动微调后导出。

## 测试

```bash
pytest tests/ -v
```

## 编辑器操作

| 操作 | 效果 |
|---|---|
| 拖动音符上下 | 改音高，实时试听 |
| 拖动音符左右 | 改时间（吸附 10ms 网格） |
| 双击空白 | 添加音符 |
| 右键音符 | 删除 |
| 缩放输入框 | 改时间轴比例（px/秒） |

## 21 白键布局

C4(60)~B6(95)，键位 `Z X C V B N M / A S D F G H J / Q W E R T Y U`。
黑键受物理限制会被就近替换为白键，听感必然变化。
