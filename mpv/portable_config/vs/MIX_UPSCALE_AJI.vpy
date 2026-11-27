### 2× 超分（AnimeJaNai · libaji 运行时）—— N 卡专用
### 依赖：vs-plugins\vsaji.dll + vs-plugins\aji\（同 MEMC_RIFE_AJI.vpy）
### 用法：vf set vapoursynth="~~/vs/MIX_UPSCALE_AJI.vpy"
### 叠加：接在 MEMC_* 之后即为「插帧 → 超分」（顺序固定 插帧 → 超分 → 时序）

import k7sfunc as k7f

############
# 用户选项 #
############

Model = "hd_perf"
Backend = "auto"
Gpu = None
Log = False
Stats = False
## <hd_perf|hd_bal|hd_perf_sharp|hd_bal_sharp|sd> 超分模型档（2×）
##   hd_* = HD_V3.1 系列（720p 以上源），sd = SD Compact（低分辨率动画源）
## <auto|conf|native|cuda|TensorRT|TensorRT-RTX> 推理后端，auto 走内置梯队
## 整数，使用的显卡序号，0 为排序一号；None 交给插件默认卡
## <True|False> 每帧向 stderr 打一行耗时
## <True|False> 往 <本脚本>.k7f-stats.json 写耗时统计（OSD 常驻行读它）

#### OSD 参数通道 ####
# 前端脚本用 vf set 的 user-data 子参数传值，例如
#   vf set @VAJI:vapoursynth="~~/vs/MIX_UPSCALE_AJI.vpy":user-data="model=hd_bal"
# 优先级：字面量 < 环境变量（VAJI_UPSCALE_*）< user_data

out = k7f.AJI_VPY(
	video_in, __file__, engine="vaji_upscale",
	user_data=globals().get("user_data"),
	model=Model, backend=Backend, gpu=Gpu, log=Log, stats=Stats,
)

out.set_output()
