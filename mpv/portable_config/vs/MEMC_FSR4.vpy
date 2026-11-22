### AMD FSR4 补帧（FidelityFX Frame Generation）—— 固定 2×
### 依赖：vs-plugins\vfsr4.dll + vs-plugins\bin\fsr4\（引擎包：smv_fsrfg_bridge.dll + AMD FidelityFX；mpv-playkit / mpv-lazy 由拓展包提供）
### 引擎包：vs-plugins\bin\fsr4\（找不到时按脚本顺序回落；缺包报 bin\fsr4 not found，且该包不在公开 zip 里，需自备）
### 用法：vf set vapoursynth="~~/vs/MEMC_FSR4.vpy"
### 特点：固定 2×，走 D3D12 后备缓冲 + Vulkan（vkd3d）共享纹理
### 注意：1080p 约 38 ms/对，4K 约 144 ms；引擎包缺失时报 "bin\fsr4 not found"

import os
import sys
import vapoursynth as vs
from vapoursynth import core

# 公用参数桥（k7f_bridge.py 与脚本同目录）：下行解析 + 上行报告；缺失也不影响播放
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import k7f_bridge as k7fb
except Exception:
    k7fb = None

############
# 用户选项 #
############

Multi = 2
Scene = 0.25
Gpu = None
Camera = 0
Pre_H = 0
Log = False
## 固定 2：FSR4 每次只生成一对相邻帧的中间帧，写别的值插件会直接报错
## 浮点，切镜阈值（三平面 8 采样平均绝对差），0 关闭
##   快节奏/粒子闪白素材建议 0.25，否则默认行为会把部分帧对 hold 成重复帧；更稳妥用 0.35
## 整数，DXGI 显卡序号，0 为排序一号；None 用 0
## 整数，>0 时按该速度（像素/帧）叠加"相机运动"到运动矢量，0 关闭
## 整数，>0 时先把源预缩到这个高度再补帧（4K 耗时吃紧时可填 1080）
## <True|False> 每帧向 stderr 打一行 evaluate 耗时

# 调试用环境变量覆盖（正常播放不用管）
if os.environ.get("VFSR4_SCENE"):
    Scene = float(os.environ["VFSR4_SCENE"])
if os.environ.get("VFSR4_PRE_H"):
    Pre_H = int(os.environ["VFSR4_PRE_H"])
if os.environ.get("VFSR4_LOG"):
    Log = True

#### OSD 参数通道：读 mpv 注入的 user_data 全局变量 ####
# 前端脚本用 vf set 的 user-data 子参数传值，格式 "scene=0.35;camera=2"（分隔符 ; 或 ,）
#   vf set @VFSR4:vapoursynth="~~/vs/MEMC_FSR4.vpy":user-data="scene=0.35;camera=2"
# user_data 默认空串；mpv 每次 seek 都会重跑本脚本，所以参数在重建/跳转后一定生效。
# 优先级：字面量 < 环境变量 < user_data（OSD 是用户当场的选择，最高）

def _parse_user_data(s):
    # 优先用公用桥（与 mpv 侧 k7f_bridge.lua 同一套语义）
    if k7fb is not None:
        return k7fb.parse_user_data(s)
    # 桥缺失时的最小解析，保证脚本单独拷走也能用
    opts = {}
    for part in (s or "").replace(",", ";").replace(" ", ";").split(";"):
        key, sep, val = part.partition("=")
        if sep and key.strip():
            opts[key.strip().lower()] = val.strip()
    return opts


# 环境变量之后再套 OSD 参数，保证用户在 OSD 上的选择优先级最高
# 注意：Multi 固定为 2，不处理 "multi" 键（插件写别的值会报错）
_ud = _parse_user_data(globals().get("user_data"))
if _ud:
    if "scene" in _ud:
        Scene = max(0.0, float(_ud["scene"]))
    if "camera" in _ud:
        Camera = max(0, int(float(_ud["camera"])))
    if "pre_h" in _ud:
        Pre_H = max(0, int(float(_ud["pre_h"])))
    if "gpu" in _ud:
        _gpu = _ud["gpu"].strip().lower()
        Gpu = None if _gpu in ("none", "auto", "") else int(_gpu)
    if "log" in _ud:
        Log = _ud["log"].strip().lower() not in ("0", "false", "off", "no", "")

