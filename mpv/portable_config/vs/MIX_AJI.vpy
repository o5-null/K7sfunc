### 叠层脚本（补帧 → 超分 → 时序）—— N 卡专用
### 补帧层可以选 vs-framegen（DLSSG / XeSS-FG / VFG / FSR4）或 AJI 的 RIFE
### 依赖：vs-plugins\vsaji.dll + vs-plugins\aji\（构建见 docs\k7-aji-rife.md）
### 用法：vf set vapoursynth="~~/vs/MIX_AJI.vpy":user-data="stages=fut"
### 为什么是一个脚本：mpv 的 vf_vapoursynth 同时只允许一个实例
###   （mpv-src\video\filter\vf_vapoursynth.c:582 断言 !p->in_node_active），
###   三层只能靠 VS 内部串联，不能在 vf 链上 vf append 多个 vapoursynth。
### 层顺序固定，缺层就跳过；stages 里没有的层不占显存也不建引擎。

import vapoursynth as vs

import k7sfunc as k7f

############
# 用户选项 #
############

Framegen = "vaji"   # <vaji|vdlssg|vxessfg|vvfg|vfsr4|off> 补帧层（f）用哪个引擎
Stages = "f"        # <f|u|t|组合> 启用的层，顺序固定 f=补帧 u=超分 t=时序，如 "fu"/"fut"/"t"
Multi = 2           # 整数倍率（f 层；FSR4 固定 2×，没有 multi）
Scene = 0.150       # 补帧切镜阈值（f 层；vaji 走 aji 内建检测，0 等价关闭=1.0）
Model = "4.14"      # RIFE 模型号（f 层 = vaji），见 aji\rife
Ensemble = False    # ensemble 推理（f 层 = vaji）
Duplicate = 0.005   # 重复帧阈值（f 层 = vsfg）
Preset = "a"        # DLSS-G 模型预设 a/c（f 层 = vdlssg）
Mode = 1            # VFG 档位（f 层 = vvfg）
Camera = 0          # 相机运动（f 层 = vfsr4）
Model_Upscale = "hd_perf"   # 超分档位（u 层）：hd_perf/hd_bal/hd_perf_sharp/hd_bal_sharp/sd
Model_Tfix = "s1"   # 时序模型（t 层）：s1/s2/s3
Tfix_Scene = 0.150  # 时序切镜阈值（t 层，另一套 RGB 度量）
Before_Upscale = False  # 时序在超分之前跑（t 层，默认否）
Backend = "auto"    # <auto|conf|native|cuda|TensorRT|TensorRT-RTX>
Gpu = None          # 显卡序号，None 交给插件默认卡
Pre_H = 0           # >0 时先把源预缩到这个高度再做补帧
Log = False
Stats = False

#### OSD 参数通道 ####
# 前端（k7f_vpypar.lua）把面板选择写进 user-data：
#   stages / framegen / multi / scene / rife_model / ensemble / upscale_model /
#   tfix_model / tfix_scene / before_upscale / backend / device / pre_h / log / stats
# stages 与 framegen 在这里先读一次——面板切换层/引擎不必改脚本，只重建滤镜。
# 其余键由各阶段函数按同一优先级解析（字面量 < 环境变量 < user_data）。

_ud = k7f.VPY_PARSE_USER_DATA(globals().get("user_data"))
stages = str(_ud.get("stages", Stages)).lower()
framegen = str(_ud.get("framegen", Framegen)).strip().lower()

for _ch in stages :
	if _ch not in "fut" :
		raise vs.Error(f"MIX_AJI: stages 只接受 f/u/t 的组合，收到 {stages!r}")

# 关掉补帧时即使 stages 里有 f 也跳过（面板把“关”写成 framegen=off）
if framegen in ("off", "none", "", "0") :
	stages = stages.replace("f", "")
if "f" in stages and not framegen :
	raise vs.Error("MIX_AJI: stages 里有 f，但 framegen 是空的")

out = video_in
last = stages[-1:] if stages else ""

# 1) 补帧：fps 变 multi 倍
if "f" in stages :
	if framegen == "vaji" :
		out = k7f.FRAMEGEN_VPY(
			out, __file__, engine="vaji", fps_in=container_fps,
			user_data=globals().get("user_data"),
			multi=Multi, scene=Scene, model=Model, ensemble=Ensemble,
			backend=Backend, pre_h=Pre_H, gpu=Gpu, log=Log, stats=False,
			publish=False,
		)
	else :
		out = k7f.FRAMEGEN_VPY(
			out, __file__, engine=framegen, fps_in=container_fps,
			user_data=globals().get("user_data"),
			multi=Multi, scene=Scene, duplicate=Duplicate, preset=Preset,
			mode=Mode, camera=Camera, backend=Backend, pre_h=Pre_H, gpu=Gpu,
			log=Log, stats=False, publish=False,
		)

# 2) 超分：2×
if "u" in stages :
	out = k7f.AJI_VPY(
		out, __file__, engine="vaji_upscale",
		user_data=globals().get("user_data"),
		model=Model_Upscale, backend=Backend, gpu=Gpu, log=Log, stats=False,
		publish=False,
	)

# 3) 时序：帧数不变
if "t" in stages :
	out = k7f.AJI_VPY(
		out, __file__, engine="vaji_tfix",
		user_data=globals().get("user_data"),
		model=Model_Tfix, scene=Tfix_Scene, before_upscale=Before_Upscale,
		backend=Backend, gpu=Gpu, log=Log, stats=(Stats and last == "t"),
		publish=False,
	)

# 统一上报：一条 sidecar 描述整条链（单层时也走这里，摘要里带层名）
if stages :
	k7f.AJI_STACK_PUBLISH(
		__file__,
		stages=stages,
		values={
			"framegen" : framegen,
			"multi" : Multi, "scene" : Scene, "model" : Model,
			"ensemble" : Ensemble, "model_upscale" : Model_Upscale,
			"model_tfix" : Model_Tfix, "tfix_scene" : Tfix_Scene,
			"before_upscale" : Before_Upscale, "backend" : Backend,
			"gpu" : Gpu, "log" : Log, "stats" : Stats,
		},
		video_in=video_in,
		out=out,
	)

out.set_output()
