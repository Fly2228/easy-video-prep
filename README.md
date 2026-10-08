# easy-video-prep · 视频预处理工作流

把五个常用的视频处理步骤合并成**一套本地 WebUI**：左侧按阶段切换，每个阶段可单独使用，
也可以把上一阶段的产物直接喂给下一阶段，串成流水线一次跑完。

**全程本地运行**，不联网、不上传素材。除「打码」外不依赖任何 Python 第三方包。

## 五个阶段

| # | 阶段 | 做什么 | 依赖 |
|---|------|--------|------|
| 1 | ☀ 曝光调整 | 自动/手动提亮偏暗片段，支持 HDR(HLG/PQ)→SDR | ffmpeg |
| 2 | ✂ 剪切 & 合并 | 无损切割（`-c copy`）、多段合并、精确重编码切割 | ffmpeg |
| 3 | ⚡ 补帧 | RIFE ncnn Vulkan 插帧到目标帧率 | RIFE + ffmpeg |
| 4 | ▩ 打码 | 人体轮廓 / 人脸遮挡，支持画面框选「模糊区 / 忽略区」 | **内嵌 OpenScrub 引擎** |
| 5 | 🗜 压缩 | 按目标大小或画质压缩，自动选硬件编码器 | ffmpeg |
| ★ | 工作流编排 | 把任意几个阶段排成链，一次跑完 | — |

## 环境要求

- **Python 3.10+**（窗口系统用标准库 `ThreadingHTTPServer`）
- **ffmpeg / ffprobe**：放在 `PATH`，或 `C:\ffmpeg\bin` / `D:\ffmpeg\bin`
- **RIFE**（仅补帧阶段需要）：[rife-ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan/releases)
  下载后解压，在补帧页表单里指定 `rife-ncnn-vulkan.exe` 和模型目录（如 `rife-v4.6`）
- **打码阶段的 Python 依赖**：

  ```bash
  pip install -r requirements.txt
  # Windows + DirectX 12 显卡建议用 DirectML 版（NVIDIA/AMD/Intel 都能加速）：
  pip install onnxruntime-directml
  ```

## 启动

```bash
python app.py "你的素材目录"        # 目录可省略，启动后在顶栏填
```

Windows 也可以双击 `启动工作流.bat`（可把素材目录拖到它上面）。

浏览器打开终端里打印的 `http://127.0.0.1:8820/`（端口被占用会自动往后找）。

## 怎么用

1. **顶栏**填「素材」目录，按 **Enter** 载入 —— 左栏「素材」区会列出该目录下的视频，点一下即选中
2. 在 **左侧选阶段** → 调参数 → **运行**（当前素材和产物预览在中间，任务日志在右侧）
3. 任意阶段跑完后，产物会进入**当前素材**
4. 切到下一阶段，点 **↳ 用上一阶段产物** 即可接上
5. 想一次跑完：左栏底部 **★ 工作流编排** → 添加阶段 → 调顺序 → 运行

几个阶段特有的交互：

- **剪切页**：预览下方是时间轴，拖拽入/出点手柄或按 `I`/`O` 设点，`Enter` 写入参数，`Esc` 清除
- **曝光页**：自动分析整段后画出**亮度曲线**和**曝光补偿曲线**；在补偿曲线上点击可生成手动控制点
- **打码页**：在画面上**拖拽画框**，每个框标记为「模糊这块」或「忽略这块」；拖动可移动，拖右下角可缩放

## 打码阶段

