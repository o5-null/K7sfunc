"""mod_vpy —— vpy 脚本宿主层（本 fork 专属）

把 mpv 端 vpy 脚本里重复的东西全收进包里，脚本只留用户选项和一次调用：

- 读 user_data（OSD 下行参数）与环境变量覆盖，带类型转换和钳制
- 定位并加载 vs-framegen 插件（vs-plugins 已自动加载时直接复用）
- 帧生成的完整主流程：预缩、YUV 直通、帧率兜底
- 写上行报告 `<脚本名>.k7f.json`，mpv 侧 `k7f_bridge.lua` 轮询显示到 OSD

约定（与 k7f_vpypar.lua 的参数表一致，改键名要两边一起改）：
    优先级  字面量 < 环境变量 `<NS>_<KEY>` < user_data
    键名    multi / scene / camera / preset / pre_h / gpu / log

本模块不抛"参数非法"以外的异常：上行报告写失败只影响 OSD，不能打断播放。
"""

import json
import os
import time
import typing

import vapoursynth as vs

from ._internal import core
from . import _vpy_debug
from .mod_memc import FRAMEGEN_ENGINES

__all__ = [
	"VPY_PARSE_USER_DATA",
	"VPY_AS_BOOL",
	"VPY_GET",
	"VPY_SIDECAR_PATH",
	"VPY_CLIP_INFO",
	"VPY_PUBLISH",
	"VPY_LOAD_PLUGIN",
	"FRAMEGEN_VPY",
]

# 报告格式版本：字段只增不改，mpv 侧按需取
SCHEMA_VERSION = 1

# 报告文件后缀：与 vpy 同目录、同名，例如 MEMC_DLSSG_NV.vpy.k7f.json
SIDECAR_SUFFIX = ".k7f.json"

# 每个引擎在 vpy 里暴露哪些参数通道（决定 env / user_data 认哪些键、报告里有哪些字段）
FRAMEGEN_VPY_OPTIONS = {
	"vdlssg" : ("multi", "scene", "preset", "pre_h", "gpu", "log"),
	"vxessfg" : ("multi", "scene", "pre_h", "gpu", "log"),
	"vvfg" : ("multi", "scene", "pre_h", "gpu", "log"),
	"vfsr4" : ("scene", "camera", "pre_h", "gpu", "log"),
}

## 插件构造函数额外接受的参数（vdlssg 的模型档、vfsr4 的相机运动）
_FRAMEGEN_PLUGIN_ARGS = {
	"vdlssg" : ("preset",),
	"vxessfg" : (),
	"vvfg" : (),
	"vfsr4" : ("camera",),
}

## 找不到 DLL 时的报错前缀，与旧脚本一致（用户照着报错找脚本）
_FRAMEGEN_SCRIPT_LABELS = {
	"vdlssg" : "MEMC_DLSSG",
	"vxessfg" : "MEMC_XESSFG",
	"vvfg" : "MEMC_VFG",
	"vfsr4" : "MEMC_FSR4",
}

## OSD 上行报告的前缀（沿用旧脚本文案，mpv 侧直接显示）
_FRAMEGEN_TITLES = {
	"vdlssg" : "DLSSG",
	"vxessfg" : "XeSS-FG",
	"vvfg" : "VFG",
	"vfsr4" : "FSR4",
}

## 各键的取值规则：cast(int/float/bool/str/"gpu") + 上下限（multi 的界由引擎决定）
_FRAMEGEN_VALUE_SPECS = {
	"multi" : (int, None, None),
	"scene" : (float, 0.0, 1.0),
	"camera" : (int, 0, None),
	"pre_h" : (int, 0, None),
	"log" : (bool, None, None),
	"preset" : (str, None, None),
	"gpu" : ("gpu", 0, None),
}

## 取值解析失败的哨兵：与合法的 None（gpu=auto）区分开
_INVALID = object()

_warned = set()


def _warn(message : str) -> None :
	"""同一个错误只提示一次，避免每次 seek 重跑脚本时刷屏。"""

	if message in _warned :
		return
	_warned.add(message)
	print(f"[vpy] {message}", flush=True)


##################################################
## 下行：# 读 user_data / 环境变量
##################################################

