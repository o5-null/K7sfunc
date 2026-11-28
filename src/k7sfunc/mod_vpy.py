"""mod_vpy —— vpy 脚本宿主层（本 fork 专属）

把 mpv 端 vpy 脚本里重复的东西全收进包里，脚本只留用户选项和一次调用：

- 读 user_data（OSD 下行参数）与环境变量覆盖，带类型转换和钳制
- 定位并加载 vs-framegen 插件（vs-plugins 已自动加载时直接复用）
- 帧生成的完整主流程：预缩、YUV 直通、帧率兜底
- 写上行报告 `<脚本名>.k7f.json`，mpv 侧 `k7f_bridge.lua` 轮询显示到 OSD

约定（与 k7f_vpypar.lua 的参数表一致，改键名要两边一起改）：
    优先级  字面量 < 环境变量 `<NS>_<KEY>` < user_data
    键名    multi / scene / camera / preset / pre_h / gpu / log / stats

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
	"VPY_STATS_PATH",
	"VPY_CLIP_INFO",
	"VPY_PLUGIN_INFO",
	"VPY_PUBLISH",
	"VPY_LOAD_PLUGIN",
	"FRAMEGEN_VPY",
	"AJI_VPY",
	"AJI_VPY_OPTIONS",
	"AJI_STACK_PUBLISH",
]

# 报告格式版本：字段只增不改，mpv 侧按需取
SCHEMA_VERSION = 1

# 报告文件后缀：与 vpy 同目录、同名，例如 MEMC_DLSSG_NV.vpy.k7f.json
SIDECAR_SUFFIX = ".k7f.json"

# 耗时统计文件后缀（OSD 常驻行用）：只在 stats 打开时每秒刷新一次
STATS_SUFFIX = ".k7f-stats.json"

# 耗时文件的刷新间隔（秒）：OSD 上 0.5s 一跳已经足够跟手，又不至于频繁落盘
STATS_INTERVAL = 0.5

# 每个引擎在 vpy 里暴露哪些参数通道（决定 env / user_data 认哪些键、报告里有哪些字段）
FRAMEGEN_VPY_OPTIONS = {
	"vdlssg" : ("multi", "scene", "duplicate", "preset", "pre_h", "gpu", "log", "stats"),
	"vxessfg" : ("multi", "scene", "duplicate", "pre_h", "gpu", "log", "stats"),
	"vvfg" : ("multi", "mode", "scene", "duplicate", "pre_h", "gpu", "log", "stats"),
	"vfsr4" : ("scene", "duplicate", "camera", "pre_h", "gpu", "log", "stats"),
	# aji 的 RIFE：没有 duplicate/camera；模型与推理后端可以现场换（重建滤镜才生效）
	"vaji" : ("multi", "scene", "model", "ensemble", "backend", "pre_h", "gpu", "log", "stats"),
}

## 插件构造函数额外接受的参数（vdlssg 的模型档、vvfg 的模型档位、vfsr4 的相机运动）
_FRAMEGEN_PLUGIN_ARGS = {
	"vdlssg" : ("preset",),
	"vxessfg" : (),
	"vvfg" : ("mode",),
	"vfsr4" : ("camera",),
	# aji 没有插件级附加参数：conf/模型/后端都由 mod_aji 拼
	"vaji" : (),
}

## vvfg 档位的显示名（0 低 / 1 中 / 2 高，对应引擎的 low/medium/high 模型）
_FRAMEGEN_MODE_LABELS = {0 : "low", 1 : "medium", 2 : "high"}

## 找不到 DLL 时的报错前缀，与旧脚本一致（用户照着报错找脚本）
_FRAMEGEN_SCRIPT_LABELS = {
	"vdlssg" : "MEMC_DLSSG",
	"vxessfg" : "MEMC_XESSFG",
	"vvfg" : "MEMC_VFG",
	"vfsr4" : "MEMC_FSR4",
	"vaji" : "MEMC_RIFE_AJI",
}

## OSD 上行报告的前缀（沿用旧脚本文案，mpv 侧直接显示）
_FRAMEGEN_TITLES = {
	"vdlssg" : "DLSSG",
	"vxessfg" : "XeSS-FG",
	"vvfg" : "VFG",
	"vfsr4" : "FSR4",
	"vaji" : "RIFE-AJI",
	"vaji_upscale" : "超分-AJI",
	"vaji_tfix" : "时序-AJI",
}

## 各键的取值规则：cast(int/float/bool/str/"gpu") + 上下限（multi 的界由引擎决定）
_FRAMEGEN_VALUE_SPECS = {
	"multi" : (int, None, None),
	"mode" : (int, 0, 2),
	"scene" : (float, 0.0, 1.0),
	"duplicate" : (float, 0.0, 0.05),
	"camera" : (int, 0, None),
	"pre_h" : (int, 0, None),
	"log" : (bool, None, None),
	"stats" : (bool, None, None),
	"preset" : (str, None, None),
	"model" : ("text", None, None),
	"ensemble" : (bool, None, None),
	"backend" : ("text", None, None),
	"before_upscale" : (bool, None, None),
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

# user_data 的对外键名 → 内部键名：面板/报告用 device、rife_model…，插件参数仍叫 gpu、model…
_UD_ALIASES = {
	"device" : "gpu",
	"rife_model" : "model",
	"upscale_model" : "model_upscale",
	"tfix_model" : "model_tfix",
}

def _normalize_ud(ud : dict) -> dict :
	"""把面板的对外键名折回内部键名；新旧两种写法都能读。"""

	out = dict(ud)
	for external, internal in _UD_ALIASES.items() :
		if external in out and internal not in out :
			out[internal] = out[external]
	return out

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
		if cast == "text" :
			# 保 case：模型号/后端名要原样交给 mod_aji 的白名单校验
			return str(raw).strip()
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
	ud = _normalize_ud(VPY_PARSE_USER_DATA(user_data))

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

def VPY_STATS_PATH(script_file) -> str :
	"""耗时统计文件路径（mpv 侧 k7f_vpypar.lua 轮询后画 OSD 常驻行）。"""

	return os.path.abspath(os.fspath(script_file)) + STATS_SUFFIX

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
			return os.path.abspath(cand)

	if not hasattr(core, plugin_ns) :
		raise vs.Error(f"{label or 'MEMC'}: 找不到 {dll}")
	return None

##################################################
## 帧生成 vpy 宿主
##################################################

## H.273 矩阵值 → resize 的 matrix_s 名。RGB 源转回 YUV 时按源声明走：
## HDR10 是 BT.2020，硬套 709 会让颜色整体偏掉（色度矩阵不一致，不是简单的亮度偏移）。
_FRAMEGEN_MATRIX_NAMES = {
	0 : "rgb", 1 : "709", 2 : "unspec", 4 : "fcc", 5 : "470bg",
	6 : "170m", 7 : "240m", 8 : "ycgco", 9 : "2020ncl", 10 : "2020cl",
}

def _framegen_matrix_s(clip : vs.VideoNode) -> str :
	"""源声明的矩阵名：`_Matrix` 优先，mpv 路径（只写 `_ColorSpace`）退到它；
	RGB 源通常没有矩阵属性，就按 `_Primaries` 选（BT.2020 → 2020ncl），否则按 709。

	取首帧属性是 vpy 构图期的常规做法（k7sfunc 其它模块读 `_ColorRange` 也一样），
	代价只有首帧解码一次，换来 RGB→YUV 那一步不会把 BT.2020 的 HDR 当成 709。
	"""
	matrix = None
	primaries = None
	try :
		props = clip.get_frame(0).props
		matrix = props.get("_Matrix")
		if matrix is None :
			matrix = props.get("_ColorSpace")
		primaries = props.get("_Primaries")
	except Exception :
		matrix = primaries = None
	if matrix is not None :
		return _FRAMEGEN_MATRIX_NAMES.get(int(matrix), "709")
	if primaries is not None and int(primaries) == 9 :  # BT.2020
		return "2020ncl"
	return "709"


def _dll_version(path) :
	"""读 DLL 的 VERSIONINFO（优先 ProductVersion），取不到返回 None。

	VS 插件跑起来后只能拿到注册时写的 `PluginVersion`（vdlssg 一直写 0.1），
	真正和发行包对得上的是文件版本资源，所以顺手读一下。
	"""

	if not path or not os.path.isfile(path) :
		return None
	try :
		import ctypes
		from ctypes import wintypes
	except Exception :
		return None

	class _Fixed(ctypes.Structure) :
		_fields_ = [(name, ctypes.c_uint32) for name in (
			"dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
			"dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags",
			"dwFileOS", "dwFileType", "dwFileSubtype", "dwFileDateMS", "dwFileDateLS",
		)]

	version = ctypes.windll.version
	size = version.GetFileVersionInfoSizeW(str(path), None)
	if not size :
		return None
	buf = ctypes.create_string_buffer(size)
	if not version.GetFileVersionInfoW(str(path), 0, size, buf) :
		return None
	ptr = ctypes.c_void_p()
	length = wintypes.UINT()
	if not version.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) :
		return None
	if not ptr.value :
		return None
	fixed = _Fixed.from_address(ptr.value)
	ms = fixed.dwProductVersionMS or fixed.dwFileVersionMS
	ls = fixed.dwProductVersionLS or fixed.dwFileVersionLS
	return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def VPY_PLUGIN_INFO(core, engine : str, func : str = "FrameGen", path = None) -> dict :
	"""读插件的标识、版本与函数签名里的参数名。

	同一个插件名的不同构建接受的参数并不一样（`duplicate_threshold` 是后加的），
	按签名过滤参数比按版本号猜可靠；版本号写进报告，方便用户报问题时对上号。
	"""
	info = {"namespace" : engine, "func" : func}
	try :
		plugin = getattr(core, engine)
		declared = getattr(plugin, func)
	except Exception :
		return info
	identifier = getattr(plugin, "identifier", None)
	if identifier :
		info["identifier"] = str(identifier)
	version = getattr(plugin, "version", None)
	if version is not None :
		try :
			info["version"] = "%d.%d" % (int(version.major), int(version.minor))
		except Exception :
			info["version"] = str(version)
	file_version = _dll_version(path)
	if file_version and file_version != info.get("version") :
		info["file_version"] = file_version
	params = []
	for part in str(getattr(declared, "signature", "") or "").split(";") :
		name = part.split(":", 1)[0].strip()
		if name and name not in ("clip", "vnode") :
			params.append(name)
	if params :
		info["params"] = params
	return info


def _framegen_call(core, engine : str, src : vs.VideoNode, args : dict) :
	"""建 vs-framegen 的 FrameGen 节点。

	`duplicate_threshold` 是插件后加的参数（vs-framegen 0.2.2 还没有），而 vpy
	侧没法按版本号判断。插件不认这个参数时会在构建期直接抛错，去掉它重试一次
	即可让新旧插件都能跑；其它错误照原样抛。
	"""
	fn = getattr(core, engine).FrameGen
	try :
		return fn(src, **args)
	except vs.Error as exc :
		if "duplicate_threshold" not in args or "duplicate_threshold" not in str(exc) :
			raise
		rest = dict(args)
		del rest["duplicate_threshold"]
		return fn(src, **rest)


def FRAMEGEN_VPY(
	input : vs.VideoNode,
	script_file,
	*,
	engine : str,
	fps_in,
	user_data = None,
	multi : int = 2,
	mode : int = 1,
	scene : float = 0.12,
	duplicate : float = 0.005,
	camera : int = 0,
	preset : typing.Optional[str] = None,
	model : typing.Optional[str] = None,
	ensemble : bool = False,
	backend : str = "auto",
	pre_h : int = 0,
	gpu : typing.Optional[int] = None,
	log : bool = False,
	stats : bool = False,
	publish : bool = True,
) -> vs.VideoNode :
	"""vs-framegen 系列的 vpy 主流程：参数解析 + 插件定位 + 补帧 + 上行报告。

	`publish=False` 时不写 sidecar（供 `MIX_AJI` 这类多阶段脚本最后统一上报）。

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

	# aji 的宿主与插件加载都在 mod_aji；lazy 导入避免模块环
	if engine == "vaji" :
		from .mod_aji import RIFE_AJI as _RIFE_AJI, _aji_load_plugin

	values = _resolve_framegen_values(engine, user_data, {
		"multi" : multi, "mode" : mode, "scene" : scene, "duplicate" : duplicate,
		"camera" : camera,
		"preset" : preset, "model" : model, "ensemble" : bool(ensemble),
		"backend" : backend,
		"pre_h" : pre_h, "gpu" : gpu, "log" : bool(log),
		"stats" : bool(stats),
	})
	# aji 的缺省档：没写就用 4.14，与旧脚本一致
	if engine == "vaji" and not values.get("model") :
		values["model"] = "4.14"

	# 埋点：log 往 stderr 打、stats 往 JSON 写（OSD 常驻行用）；两者都关时什么都不建
	debug = bool(values["log"])
	stats_on = bool(values["stats"])
	stages = _vpy_debug.Stages() if (debug or stats_on) else None
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

	plugin_path = None
	if engine != "vaji" :
		plugin_path = VPY_LOAD_PLUGIN(engine, script_file, _FRAMEGEN_SCRIPT_LABELS[engine])
	else :
		_aji_load_plugin()
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
			"duplicate_threshold" : values["duplicate"],
			"log" : 1 if values["log"] else 0}
	for key in _FRAMEGEN_PLUGIN_ARGS[engine] :
		# camera/mode 的 0 是合法取值，不能按真假丢；其余键是"有值才传"
		if key in ("camera", "mode") or values[key] :
			args[key] = values[key]
	if values["gpu"] is not None :
		args["gpu"] = values["gpu"]

	# 只传插件签名里真有的参数：老构建没有 duplicate_threshold，新构建才有
	plugin_info = VPY_PLUGIN_INFO(core, engine, path=plugin_path) if engine != "vaji" else None
	declared = set(plugin_info.get("params") or ()) if plugin_info else set()
	if declared :
		args = {key : value for key, value in args.items() if key in declared}

	# 建节点包含引擎/代理初始化与首帧预热，是构图耗时的大头
	if engine == "vaji" :
		# aji 不走 vs-framegen 的 FrameGen：conf/模型/后端都在 mod_aji 里拼。
		# 切镜走 aji 自己的阈值检测（sc_mode=0）；阈值 0 视为关闭，给 1.0。
		_threshold = values["scene"] if values["scene"] > 0 else 1.0
		fg = _RIFE_AJI(
			input=src,
			model=values["model"],
			ensemble=bool(values["ensemble"]),
			fps_in=fps,
			fps_num=values["multi"],
			fps_den=1,
			sc_mode=0,
			scene_threshold=_threshold,
			gpu=values["gpu"] if values["gpu"] is not None else 0,
			backend=values["backend"],
		)
	else :
		fg = _framegen_call(core, engine, src, args)
	if stages :
		stages.mark("framegen")

	## 输出格式与输入相同：YUV 源原样交给 mpv；只有 RGB 源才转回 10bit YUV
	out = fg if yuv_in else core.resize.Bilinear(
		fg, format=vs.YUV420P10, matrix_s=_framegen_matrix_s(input))

	## 插件已把输出帧率乘以 multi，并逐帧写好 _Duration/_AbsoluteTime；只有源没有帧率时才兜底
	if not out.fps_num or not out.fps_den :
		out = core.std.AssumeFPS(
			out,
			fpsnum=int(round(fps * values["multi"] * 1e6)),
			fpsden=1000000,
		)
	if stages :
		stages.mark("fps")

	if publish :
		_publish_framegen(script_file, engine, values, fps, input, out, plugin_info=plugin_info)
	if stages :
		stages.mark("publish")

	if stages :
		if debug :
			_vpy_debug.dbg(_debug_config(engine, values, fps, input, out, plugin_info))
		# 逐帧探针：请求/就绪时间、单帧延迟、在飞帧数、线程号（见 _vpy_debug）
		writer = None
		if stats_on :
			writer = _make_stats_writer(script_file, engine, values, fps, input, out, stages)
		out = _vpy_debug.attach_frame_probe(core, out, tag = engine, on_ready = writer)
		stages.mark("probe")
		if debug :
			_vpy_debug.dbg(f"stage {stages.report()}")
		if writer :
			writer(None)      # 构图完成后先写一份，OSD 打开不必等第一帧

	return out


