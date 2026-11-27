"""补帧 - RIFE

"""

import typing
import math
import fractions
import os
import vapoursynth as vs
from ._internal import (
	core,
	_validate_input_clip,
	_validate_bool,
	_validate_literal,
	_validate_numeric,
	_check_plugin,
	_check_trt,
	_trt_plugin_dir,
	_trt_backend,
)
from .mod_helper import (
	SCENE_DETECT,
)

##################################################
## RIFE补帧 ncnn Vulkan
##################################################

def RIFE_STD(
	input : vs.VideoNode,
	model : typing.Literal[23, 70, 72, 73] = 23,
	turbo : typing.Literal[0, 1, 2] = 2,
	fps_num : int = 2,
	fps_den : int = 1,
	sc_mode : typing.Literal[0, 1, 2] = 1,
	stat_th : float = 60.0,
	gpu : typing.Literal[0, 1, 2] = 0,
	gpu_t : int = 2,
) -> vs.VideoNode :

	func_name = "RIFE_STD"
	_validate_input_clip(func_name, input)
	_validate_literal(func_name, "model", model, [23, 70, 72, 73])
	_validate_literal(func_name, "turbo", turbo, [0, 1, 2])
	_validate_numeric(func_name, "fps_num", fps_num, min_val=2, int_only=True)
	if not isinstance(fps_den, int) or fps_den >= fps_num or fps_num/fps_den <= 1 :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_den 的值无效")
	_validate_literal(func_name, "sc_mode", sc_mode, [0, 1, 2])
	_validate_numeric(func_name, "stat_th", stat_th, min_val=0.0, max_val=60.0)
	_validate_literal(func_name, "gpu", gpu, [0, 1, 2])
	_validate_numeric(func_name, "gpu_t", gpu_t, min_val=1, int_only=True)

	_check_plugin(func_name, "rife")
	if skip :
		_check_plugin(func_name, "vmaf")
	if sc_mode == 2 :
		_check_plugin(func_name, "mv")

	fmt_in = input.format.id
	colorlv = getattr(input.get_frame(0).props, "_ColorRange", 0)

	if turbo == 0 :
		skip = False
		s_tta = True
	elif turbo == 1 :
		skip = False
		s_tta = False
	elif turbo == 2 :
		skip = True
		s_tta = False
	if model >= 63 :
		s_tta = False

	cut0 = SCENE_DETECT(input=input, sc_mode=sc_mode)

	cut1 = core.resize.Bilinear(clip=cut0, format=vs.RGBS, matrix_in_s="709")
	cut2 = core.rife.RIFE(clip=cut1, model=(model+1) if s_tta else model,
		factor_num=fps_num, factor_den=fps_den, gpu_id=gpu, gpu_thread=gpu_t,
		sc=True if sc_mode else False, skip=skip, skip_threshold=stat_th)
	output = core.resize.Bilinear(clip=cut2, format=fmt_in, matrix_s="709", range=1 if colorlv==0 else None)

	return output

##################################################
## RIFE补帧 # helper
##################################################