def VPY_PARSE_USER_DATA(text) -> dict :
	"""把 user-data 字符串解析成小写键的字典。

	兼容 mpv 的两种分隔符：`;` 与 `,`；键和值两端的空白会被去掉，
	但值内部不能有空白、逗号、分号、冒号——真需要时请改用环境变量。
	"""

	opts = {}
	if not text :
		return opts
	for part in str(text).replace(",", ";").split(";") :
		key, sep, val = part.partition("=")
		key = key.strip().lower()
		if sep and key :
			opts[key] = val.strip()
	return opts

def VPY_AS_BOOL(value) -> bool :
	"""OSD/环境变量里的布尔写法统一：0/false/off/no/空 为假，其余为真。"""

	return str(value).strip().lower() not in ("0", "false", "off", "no", "")

def VPY_GET(ud : dict, key : str, default, cast = None, lo = None, hi = None) :
	"""从 user-data 取一个值：带类型转换与上下限钳制，非法值保持默认。

	示例：
		Multi = VPY_GET(_ud, "multi", Multi, int, 2, 6)
		Scene = VPY_GET(_ud, "scene", Scene, float, 0.0, 1.0)
		Log   = VPY_GET(_ud, "log", Log, VPY_AS_BOOL)
	"""

	if not ud or key not in ud :
		return default

	value = _coerce(key, ud[key], cast)
	if value is _INVALID :
		return default

	if lo is not None and value < lo :
		value = lo
	if hi is not None and value > hi :
		value = hi

	return value

def _coerce(key : str, raw, cast) :
	"""把字符串转成目标类型；解析失败提示一次并返回哨兵 `_INVALID`。"""

	try :
		if cast is bool :
			return VPY_AS_BOOL(raw)
		if cast is int :
			# 允许 "4" 和 "4.0"：OSD 菜单里两种写法都出现过
			return int(float(raw))
		if cast is float :
			return float(raw)
		if cast is str :
			return str(raw).strip().lower()
		if cast == "gpu" :
			# None / auto / 空 = 跟随默认卡；否则是显卡序号
			text = str(raw).strip().lower()
			return None if text in ("none", "auto", "") else int(float(text))
		return cast(raw)
	except (TypeError, ValueError) :
		_warn(f"忽略非法取值 {key}={raw!r}")
		return _INVALID

def _resolve_framegen_values(engine : str, user_data, literal : dict) -> dict :
	"""按 字面量 → 环境变量 → user_data 的优先级，得到一套钳制过的实际参数。"""

	spec = FRAMEGEN_ENGINES[engine]
	multi_lo, multi_hi = min(spec["multi"]), max(spec["multi"])

	# 引擎没暴露的键（如 vfsr4 的 multi）不读任何通道，保持字面量
	values = dict(literal)
	ud = VPY_PARSE_USER_DATA(user_data)

	for key in FRAMEGEN_VPY_OPTIONS[engine] :
		cast, lo, hi = _FRAMEGEN_VALUE_SPECS[key]
		if key == "multi" :
			lo, hi = multi_lo, multi_hi

		# 环境变量：调试用，正常播放不用管；log 只要非空就打开（与旧脚本一致）
		raw_env = os.environ.get(f"{engine.upper()}_{key.upper()}")
		if raw_env :
			if key == "log" :
				values[key] = True
			else :
				value = _coerce(key, raw_env, cast)
				if value is not _INVALID :
					values[key] = value

		# OSD 是用户当场的选择，优先级最高
		if key in ud :
			value = _coerce(key, ud[key], cast)
			if value is not _INVALID :
				values[key] = value

		# preset 只挡住空值；非法档位交给插件报错（错误文案只维护一份）
		if key == "preset" and not values[key] :
			values[key] = literal[key]

	# 统一再钳一次：字面量写超界也不让插件在求值阶段报错
	for key, value in values.items() :
		if value is None or key == "preset" :
			continue
		cast, lo, hi = _FRAMEGEN_VALUE_SPECS[key]
		if key == "multi" :
			lo, hi = multi_lo, multi_hi
		if lo is not None and value < lo :
			values[key] = lo
		if hi is not None and value > hi :
			values[key] = hi

	# DLSS-G 的模型档位：没指定就用插件的默认档（与旧脚本 Preset="a" 一致），
	# 免得摘要里出现 "预设 None"
	if engine == "vdlssg" and not values.get("preset") :
		values["preset"] = "a"

	return values

