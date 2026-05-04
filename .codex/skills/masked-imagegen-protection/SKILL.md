---
name: masked-imagegen-protection
description: 当用户希望使用内置 image_gen 对现有图片做局部生成式编辑，同时通过灰黑分区遮罩尽量只修改指定区域，并让非编辑区域只发生轻微光影联动时使用。适用于用户直接手画灰区遮罩、把遮罩作为参考图交给 built-in image_gen、必要时按联通块手动对齐灰区、再用原图联动区混合回生成结果的工作流。
---

# 局部保护式 ImageGen

## 概述

这个 skill 用于项目内的局部图片编辑流程，目标是在不使用 CLI 图像编辑和 OpenAI API 的前提下，尽量只修改指定区域，并让非编辑区域保持结构不变，只联动新的环境光。

这条流程依赖内置 `image_gen` 做生成，因此遮罩图只能作为参考图使用，不是硬遮罩。为了降低模型漂移，最终必须再做一次“原图联动区混合回盖”。

## 适用场景

- 用户明确要求只使用内置 `image_gen`。
- 用户希望先得到一版初始遮罩，再手工调整。
- 用户允许“生成时软约束，生成后硬回盖”的方案。
- 用户要求人物、建筑、文字、logo 或其他主体结构尽量不变，但允许轻微的环境光联动。

下列情况不要使用这条流程：

- 用户要求生成阶段就必须像素级硬锁定非编辑区。
- 用户要求显式传入 `mask` 参数进行严格遮罩编辑。

原因是当前内置 `image_gen` 工具不提供显式 `mask` 参数。

## 工作流

1. 用户直接手动画一版灰黑分区遮罩。
2. 确认灰区只覆盖允许发生光影联动的区域。
3. 把原图和遮罩图都加载进当前对话上下文。
4. 调用内置 `image_gen`：
   - 原图作为编辑目标图
   - 遮罩图作为参考图
   - 提示词中明确说明灰色区只能做光影联动、黑色区可以改内容
5. 把选中的生成结果复制或移动回工作区。
6. 如果发现 raw 图在灰区附近存在局部偏移，先按联通块手动微调原图灰区位置。
7. 用原图灰色联动区混合回生成结果。
8. 检查边缘和整体一致性，必要时再做一次更收紧的二次生成。

## 目录结构

- `scripts/manual_mask_editor.py`
  提供空白灰黑分区画布，供用户直接手工画灰区。
- `scripts/align_gray_regions.py`
  提供联通块级别的局部对齐工具，供用户在最终合并前微调原图灰区位置。
- `scripts/apply_protection_overlay.py`
  把原图灰色联动区混合回生成结果上。
- `references/built-in-prompt-template.md`
  内置 `image_gen` 的提示词模板，包含“灰黑遮罩模式”和“无遮罩直改天空模式”。

## 本地脚本

### 画灰色区时必须给出的 3 条命令

当这条流程需要用户手动画灰色联动区时，默认要把下面 3 条命令一起给用户，不要只给后两条。

命令 1：手工绘制灰黑分区

```powershell
python .\.codex\skills\masked-imagegen-protection\scripts\manual_mask_editor.py .\原图\original.png --out-dir .\中间图\生成式替换样例\manual-mask
```

命令 2：按联通块手动对齐灰区

```powershell
python .\.codex\skills\masked-imagegen-protection\scripts\align_gray_regions.py --original .\原图\original.png --generated .\中间图\修图过程\edited-v1-raw.png --mask .\中间图\生成式替换样例\manual-mask\original-manual-api-mask.png --match-tones --match-erode 4 --feather 1.0 --auto-out .\中间图\修图过程\edited-v1-auto-aligned.png
```

命令 3：读取联通块位移参数并回盖

```powershell
python .\.codex\skills\masked-imagegen-protection\scripts\apply_protection_overlay.py --original .\原图\original.png --generated .\中间图\修图过程\edited-v1-raw.png --mask .\中间图\生成式替换样例\manual-mask\original-manual-api-mask.png --region-transforms .\中间图\生成式替换样例\manual-mask\original-region-transforms.json --match-tones --match-threshold 32 --match-erode 4 --feather 1.0 --out .\中间图\修图过程\edited-v1.png
```

约定：