def _debug_config(engine : str, values : dict, fps, video_in, out,
		plugin_info = None) -> str :
	"""调试模式下的一行构图摘要：实际参数 + 进出尺寸/帧率，方便和跑分表对齐。"""

	params = " ".join(
		f"{key}={values[key]}" for key in FRAMEGEN_VPY_OPTIONS[engine]
		if key not in ("log", "stats")
	)
	in_info = VPY_CLIP_INFO(video_in, fps = fps) or {}
	out_info = VPY_CLIP_INFO(out) or {}
	line = (f"config {params} in={in_info.get('w')}x{in_info.get('h')}"
		f"@{in_info.get('fps')} out={out_info.get('w')}x{out_info.get('h')}"
		f"@{out_info.get('fps')}")
	if plugin_info and plugin_info.get("version") :
		line += f" plugin={plugin_info['version']}"
	return line

def _publish_framegen(script_file, engine : str, values : dict, fps, video_in, out,
		plugin_info = None) -> None :
	"""按引擎拼 OSD 摘要与参数字段，写 sidecar；失败不影响播放。"""

	keys = FRAMEGEN_VPY_OPTIONS[engine]
	# FSR4 固定 2×，摘要里写死（与旧脚本文案一致）
	label = "2×（固定）" if engine == "vfsr4" else "%d×" % values["multi"]
	# VFG 的模型档位直接写进摘要：换档是用户当场改的，OSD 上要能看见
	if "mode" in keys :
		label += " · 档位 %s" % _FRAMEGEN_MODE_LABELS.get(values["mode"], values["mode"])
	# aji 的模型号与推理后端也写进摘要（换模型要重建滤镜，OSD 上必须能确认）
	if "model" in keys :
		label += " · %s" % values["model"]
	if "ensemble" in keys and values["ensemble"] :
		label += " · ensemble"
	if "backend" in keys and values.get("backend") not in (None, "", "auto") :
		label += " · %s" % values["backend"]

	if "preset" in keys :
		summary = "%s · 预设 %s · 切镜 %g" % (label, values["preset"], values["scene"])
	else :
		summary = "%s · 切镜 %g" % (label, values["scene"])
	if "camera" in keys and values["camera"] :
		summary += " · 相机 %d" % values["camera"]
	if values["pre_h"] :
		summary += " · 预缩 %dp" % values["pre_h"]

	params = {"multi" : values["multi"], "scene" : values["scene"]}
	if "mode" in keys :
		params["mode"] = values["mode"]
	if "preset" in keys :
		params["preset"] = values["preset"]
	if "camera" in keys :
		params["camera"] = values["camera"]
	if "model" in keys :
		# sidecar 用对外键名，面板直接把它当 user_data 的键
		params["rife_model"] = values["model"]
	if "ensemble" in keys :
		params["ensemble"] = values["ensemble"]
	if "backend" in keys :
		params["backend"] = values["backend"]
	params["device"] = "auto" if values["gpu"] is None else values["gpu"]
	params["pre_h"] = values["pre_h"]
	params["log"] = values["log"]
	params["stats"] = values["stats"]

	VPY_PUBLISH(
		script_file,
		title=_FRAMEGEN_TITLES[engine],
		engine=engine,
		params=params,
		summary=summary,
		in_info=VPY_CLIP_INFO(video_in, fps=fps),
		out_info=VPY_CLIP_INFO(out),
		**({"plugin" : plugin_info} if plugin_info else {}),
	)

