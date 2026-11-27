"""补帧 - 帧生成（vs-framegen：DLSS-G / XeSS-FG / VFG / FSR4）

"""

import typing
import os
import vapoursynth as vs
from ._internal import (
	core,
	_validate_input_clip,
	_validate_bool,
	_validate_literal,
	_validate_numeric,
	_check_plugin,
)

##################################################
## 帧生成补帧 # helper
## 四个引擎共用一套宿主：vdlssg / vxessfg / vvfg / vfsr4（vs-framegen）
##################################################

## 每个命名空间支持的倍率由引擎决定：vfsr4 固定 2；vxessfg 在 sm86 上也只放行 2
## （3×/4× 会触发 0xe0d55001 引擎自杀，见 mod_memc 的 XESSFG_NV 说明）
FRAMEGEN_ENGINES = {
	"vdlssg" : {"name" : "DLSS-G", "multi" : [2, 3, 4, 5, 6]},
	"vxessfg" : {"name" : "XeSS-FG", "multi" : [2]},
	"vvfg" : {"name" : "VFG", "multi" : [2, 3, 4, 5, 6]},
	"vfsr4" : {"name" : "FSR4", "multi" : [2]},
	# 这里只登记倍率范围，给 mod_vpy 的参数解析用；aji 不走 FRAMEGEN_HUB
	# （插件函数是 vsaji.Rife，宿主由 mod_aji.RIFE_AJI 负责）
	"vaji" : {"name" : "RIFE-AJI", "multi" : [2, 3, 4, 5, 6]},
}

def FRAMEGEN_HUB(
	input : vs.VideoNode,
	engine_ns : str,
	multi : int,
	scene_threshold : float,
	camera : int,
	fps_in : float,
	gpu,
	proxy_dir,
	log : bool,
	func_name : str,
	mode : int = 1,
) -> vs.VideoNode :

	_validate_input_clip(func_name, input)
	_validate_literal(func_name, "multi", multi, FRAMEGEN_ENGINES[engine_ns]["multi"])
	# 模型档位只有 VFG 有（low/medium/high）；其余引擎收到 mode 也不会往下传
	if engine_ns == "vvfg" :
		_validate_literal(func_name, "mode", mode, [0, 1, 2])
	_validate_numeric(func_name, "scene_threshold", scene_threshold, min_val=0.0, max_val=1.0)
	_validate_numeric(func_name, "camera", camera, min_val=0, int_only=True)
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	if gpu is not None :
		_validate_numeric(func_name, "gpu", gpu, min_val=0, int_only=True)
	if proxy_dir is not None :
		if not isinstance(proxy_dir, str) :
			raise vs.Error(f"模块 {func_name} 的子参数 proxy_dir 的值无效")
		if not os.path.isdir(proxy_dir) :
			raise vs.Error(f"模块 {func_name} 所请求的引擎包目录不存在")
	_validate_bool(func_name, "log", log)

	_check_plugin(func_name, engine_ns)

	yuv_in = input.format.color_family != vs.RGB
	fmt_in = input.format.id
	colorlv = getattr(input.get_frame(0).props, "_ColorRange", 0)

	## 插件只接受 RGBS（float32 三平面），YUV 源按 709 转换
	cut0 = core.resize.Bilinear(clip=input, format=vs.RGBS,
								matrix_in_s="709" if yuv_in else None)

	args = {"multi" : multi, "scene_threshold" : scene_threshold, "log" : 1 if log else 0}
	if engine_ns == "vvfg" :
		args["mode"] = mode
	if camera :
		args["camera"] = camera
	if gpu is not None :
		args["gpu"] = gpu
	if proxy_dir is not None :
		args["proxy_dir"] = proxy_dir
	fin = getattr(core, engine_ns).FrameGen(cut0, **args)

	## 回到源像素格式；插件输出恒为 full range
	output = core.resize.Bilinear(clip=fin, format=fmt_in,
								  matrix_s="709" if yuv_in else None,
								  range_in_s="full" if yuv_in else None,
								  range_s=("limited" if colorlv == 0 else "full") if yuv_in else None)
	## 插件保持输入 fps、帧数乘 multi，必须改回真实输出帧率，否则播放成慢动作
	output = core.std.AssumeFPS(clip=output, fpsnum=int(round(fps_in * multi * 1e6)), fpsden=1000000)

	return output