##################################################
## 上行：# 写 sidecar 报告（mpv 侧 k7f_bridge.lua 读）
##################################################

def VPY_SIDECAR_PATH(script_file) -> str :
	"""报告文件的绝对路径：与脚本同目录，避免传绝对路径时被 vf 的 `:` 截断。"""

	return os.path.abspath(os.fspath(script_file)) + SIDECAR_SUFFIX

def VPY_CLIP_INFO(clip, fps = None) :
	"""从 VS clip 取宽高与帧率；拿不到就返回 None（不抛异常）。

	video_in 的 clip.fps 往往是 0（容器帧率只在 mpv 的 container_fps 里），
	所以调用方可以把 fps_in 显式传进来。
	"""

	try :
		info = {"w" : int(clip.width), "h" : int(clip.height)}
	except Exception :
		return None

	if fps is None :
		try :
			fps = clip.fps
		except Exception :
			fps = None

	try :
		if fps and float(fps) > 0 :
			info["fps"] = round(float(fps), 3)
	except Exception :
		pass

	return info

def VPY_PUBLISH(
	script_file,
	*,
	title = None,
	engine = None,
	params = None,
	summary = None,
	in_info = None,
	out_info = None,
	ok = True,
	note = None,
	**extra,
) -> bool :
	"""把实际生效的参数写到 `<script_file>.k7f.json`（原子替换）。

	参数：
		title    OSD 前缀，如 "DLSSG"
		engine   实际使用的引擎，如 "vdlssg"
		params   实际生效的参数字典（JSON 可序列化）
		summary  一行摘要；省略时用 `k=v · k=v` 自动拼
		in_info/out_info  `VPY_CLIP_INFO()` 的结果
		ok       初始化是否成功；失败时 OSD 会带 ⚠
		note     额外提示，如 "已预缩到 1080p"
		extra    其它要一起带给 mpv 的字段
	"""

	payload = {
		"v" : SCHEMA_VERSION,
		"ts" : round(time.time(), 3),
		"pid" : os.getpid(),
		"script" : os.path.basename(os.fspath(script_file)),
		"ok" : bool(ok),
	}

	if title :
		payload["title"] = str(title)
	if engine :
		payload["engine"] = str(engine)
	if params :
		payload["params"] = params
	if summary :
		payload["summary"] = str(summary)
	elif params :
		payload["summary"] = " · ".join(f"{k}={v}" for k, v in params.items())
	if in_info :
		payload["input"] = in_info
	if out_info :
		payload["output"] = out_info
	if note :
		payload["note"] = str(note)
	payload.update(extra)

	try :
		text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
	except Exception as exc :
		_warn(f"报告序列化失败：{exc}")
		return False

	return _write_atomic(VPY_SIDECAR_PATH(script_file), text)

def _write_atomic(path : str, text : str) -> bool :
	"""先写临时文件再 os.replace；mpv 恰好在读时重试几次。

	Windows 上读取方没开 FILE_SHARE_DELETE 时 replace 会失败，所以最多退 3 次，
	再不行就直接覆盖写：报告延迟/错一帧都能接受，丢报告才是问题。
	"""

	tmp = f"{path}.{os.getpid()}.tmp"

	try :
		with open(tmp, "w", encoding="utf-8", newline="\n") as fh :
			fh.write(text)
	except Exception as exc :
		_warn(f"报告临时文件写入失败：{exc}")
		return False

	for _ in range(3) :
		try :
			os.replace(tmp, path)
			return True
		except OSError :
			time.sleep(0.01)

	try :
		os.remove(tmp)
	except OSError :
		pass

	try :
		with open(path, "w", encoding="utf-8", newline="\n") as fh :
			fh.write(text)
		return True
	except Exception as exc :
		_warn(f"报告写入失败：{exc}")
		return False

##################################################
## 插件定位：# 包里没自动加载时，按候选路径 LoadPlugin
##################################################

def _pack_root() -> str :
	"""播放器包根：k7sfunc 装在 <包根>\\Lib\\site-packages\\k7sfunc 下。"""

	return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir))

