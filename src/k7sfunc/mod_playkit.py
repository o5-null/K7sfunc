"""播放器预设模块（PlayKit）

本模块属于 o5-null/K7sfunc 这个 fork，不属于上游 hooke007/K7sfunc：
它把 mod_scale / mod_memc / mod_dbdn / mod_etc 里分散的算子组合成"开箱即用"
的预设，并保证任何一步失败都逐级降级，而不是让播放中断。

约定：
- 只调用本包已有的公开算子，不复制算子实现；
- 失败时打印一行警告（mpv 日志可见）后继续，不向播放器抛异常；
- 本文件只新增，不改动上游同名模块，便于与上游合并。
"""

import typing

import vapoursynth as vs

from fractions import Fraction

from ._internal import (
	core,
	_validate_input_clip,
	_validate_literal,
	_validate_numeric,
)

from .mod_scale import ACNET_STD, ARTCNN_NV, CUGAN_NV, EDI_US_STD, NGU_HQ
from .mod_memc import RIFE_AJI, RIFE_DML, RIFE_NV, TEMPORAL_FIX_AJI, UPSCALE_AJI
from .mod_dbdn import NLM_STD
from .mod_etc import DEBAND_STD

__all__ = [
	"PLAYKIT_RUN",
	"PLAYKIT_AUTO",
	"PLAYKIT_UPSCALE",
	"PLAYKIT_MEMC",
	"PLAYKIT_TEMPORAL_FIX",
	"PLAYKIT_DENOISE",
	"PLAYKIT_DEBAND",
	"PLAYKIT_CLEAN",
	"PLAYKIT_ANIME",
	"PLAYKIT_LIVE",
	"PLAYKIT_FIT",
	"PLAYKIT_PRESETS",
	"PLAYKIT_UPSCALE_ORDER",
	"PLAYKIT_MEMC_ORDER",
]

##################################################
## 预设名与引擎降级顺序
##################################################

PLAYKIT_PRESETS = (
	"auto",
	"upscale",
	"memc",
	"denoise",
	"deband",
	"clean",
	"anime",
	"live",
)

# 从左到右尝试，第一项可用即停止；none 表示直接缩放
PLAYKIT_UPSCALE_ORDER = ("artcnn", "acnet", "cugan", "edi", "ngu", "none")
# aji 只支持整数倍率，倍率不整除时它自己会报错，降级链会继续往下走
PLAYKIT_MEMC_ORDER = ("rife_nv", "aji", "rife_dml", "none")

##################################################
## 日志
##################################################

def _playkit_log(
	message : str,
) -> None :

	print(f"[playkit] {message}", flush=True)

def _playkit_warn(
	message : str,
) -> None :

	print(f"[playkit] 警告: {message}", flush=True)

##################################################
## 基础缩放
##################################################

def PLAYKIT_FIT(
	input : vs.VideoNode,
	target_h : int,
) -> vs.VideoNode :
	"""等比缩放到目标高度，高度相同（或 target_h<=0）时原样返回。"""

	func_name = "PLAYKIT_FIT"

	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "target_h", target_h, min_val=0, int_only=True)

	if target_h <= 0 or input.height == target_h :
		return input

	return core.resize.Lanczos(clip=input, height=target_h)

##################################################
## 超分单引擎
##################################################

def _playkit_upscale_engine(
	input : vs.VideoNode,
	engine : str,
	gpu : int,
	gpu_m : int,
	gpu_t : int,
) -> vs.VideoNode :

	lt_hd = input.height <= 720

	if engine == "acnet" :
		return ACNET_STD(input, model=1, turbo=True, gpu=gpu, gpu_m=gpu_m)
	if engine == "artcnn" :
		return ARTCNN_NV(input, lt_hd=lt_hd, model=8, gpu=gpu, gpu_t=gpu_t)
	if engine == "cugan" :
		return CUGAN_NV(input, lt_hd=lt_hd, nr_lv=-1, gpu=gpu, gpu_t=gpu_t)
	if engine == "edi" :
		return EDI_US_STD(input, cpu=True)
	if engine == "ngu" :
		return NGU_HQ(input)

	raise ValueError(f"未知超分引擎 {engine}")