打码**不重新实现**，而是把 [OpenScrub](https://github.com/austinmabry/OpenScrub) 的引擎源码
vendor 到 `third_party/OpenScrub/`，用 `importlib` 载入后**进程内直接调用**——
不需要另外装 OpenScrub，也不需要它的 `openscrub.exe`。

类别**锁死为 `person,face`**。这是刻意的安全约束：一旦包含文字类别，引擎会去下载 OCR
模型，引入一堆本阶段用不到的依赖。

### 画面框选（模糊区 / 忽略区）

| 模式 | 映射到引擎 | 含义 |
|---|---|---|
| 模糊这块 | `--zones` | **只在这些框内检测** person/face —— 等于「选中要糊的对象」 |
| 忽略这块 | `--ignore-region` | 这些框**绝不模糊** —— 保留对象 |

坐标是 0~1 归一化（自动扣掉竖屏黑边，只在实际画面区域内），随参数一起存进 `regions` 字段。

### 模型：用户自行下载，绝不随项目分发

**本仓库不包含任何模型权重。** 打码阶段会按需下载到你的用户数据目录
（Windows 下是 `%LOCALAPPDATA%\OpenScrub\`）。

| 模型 | 许可 | 说明 |
|---|---|---|
| **YOLO11-seg**（人体轮廓） | **AGPL-3.0** | 强 copyleft，**商用前务必确认** |
| **SCRFD det_10g**（人脸） | **非商业研究用途** | 不可商用 |
| CenterFace | MIT | 宽松 |
| YuNet / SFace / VitTrack（内置） | Apache-2.0 | 自动下载 |

> **许可证不是由一个文件决定的。** OpenScrub 的代码是 Apache-2.0，但模型各自独立授权。
> 上游刻意不打包这些模型，正是为了不把 AGPL / 非商业条款引入自己的项目——本项目沿用同一策略。
> 详见 [`third_party/OpenScrub/MODELS.md`](third_party/OpenScrub/MODELS.md)。
>
> ⚠️ **本项目不构成法律意见。** 若要公开发布或商用，请自行确认模型许可。

## 架构

```
easy-video-prep/
├── app.py                  统一 HTTP 服务（标准库，四个阶段零第三方依赖）
├── core/
│   ├── tool.py             ffmpeg/ffprobe 定位与媒体探测
│   ├── job.py              任务引擎：进度 / 日志 / 取消
│   ├── stage.py            阶段契约 Stage + 注册表 + 表单字段描述
│   └── state.py            工作区状态 + 工作流串联执行器
├── stages/                 五个阶段（导入即注册）
├── static/                 外壳 + 自动表单渲染 + 预览时间轴 + 曲线图 + 框选
└── third_party/OpenScrub/  内嵌的 OpenScrub 引擎（Apache-2.0，未修改）
```

**加一个新阶段**：在 `stages/` 放一个 `.py`，定义 `class X(Stage)` 并 `@register`，
左栏导航项和参数表单会自动出现——不用改任何前端代码。

参数表单由 `schema()` 描述，支持 `select / number / range / checkbox / text / textarea / path`
七种字段；`describe()` 会把 `meta()` 的键同时抬到描述符顶层，前端用它们驱动按钮和链接。

## 已知限制

1. **H.264 不支持 10bit**：源是 10bit HDR 而选了 H.264 编码器时，会自动做 HDR→SDR(bt709)
   色调映射并明确记日志；要真 HDR 输出必须选 `hevc_nvenc` 或 `libx265`。
2. **RIFE 只能插帧不能降帧**：`target_fps` ≤ 源帧率会直接报错。
3. **PNG 中间帧极占磁盘**：1080p60×4min ≈ 70GB，建议切 jpg。
4. **无损流拷贝的起点会吸附到关键帧**（`-c copy` 的固有限制），要帧级精准请开「精确切割」。
5. **Dolby Vision 动态元数据无法保留**，只能保 HDR10 基础层。
6. **改 `stages/` 或 `app.py` 后必须重启服务**——模块加载在内存里，改磁盘不生效。
7. **打码需要人体分割模型**：它不在仓库里，首次运行需下载，没有网络时请手动放置并指定路径。

## 踩过的坑

### 1. 改 `stages/` 下的文件后必须重启服务
服务把模块加载进内存，改磁盘不生效。

### 2. H.264 编不出 10bit
`libx264` 和 `h264_nvenc` **都编不出 10bit**。源是 10bit HDR 时若选错，ffmpeg 会直接报
`Nothing was written into output file`（一个包都没编出来）。正确矩阵：

| 编码器 | 10bit | 像素格式 |
|---|---|---|
| `libx264` / `h264_nvenc` | 否 | `yuv420p` |
| `hevc_nvenc` | 是 | `p010le` |
| `libx265` | 是 | `yuv420p10le` |

### 3. 别留下「HLG 数据 + bt2020 标签」的半残文件
源是 HDR 但输出 8bit 时，若仍打 `arib-std-b67`/`bt2020` 标签，支持 HDR 的播放器按 HLG 解释、
不支持按 SDR 解释——**同一份文件两种观感**。8bit 输出必须显式标 `bt709`。

### 4. 浏览器阻止 `http` 页面跳 `file://`
「打开目录」这类功能必须由**服务端**调资源管理器（本项目走 `/api/openfolder`），
前端 `window.open("file:///…")` 会静默失败。

### 5. 静态资源缓存会把新 HTML 和旧 JS 混在一起
服务给 HTML 发 `no-store` 却给 CSS/JS 发 `max-age=86400`，刷新后页面会「没样式 + 视频不加载」。
修法是给静态资源发 `no-cache` + `ETag`，并在 HTML 里给资源 URL 加 `?v=<mtime>` 戳。

### 6. 进度必须在「正确的时机」汇报
引擎的分割模型推理很慢，如果只在阶段切换时汇报进度，进度条会长时间不动。
本项目把引擎的 `post`（检测）和 `render`（渲染）两个阶段分别映射到整体进度的
0.05~0.60 和 0.60~0.99，实现连续反馈。

### 7. 取消不能杀进程树 —— 要让它能中断
进程内调用时，取消靠回调里检查取消标记并抛异常，比
`taskkill /T /F /PID` 干净得多，也不会误杀你正在用的其他程序。

### 8. RIFE 非 verbose 模式不输出任何进度行
要加 `-v` 并数 `done` 行，同时用输出目录里的帧数取 max 兜底。
另外本版 `-n` 是**目标总帧数**，不是线程数。

### 9. `available()` 看不到用户在表单里填的值
它只能读模块默认值。所以**不要**用 `available()` 去校验「用户将要填的路径」——
默认留空会让运行按钮永远点不动。这类校验放进 `run()`，在开跑前报人话错误。

## 许可与致谢

### 第三方

本项目内嵌了 [OpenScrub](https://github.com/austinmabry/OpenScrub) 的引擎源码，
以 **Apache License 2.0** 授权（© 2026 Austin Mabry）。

> OpenScrub — Copyright 2026 Austin Mabry
> This product includes software developed as part of the OpenScrub project
> (https://github.com/austinmabry/OpenScrub).

按 Apache-2.0 §4 的要求：

- (a) 许可证全文见 [`third_party/OpenScrub/LICENSE`](third_party/OpenScrub/LICENSE)
- (b) 上游文件**未做任何修改**，声明见
  [`third_party/OpenScrub/MODIFICATIONS.md`](third_party/OpenScrub/MODIFICATIONS.md)
- (c) 版权与归属声明原样保留
- (d) 上游 `NOTICE` 全文见 [`third_party/OpenScrub/NOTICE`](third_party/OpenScrub/NOTICE)

Apache-2.0 §6 不授予商标权：本项目与 OpenScrub 项目**无官方关联**，也未获其背书。

**模型权重不在本许可范围内**，且不随本项目分发——见
[`third_party/OpenScrub/MODELS.md`](third_party/OpenScrub/MODELS.md)。
