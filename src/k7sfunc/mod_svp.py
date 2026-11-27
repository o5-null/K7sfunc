"""补帧 - SVP

"""

import typing
import vapoursynth as vs
from ._internal import (
	core,
	_validate_input_clip,
	_validate_bool,
	_validate_literal,
	_validate_numeric,
	_check_plugin,
)
from .mod_helper import (
	FMT2YUV_SP,
)

##################################################
## SVP补帧
##################################################

def SVP_LQ(
	input : vs.VideoNode,
	fps_in : float = 23.976,
	fps_num : typing.Literal[2, 3, 4] = 2,
	cpu : typing.Literal[0, 1] = 0,
	gpu : typing.Literal[0, 11, 12, 21] = 0,
) -> vs.VideoNode :

	func_name = "SVP_LQ"
	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	_validate_literal(func_name, "fps_num", fps_num, [2, 3, 4])
	_validate_literal(func_name, "cpu", cpu, [0, 1])
	_validate_literal(func_name, "gpu", gpu, [0, 11, 12, 21])

	_check_plugin(func_name, "svp1")
	_check_plugin(func_name, "svp2")

	fps_num = fps_num
	acc = 1 if cpu == 0 else 0

	smooth_param = "{rate:{num:%d,den:1,abs:false},algo:21,gpuid:%d,mask:{area:100},scene:{mode:0,limits:{m1:1800,m2:3600,scene:5200,zero:100,blocks:45}}}" % (fps_num, gpu)
	super_param = "{pel:2,gpu:%d,scale:{up:2,down:4}}" % (acc)
	analyse_param = "{block:{w:32,h:16,overlap:2},main:{levels:4,search:{type:4,distance:-8,coarse:{type:2,distance:-5,bad:{range:0}}},penalty:{lambda:10.0,plevel:1.5,pzero:110,pnbour:65}},refine:[{thsad:200,search:{type:4,distance:2}}]}"

	clip, clip8 = FMT2YUV_SP(input)
	svps = core.svp1.Super(clip8, super_param)
	svpv = core.svp1.Analyse(svps["clip"], svps["data"], clip if acc else clip8, analyse_param)
	output = core.svp2.SmoothFps(clip if acc else clip8,
		svps["clip"], svps["data"], svpv["clip"], svpv["data"],
		smooth_param, src=clip if acc else clip8, fps=fps_in)

	return output

##################################################
## PORT https://github.com/natural-harmonia-gropius
## SVP补帧
##################################################

def SVP_HQ(
	input : vs.VideoNode,
	fps_in : float = 23.976,
	fps_dp : float = 59.940,
	cpu : typing.Literal[0, 1] = 0,
	gpu : typing.Literal[0, 11, 12, 21] = 0,
) -> vs.VideoNode :

	func_name = "SVP_HQ"
	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	_validate_numeric(func_name, "fps_dp", fps_dp, min_val=23.976)
	_validate_literal(func_name, "cpu", cpu, [0, 1])
	_validate_literal(func_name, "gpu", gpu, [0, 11, 12, 21])

	_check_plugin(func_name, "svp1")
	_check_plugin(func_name, "svp2")

	fps = fps_in or 23.976
	freq = fps_dp or 59.970
	acc = 1 if cpu == 0 else 0
	overlap = 2 if cpu == 0 else 3
	w, h = input.width, input.height

	if (freq - fps < 2) :
		raise Exception("Interpolation is not necessary.")

	target_fps = 60

	sp = "{gpu:%d}" % (acc)
	ap = "{block:{w:32,h:16,overlap:%d},main:{levels:5,search:{type:4,distance:-12,coarse:{type:4,distance:-1,trymany:true,bad:{range:0}}},penalty:{lambda:3.33,plevel:1.33,lsad:3300,pzero:110,pnbour:50}},refine:[{thsad:400},{thsad:200,search:{type:4,distance:-4}}]}" % (overlap)
	fp = "{gpuid:%d,algo:23,rate:{num:%d,den:%d,abs:true},mask:{cover:80,area:30,area_sharp:0.75},scene:{mode:0,limits:{scene:6000,zero:100,blocks:40}}}" % (gpu, round(min(max(target_fps, fps * 2, freq / 2), freq)) * 1000, 1001)

	def _svpflow(clip, fps, sp, ap, fp) :
		clip, clip8 = FMT2YUV_SP(clip)
		s = core.svp1.Super(clip8, sp)
		r = s["clip"], s["data"]
		v = core.svp1.Analyse(*r, clip, ap)
		r = *r, v["clip"], v["data"]
		clip = core.svp2.SmoothFps(clip if acc else clip8, *r, fp, src=clip, fps=fps)
		return clip

	output = _svpflow(input, fps, sp, ap, fp)

	return output