##################################################
## 插帧倍率
##################################################

def _playkit_fps_factor(
	fps_in : float,
	fps_out : float,
) -> tuple[int, int] :
	"""把目标帧率化成整数比，避免把 2.5025 这类值直接丢给 RIFE。"""

	if fps_in <= 0 or fps_out <= fps_in :
		return 2, 1

	ratio = Fraction(round(fps_out / fps_in, 2)).limit_denominator(100)

	return max(1, ratio.numerator), max(1, ratio.denominator)

def _playkit_memc_engine(
	input : vs.VideoNode,
	engine : str,
	fps_in : float,
	fps_out : float,
	gpu : int,
	gpu_t : int,
) -> vs.VideoNode :

	fps_num, fps_den = _playkit_fps_factor(fps_in, fps_out)

	if engine == "rife_nv" :
		return RIFE_NV(input, model=46, fps_in=fps_in, fps_num=fps_num, fps_den=fps_den,
						gpu=gpu, gpu_t=gpu_t)
	if engine == "aji" :
		## aji 的 factor 只有整数：fps_num/fps_den 必须整除，否则直接降级给下一个引擎
		if fps_num % fps_den != 0 :
			raise ValueError(f"aji 只支持整数倍率，{fps_in:.3f} -> {fps_out:.3f} fps 是 {fps_num}/{fps_den}")
		## 模型跟随 vsaji 发行包（component-rife 3.12.5 里的 4.14），只支持整数倍
		return RIFE_AJI(input, model="4.14", fps_in=fps_in,
						fps_num=fps_num // fps_den, fps_den=1, gpu=gpu)
	if engine == "rife_dml" :
		return RIFE_DML(input, model=46, fps_in=fps_in, fps_num=fps_num, fps_den=fps_den,
						gpu=gpu, gpu_t=gpu_t)

	raise ValueError(f"未知插帧引擎 {engine}")

##################################################
## 超分 预设
##################################################

def PLAYKIT_UPSCALE(
	input : vs.VideoNode,
	target_h : int = 1440,
	engine : str = "auto",
	gpu : int = 0,
	gpu_m : int = 1,
	gpu_t : int = 2,
) -> vs.VideoNode :
	"""超分到 target_h；engine="auto" 时按 PLAYKIT_UPSCALE_ORDER 降级。"""

	func_name = "PLAYKIT_UPSCALE"

	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "target_h", target_h, min_val=0, int_only=True)
	_validate_literal(func_name, "engine", engine, ["auto"] + list(PLAYKIT_UPSCALE_ORDER))

	if target_h <= 0 or input.height >= target_h :
		return input

	order = PLAYKIT_UPSCALE_ORDER if engine == "auto" else (engine,)
	for name in order :
		if name == "none" :
			break
		try :
			clip = _playkit_upscale_engine(input, name, gpu, gpu_m, gpu_t)
		except Exception as exc :
			_playkit_warn(f"超分引擎 {name} 不可用: {exc}")
			continue
		_playkit_log(f"超分引擎 {name} 生效: {input.height} -> {clip.height}")
		return PLAYKIT_FIT(clip, target_h)

	return PLAYKIT_FIT(input, target_h)

##################################################
## 插帧 预设
##################################################