- 第 2 条命令在打开手动对齐窗口前，会先自动在上下左右各 `20` 像素范围内做双重循环搜索，给每个联通块找一组像素差和最小的初始位移。
- 第 2 条命令会自动保存：
  - `*-region-transforms.json`
  - `*-alignment-preview.png`
  - 一张自动回盖图，例如 `edited-v1-auto-aligned.png`
- 如果自动对齐已经够准，可以直接使用第 2 条命令产出的自动回盖图。
- 如果自动对齐还不够准，再在窗口里手动微调，按 `v` 保存；第 3 条命令会读取更新后的 `*-region-transforms.json` 再输出最终图。
- 如果当前图片还没做 raw 生成，第 2 条和第 3 条命令里的 `--generated` 应改成当前 raw 图路径。
- 如果用户这轮明确要画灰色区，就优先给出这 3 条命令，而不是只给“回盖命令”。

### 直接绘制灰黑分区

使用 `./scripts/manual_mask_editor.py`。

示例命令：

```powershell
python .\.codex\skills\masked-imagegen-protection\scripts\manual_mask_editor.py .\original.png --out-dir .\work\mask
```

语义约定：

- 灰色区域：联动区
- 黑色区域：可编辑区

预期输出：

- `*-manual-mask.png`
  普通灰黑分区遮罩预览
- `*-manual-api-mask.png`
  带 alpha 的遮罩，可直接给“保护区回盖”脚本使用
- `*-manual-mask-overlay.png`
  原图上的联动区叠加预览

### 联动区混合回盖

使用 `./scripts/apply_protection_overlay.py`。

示例命令：

```powershell
python .\.codex\skills\masked-imagegen-protection\scripts\apply_protection_overlay.py --original .\原图\original.png --generated .\generated.png --mask .\中间图\生成式替换样例\manual-mask\original-manual-api-mask.png --out .\中间图\修图过程\edited-v1.png
```

### 联通块手动对齐

使用 `./scripts/align_gray_regions.py`。

当 raw 生成图在灰区附近出现轻微几何偏移时，不要直接把生成图灰区混进最终结果。先让脚本自动预对齐一轮，再在需要时让用户按灰区联通块微调原图位置，最后进入最终合并。

示例命令：

```powershell
python .\.codex\skills\masked-imagegen-protection\scripts\align_gray_regions.py --original .\原图\original.png --generated .\中间图\修图过程\edited-v1-raw.png --mask .\中间图\生成式替换样例\manual-mask\original-manual-api-mask.png --match-tones --match-erode 4 --feather 1.0 --auto-out .\中间图\修图过程\edited-v1-auto-aligned.png
```

自动预对齐规则：

- 每个灰区联通块都会先在 `dx, dy ∈ [-20, 20]` 的范围里做双重循环搜索。
- 评分依据是该联通块区域内，原图像素平移后与 raw 生成图对应像素的绝对差之和。
- 取像素差之和最小的位置，作为手动界面打开前的初始位移。
- 脚本会基于这组自动位移直接输出一版自动回盖图；如果已经足够好，可以先看这版。

交互约定：

- 鼠标左键
  选中当前联通块
- `w a s d`
  当前联通块每次移动 1 像素
- `W A S D`
  当前联通块每次移动 5 像素
- `n / p`
  切换联通块
- `r`
  重置当前联通块到自动对齐结果
- `0`
  重置全部联通块到自动对齐结果
- `v`
  保存位移参数 JSON 和对齐预览图

预期输出：

- `*-region-transforms.json`
  联通块位移参数，默认先写入自动对齐结果；手动保存后会更新为最新结果
- `*-alignment-preview.png`
  原图灰区叠到 raw 生成图上的预览
- `*-auto-aligned.png` 或命令中 `--auto-out` 指定的路径
  自动预对齐后直接生成的一版回盖图

这一步是这条流程的补偿步骤，不能省略。它负责把需要保结构的区域，用原图像素和受控混合带回结果图。

如果直接回盖后出现光影不协调，可以打开色调匹配和轻微羽化：

```powershell
python .\.codex\skills\masked-imagegen-protection\scripts\apply_protection_overlay.py --original .\原图\original.png --generated .\generated.png --mask .\中间图\生成式替换样例\manual-mask\original-manual-api-mask.png --match-tones --match-erode 4 --feather 1.8 --out .\中间图\修图过程\edited-v2.png
```

