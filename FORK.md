# 本仓库是 K7sfunc 的自维护 fork

上游：[hooke007/K7sfunc](https://github.com/hooke007/K7sfunc)（现址 [LumeCraft-Labs/K7sfunc](https://github.com/LumeCraft-Labs/K7sfunc)）
本 fork：[o5-null/K7sfunc](https://github.com/o5-null/K7sfunc)

用途：给 [mpv-playkit](https://github.com/o5-null/mpv-playkit) 提供"带自定义功能"的 K7sfunc。
mpv-playkit 不再从 PyPI 装 `k7sfunc`，而是按 commit 钉住本 fork 的源码树直接装进包里，
所以这里的改动会原样出现在播放器的 `Lib/site-packages/k7sfunc/` 下。

## 文档

- 本仓库 wiki：[3_K7sfunc](https://github.com/o5-null/K7sfunc/wiki/3_K7sfunc)（fork 增量与四个帧生成模块）、
  [4_FrameGen](https://github.com/o5-null/K7sfunc/wiki/4_FrameGen)（部署与参数）、
  [4_FrameGen_基准与FAQ](https://github.com/o5-null/K7sfunc/wiki/4_FrameGen_%E5%9F%BA%E5%87%86%E4%B8%8EFAQ)、
  [5_许可与第三方](https://github.com/o5-null/K7sfunc/wiki/5_%E8%AE%B8%E5%8F%AF%E4%B8%8E%E7%AC%AC%E4%B8%89%E6%96%B9)
- 模块级参数表：`doc/Windows.md`、`doc/MacOS.md`（沿用上游格式）

## 相对上游的改动

| 文件 | 性质 |
| --- | --- |
| `src/k7sfunc/mod_playkit.py` | 新增：预设层，把上游算子组合成开箱即用的滤镜链 |
| `src/k7sfunc/mod_memc.py` | 仅追加：新增帧生成补帧 `DLSSG_NV` / `XESSFG_NV` / `VFG_NV` / `FSR4` 与公用 helper `FRAMEGEN_HUB`（上游函数一行未改） |
| `src/k7sfunc/__init__.py` | 一行导出 + 版本号改为 `1.9.0+pre.4` |
| `mpv/portable_config/scripts/k7f_playkit.lua` | 新增：mpv 前端，按配置生成临时 vpy 并挂到 `@PLAYKIT` 滤镜标签 |
| `mpv/portable_config/script-opts/k7f_playkit.conf` | 新增：前端配置 |
| `mpv/portable_config/vs/MEMC_*.vpy` | 新增：vs-framegen 的四个补帧脚本（DLSS-G / XeSS-FG / VFG / FSR4） |
| `FORK.md` | 新增：本文件 |

除 `mod_memc.py` 的纯追加外**不改上游已有模块**，上游更新时合并冲突面只有 `__init__.py` 三行。

## PLAYKIT_* 接口

所有函数第一个参数是 `vs.VideoNode`（vpy 里的 `video_in`），返回 `vs.VideoNode`。
任何一步失败（缺模型、缺插件、显存不足）都打印 `[playkit] 警告: ...` 后降级继续，不中断播放。

| 函数 | 说明 |
| --- | --- |
| `PLAYKIT_RUN(input, preset="auto", target_h=0, engine="auto", memc_engine="auto", memc_fps=60, fps_in=None, denoise_lv=3.0, deband_rth=48.0, gpu=0, gpu_m=1, gpu_t=2)` | 唯一入口，按 `preset` 分发 |
| `PLAYKIT_AUTO(...)` | 只在 `input.height < target_h` 时超分 |
| `PLAYKIT_UPSCALE(input, target_h=1440, engine="auto", ...)` | 超分并等比缩放到 `target_h` |
| `PLAYKIT_MEMC(input, fps_out=60, engine="auto", fps_in=None, ...)` | 插帧到 `fps_out` |
| `PLAYKIT_DENOISE(input, strength=3.0, gpu=0)` | `NLM_STD` 降噪 |
| `PLAYKIT_DEBAND(input, threshold=48.0)` | `DEBAND_STD` 去色带 |
| `PLAYKIT_CLEAN(...)` | 去色带 + 降噪 |
| `PLAYKIT_ANIME(...)` | 超分 + 去色带 |
| `PLAYKIT_LIVE(...)` | 降噪 + 插帧 |
| `PLAYKIT_FIT(input, target_h)` | 仅缩放 |
| `PLAYKIT_PRESETS` / `PLAYKIT_UPSCALE_ORDER` / `PLAYKIT_MEMC_ORDER` | 预设名与降级顺序 |

超分引擎：`artcnn`(ARTCNN_NV) → `acnet`(ACNET_STD) → `cugan`(CUGAN_NV) → `edi`(EDI_US_STD) → `ngu`(NGU_HQ) → `none`（直接缩放）。
插帧引擎：`rife_nv`(RIFE_NV) → `rife_dml`(RIFE_DML) → `none`。

## 用法

1. 直接用前端：把 `mpv/portable_config/` 下的 `scripts/k7f_playkit.lua` 与
   `script-opts/k7f_playkit.conf` 放进播放器对应目录，在 `input.conf` 绑定
   `Ctrl+Shift+p  script-binding k7f_playkit/toggle`。
2. 自己写 vpy：

```python
import vapoursynth as vs
import k7sfunc as k7f

fps_in = globals().get("container_fps", 24.0)
clip = k7f.PLAYKIT_RUN(video_in, preset="anime", target_h=1440, engine="auto")
clip.set_output()
```

3. 只想要原子算子：继续用上游函数（`ACNET_STD`、`RIFE_NV` 等），本 fork 未改动它们的实现。

## 帧生成补帧（vs-framegen）

`mod_memc.py` 追加了四个引擎的封装，宿主源码来自 [o5-null/vs-framegen](https://github.com/o5-null/vs-framegen)：
`DLSSG_NV`(vdlssg) / `XESSFG_NV`(vxessfg) / `VFG_NV`(vvfg) / `FSR4`(vfsr4)。
它们都接受 `RGBS` 或 YUV 输入（内部按 709 转换），输出回到源格式与真实输出帧率（`fps_in * multi`）。

- 倍率：`DLSSG_NV` / `VFG_NV` 为 2–6，`XESSFG_NV` 为 2–4，`FSR4` 固定 2
- 单进程只支持一种帧尺寸；同一进程只能初始化一次 NGX（`vdlssg` 与 `vxessfg` 不能同时用）
- 现成的前端脚本在 `mpv/portable_config/vs/MEMC_*.vpy`，插件与引擎包不在本仓库，
  由播放器的拓展包提供（`<包根>\vs-plugins\`，引擎包在 `vs-plugins\bin\<引擎>\`）

模型与 GPU 组件不在本仓库：仍按上游规则从
[hooke007/dotfiles 的 mpv_addones](https://github.com/hooke007/dotfiles/releases/tag/mpv_addones)
取，放进播放器根目录的 `vs-plugins/`。

## 版本与同步

- 版本号 `1.9.0+pre.4`：基线是上游 1.8.1，`+pre.N` 表示本 fork 的预览迭代号。
- 同步上游：`git remote add upstream https://github.com/LumeCraft-Labs/K7sfunc.git`，
  合并后把 `+pre.N` 递增；因为只新增文件，冲突通常只出现在 `__init__.py`。
- 本包按源码树安装（不走 PyPI），所以 `src/k7sfunc/` 必须是可导入的完整包，
  不能只提交 `.whl`。

## 许可

上游 `src/` 为 GPL-3.0-or-later，其他部分 MIT；本 fork 新增文件沿用同一规则。