def PLAYKIT_MEMC(
	input : vs.VideoNode,
	fps_out : float = 60.0,
	engine : str = "auto",
	fps_in : typing.Optional[float] = None,
	gpu : int = 0,
	gpu_t : int = 2,
	temporal_fix : str = "off",
) -> vs.VideoNode :
	"""插帧到 fps_out；engine="auto" 时按 PLAYKIT_MEMC_ORDER 降级。

	temporal_fix 不是 off 时，在插帧结果上再叠一层 AnimeJaNai 时序修复（s1/s2/s3）。
	"""

	func_name = "PLAYKIT_MEMC"

	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "fps_out", fps_out, min_val=0)
	_validate_literal(func_name, "engine", engine, ["auto"] + list(PLAYKIT_MEMC_ORDER))
	_validate_literal(func_name, "temporal_fix", temporal_fix, ["off", "s1", "s2", "s3"])

	fps_in = float(input.fps if fps_in is None else fps_in)

	if fps_out <= 0 or fps_in <= 0 or fps_in >= fps_out :
		return input

	def _finish(clip : vs.VideoNode) -> vs.VideoNode :
		if temporal_fix == "off" :
			return clip
		return PLAYKIT_TEMPORAL_FIX(clip, model=temporal_fix, gpu=gpu)

	order = PLAYKIT_MEMC_ORDER if engine == "auto" else (engine,)
	for name in order :
		if name == "none" :
			break
		try :
			clip = _playkit_memc_engine(input, name, fps_in, fps_out, gpu, gpu_t)
		except Exception as exc :
			_playkit_warn(f"插帧引擎 {name} 不可用: {exc}")
			continue
		_playkit_log(f"插帧引擎 {name} 生效: {fps_in:.3f} -> {clip.fps:.3f} fps")
		return _finish(clip)

	return input

def PLAYKIT_TEMPORAL_FIX(
	input : vs.VideoNode,
	model : str = "s1",
	scene_threshold : float = 0.150,
	gpu : int = 0,
) -> vs.VideoNode :
	"""AnimeJaNai 时序修复（temporalfix），1 帧进 1 帧出。

	可以单独叠加在任意滤镜之后；插件/模型不可用时原样返回。
	"""

	func_name = "PLAYKIT_TEMPORAL_FIX"

	_validate_input_clip(func_name, input)
	_validate_literal(func_name, "model", model, ["s1", "s2", "s3"])
	_validate_numeric(func_name, "scene_threshold", scene_threshold, min_val=0.0, max_val=1.0)

	try :
		return TEMPORAL_FIX_AJI(input, model=model, scene_threshold=scene_threshold, gpu=gpu)
	except Exception as exc :
		_playkit_warn(f"时序修复不可用: {exc}")
		return input

##################################################
## 其它 预设
##################################################

def PLAYKIT_DENOISE(
	input : vs.VideoNode,
	strength : float = 3.0,
	gpu : int = 0,
) -> vs.VideoNode :
	"""非局部均值降噪（NLM_STD）。"""

	func_name = "PLAYKIT_DENOISE"

	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "strength", strength, min_val=0)

	try :
		return NLM_STD(input, nr_lv=float(strength), gpu=gpu)
	except Exception as exc :
		_playkit_warn(f"降噪不可用: {exc}")
		return input

def PLAYKIT_DEBAND(
	input : vs.VideoNode,
	threshold : float = 48.0,
) -> vs.VideoNode :
	"""去色带（DEBAND_STD），threshold 越高越激进。"""

	func_name = "PLAYKIT_DEBAND"

	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "threshold", threshold, min_val=0)

	try :
		return DEBAND_STD(input, bdy_rth=float(threshold))
	except Exception as exc :
		_playkit_warn(f"去色带不可用: {exc}")
		return input

def PLAYKIT_CLEAN(
	input : vs.VideoNode,
	strength : float = 3.0,
	threshold : float = 48.0,
	gpu : int = 0,
) -> vs.VideoNode :
	"""去色带 + 降噪，适合压缩痕迹明显的片源。"""

	clip = PLAYKIT_DEBAND(input, threshold=threshold)

	return PLAYKIT_DENOISE(clip, strength=strength, gpu=gpu)

def PLAYKIT_ANIME(
	input : vs.VideoNode,
	target_h : int = 1440,
	engine : str = "auto",
	threshold : float = 48.0,
	gpu : int = 0,
	gpu_m : int = 1,
	gpu_t : int = 2,
) -> vs.VideoNode :
	"""动画：先超分再去色带（去色带放在成品上更明显）。"""

	clip = PLAYKIT_UPSCALE(input, target_h=target_h, engine=engine,
							gpu=gpu, gpu_m=gpu_m, gpu_t=gpu_t)

	return PLAYKIT_DEBAND(clip, threshold=threshold)