#### 插件定位：优先自动加载（vs-plugins），否则从本脚本旁加载 ####

_here = os.path.dirname(os.path.abspath(__file__))

if not hasattr(core, "vfsr4"):
    for _cand in (
        # mpv-playkit / mpv-lazy：<包根>\vs-plugins\vfsr4.dll
        os.path.join(_here, os.pardir, os.pardir, "vs-plugins", "vfsr4.dll"),
        os.path.join(_here, os.pardir, "vs-plugins", "vfsr4.dll"),
        os.path.join(_here, "vfsr4.dll"),
        # 开发树：..\..\vs-dlssg\bin\vfsr4.dll（脚本就在仓库 vs-dlssg\mpv\ 下时）
        os.path.join(_here, os.pardir, os.pardir, "vs-dlssg", "bin", "vfsr4.dll"),
        # 开发树：..\..\..\..\vs-dlssg\bin\vfsr4.dll（mpv-lazy 放在仓库的 测试环境\ 下时）
        os.path.join(_here, os.pardir, os.pardir, os.pardir, os.pardir, "vs-dlssg", "bin", "vfsr4.dll"),
    ):
        if os.path.isfile(_cand):
            core.std.LoadPlugin(os.path.abspath(_cand))
            break
    if not hasattr(core, "vfsr4"):
        raise vs.Error("MEMC_FSR4: 找不到 vfsr4.dll")

#### 引擎包定位：发行包放在 vs-plugins\bin\fsr4\，开发树在 vs-dlssg\bin\fsr4\ ####

_proxy = None
for _cand in (
    os.path.join(_here, os.pardir, os.pardir, "vs-plugins", "bin", "fsr4"),
    os.path.join(_here, os.pardir, os.pardir, "vs-plugins", "fsr4"),
    os.path.join(_here, os.pardir, "vs-plugins", "bin", "fsr4"),
    os.path.join(_here, os.pardir, "vs-plugins", "fsr4"),
    os.path.join(_here, os.pardir, os.pardir, "vs-dlssg", "bin", "fsr4"),
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
fg = core.vfsr4.FrameGen(src, multi=Multi, scene_threshold=Scene, camera=Camera,
                         log=1 if Log else 0,
                         **({} if _proxy is None else {"proxy_dir": _proxy}),
                         **({} if Gpu is None else {"gpu": Gpu}))

# 回到源像素格式；mpv 的 vf=vapoursynth 只接受有限的 8/10bit YUV 格式
out = core.resize.Bilinear(
    fg, format=fmt_in,
    matrix_s="709" if yuv_in else None,
    range_in_s="full",
    range_s="limited" if yuv_in else "full",
)

# 插件保持输入的 fps，帧数却变成 2 倍，必须改回真实输出帧率
out = core.std.AssumeFPS(out, fpsnum=int(round(fps_in * Multi * 1e6)), fpsden=1000000)

# 上行报告：告诉 mpv 实际生效的参数；写盘失败只影响 OSD，不会被这里打断
if k7fb is not None:
    _summary = "2×（固定） · 切镜 %g" % Scene
    if Camera:
        _summary += " · 相机 %d" % Camera
    if Pre_H:
        _summary += " · 预缩 %dp" % Pre_H
    if _proxy is not None:
        _summary += " · 引擎包"
    k7fb.publish(
        __file__, title="FSR4", engine="vfsr4",
        params={"multi": Multi, "scene": Scene, "camera": Camera,
                "gpu": "auto" if Gpu is None else Gpu,
                "pre_h": Pre_H, "log": Log,
                "engine_pack": _proxy is not None},
        summary=_summary,
        in_info=k7fb.clip_info(video_in, fps=fps_in), out_info=k7fb.clip_info(out),
    )

out.set_output()
