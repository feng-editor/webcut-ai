# WebCut 产品级全面升级方案（v0.3）

> 阶段2交付物。基于阶段1对全部源码（index.html 4901 行 / 7 个 JS 模块 / Flask 后端 525 行）的完整检查。

## 一、保留清单（不动或仅外围绕过）

| 模块 | 决策 |
|---|---|
| VideoDecoderService（MP4 解复用/解码/缩略图/波形） | 完整保留 |
| AudioPlayback（WebAudio 排程/分段预取） | 完整保留 |
| muxMp4AV + encodeCanvasToMp4（H.264+AAC 导出、fastStart、软硬回退） | 保留核心，外层加参数（见导出升级） |
| ProjectStore / CloudSync / PayClient / AIClient | 完整保留，加项目管理外层 |
| dispatch.js / dag-engine.js / render-*.js / cloud-sync.js | 完整保留，属性面板接入 NodeRegistry |
| 时间线 Canvas 引擎（drawTimeline/drawClip/拖拽状态机/吸附/触控捏合） | 保留核心，加多选/右键菜单/复制粘贴 |
| entitlements-config.js、协议弹窗、支付闭环 | 完整保留 |
| Flask 后端现有端点（video/tts/orders/cloud） | 保留，仅新增端点 |
| 移动端底部导航 + 抽屉框架 | 保留，适配新 UI |

## 二、修改清单（按阶段）

### 阶段3 整体 UI 重构
- **设计系统**：深石墨黑底（#0f1115 系）、银白文字、冰蓝强调（#6cb6ff 系）、紫为 AI 辅助（#8b7cf6）。去霓虹发光/大渐变/玻璃滥用 → 细 1px 边框 + 克制阴影。
- **类名契约不变**：JS 引用的所有 class（.tab/.tile/.sec/.prow/.btn/.menu/…）全部保留，只换视觉实现 → JS 零破坏。
- 布局：顶栏（品牌/项目名+保存态/撤销重做/设置/AI/导出/用户）+ 左（素材：媒体/文字/AI/特效 四页签，媒体内分 视频/图片/音频/AI素材）+ 中（预览+播放条：时间码/比例/预览画质/全屏）+ 右（属性）+ 底（时间线）。
- Canvas 内配色同步换装（时间线/播放头/片段样式/预览占位）。

### 阶段4a 时间线专业升级
- 多片段选择（Ctrl/Shift 点选 + 空白框选）、框选高亮。
- 右键上下文菜单：分割/复制/粘贴/副本/删除/自动适配。
- 复制/粘贴/副本（Ctrl+C/V/D），跨轨道粘贴。
- 片段上渲染关键帧菱形标记；双击菱形定位。
- 快捷键扩展（A 全选、Ctrl+A、Home/End、±帧步进）。

### 阶段4b 属性面板 Schema 化 + 真实效果渲染
- node-registry.js 注册第二批节点：blur / mask / chromaKey / sharpen（含中文 label、参数校验）。
- renderProps 改为 NodeRegistry.uiSchema() 驱动生成（transform/color/blur/mask/chromaKey/sharpen 全 Schema 化；文字/音频/关键帧保留手写但走同一控件工厂）。
- **效果真实化**（不允许假滑块）：
  - 变换/不透明度 → 预览与导出用 canvas 矩阵变换 + globalAlpha；
  - 亮度/对比度/饱和度/色温/色调/模糊 → ctx.filter 链（预览+导出一致）；
  - 锐化 → 内联 SVG feConvolveMatrix 引用（Chrome/Edge）；
  - 色度键 → GPU Worker 路径预览（已有）+ 导出 CPU 逐帧像素处理（启用时提示较慢）。
- 参数写入 clip.props 且同步 renderGraph 节点（Worker/GPU 路径一致取数）。

### 阶段4c AI 创作中心（10 项，真实状态标注）
| 功能 | 状态 | 实现 |
|---|---|---|
| AI 文生视频 | 已上线 | 现有 /api/ai/video |
| AI 图生视频 | 新接入 | 后端新增 image 参数走 DashScope 图生视频模型，失败如实报错 |
| AI 图片生成 | 新接入 | 后端新增 /api/ai/image（DashScope 文生图） |
| AI 配音 | 已上线 | 现有 /api/ai/tts |
| AI 智能字幕 | 开发中 | 需接入 Paraformer ASR，界面明确标注依赖与状态 |
| AI 背景移除 | 开发中 | 需人像分割模型，明确标注 |
| AI 智能抠像 | 已上线 | 色度键 GPU 着色器（真实可用） |
| AI 视频增强 | 基础版已上线 | 锐化+色彩（真实处理管线）；超分标注开发中 |
| AI 风格转换 | 滤镜版已上线 | 调色预设（真实 GPU/Canvas 处理）；神经风格迁移标注开发中 |
| AI 创意辅助 | 新接入 | 后端新增 /api/ai/prompt（DashScope 文本模型润色提示词） |
- 独立工作区：左侧功能列表 + 右侧参数配置/进度/取消/重试/结果预览/一键入时间线。生成结果同时进「AI素材」库。

### 阶段5 导出系统 + 架构升级
- 导出：720P/1080P/2K/4K × 24/30/60FPS × 视频码率(自动/8/12/20/40Mbps) × 音频码率(128/192/256k)，剩余时间估算（按已编码帧速度外推），失败重试按钮。
- dispatch 集成：新增 EditorActions 层——结构操作（增删/移动/分割/属性 set）经 App.dispatch(op) 走 DispatchCore（日志/订阅/校验），并与现有快照式撤销栈桥接；迁移属性面板与时间线操作入口。
- Worker 异常恢复：render-coordinator 崩溃后自动重建（已有回退链，加自动重启计数）。

### 阶段6 项目管理 / 云同步 / 移动端
- 项目管理器：多项目（kv 存储列表）、新建/打开/重命名/副本/删除/导入导出 JSON（含媒体引用清单）、自动版本快照（每项目保留最近 5 版可恢复）。
- 云同步：进度/用量/冲突（远端较新提示拉取）已有，补 UI 入口到顶栏保存态。
- 移动端：新 UI 全量适配（底部导航、抽屉、触控时间线、捏合缩放保留）。

### 阶段7/8 测试与交付
- 自动化：test/selftest.html 自检页（页面加载/无控制台严重错误/时间线渲染/选片/撤销重做/弹窗链/导出模态/Schema 面板生成/NodeRegistry 校验），dispatch-test.js 扩展。
- 真浏览器验收 + 截图；后端新端点 curl 测试。
- 交付：测试报告/安全审计报告/性能报告/部署说明/更新日志，更新 C:\剪辑\ 打包（不覆盖原件，生成新版本包）。

## 三、风险控制
- 每阶段独立 Git 提交，可回滚。
- 类名契约不变策略保证 JS 零破坏；每阶段完成后真浏览器加载验证。
- 导出链路（最珍贵资产）最后统一回归测试：导出 MP4 → ffprobe 验证。