def PLAYKIT_LIVE(
	input : vs.VideoNode,
	strength : float = 3.0,
	fps_out : float = 60.0,
	memc_engine : str = "auto",
	fps_in : typing.Optional[float] = None,
	gpu : int = 0,
	gpu_t : int = 2,
) -> vs.VideoNode :
	"""实拍：轻度降噪 + 插帧。"""

	clip = PLAYKIT_DENOISE(input, strength=strength, gpu=gpu)

	return PLAYKIT_MEMC(clip, fps_out=fps_out, engine=memc_engine,
						fps_in=fps_in, gpu=gpu, gpu_t=gpu_t)

def PLAYKIT_AUTO(
	input : vs.VideoNode,
	target_h : int = 0,
	engine : str = "auto",
	gpu : int = 0,
	gpu_m : int = 1,
	gpu_t : int = 2,
) -> vs.VideoNode :
	"""默认预设：只在低于目标高度时超分，符合"打开就能看"的预期。"""

	if target_h <= 0 or input.height >= target_h :
		return input

	return PLAYKIT_UPSCALE(input, target_h=target_h, engine=engine,
							gpu=gpu, gpu_m=gpu_m, gpu_t=gpu_t)

##################################################
## 唯一入口
##################################################

def PLAYKIT_RUN(
	input : vs.VideoNode,
	preset : str = "auto",
	target_h : int = 0,
	engine : str = "auto",
	memc_engine : str = "auto",
	memc_fps : float = 60.0,
	fps_in : typing.Optional[float] = None,
	denoise_lv : float = 3.0,
	deband_rth : float = 48.0,
	gpu : int = 0,
	gpu_m : int = 1,
	gpu_t : int = 2,
) -> vs.VideoNode :
	"""vpy 侧唯一入口：k7f.PLAYKIT_RUN(video_in, preset="anime", target_h=1440)

	preset 取值见 PLAYKIT_PRESETS；任何一步失败都降级而不是中断播放。
	"""

	func_name = "PLAYKIT_RUN"

	_validate_input_clip(func_name, input)
	_validate_literal(func_name, "preset", preset, list(PLAYKIT_PRESETS))
	_validate_numeric(func_name, "target_h", target_h, min_val=0, int_only=True)
	_validate_numeric(func_name, "memc_fps", memc_fps, min_val=0)
	_validate_numeric(func_name, "denoise_lv", denoise_lv, min_val=0)
	_validate_numeric(func_name, "deband_rth", deband_rth, min_val=0)

	preset = preset.lower()

	_playkit_log(f"预设 {preset} 开始，输入 {input.width}x{input.height} @ {input.fps:.3f}fps")

	if preset == "auto" :
		output = PLAYKIT_AUTO(input, target_h=target_h, engine=engine,
								gpu=gpu, gpu_m=gpu_m, gpu_t=gpu_t)
	elif preset == "upscale" :
		output = PLAYKIT_UPSCALE(input, target_h=target_h or 1440, engine=engine,
								gpu=gpu, gpu_m=gpu_m, gpu_t=gpu_t)
	elif preset == "memc" :
		output = PLAYKIT_MEMC(input, fps_out=memc_fps, engine=memc_engine,
								fps_in=fps_in, gpu=gpu, gpu_t=gpu_t)
	elif preset == "denoise" :
		output = PLAYKIT_DENOISE(input, strength=denoise_lv, gpu=gpu)
	elif preset == "deband" :
		output = PLAYKIT_DEBAND(input, threshold=deband_rth)
	elif preset == "clean" :
		output = PLAYKIT_CLEAN(input, strength=denoise_lv, threshold=deband_rth, gpu=gpu)
	elif preset == "anime" :
		output = PLAYKIT_ANIME(input, target_h=target_h or 1440, engine=engine,
								threshold=deband_rth, gpu=gpu, gpu_m=gpu_m, gpu_t=gpu_t)
	else :  # live
		output = PLAYKIT_LIVE(input, strength=denoise_lv, fps_out=memc_fps,
								memc_engine=memc_engine, fps_in=fps_in, gpu=gpu, gpu_t=gpu_t)

	_playkit_log(f"预设 {preset} 完成，输出 {output.width}x{output.height} @ {output.fps:.3f}fps")

	return output
