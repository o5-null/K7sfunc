### DLSS-G 补帧（DLSS 4.5 · sm86 解锁引擎）—— N 卡专用
### 依赖：vs-plugins\vdlssg.dll + vs-plugins\dlss45\（引擎包，没有它就只能到 4×；mpv-playkit / mpv-lazy 由拓展包提供）
### 引擎包：vs-plugins\bin\dlss45\（找不到时按脚本顺序回落；缺包只有原版 4×）
### 用法：vf set vapoursynth="~~/vs/MEMC_DLSSG_NV.vpy"
### 特点：直接在相邻两帧之间插 Multi-1 张，输出帧率 = 源帧率 × Multi
### 注意：真实 6× 没有真值可比，只有观感；4K 上 6× 显存放不下，跑不动

import os
import vapoursynth as vs
from vapoursynth import core

############
# 用户选项 #
############

Multi = 3
Scene = 0.25
Gpu = None
Pre_H = 0
Log = False
## <2|3|4|5|6> 整数，帧率倍数；每对相邻源帧插 Multi-1 张
##   1080p 可以 6（单次约 3 ms），1440p 用 3~4，4K 只用 2（6× 是 22 ms 级且会爆显存）
## 浮点，切镜阈值（三平面 8 采样平均绝对差），0 关闭
##   快节奏/粒子闪白素材建议 0.25，否则默认行为会把 11% 的帧对 hold 成重复帧；更稳妥用 0.35
## 整数，使用的显卡序号，0 为排序一号；None 跟随 sm86 代理在安装时选中的那张卡（多显卡机器推荐）
## 整数，>0 时先把源预缩到这个高度再补帧（4K 6× 显存吃紧时可填 1080）
## <True|False> 每帧向 stderr 打一行 evaluate 耗时

# 调试用环境变量覆盖（正常播放不用管）
if os.environ.get("VDLSSG_MULTI"):
    Multi = int(os.environ["VDLSSG_MULTI"])
if os.environ.get("VDLSSG_SCENE"):
    Scene = float(os.environ["VDLSSG_SCENE"])
if os.environ.get("VDLSSG_PRE_H"):
    Pre_H = int(os.environ["VDLSSG_PRE_H"])
if os.environ.get("VDLSSG_LOG"):
    Log = True

#### 插件定位：优先自动加载（vs-plugins），否则从本脚本旁加载 ####

_here = os.path.dirname(os.path.abspath(__file__))

if not hasattr(core, "vdlssg"):
    for _cand in (
        # mpv-playkit / mpv-lazy：<包根>\vs-plugins\vdlssg.dll
        os.path.join(_here, os.pardir, os.pardir, "vs-plugins", "vdlssg.dll"),
        os.path.join(_here, os.pardir, "vs-plugins", "vdlssg.dll"),
        os.path.join(_here, "vdlssg.dll"),
        # 开发树：..\..\vs-dlssg\bin\vdlssg.dll（脚本就在仓库 vs-dlssg\mpv\ 下时）
        os.path.join(_here, os.pardir, os.pardir, "vs-dlssg", "bin", "vdlssg.dll"),
        # 开发树：..\..\..\..\vs-dlssg\bin\vdlssg.dll（mpv-lazy 放在仓库的 测试环境\ 下时）
        os.path.join(_here, os.pardir, os.pardir, os.pardir, os.pardir, "vs-dlssg", "bin", "vdlssg.dll"),
    ):
        if os.path.isfile(_cand):
            core.std.LoadPlugin(os.path.abspath(_cand))
            break
    if not hasattr(core, "vdlssg"):
        raise vs.Error("MEMC_DLSSG: 找不到 vdlssg.dll")

#### 引擎包定位：发行包放在 vs-plugins\bin\dlss45\，开发树在 vs-dlssg\bin\dlss45\ ####

_proxy = None
for _cand in (
    os.path.join(_here, os.pardir, os.pardir, "vs-plugins", "bin", "dlss45"),
    os.path.join(_here, os.pardir, os.pardir, "vs-plugins", "dlss45"),
    os.path.join(_here, os.pardir, "vs-plugins", "bin", "dlss45"),
    os.path.join(_here, os.pardir, "vs-plugins", "dlss45"),
    os.path.join(_here, os.pardir, os.pardir, "vs-dlssg", "bin", "dlss45"),
):
    if os.path.isdir(_cand):
        _proxy = os.path.abspath(_cand)
        break

#### 主流程 ####

clip = video_in
fps_in = float(container_fps) if container_fps and float(container_fps) > 1.0 else clip.fps

if Pre_H and clip.height > Pre_H:
    _w = int(round(clip.width * Pre_H / clip.height / 2.0)) * 2
    clip = core.resize.Bicubic(clip, width=_w, height=Pre_H)

yuv_in = clip.format.color_family != vs.RGB
fmt_in = clip.format.id

# 插件只吃 RGBS（float32 三平面）
src = core.resize.Bilinear(clip, format=vs.RGBS, matrix_in_s="709" if yuv_in else None)
fg = core.vdlssg.FrameGen(src, multi=Multi, scene_threshold=Scene, log=1 if Log else 0,
                          **({} if _proxy is None else {"proxy_dir": _proxy}),
                          **({} if Gpu is None else {"gpu": Gpu}))

# 回到源像素格式；mpv 的 vf=vapoursynth 只接受有限的 8/10bit YUV 格式
out = core.resize.Bilinear(
    fg, format=fmt_in,
    matrix_s="709" if yuv_in else None,
    range_in_s="full",
    range_s="limited" if yuv_in else "full",
)

# 插件保持输入的 fps，帧数却变成 Multi 倍，必须改回真实输出帧率
out = core.std.AssumeFPS(out, fpsnum=int(round(fps_in * Multi * 1e6)), fpsden=1000000)

out.set_output()