##################################################
## CREDIT https://github.com/BlackMickey
## SVP补帧
##################################################

def SVP_PRO(
	input : vs.VideoNode,
	fps_in : float = 23.976,
	fps_num : int = 2,
	fps_den : int = 1,
	abs : bool = False,
	cpu : typing.Literal[0, 1] = 0,
	nvof : bool = False,
	gpu : typing.Literal[0, 11, 12, 21] = 0,
) -> vs.VideoNode :

	func_name = "SVP_PRO"
	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	_validate_numeric(func_name, "fps_num", fps_num, min_val=2, int_only=True)
	if not (isinstance(fps_den, int) and fps_den >= 1 and fps_den < fps_num) :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_den 的值无效")
	_validate_bool(func_name, "abs", abs)
	if abs and (fps_num / fps_den <= fps_in) :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_num 或 fps_den 的值无效")
	_validate_literal(func_name, "cpu", cpu, [0, 1])
	_validate_bool(func_name, "nvof", nvof)
	if nvof and cpu :
		raise vs.Error(f"模块 {func_name} 的子参数 cpu 的值无效")
	_validate_literal(func_name, "gpu", gpu, [0, 11, 12, 21])

	_check_plugin(func_name, "svp1")
	_check_plugin(func_name, "svp2")

	w_in, h_in = input.width, input.height
	size_in = w_in * h_in
	multi = "true" if abs else "false"
	acc = 1 if cpu == 0 else 0
	ana_lv = 5 if size_in > 510272 else 2

	super_param = "{pel:1,gpu:%d,full:true,scale:{up:2,down:4}}" % (acc)
	analyse_param = "{vectors:3,block:{w:32,h:32,overlap:1},main:{levels:%d,search:{type:4,distance:3,sort:true,satd:false,coarse:{width:960,type:4,distance:4,satd:true,trymany:false,bad:{sad:1000,range:0}}},penalty:{lambda:3.0,plevel:1.3,lsad:8000,pnew:50,pglobal:50,pzero:100,pnbour:65,prev:0}},refine:[{thsad:800,search:{type:4,distance:1,satd:false},penalty:{pnew:50}}]}" % (ana_lv)
	smooth_param = "{rate:{num:%d,den:%d,abs:%s},algo:13,block:false,cubic:%d,gpuid:%d,linear:true,mask:{cover:40,area:16,area_sharp:0.7},scene:{mode:3,blend:false,limits:{m1:2400,m2:3601,scene:5002,zero:125,blocks:40},luma:1.5}}" % (fps_num, fps_den, multi, acc, gpu)
	smooth_nvof_param = smooth_param

	clip, clip8 = FMT2YUV_SP(input)
	if nvof :
		output  = core.svp2.SmoothFps_NVOF(clip, smooth_nvof_param, nvof_src=clip8, src=clip,fps=fps_in)
	else :
		super = core.svp1.Super(clip8, super_param)
		vectors = core.svp1.Analyse(super["clip"], super["data"], clip if acc else clip8, analyse_param)
		output = core.svp2.SmoothFps(clip if acc else clip8,
			super["clip"], super["data"], vectors["clip"], vectors["data"],
			smooth_param, src=clip if acc else clip8, fps=fps_in)

	return output
