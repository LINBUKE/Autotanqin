# 风物之诗琴自动化工具

把歌曲伴奏（MP3/WAV）自动转成可用的按键谱，并提供可视化微调、四点校准、逐键延迟补偿与模拟点击播放。

> ，不注入、不读内存、不修改游戏文件，不连接《原神》游戏本体。个人研究使用

## 流程

```
音频 MP3/WAV → ① 音频转 MIDI → ② MIDI 映射 21 白键 清理碎音块 → ③ 浏览器编辑器微调
            → ④ 四点定位校准 → ⑤ 逐键延迟采样/补偿 → ⑥ 模拟点击播放 → ⑦ 端到端测试
```

## 目录

```
piano_tool_project/
├── piano_tool/        # Python 模块（转写/映射/定位/延迟探测/播放/CLI）
├── editor/editor.html # 单文件可视化编辑器（零依赖，双击即开）
├── tests/             # pytest 单元测试
├── data/              # 输入/输出/校准数据
├── requirements.txt
├── run.py             # 一键启动菜单
├── 启动.bat            # 用户入口（双击）
├── launcher.ps1       # Python 环境检测 / 自动安装
├── bootstrap.py       # venv 健康检查 / 依赖自动安装（自动换源）
└── install_python.ps1 # Python 3.10 自动下载安装
```

## 安装（全自动）

**Windows：双击 `启动.bat` 即可**，脚本会自动完成：

1. 检测 Python：兼容窗口 **3.9 ~ 3.11**（basic-pitch 0.4.0 限制）。版本太新（3.12/3.13）或太老时会**明确提示原因**，并自动下载安装一个独立的 Python 3.10（仅装到当前用户、无需管理员权限，华为云 / npmmirror / python.org 源自动切换，不影响电脑上已有的 Python）。Microsoft Store 的 python 占位符会被识别并跳过
2. 自动创建 `.venv`；已有 venv 会做三重健康检查——解释器能跑、pyvenv.cfg 路径一致（**从别的目录/电脑拷过来的 venv 会被识别**）、关键库能真正导入（抓 DLL 损坏）；不通过则先备份再自动重建，失败可回滚
3. 安装依赖；某镜像下载失败/超时自动换源：清华 TUNA → 阿里云 → 中科大 → 华为云 → PyPI 官方。Python 3.10 走 onnxruntime 后端（无需下载 284MB 的 tensorflow）
4. 自动启动桌面版 GUI

启动网页控制台 / 命令行菜单：`启动.bat web` 或 `启动.bat menu`。

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
1. **音频 → MIDI**：上传 MP3/WAV 转写
2. **MIDI → 琴谱**：映射为 events.json，并可一键在「音符编辑器」中微调。
3. **四点校准**：上传模拟器截图，在网页上点 左上→右上→右下→左下 四角，自动算 21 键坐标（比 OpenCV 弹窗更方便）。
4. **模拟播放**：按琴谱+坐标模拟点击；支持 dry-run 试跑与真实播放，急停把鼠标移到屏幕左上角。

顶部「打开音符编辑器」按钮即原 `editor/editor.html`，并支持 `?events=` 自动载入琴谱。




## 编辑器操作

| 操作 | 效果 |
|---|---|
| 拖动音符上下 | 改音高，实时试听 |
| 拖动音符左右 | 改时间（吸附 10ms 网格） |
| 双击空白 | 添加音符 |
| 右键音符 | 删除 |
| 缩放输入框 | 改时间轴比例（px/秒） |


黑键受物理限制会被就近替换为白键，听感必然变化。