##################################################
## 帧生成补帧 DLSS-G (vs-framegen)
##################################################

def DLSSG_NV(
	input : vs.VideoNode,
	multi : typing.Literal[2, 3, 4, 5, 6] = 2,
	scene_threshold : float = 0.25,
	camera : typing.Literal[0, 1, 2, 3] = 0,
	fps_in : float = 23.976,
	gpu : typing.Optional[int] = None,
	proxy_dir : typing.Optional[str] = None,
	log : bool = False,
) -> vs.VideoNode :

	func_name = "DLSSG_NV"
	_validate_literal(func_name, "camera", camera, [0, 1, 2, 3])

	return FRAMEGEN_HUB(
		input=input,
		engine_ns="vdlssg",
		multi=multi,
		scene_threshold=scene_threshold,
		camera=camera,
		fps_in=fps_in,
		gpu=gpu,
		proxy_dir=proxy_dir,
		log=log,
		func_name=func_name,
	)

##################################################
## 帧生成补帧 XeSS-FG (vs-framegen)
##################################################

def XESSFG_NV(
	input : vs.VideoNode,
	multi : typing.Literal[2] = 2,
	scene_threshold : float = 0.25,
	camera : typing.Literal[0, 1, 2, 3] = 0,
	fps_in : float = 23.976,
	gpu : typing.Optional[int] = None,
	proxy_dir : typing.Optional[str] = None,
	log : bool = False,
) -> vs.VideoNode :

	func_name = "XESSFG_NV"
	_validate_literal(func_name, "camera", camera, [0, 1, 2, 3])

	return FRAMEGEN_HUB(
		input=input,
		engine_ns="vxessfg",
		multi=multi,
		scene_threshold=scene_threshold,
		camera=camera,
		fps_in=fps_in,
		gpu=gpu,
		proxy_dir=proxy_dir,
		log=log,
		func_name=func_name,
	)

##################################################
## 帧生成补帧 NVIDIA VFG (vs-framegen)
##################################################

def VFG_NV(
	input : vs.VideoNode,
	multi : typing.Literal[2, 3, 4, 5, 6] = 2,
	scene_threshold : float = 0.25,
	mode : typing.Literal[0, 1, 2] = 1,
	camera : typing.Literal[0, 1, 2, 3] = 0,
	fps_in : float = 23.976,
	gpu : typing.Optional[int] = None,
	proxy_dir : typing.Optional[str] = None,
	log : bool = False,
) -> vs.VideoNode :

	func_name = "VFG_NV"
	_validate_literal(func_name, "camera", camera, [0, 1, 2, 3])
	# 0 低 / 1 中 / 2 高；medium 与 high 只差 0.4 dB PSNR-Y，速度差 2 倍多
	_validate_literal(func_name, "mode", mode, [0, 1, 2])

	return FRAMEGEN_HUB(
		input=input,
		engine_ns="vvfg",
		multi=multi,
		scene_threshold=scene_threshold,
		camera=camera,
		fps_in=fps_in,
		gpu=gpu,
		proxy_dir=proxy_dir,
		log=log,
		func_name=func_name,
		mode=mode,
	)

##################################################
## 帧生成补帧 AMD FSR 4 (vs-framegen)
##################################################

def FSR4(
	input : vs.VideoNode,
	multi : typing.Literal[2] = 2,
	scene_threshold : float = 0.25,
	camera : int = 0,
	fps_in : float = 23.976,
	gpu : typing.Optional[int] = None,
	proxy_dir : typing.Optional[str] = None,
	log : bool = False,
) -> vs.VideoNode :

	func_name = "FSR4"

	return FRAMEGEN_HUB(
		input=input,
		engine_ns="vfsr4",
		multi=multi,
		scene_threshold=scene_threshold,
		camera=camera,
		fps_in=fps_in,
		gpu=gpu,
		proxy_dir=proxy_dir,
		log=log,
		func_name=func_name,
	)