def _make_stats_writer(script_file, engine : str, values : dict, fps, video_in, out, stages) :
	"""构造耗时写入器：`live` 为 None 时只落构图阶段，否则带上逐帧统计。

	mpv 侧 k7f_vpypar.lua 以 1s 间隔读这个文件，画在 OSC 上方；
	写失败只影响那一行显示，不影响播放（`_write_atomic` 会提示一次）。
	"""

	path = VPY_STATS_PATH(script_file)
	meta = {
		"v" : SCHEMA_VERSION,
		"script" : os.path.basename(os.fspath(script_file)),
		"engine" : engine,
		"title" : _FRAMEGEN_TITLES.get(engine, engine),
		"pid" : os.getpid(),
		"multi" : values.get("multi"),
		"input" : VPY_CLIP_INFO(video_in, fps = fps),
		"output" : VPY_CLIP_INFO(out),
	}
	# 帧回调在 VS 的工作线程上，节流状态用普通闭包变量即可（同一进程内单写者）
	last = [0.0]

	def write(live) :
		now = time.perf_counter()
		if live is not None and now - last[0] < STATS_INTERVAL :
			return
		last[0] = now

		payload = dict(meta)
		payload["ts"] = round(time.time(), 3)
		if stages is not None :
			payload["stages"] = stages.snapshot()
		if live is not None :
			payload["live"] = live

		try :
			text = json.dumps(payload, ensure_ascii = False, separators = (",", ":"))
		except Exception :
			return              # 统计文件丢了无所谓，绝不能影响播放
		_write_atomic(path, text)

	return write