def RIFE_ORT_HUB(
	input : vs.VideoNode,
	model : typing.Literal[46, 4251, 426, 4262],
	turbo : bool,
	fps_in : float,
	fps_num : int,
	fps_den : int,
	sc_mode : typing.Literal[0, 1, 2],
	gpu_t : int,
	backend_type : str,
	backend_param,
	func_name : str,
	vsmlrt,
) -> vs.VideoNode :

	_validate_input_clip(func_name, input)
	_validate_literal(func_name, "model", model, [46, 4251, 426, 4262])
	_validate_bool(func_name, "turbo", turbo)
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	_validate_numeric(func_name, "fps_num", fps_num, min_val=2, int_only=True)
	if not isinstance(fps_den, int) or fps_den >= fps_num or fps_num/fps_den <= 1 :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_den 的值无效")
	_validate_literal(func_name, "sc_mode", sc_mode, [0, 1, 2])
	_validate_numeric(func_name, "gpu_t", gpu_t, min_val=1, int_only=True)

	_check_plugin(func_name, "ort")
	if sc_mode == 2 :
		_check_plugin(func_name, "mv")

	if backend_type == "coreml" :
		## CoreML 缺失 akarin，不支持 ext_proc
		cond_support = False
	else :
		## 其它后端需要 akarin 支持
		_check_plugin(func_name, "akarin")
		cond_support = True

	#ext_proc = True
	#s_tta = False
	if turbo :
		ext_proc = False
		s_tta = False
	else :
		ext_proc = cond_support
		s_tta = True

	plg_dir = os.path.dirname(core.ort.Version()["path"]).decode()
	mdl_pname = "rife/" if ext_proc else "rife_v2/"
	if model in [4251, 426, 4262] :
		## https://github.com/AmusementClub/vs-mlrt/blob/2adfbab790eebe51c62c886400b0662570dfe3e9/scripts/vsmlrt.py#L1031-L1032
		s_tta = False
	if s_tta :
		mdl_fname = ["rife_v4.6_ensemble"][[46].index(model)]
	else :
		mdl_fname = ["rife_v4.6", "rife_v4.25_lite", "rife_v4.26", "rife_v4.26_heavy"][[46, 4251, 426, 4262].index(model)]
	mdl_pth = plg_dir + "/models/" + mdl_pname + mdl_fname + ".onnx"
	if not os.path.exists(mdl_pth) :
		raise vs.Error(f"模块 {func_name} 所请求的模型缺失")

	w_in, h_in = input.width, input.height
	size_in = w_in * h_in
	colorlv = getattr(input.get_frame(0).props, "_ColorRange", 0)
	fmt_in = input.format.id
	fps_factor = fps_num/fps_den

	if (size_in > 4096 * 2176) :
		raise Exception("源分辨率超过限制的范围，已临时中止。")

	scale_model = 1
	if (size_in > 2048 * 1088) :
		scale_model = 0.5
		if not ext_proc :
			## https://github.com/AmusementClub/vs-mlrt/blob/abc5b1c777a5dde6bad51a099f28eba99375ef4e/scripts/vsmlrt.py#L1002
			scale_model = 1
	if model >= 47 :
		## https://github.com/AmusementClub/vs-mlrt/blob/2adfbab790eebe51c62c886400b0662570dfe3e9/scripts/vsmlrt.py#L1036-L1037
		scale_model = 1

	## https://github.com/AmusementClub/vs-mlrt/blob/2adfbab790eebe51c62c886400b0662570dfe3e9/scripts/vsmlrt.py#L1014-L1023
	tile_size = 32
	if model == 4251 :
		tile_size = 128
	elif model in [426, 4262] :
		tile_size = 64
	tile_size = tile_size / scale_model
	w_tmp = math.ceil(w_in / tile_size) * tile_size - w_in
	h_tmp = math.ceil(h_in / tile_size) * tile_size - h_in

	cut0 = SCENE_DETECT(input=input, sc_mode=sc_mode)
	cut1 = core.resize.Bilinear(clip=cut0, format=vs.RGBS, matrix_in_s="709")

	backend = backend_param(ext_proc)
	if ext_proc :
		if w_tmp + h_tmp > 0 :
			cut1 = core.std.AddBorders(clip=cut1, right=w_tmp, bottom=h_tmp)
		fin = vsmlrt.RIFE(clip=cut1, multi=fractions.Fraction(fps_num, fps_den),
						  scale=scale_model, model=model, ensemble=s_tta,
						  _implementation=1, video_player=True, backend=backend)
		if w_tmp + h_tmp > 0 :
			fin = core.std.Crop(clip=fin, right=w_tmp, bottom=h_tmp)
	else :
		fin = vsmlrt.RIFE(clip=cut1, multi=fractions.Fraction(fps_num, fps_den),
						  scale=scale_model, model=model, ensemble=s_tta,
						  _implementation=2, video_player=True, backend=backend)

	output = core.resize.Bilinear(clip=fin, format=fmt_in, matrix_s="709", range=1 if colorlv==0 else None)
	if not fps_factor.is_integer() :
		output = core.std.AssumeFPS(clip=output, fpsnum=fps_in * fps_num * 1e6, fpsden=fps_den * 1e6)

	return output

##################################################
## RIFE补帧 CoreML
##################################################

def RIFE_COREML(
	input : vs.VideoNode,
	model : typing.Literal[46, 4251, 426, 4262] = 46,
	turbo : bool = True,
	fps_in : float = 23.976,
	fps_num : int = 2,
	fps_den : int = 1,
	sc_mode : typing.Literal[0, 1, 2] = 1,
	be : typing.Literal[0, 1] = 0,
	gpu_t : int = 2,
) -> vs.VideoNode :

	func_name = "RIFE_COREML"
	_validate_literal(func_name, "be", be, [0, 1])

	from ._external import vsmlrt

	fps_den = 1 ## akarin 缺失mac版，暂锁整数倍
	def backend_param(ext_proc):
		return vsmlrt.BackendV2.ORT_COREML(num_streams=gpu_t, fp16=False, ml_program=be) ## CoreML 因未知性能问题禁用fp16

	return RIFE_ORT_HUB(
		input=input,
		model=model,
		turbo=turbo,
		fps_in=fps_in,
		fps_num=fps_num,
		fps_den=fps_den,
		sc_mode=sc_mode,
		gpu_t=gpu_t,
		backend_type="coreml",
		backend_param=backend_param,
		func_name=func_name,
		vsmlrt=vsmlrt,
	)

