### RIFE 补帧（AnimeJaNai · libaji 运行时）—— N 卡专用
### 依赖：vs-plugins\vsaji.dll + vs-plugins\aji\（aji.dll / aji_trt.dll / cudart64_*.dll /
###       trt\nvinfer_11.dll / aji-runtime.json / conf\ / rife\*.onnx；构建见 docs\k7-aji-rife.md）
### 用法：vf set vapoursynth="~~/vs/MEMC_RIFE_AJI.vpy"
### 特点：固定整数倍率，每对相邻源帧按 phase 插 Multi-1 张
### 注意：aji 的 factor 只吃整数；本脚本的配置/模型/后端全部交给 k7f.FRAMEGEN_VPY

import k7sfunc as k7f

############
# 用户选项 #
############

Multi = 2
Scene = 0.150
Model = "4.14"
Ensemble = False
Backend = "auto"
Gpu = None
Pre_H = 0
Log = False
Stats = False
## <2|3|4|5|6> 整数，帧率倍数；aji 的 factor 只吃整数，所以这里直接给倍率
## 浮点，切镜阈值（aji 自己的场景检测），0 关闭（等价于 1.0）
## 字符串，RIFE 模型号（aji\rife 下的 rife_v<Model>[_ensemble].onnx），如 "4.14" / "4.26"
## <True|False> ensemble 推理（更稳、更慢，需要同名的 _ensemble 模型）
## <auto|conf|native|cuda|TensorRT|TensorRT-RTX> 推理后端，auto 走内置梯队
## 整数，使用的显卡序号，0 为排序一号；None 交给插件默认卡
## 整数，>0 时先把源预缩到这个高度再补帧（4K 高倍率显存吃紧时可填 1080）
## <True|False> 每帧向 stderr 打一行耗时
## <True|False> 往 <本脚本>.k7f-stats.json 写耗时统计（OSD 常驻行读它）

#### OSD 参数通道 ####
# 前端脚本用 vf set 的 user-data 子参数传值，例如
#   vf set @VAJI:vapoursynth="~~/vs/MEMC_RIFE_AJI.vpy":user-data="multi=3;model=4.26;scene=0.15"
# 优先级：字面量 < 环境变量（VAJI_*）< user_data；解析/插件定位/上行报告都在 k7f.FRAMEGEN_VPY 里

out = k7f.FRAMEGEN_VPY(
	video_in, __file__, engine="vaji", fps_in=container_fps,
	user_data=globals().get("user_data"),
	multi=Multi, scene=Scene, model=Model, ensemble=Ensemble, backend=Backend,
	pre_h=Pre_H, gpu=Gpu, log=Log, stats=Stats,
)

out.set_output()