##################################################
## 超分 / 时序（aji）vpy 宿主
##################################################

## 这两层都是 1 帧进 1 帧出：超分只改尺寸，时序只改像素
## （时序链内部会自己向前后要多帧，vpy 侧不用管）
AJI_VPY_OPTIONS = {
	"vaji_upscale" : ("model", "backend", "gpu", "log", "stats"),
	"vaji_tfix" : ("model", "scene", "before_upscale", "backend", "gpu", "log", "stats"),
}

## 面板没写模型时的缺省档位（短名表见 mod_aji._AJI_UPSCALE_MODELS / _AJI_TEMPORAL_MODELS）
_AJI_VPY_DEFAULTS = {
	"vaji_upscale" : {"model" : "hd_perf"},
	"vaji_tfix" : {"model" : "s1"},
}

def _resolve_aji_values(engine : str, user_data, literal : dict) -> dict :
	"""与 `_resolve_framegen_values` 同一套优先级（字面量 < 环境变量 < user_data）。

	区别只是没有 multi，所以不需要按引擎取倍率上下界。
	"""

	values = dict(literal)
	ud = _normalize_ud(VPY_PARSE_USER_DATA(user_data))

	# 叠加脚本（MIX_AJI.vpy）用带层前缀的键，单层脚本用短键
	if engine == "vaji_upscale" and "model_upscale" in ud :
		ud["model"] = ud["model_upscale"]
	if engine == "vaji_tfix" :
		if "model_tfix" in ud :
			ud["model"] = ud["model_tfix"]
		if "tfix_scene" in ud :
			ud["scene"] = ud["tfix_scene"]

	for key in AJI_VPY_OPTIONS[engine] :
		cast, lo, hi = _FRAMEGEN_VALUE_SPECS[key]

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

	# 数值键统一钳一次：超界只收窄，不让插件在求值阶段报错
	for key in AJI_VPY_OPTIONS[engine] :
		value = values.get(key)
		if not isinstance(value, (int, float)) or isinstance(value, bool) :
			continue
		cast, lo, hi = _FRAMEGEN_VALUE_SPECS[key]
		if lo is not None and value < lo :
			values[key] = lo
		if hi is not None and value > hi :
			values[key] = hi

	# 模型号没给（面板留空/旧 user-data）时回到缺省档，免得摘要里出现 "None"
	if not values.get("model") :
		values["model"] = _AJI_VPY_DEFAULTS[engine]["model"]

	return values