def VPY_LOAD_PLUGIN(plugin_ns : str, script_file, label : str = None) -> None :
	"""确保 `core.<plugin_ns>` 可用；加载不到就抛 vs.Error。

	候选顺序（先包内、再脚本旁、最后开发树）：
		1. <包根>/vs-plugins/<ns>.dll          mpv-playkit 发行包（不进 plugins 自动加载）
		2. <脚本>/../../vs-plugins/<ns>.dll     vpy 在 portable_config/vs 下
		3. <脚本>/../vs-plugins/<ns>.dll        mpv-lazy 旧布局
		4. <脚本>/<ns>.dll                      脚本与 DLL 放一起
		5. <脚本>/../../vs-dlssg/bin/<ns>.dll   开发树：vs-dlssg/mpv/
		6. 更上一层再试 vs-dlssg/bin            测试环境 放在仓库里时
	"""

	if hasattr(core, plugin_ns) :
		return

	here = os.path.dirname(os.path.abspath(os.fspath(script_file)))
	dll = plugin_ns + ".dll"

	for cand in (
		os.path.join(_pack_root(), "vs-plugins", dll),
		os.path.join(here, os.pardir, os.pardir, "vs-plugins", dll),
		os.path.join(here, os.pardir, "vs-plugins", dll),
		os.path.join(here, dll),
		os.path.join(here, os.pardir, os.pardir, "vs-dlssg", "bin", dll),
		os.path.join(here, os.pardir, os.pardir, os.pardir, os.pardir, "vs-dlssg", "bin", dll),
	) :
		if os.path.isfile(cand) :
			core.std.LoadPlugin(os.path.abspath(cand))
			break

	if not hasattr(core, plugin_ns) :
		raise vs.Error(f"{label or 'MEMC'}: 找不到 {dll}")

##################################################
## 帧生成 vpy 宿主
##################################################

def FRAMEGEN_VPY(
	input : vs.VideoNode,
	script_file,
	*,
	engine : str,
	fps_in,
	user_data = None,
	multi : int = 2,
	scene : float = 0.25,
	camera : int = 0,
	preset : typing.Optional[str] = None,
	pre_h : int = 0,
	gpu : typing.Optional[int] = None,
	log : bool = False,
) -> vs.VideoNode :
	"""vs-framegen 系列的 vpy 主流程：参数解析 + 插件定位 + 补帧 + 上行报告。

	vpy 里只需要写用户选项，然后：

		out = k7f.FRAMEGEN_VPY(
			video_in, __file__, engine="vdlssg", fps_in=container_fps,
			user_data=globals().get("user_data"),
			multi=Multi, scene=Scene, preset=Preset, pre_h=Pre_H, gpu=Gpu, log=Log,
		)

	与 `DLSSG_NV` 等模块算子的区别：这里面向 mpv，YUV 源直接交给插件
	（矩阵/范围/色度位置从帧属性读），只有 RGB 源才转回 mpv 能收的 10bit YUV。
	"""

	if engine not in FRAMEGEN_VPY_OPTIONS :
		raise vs.Error(f"FRAMEGEN_VPY: 不认识的引擎 {engine!r}")

	values = _resolve_framegen_values(engine, user_data, {
		"multi" : multi, "scene" : scene, "camera" : camera,
		"preset" : preset, "pre_h" : pre_h, "gpu" : gpu, "log" : bool(log),
	})

	# 调试埋点：只有 log 打开时才建计时器/套探针，正常播放这两行什么都不做
	stages = _vpy_debug.Stages() if values["log"] else None
	if stages :
		stages.mark("resolve")

	# 容器帧率缺失时退回 clip.fps；两者都没有才会走 AssumeFPS 的 0 帧率
	fps = float(fps_in) if fps_in and float(fps_in) > 1.0 else input.fps

	clip = input
	if values["pre_h"] and clip.height > values["pre_h"] :
		_width = int(round(clip.width * values["pre_h"] / clip.height / 2.0)) * 2
		clip = core.resize.Bicubic(clip, width=_width, height=values["pre_h"])
	if stages :
		stages.mark("resize")

	VPY_LOAD_PLUGIN(engine, script_file, _FRAMEGEN_SCRIPT_LABELS[engine])
	if stages :
		stages.mark("load")

	## 插件直接吃平面 YUV，省掉进出两趟全分辨率转换；RGB 只把 8/16bit 提到 RGBS
	yuv_in = clip.format.color_family != vs.RGB
	if yuv_in :
		src = clip
	elif clip.format.sample_type == vs.FLOAT and clip.format.bits_per_sample == 32 :
		src = clip
	else :
		src = core.resize.Bilinear(clip, format=vs.RGBS)
	if stages :
		stages.mark("prep")

	args = {"multi" : values["multi"], "scene_threshold" : values["scene"],
			"log" : 1 if values["log"] else 0}
	for key in _FRAMEGEN_PLUGIN_ARGS[engine] :
		if key == "camera" or values[key] :
			args[key] = values[key]
	if values["gpu"] is not None :
		args["gpu"] = values["gpu"]

	# 建节点包含引擎/代理初始化与首帧预热，是构图耗时的大头
	fg = getattr(core, engine).FrameGen(src, **args)
	if stages :
		stages.mark("framegen")

	## 输出格式与输入相同：YUV 源原样交给 mpv；只有 RGB 源才转回 10bit YUV
	out = fg if yuv_in else core.resize.Bilinear(fg, format=vs.YUV420P10, matrix_s="709")

	## 插件已把输出帧率乘以 multi，并逐帧写好 _Duration/_AbsoluteTime；只有源没有帧率时才兜底
	if not out.fps_num or not out.fps_den :
		out = core.std.AssumeFPS(
			out,
			fpsnum=int(round(fps * values["multi"] * 1e6)),
			fpsden=1000000,
		)
	if stages :
		stages.mark("fps")

	_publish_framegen(script_file, engine, values, fps, input, out)
	if stages :
		stages.mark("publish")

	if stages :
		_vpy_debug.dbg(_debug_config(engine, values, fps, input, out))
		# 逐帧探针：请求/就绪时间、单帧延迟、在飞帧数、线程号（见 _vpy_debug）
		out = _vpy_debug.attach_frame_probe(core, out, tag = engine)
		stages.mark("probe")
		_vpy_debug.dbg(f"stage {stages.report()}")

	return out