如果灰区边界有一圈发亮、发硬或接缝感明显，可以再加接缝带局部模糊：

```powershell
python .\.codex\skills\masked-imagegen-protection\scripts\apply_protection_overlay.py --original .\原图\original.png --generated .\generated.png --mask .\中间图\生成式替换样例\manual-mask\original-manual-api-mask.png --match-tones --match-erode 4 --feather 1.0 --seam-blur 1.6 --seam-width 3 --out .\中间图\修图过程\edited-v3.png
```

参数说明：

- `--match-tones`
  读取 raw 生成图在灰色联动区内的亮度和色偏统计，用它来校正原图联动区后再混合回去。
- `--region-transforms`
  读取联通块位移参数，先把原图灰区按联通块局部对齐，再执行调光调色和回盖。
- 联通区域匹配
  当前不是整片灰区共用一套参数，而是把灰区按联通区域拆开，每一块单独计算自己的亮度、反差和色偏修正。
- `--match-erode`
  先向内收缩联动区若干像素，再做统计，避免边界被生成痕迹污染。
- `--feather`
  对联动区 alpha 做轻微羽化，减少生硬拼接边界。
- `--seam-blur`
  只对灰区接缝带做局部模糊，压掉边缘发亮或发硬的一圈，不影响灰区内部细节。
- `--seam-width`
  定义接缝带半宽，通常从 `2` 到 `4` 像素开始试。
- 混合规则
  灰区内部不再直接按灰度值与生成图做半透明叠加，否则一旦生成图结构有轻微漂移就会出现重影。当前逻辑是：灰区内部使用调色后的原图，只在灰区边界做羽化过渡。

## 内置 image_gen 规则

- 默认只用内置 `image_gen`，不要自动切到 CLI 或 API。
- 如果原图和遮罩图只是本地文件，先用 `view_image` 把它们加载到对话上下文。
- 遮罩图只作为参考图，不要把它描述成真正的硬遮罩参数。
- 提示词里必须强约束“灰色区不能改结构和文字，只允许轻微光影联动”，但不要承诺像素级绝对不变。
- 每次生成尽量只做一类改动，例如只换天空、只清理背景、只改氛围。
- 遮罩边界附近优先要求自然过渡，避免光晕、涂抹和整体串色。
- 灰区应尽量画得克制，只覆盖真正需要吃到环境光变化的连续表面，不要把文字和尖锐轮廓大面积并入灰区。
- 如果 raw 图在灰区附近已有明显几何漂移，不要直接合并；优先进入联通块手动对齐步骤。

## 提示词

模板见 [built-in-prompt-template.md](./references/built-in-prompt-template.md)。

使用时遵守这些原则：

- 只替换模板里的“具体改动目标”，保留保护区约束段落。
- 如果用户明确不要遮罩，但仍要求主体尽量不变，优先使用其中的“无遮罩直改天空模板”，不要沿用宽泛的整图氛围重绘提示词。
- 如果第一次生成改动范围太大，下一轮要收紧提示词，不要扩大编辑目标。
- 如果第一次生成边界脏，下一轮优先补“边界更干净、不要溢出到灰色联动区”。
- 如果第一次生成整体色调漂移，下一轮优先补“不要全局改色，不要整体重风格化”。

## 输出处理

- 内置 `image_gen` 会先把图片保存到 Codex 默认输出目录。
- 如果图片是项目结果，结束前必须把选中的结果复制或移动回工作区。
- 最终交付文件应当是“联动区混合回盖后”的结果，而不是原始生成图。

## 最小执行清单

- 确认编辑目标图路径。
- 确认用户最新认可的灰黑分区遮罩文件。
- 如果这轮需要画灰色区，明确给出“画遮罩、联通块对齐、读取联通块位移后回盖”这 3 条命令。
- 把原图和遮罩图加载进上下文。
- 按模板组织 built-in `image_gen` 提示词。
- 保存 raw 生成结果到工作区。
- 如有必要，先运行联通块手动对齐工具并保存位移参数。
- 运行联动区混合回盖脚本。
- 复查联动区边界、文字和主体是否稳定。
