### RIFE 补帧（AnimeJaNai · libaji 运行时）—— N 卡专用
### 依赖（按 K7 规则分两处）：plugins\rife_aji.dll（VS 插件，自动加载）
###       + vs-plugins\aji\（aji.dll、aji_trt.dll、cudart64_*.dll、trt\nvinfer_11.dll、
###       aji-runtime.json、conf\、rife\*.onnx；都是运行时资源，构建方法见
###       https://github.com/o5-null/vsaji）
### 用法：vf set vapoursynth="~~/vs/MEMC_RIFE_AJI.vpy"
### 特点：固定整数倍率，每对相邻源帧按 phase 插 (fps_num/fps_den - 1) 张
### 注意：aji 的 factor 只吃整数，fps_num/fps_den 必须整除

import os
import vapoursynth as vs
from vapoursynth import core

############
# 用户选项 #
############

Model = "4.14"
Ensemble = False
Fps_In = None
Fps_Num = 2
Fps_Den = 1
Sc_Mode = 1
Gpu = 0
## 字符串，模型名（aji\rife 下的 rife_v<Model>[_ensemble].onnx）；"4.14" / "4.7" / "4.25_lite"
## <True|False> 是否用 ensemble 推理（更稳、更慢，需要同名的 _ensemble 模型）
## 浮点，None 跟随播放器容器帧率（container_fps），手动填可强制指定源帧率
## 整数，帧率倍数分子/分母；必须整除，否则模块直接报错
## <0|1> 场景切换：0 交给 aji 自己的阈值检测，1 用 VS 侧 _SceneChangeNext 属性
## 整数，使用的显卡序号，0 为排序一号

# 调试用环境变量覆盖（正常播放不用管）
if os.environ.get("AJI_MODEL"):
    Model = os.environ["AJI_MODEL"]
if os.environ.get("AJI_ENSEMBLE"):
    Ensemble = True
if os.environ.get("AJI_FPS_NUM"):
    Fps_Num = int(os.environ["AJI_FPS_NUM"])
if os.environ.get("AJI_FPS_DEN"):
    Fps_Den = int(os.environ["AJI_FPS_DEN"])
if os.environ.get("AJI_SC_MODE"):
    Sc_Mode = int(os.environ["AJI_SC_MODE"])
if os.environ.get("AJI_GPU"):
    Gpu = int(os.environ["AJI_GPU"])

#### 插件定位：规范布局由 VapourSynth 自动加载；下面只为手动解压的包兜底 ####

_here = os.path.dirname(os.path.abspath(__file__))

if not hasattr(core, "rife_aji"):
    for _cand in (
        # 手动解压到 <包根>\vs-plugins\ 或与运行时同目录（非规范，仅兜底）
        os.path.join(_here, os.pardir, os.pardir, "vs-plugins", "rife_aji.dll"),
        os.path.join(_here, os.pardir, os.pardir, "vs-plugins", "aji", "rife_aji.dll"),
        os.path.join(_here, os.pardir, "vs-plugins", "rife_aji.dll"),
        os.path.join(_here, "rife_aji.dll"),
    ):
        if os.path.isfile(_cand):
            core.std.LoadPlugin(os.path.abspath(_cand))
            break
    if not hasattr(core, "rife_aji"):
        raise vs.Error("MEMC_RIFE_AJI: 找不到 rife_aji.dll（应放在 Lib\\site-packages\\vapoursynth\\plugins\\）")

#### 主流程 ####

clip = video_in
fps_in = float(container_fps) if container_fps and float(container_fps) > 1.0 else clip.fps
if Fps_In:
    fps_in = float(Fps_In)

# k7sfunc 负责模型/conf 定位与插件调用，这里只做参数转交
import k7sfunc

out = k7sfunc.RIFE_AJI(
    input=clip,
    model=Model,
    ensemble=Ensemble,
    fps_in=fps_in,
    fps_num=Fps_Num,
    fps_den=Fps_Den,
    sc_mode=Sc_Mode,
    gpu=Gpu,
)

out.set_output()
