"""补帧 - DRBA

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
## DRBA补帧 # helper
##################################################

def DRBA_HUB(
	input : vs.VideoNode,
	model : typing.Literal[1, 2],
	turbo : typing.Literal[0, 1, 2],
	fps_in : float,
	fps_num : int,
	fps_den : int,
	sc_mode : typing.Literal[0, 1, 2],
	gpu : typing.Literal[0, 1, 2],
	gpu_t : int,
	backend_type : str,
	backend_param,
	func_name : str,
	vsmlrt,
) -> vs.VideoNode :

	_validate_input_clip(func_name, input)
	_validate_literal(func_name, "model", model, [1, 2])
	_validate_literal(func_name, "turbo", turbo, [0, 1, 2])
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	_validate_numeric(func_name, "fps_num", fps_num, min_val=2, int_only=True)
	if not isinstance(fps_den, int) or fps_den >= fps_num or fps_num/fps_den <= 1 :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_den 的值无效")
	_validate_literal(func_name, "sc_mode", sc_mode, [0, 1, 2])
	_validate_literal(func_name, "gpu", gpu, [0, 1, 2])
	_validate_numeric(func_name, "gpu_t", gpu_t, min_val=1, int_only=True)

	trt_ns = None
	if backend_type == "dml" :
		_check_plugin(func_name, "ort")
	else :
		trt_ns = _check_trt(func_name)
	if sc_mode == 2 :
		_check_plugin(func_name, "mv")
	_check_plugin(func_name, "akarin")

	model_scale = False
	model_ap = False
	model_half = False
	if turbo :
		model_ap = True
		if turbo == 2 :
			model_scale = True

	if backend_type == "dml" :
		plg_dir = os.path.dirname(core.ort.Version()["path"]).decode()
		model_half = True
	else :
		plg_dir = _trt_plugin_dir(trt_ns)
	mdl_pname = "drba/"
	mdl_fname = ["distilDRBA_v1", "distilDRBA_v2_lite"][[1, 2].index(model)]
	mdl_var_parts = []
	if model_scale:
		mdl_var_parts.append("_scale")
	if model_ap:
		mdl_var_parts.append("_ap")
	if model_half:
		mdl_var_parts.append("_fp16")
	mdl_var = "".join(mdl_var_parts)
	mdl_pth = plg_dir + "/models/" + mdl_pname + mdl_fname + mdl_var + ".onnx"
	if not os.path.exists(mdl_pth) :
		raise vs.Error(f"模块 {func_name} 所请求的模型缺失")

	w_in, h_in = input.width, input.height
	size_in = w_in * h_in
	colorlv = getattr(input.get_frame(0).props, "_ColorRange", 0)
	fmt_in = input.format.id
	fps_factor = fps_num/fps_den

	if (size_in > 4096 * 2176) :
		raise Exception("源分辨率超过限制的范围，已临时中止。")

	tile_size = 64
	scale = 1.0
	if model_scale :
		tile_size = 128
		scale = 0.5
	w_tmp = math.ceil(w_in / tile_size) * tile_size - w_in
	h_tmp = math.ceil(h_in / tile_size) * tile_size - h_in

	cut0 = SCENE_DETECT(input=input, sc_mode=sc_mode)
	cut1 = core.resize.Bilinear(clip=cut0, format=vs.RGBH, matrix_in_s="709")

	backend = backend_param(model_ap, scale, w_in, h_in, w_tmp, h_tmp, tile_size)
	if model_ap :
		fin = vsmlrt.DRBA(clip=cut1, multi=fractions.Fraction(fps_num, fps_den),
		                  scale=scale, ap=model_ap, sp_layer=True,
		                  model=model, video_player=True, **backend)
	else :
		if w_tmp + h_tmp > 0 :
			cut1 = core.std.AddBorders(clip=cut1, right=w_tmp, bottom=h_tmp)
		fin = vsmlrt.DRBA(clip=cut1, multi=fractions.Fraction(fps_num, fps_den),
		                  scale=scale, ap=model_ap, sp_layer=True,
		                  model=model, video_player=True, **backend)
		if w_tmp + h_tmp > 0 :
			fin = core.std.Crop(clip=fin, right=w_tmp, bottom=h_tmp)

	output = core.resize.Bilinear(clip=fin, format=fmt_in, matrix_s="709", range=1 if colorlv==0 else None)
	if not fps_factor.is_integer() :
		output = core.std.AssumeFPS(clip=output, fpsnum=fps_in * fps_num * 1e6, fpsden=fps_den * 1e6)

	return output

##################################################
## DRBA补帧 DirectML
##################################################

def DRBA_DML(
	input : vs.VideoNode,
	model : typing.Literal[1, 2] = 2,
	turbo : typing.Literal[0, 1, 2] = 1,
	fps_in : float = 23.976,
	fps_num : int = 2,
	fps_den : int = 1,
	sc_mode : typing.Literal[0, 1, 2] = 0,
	gpu : typing.Literal[0, 1, 2] = 0,
	gpu_t : int = 2,
) -> vs.VideoNode :

	func_name = "DRBA_DML"

	from ._external import vsmlrt

	def backend_param(model_ap, scale, w_in, h_in, w_tmp, h_tmp, tile_size):
		return {"halfm": True, "backend": vsmlrt.BackendV2.ORT_DML(
			num_streams=gpu_t, fp16=False, device_id=gpu)}

	return DRBA_HUB(
		input=input,
		model=model,
		turbo=turbo,
		fps_in=fps_in,
		fps_num=fps_num,
		fps_den=fps_den,
		sc_mode=sc_mode,
		gpu=gpu,
		gpu_t=gpu_t,
		backend_type="dml",
		backend_param=backend_param,
		func_name=func_name,
		vsmlrt=vsmlrt,
	)

##################################################
## DRBA补帧 TensorRT
##################################################

def DRBA_NV(
	input : vs.VideoNode,
	model : typing.Literal[1, 2] = 2,
	int8_qnt : bool = False,
	turbo : typing.Literal[0, 1, 2] = 1,
	fps_in : float = 23.976,
	fps_num : int = 2,
	fps_den : int = 1,
	sc_mode : typing.Literal[0, 1, 2] = 0,
	gpu : typing.Literal[0, 1, 2] = 0,
	gpu_t : int = 2,
	ws_size : int = 0,
) -> vs.VideoNode :

	func_name = "DRBA_NV"
	_validate_bool(func_name, "int8_qnt", int8_qnt)
	_validate_numeric(func_name, "ws_size", ws_size, min_val=0, int_only=True)

	from ._external import vsmlrt

	# backend_param 闭包要用 trt_ns；这里先解析一次（DRBA_HUB 里也会再取，命中缓存不影响）
	trt_ns = _check_trt(func_name)

	w_in, h_in = input.width, input.height
	size_in = w_in * h_in
	st_eng = True if turbo else False

	if not st_eng and (((w_in > 4096) or (h_in > 2176)) or ((w_in < 384) or (h_in < 384))) :
		raise Exception("源分辨率不属于动态引擎支持的范围，已临时中止。")

	shape_list = {
		64: {"min": (5, 4), "opt": (30, 17), "max1": (64, 34), "max2": (32, 17)},
		128: {"min": (3, 2), "opt": (15, 9), "max1": (32, 17), "max2": (16, 9)},}

	def backend_param(model_ap, scale, w_in, h_in, w_tmp, h_tmp, tile_size):
		shape_cfg = shape_list[tile_size]
		min_shapes = [tile_size * x for x in shape_cfg["min"]]
		opt_shapes = [tile_size * x for x in shape_cfg["opt"]]
		max_shapes1 = [tile_size * x for x in shape_cfg["max1"]]
		max_shapes2 = [tile_size * x for x in shape_cfg["max2"]]

		if model_ap :
			return {"halfm": False, "backend": _trt_backend(vsmlrt, trt_ns,
				num_streams=gpu_t, int8=int8_qnt, fp16=True, output_format=1,
				workspace=None if ws_size < 128 else ws_size,
				use_cuda_graph=True, use_cublas=False, use_cudnn=False,
				static_shape=st_eng, min_shapes=[0, 0],
				opt_shapes=None, max_shapes=None,
				device_id=gpu, short_path=True)}
		else :
			return {"halfm": False, "backend": _trt_backend(vsmlrt, trt_ns,
				num_streams=gpu_t, int8=int8_qnt, fp16=True, output_format=1,
				workspace=None if ws_size < 128 else (ws_size if st_eng else ws_size * 2),
				use_cuda_graph=True, use_cublas=False, use_cudnn=False,
				static_shape=st_eng, min_shapes=[0, 0] if st_eng else min_shapes,
				opt_shapes=None if st_eng else opt_shapes,
				max_shapes=None if st_eng else (max_shapes1 if (size_in > 2048 * 1088) else max_shapes2),
				device_id=gpu, short_path=True)}

	return DRBA_HUB(
		input=input,
		model=model,
		turbo=turbo,
		fps_in=fps_in,
		fps_num=fps_num,
		fps_den=fps_den,
		sc_mode=sc_mode,
		gpu=gpu,
		gpu_t=gpu_t,
		backend_type="trt",
		backend_param=backend_param,
		func_name=func_name,
		vsmlrt=vsmlrt,
	)