##################################################
## RIFE补帧 DirectML
##################################################

def RIFE_DML(
	input : vs.VideoNode,
	model : typing.Literal[46, 4251, 426, 4262] = 46,
	turbo : bool = True,
	fps_in : float = 23.976,
	fps_num : int = 2,
	fps_den : int = 1,
	sc_mode : typing.Literal[0, 1, 2] = 1,
	gpu : typing.Literal[0, 1, 2] = 0,
	gpu_t : int = 2,
) -> vs.VideoNode :

	func_name = "RIFE_DML"
	_validate_literal(func_name, "gpu", gpu, [0, 1, 2])

	from ._external import vsmlrt

	def backend_param(ext_proc):
		fp16 = True if ext_proc else False ## https://github.com/AmusementClub/vs-mlrt/issues/56#issuecomment-2801745592
		return vsmlrt.BackendV2.ORT_DML(num_streams=gpu_t, fp16=fp16, device_id=gpu)

	return RIFE_ORT_HUB(
		input=input,
		model=model,
		turbo=turbo,
		fps_in=fps_in,
		fps_num=fps_num,
		fps_den=fps_den,
		sc_mode=sc_mode,
		gpu_t=gpu_t,
		backend_type="dml",
		backend_param=backend_param,
		func_name=func_name,
		vsmlrt=vsmlrt,
	)

##################################################
## RIFE补帧 TensorRT
##################################################