def _debug_config(engine : str, values : dict, fps, video_in, out) -> str :
	"""调试模式下的一行构图摘要：实际参数 + 进出尺寸/帧率，方便和跑分表对齐。"""

	params = " ".join(
		f"{key}={values[key]}" for key in FRAMEGEN_VPY_OPTIONS[engine] if key != "log"
	)
	in_info = VPY_CLIP_INFO(video_in, fps = fps) or {}
	out_info = VPY_CLIP_INFO(out) or {}
	return (f"config {params} in={in_info.get('w')}x{in_info.get('h')}"
		f"@{in_info.get('fps')} out={out_info.get('w')}x{out_info.get('h')}"
		f"@{out_info.get('fps')}")

def _publish_framegen(script_file, engine : str, values : dict, fps, video_in, out) -> None :
	"""按引擎拼 OSD 摘要与参数字段，写 sidecar；失败不影响播放。"""

	keys = FRAMEGEN_VPY_OPTIONS[engine]
	# FSR4 固定 2×，摘要里写死（与旧脚本文案一致）
	label = "2×（固定）" if engine == "vfsr4" else "%d×" % values["multi"]

	if "preset" in keys :
		summary = "%s · 预设 %s · 切镜 %g" % (label, values["preset"], values["scene"])
	else :
		summary = "%s · 切镜 %g" % (label, values["scene"])
	if "camera" in keys and values["camera"] :
		summary += " · 相机 %d" % values["camera"]
	if values["pre_h"] :
		summary += " · 预缩 %dp" % values["pre_h"]

	params = {"multi" : values["multi"], "scene" : values["scene"]}
	if "preset" in keys :
		params["preset"] = values["preset"]
	if "camera" in keys :
		params["camera"] = values["camera"]
	params["gpu"] = "auto" if values["gpu"] is None else values["gpu"]
	params["pre_h"] = values["pre_h"]
	params["log"] = values["log"]

	VPY_PUBLISH(
		script_file,
		title=_FRAMEGEN_TITLES[engine],
		engine=engine,
		params=params,
		summary=summary,
		in_info=VPY_CLIP_INFO(video_in, fps=fps),
		out_info=VPY_CLIP_INFO(out),
	)