def AJI_VPY(
	input : vs.VideoNode,
	script_file,
	*,
	engine : str,
	user_data = None,
	model : typing.Optional[str] = None,
	scene : float = 0.150,
	before_upscale : bool = False,
	backend : str = "auto",
	gpu : typing.Optional[int] = 0,
	log : bool = False,
	stats : bool = False,
	publish : bool = True,
) -> vs.VideoNode :
	"""aji 的超分 / 时序阶段：1 帧进 1 帧出，参数解析 + 插件定位 + 上行报告。

	`publish=False` 时不写 sidecar（供 `MIX_AJI` 这类多阶段脚本最后统一上报）。

	vpy 里：

		out = k7f.AJI_VPY(
			video_in, __file__, engine="vaji_upscale",
			user_data=globals().get("user_data"),
			model=Model, gpu=Gpu, backend=Backend, log=Log, stats=Stats,
		)

	engine 取 `vaji_upscale`（超分）或 `vaji_tfix`（时序）。叠在补帧之后，
	顺序固定为 插帧 → 超分 → 时序（见 mpv 侧菜单里的组合预设）。
	"""

	if engine not in AJI_VPY_OPTIONS :
		raise vs.Error(f"AJI_VPY: 不认识的阶段 {engine!r}")

	# 插件加载与 conf 拼装都在 mod_aji；lazy 导入避免模块环
	from .mod_aji import UPSCALE_AJI, TEMPORAL_FIX_AJI, _aji_load_plugin

	values = _resolve_aji_values(engine, user_data, {
		"model" : model if model else _AJI_VPY_DEFAULTS[engine]["model"],
		"scene" : scene,
		"before_upscale" : bool(before_upscale),
		"backend" : backend,
		"gpu" : gpu,
		"log" : bool(log),
		"stats" : bool(stats),
	})

	debug = bool(values["log"])
	stats_on = bool(values["stats"])
	stages = _vpy_debug.Stages() if (debug or stats_on) else None
	if stages :
		stages.mark("resolve")

	_aji_load_plugin()
	if stages :
		stages.mark("load")

	# aji 的三个阶段各是一个函数，签名同样能读出来（模型/后端之类都由 k7 解析，不传插件）
	plugin_info = VPY_PLUGIN_INFO(
		core, "vsaji",
		{"vaji_upscale" : "Upscale", "vaji_tfix" : "TemporalFix"}.get(engine, "Rife"))

	# 显卡序号：面板的 auto 交给插件默认卡
	device = values["gpu"] if values["gpu"] is not None else 0

	if engine == "vaji_upscale" :
		out = UPSCALE_AJI(
			input=input,
			model=values["model"],
			gpu=device,
			backend=values["backend"],
		)
	else :
		out = TEMPORAL_FIX_AJI(
			input=input,
			model=values["model"],
			scene_threshold=float(values["scene"]),
			before_upscale=bool(values["before_upscale"]),
			gpu=device,
			backend=values["backend"],
		)
	if stages :
		stages.mark("aji")

	if publish :
		_publish_aji(script_file, engine, values, input, out, plugin_info=plugin_info)
	if stages :
		stages.mark("publish")

	if stages :
		if debug :
			in_info = VPY_CLIP_INFO(input) or {}
			out_info = VPY_CLIP_INFO(out) or {}
			_vpy_debug.dbg(
				f"{engine} model={values['model']} backend={values['backend']}"
				f" in={in_info.get('w')}x{in_info.get('h')}"
				f" out={out_info.get('w')}x{out_info.get('h')}")
		# 逐帧探针：与补帧侧同一套写手，mpv 侧 OSD 常驻行不分阶段
		writer = None
		if stats_on :
			writer = _make_stats_writer(script_file, engine, values, input.fps, input, out, stages)
		out = _vpy_debug.attach_frame_probe(core, out, tag=engine, on_ready=writer)
		stages.mark("probe")
		if debug :
			_vpy_debug.dbg(f"stage {stages.report()}")
		if writer :
			writer(None)      # 构图后先写一份，OSD 打开不必等第一帧

	return out