def RIFE_NV(
	input : vs.VideoNode,
	model : typing.Literal[46, 4251, 426, 4262] = 46,
	int8_qnt : bool = False,
	turbo : typing.Literal[0, 1, 2] = 2,
	fps_in : float = 23.976,
	fps_num : int = 2,
	fps_den : int = 1,
	sc_mode : typing.Literal[0, 1, 2] = 1,
	gpu : typing.Literal[0, 1, 2] = 0,
	gpu_t : int = 2,
	ws_size : int = 0,
) -> vs.VideoNode :

	func_name = "RIFE_NV"
	_validate_input_clip(func_name, input)
	_validate_literal(func_name, "model", model, [46, 4251, 426, 4262])
	_validate_bool(func_name, "int8_qnt", int8_qnt)
	_validate_literal(func_name, "turbo", turbo, [0, 1, 2])
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	_validate_numeric(func_name, "fps_num", fps_num, min_val=2, int_only=True)
	if not isinstance(fps_den, int) or fps_den >= fps_num or fps_num/fps_den <= 1 :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_den 的值无效")
	_validate_literal(func_name, "sc_mode", sc_mode, [0, 1, 2])
	_validate_literal(func_name, "gpu", gpu, [0, 1, 2])
	_validate_numeric(func_name, "gpu_t", gpu_t, min_val=1, int_only=True)
	_validate_numeric(func_name, "ws_size", ws_size, min_val=0, int_only=True)

	trt_ns = _check_trt(func_name)
	if sc_mode == 2 :
		_check_plugin(func_name, "mv")
	_check_plugin(func_name, "akarin")

	from ._external import vsmlrt

	#ext_proc = True
	#s_tta = False
	if turbo == 0 :
		ext_proc = True
		s_tta = True
	elif turbo == 1 :
		ext_proc = True
		s_tta = False
	elif turbo == 2 :
		ext_proc = False
		s_tta = False

	plg_dir = _trt_plugin_dir(trt_ns)
	mdl_pname = "rife/" if ext_proc else "rife_v2/"
	if model in [4251, 426, 4262] : ## https://github.com/AmusementClub/vs-mlrt/blob/2adfbab790eebe51c62c886400b0662570dfe3e9/scripts/vsmlrt.py#L1031-L1032
		s_tta = False
	if s_tta :
		mdl_fname = ["rife_v4.6_ensemble"][[46].index(model)]
	else :
		mdl_fname = ["rife_v4.6", "rife_v4.25_lite", "rife_v4.26", "rife_v4.26_heavy"][[46, 4251, 426, 4262].index(model)]
	mdl_pth = plg_dir + "/models/" + mdl_pname + mdl_fname + ".onnx"
	if not os.path.exists(mdl_pth) :
		raise vs.Error(f"模块 {func_name} 所请求的模型缺失")

	w_in, h_in = input.width, input.height
	size_in = w_in * h_in
	colorlv = getattr(input.get_frame(0).props, "_ColorRange", 0)
	fmt_in = input.format.id
	fps_factor = fps_num/fps_den

	st_eng = False
	if not ext_proc and model >= 47 : # https://github.com/AmusementClub/vs-mlrt/issues/72
		st_eng = True
	if (size_in > 4096 * 2176) :
		raise Exception("源分辨率超过限制的范围，已临时中止。")
	if not st_eng and (((w_in > 4096) or (h_in > 2176)) or ((w_in < 384) or (h_in < 384))) :
		raise Exception("源分辨率不属于动态引擎支持的范围，已临时中止。")

	scale_model = 1
	if st_eng and (size_in > 2048 * 1088) :
		scale_model = 0.5
		if not ext_proc : ## https://github.com/AmusementClub/vs-mlrt/blob/abc5b1c777a5dde6bad51a099f28eba99375ef4e/scripts/vsmlrt.py#L1002
			scale_model = 1
	if model >= 47 : ## https://github.com/AmusementClub/vs-mlrt/blob/2adfbab790eebe51c62c886400b0662570dfe3e9/scripts/vsmlrt.py#L1036-L1037
		scale_model = 1

	tile_size = 32 ## https://github.com/AmusementClub/vs-mlrt/blob/2adfbab790eebe51c62c886400b0662570dfe3e9/scripts/vsmlrt.py#L1014-L1023
	if model == 4251 :
		tile_size = 128
	elif model in [426, 4262] :
		tile_size = 64
	tile_size = tile_size / scale_model
	w_tmp = math.ceil(w_in / tile_size) * tile_size - w_in
	h_tmp = math.ceil(h_in / tile_size) * tile_size - h_in

	shape_list = {
		32: {"min": (10, 8), "opt": (60, 34), "max1": (128, 68), "max2": (64, 34)},
		64: {"min": (5, 4), "opt": (30, 17), "max1": (64, 34), "max2": (32, 17)},
		128: {"min": (3, 2), "opt": (15, 9), "max1": (32, 17), "max2": (16, 9)},}
	shape_cfg = shape_list[tile_size]
	min_shapes = [tile_size * x for x in shape_cfg["min"]]
	opt_shapes = [tile_size * x for x in shape_cfg["opt"]]
	max_shapes1 = [tile_size * x for x in shape_cfg["max1"]]
	max_shapes2 = [tile_size * x for x in shape_cfg["max2"]]

	cut0 = SCENE_DETECT(input=input, sc_mode=sc_mode)

	cut1 = core.resize.Bilinear(clip=cut0, format=vs.RGBH, matrix_in_s="709")
	if ext_proc :
		if w_tmp + h_tmp > 0 :
			cut1 = core.std.AddBorders(clip=cut1, right=w_tmp, bottom=h_tmp)
		fin = vsmlrt.RIFE(clip=cut1, multi=fractions.Fraction(fps_num, fps_den), scale=scale_model, model=model, ensemble=s_tta, _implementation=1, video_player=True, backend=_trt_backend(vsmlrt, trt_ns,
			num_streams=gpu_t, int8=int8_qnt, fp16=True, output_format=1,
			workspace=None if ws_size < 128 else (ws_size if st_eng else ws_size * 2),
			use_cuda_graph=True, use_cublas=False, use_cudnn=False,
			static_shape=st_eng, min_shapes=[0, 0] if st_eng else min_shapes,
			opt_shapes=None if st_eng else opt_shapes,
			max_shapes=None if st_eng else (max_shapes1 if (size_in > 2048 * 1088) else max_shapes2),
			device_id=gpu, short_path=True))
		if w_tmp + h_tmp > 0 :
			fin = core.std.Crop(clip=fin, right=w_tmp, bottom=h_tmp)
	else :
		fin = vsmlrt.RIFE(clip=cut1, multi=fractions.Fraction(fps_num, fps_den), scale=scale_model, model=model, ensemble=s_tta, _implementation=2, video_player=True, backend=_trt_backend(vsmlrt, trt_ns,
			num_streams=gpu_t, int8=int8_qnt, fp16=True, output_format=1,
			workspace=None if ws_size < 128 else ws_size,
			use_cuda_graph=True, use_cublas=False, use_cudnn=False,
			static_shape=st_eng, min_shapes=[0, 0],
			opt_shapes=None, max_shapes=None,
			device_id=gpu, short_path=True))
	output = core.resize.Bilinear(clip=fin, format=fmt_in, matrix_s="709", range=1 if colorlv==0 else None)
	if not fps_factor.is_integer() :
		output = core.std.AssumeFPS(clip=output, fpsnum=fps_in * fps_num * 1e6, fpsden=fps_den * 1e6)

	return output
