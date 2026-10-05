# 风物之诗琴自动化工具

把歌曲伴奏（MP3/WAV）自动转成**浏览器模拟器**可用的按键谱，并提供可视化微调、四点校准、逐键延迟补偿、键盘弹奏与模拟点击播放。

> ⚠️ 安全边界：本工具**仅针对浏览器版风物之诗琴模拟器**，不注入、不读内存、不修改游戏文件，不连接《原神》游戏本体。把同类脚本用于游戏本体风险自负。

## 流程

```
音频 MP3/WAV → ① 音频转 MIDI → ② MIDI 映射 21 白键 → ③ 浏览器编辑器微调
            → ④ 四点定位校准 → ⑤ 逐键延迟采样/补偿 → ⑥ 模拟点击 / 键盘弹奏 → ⑦ 端到端测试
```

## 目录

```
piano_tool_project/
├── piano_tool/        # Python 模块（转写/映射/定位/延迟探测/播放/键盘弹奏/CLI）
├── editor/editor.html # 单文件可视化编辑器（零依赖，双击即开）
├── 测试音乐/           # 内置测试用原声
├── tests/             # pytest 单元测试（106 项）
├── data/              # 输入/输出/校准数据
├── requirements.txt
├── run.py             # 一键启动菜单
├── 启动.bat            # 用户入口（双击）
├── launcher.ps1       # Python 环境检测 / 自动安装
├── bootstrap.py       # venv 健康检查 / 依赖自动安装（自动换源）
├── install_python.ps1 # Python 3.10 自动下载安装
└── README.md
```

## 安装（全自动）

**Windows：双击 `启动.bat` 即可**，脚本会自动完成：

1. 检测 Python：兼容窗口 **3.9 ~ 3.11**（basic-pitch 0.4.0 限制）。版本太新（3.12/3.13）或太老时会**明确提示原因**，并自动下载安装一个独立的 Python 3.10（仅装到当前用户、无需管理员权限，华为云 / npmmirror / python.org 源自动切换，不影响电脑上已有的 Python）。Microsoft Store 的 python 占位符会被识别并跳过
2. 自动创建 `.venv`；已有 venv 做健康检查——解释器能跑、pyvenv.cfg 路径一致（**从别的目录/电脑拷过来的会被识别**）、关键库真正导入（抓 DLL 损坏）；不通过则先备份再自动重建，失败可回滚
3. 安装依赖；某镜像失败/超时自动换源：清华 TUNA → 阿里云 → 中科大 → 华为云 → PyPI 官方；证书校验失败（代理/安全软件 SSL 检查）自动按可信主机重试。Python 3.10 走 onnxruntime 后端（无需下载 284MB 的 tensorflow）
4. 自动启动桌面版 GUI

启动网页控制台 / 命令行菜单：`启动.bat web` 或 `启动.bat menu`。

> 若系统装了代理加速工具（如 Watt Toolkit / Steam++）且其代理例外列表不含 `127.0.0.1`，浏览器打开本地编辑器可能失败，在代理工具里把 `127.0.0.1;localhost` 加入例外即可。

手动安装（高级用户）：

```bash
cd piano_tool_project
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

## 用法

### 网页控制台（推荐）

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