def _publish_aji(script_file, engine : str, values : dict, video_in, out,
		plugin_info = None) -> None :
	"""写 sidecar：超分标倍数与模型，时序标模型与阈值（OSD 上要能一眼看出叠了哪层）。"""

	backend = values.get("backend")
	tail = "" if not backend or backend == "auto" else " · %s" % backend

	if engine == "vaji_upscale" :
		summary = "2× · %s%s" % (values["model"], tail)
	else :
		summary = "%s · 阈 %.3f%s" % (values["model"], float(values["scene"]), tail)
		if values["before_upscale"] :
			summary += " · 缩前"

	# sidecar 的 params 用对外键名（面板把它直接当 user_data 的键）
	model_key = "upscale_model" if engine == "vaji_upscale" else "tfix_model"
	params = {model_key : values["model"], "backend" : backend}
	if "scene" in AJI_VPY_OPTIONS[engine] :
		params["tfix_scene"] = values["scene"]
	if "before_upscale" in AJI_VPY_OPTIONS[engine] :
		params["before_upscale"] = values["before_upscale"]
	params["device"] = "auto" if values["gpu"] is None else values["gpu"]
	params["log"] = values["log"]
	params["stats"] = values["stats"]

	VPY_PUBLISH(
		script_file,
		title=_FRAMEGEN_TITLES[engine],
		engine=engine,
		params=params,
		summary=summary,
		in_info=VPY_CLIP_INFO(video_in),
		out_info=VPY_CLIP_INFO(out),
		**({"plugin" : plugin_info} if plugin_info else {}),
	)

