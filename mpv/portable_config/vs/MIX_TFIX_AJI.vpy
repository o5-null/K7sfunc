### 时序修复 temporalfix（AnimeJaNai · libaji 运行时）—— N 卡专用
### 依赖：vs-plugins\vsaji.dll + vs-plugins\aji\（同 MEMC_RIFE_AJI.vpy）
### 用法：vf set vapoursynth="~~/vs/MIX_TFIX_AJI.vpy"
### 叠加：接在超分之后即为「插帧 → 超分 → 时序」（顺序固定）
### 特点：逐帧稳定细节、抑制超分/插帧的闪烁；平坦画面近似恒等，代价是延迟（要多要前后帧）

import k7sfunc as k7f

############
# 用户选项 #
############

Model = "s1"
Scene = 0.150
Before_Upscale = False
Backend = "auto"
Gpu = None
Log = False
Stats = False
## <s1|s2|s3> temporalfix 模型档（s1 半径 3 = 7 帧窗；s2/s3 更大、更慢）
## 浮点，切镜阈值（0~1），0 关闭；快节奏素材可调到 0.2~0.3
## <True|False> 仅当同一条 conf 内还含超分链时才有意义（本项目把两层拆开，通常保持 False）
## <auto|conf|native|cuda|TensorRT|TensorRT-RTX> 推理后端，auto 走内置梯队
## 整数，使用的显卡序号，0 为排序一号；None 交给插件默认卡
## <True|False> 每帧向 stderr 打一行耗时
## <True|False> 往 <本脚本>.k7f-stats.json 写耗时统计（OSD 常驻行读它）

#### OSD 参数通道 ####
# 前端脚本用 vf set 的 user-data 子参数传值，例如
#   vf set @VAJI:vapoursynth="~~/vs/MIX_TFIX_AJI.vpy":user-data="model=s2;scene=0.2"
# 优先级：字面量 < 环境变量（VAJI_TFIX_*）< user_data

out = k7f.AJI_VPY(
	video_in, __file__, engine="vaji_tfix",
	user_data=globals().get("user_data"),
	model=Model, scene=Scene, before_upscale=Before_Upscale,
	backend=Backend, gpu=Gpu, log=Log, stats=Stats,
)

out.set_output()