def AJI_STACK_PUBLISH(
	script_file,
	*,
	stages : str,
	values : dict,
	user_data = None,
	video_in,
	out,
) -> None :
	"""多阶段脚本（`MIX_AJI.vpy`）的统一上报：一条 sidecar 说明整条链。

	`stages` 是启用层的短字符组合，顺序固定 `f` → `u` → `t`；`values` 里带
	各层的模型/阈值。单层脚本不走这里（它们自己上报更细）。
	"""

	# 面板写进 user_data 的值优先于脚本里的字面量，摘要要反映实际生效的设置
	ud = _normalize_ud(VPY_PARSE_USER_DATA(user_data))
	values = dict(values)
	for key in ("multi", "scene", "model", "ensemble", "model_upscale",
			"model_tfix", "tfix_scene", "before_upscale", "backend", "gpu",
			"log", "stats") :
		if key not in ud :
			continue
		cast, lo, hi = _FRAMEGEN_VALUE_SPECS[key]
		value = _coerce(key, ud[key], cast)
		if value is not _INVALID :
			values[key] = value

	parts = []
	labels = []
	framegen = str(values.get("framegen") or "vaji").strip()
	if "f" in stages :
		labels.append("补帧")
		if framegen == "vaji" :
			parts.append("%d× · %s" % (int(values["multi"]), values["model"]))
		else :
			# vsfg 系没有模型号，摘要里报引擎名；FSR4 固定 2×
			parts.append("%d× · %s" % (2 if framegen == "vfsr4" else int(values["multi"]),
				_FRAMEGEN_TITLES.get(framegen, framegen)))
		if values["ensemble"] and framegen == "vaji" :
			parts.append("ensemble")
	if "u" in stages :
		labels.append("超分")
		parts.append("2× · %s" % values["model_upscale"])
	if "t" in stages :
		labels.append("时序")
		parts.append("%s · 阈 %.3f" % (values["model_tfix"], float(values["tfix_scene"])))

	backend = values.get("backend")
	if backend and backend != "auto" :
		parts.append(str(backend))

	params = {"stages" : stages, "backend" : backend}
	params["device"] = "auto" if values.get("gpu") is None else values["gpu"]
	params["log"] = values["log"]
	params["stats"] = values["stats"]
	if "f" in stages :
		params.update({"framegen" : framegen, "multi" : values["multi"], "scene" : values["scene"],
			"rife_model" : values["model"], "ensemble" : values["ensemble"]})
	if "u" in stages :
		params["upscale_model"] = values["model_upscale"]
	if "t" in stages :
		params.update({"tfix_model" : values["model_tfix"], "tfix_scene" : values["tfix_scene"],
			"before_upscale" : values["before_upscale"]})

	VPY_PUBLISH(
		script_file,
		title="叠层-AJI（%s）" % "+".join(labels),
		engine="vaji_stack",
		params=params,
		summary=" / ".join(parts),
		in_info=VPY_CLIP_INFO(video_in),
		out_info=VPY_CLIP_INFO(out),
	)

