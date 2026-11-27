"""补帧 / 超分 - AnimeJaNai（libaji）

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
from .mod_helper import (
	SCENE_DETECT,
)

##################################################
## RIFE补帧 AnimeJaNai (libaji)
##################################################

def _aji_pack_root() -> str :
	"""播放器包根：<包根>\\Lib\\site-packages\\k7sfunc\\mod_memc.py 往上三层。

	本包按源码树装进播放器，插件目录与 vs-plugins 都相对包根固定，可以据此
	定位资源；k7sfunc 从 PyPI 装到别处时这个路径不存在，调用方会继续降级。
	"""
	return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
										os.pardir, os.pardir, os.pardir))

def _aji_load_plugin() -> None :
	"""兜底加载 vsaji.dll（正常情况由 VapourSynth 自动扫描插件目录）。

	按 K7 的依赖规范，插件走 `Lib\\site-packages\\vapoursynth\\plugins\\`，进程
	启动时自动 LoadPlugin；这里只是为"手动解压到 vs-plugins"与 PLAYKIT 预设
	（纯 Python，没有 vpy 脚本层可写 LoadPlugin）留一条路。环境变量 AJI_PLUGIN
	可指定具体 DLL，便于换版本调试。
	"""
	if hasattr(core, "vsaji") :
		return

	root = _aji_pack_root()
	cands = []
	if os.environ.get("AJI_PLUGIN") :
		cands.append(os.environ["AJI_PLUGIN"])
	cands += [
		## 非规范布局：插件与运行时都解压在 vs-plugins 下
		os.path.join(root, "vs-plugins", "vsaji.dll"),
		os.path.join(root, "vs-plugins", "aji", "vsaji.dll"),
	]
	for path in cands :
		if not os.path.isfile(path) :
			continue
		try :
			core.std.LoadPlugin(os.path.abspath(path))
		except Exception :
			continue
		if hasattr(core, "vsaji") :
			return

def _aji_version_paths() -> tuple[str, str] :
	"""插件自报的 (插件所在目录, 它找到的 aji 运行时目录)，拿不到时给空串。

	vsaji 的 Version() 一次返回 `path`（本 DLL 所在目录）与 `aji`（按"同级 aji.dll"
	或"下级 aji\\"探测到的运行时目录），所以优先信任插件自己的判断。
	注意：插件把 Version 注册成无返回签名，VapourSynth 会顺带打印一行
	`Version: no return value(s) named ...`，属于上游噪声，不影响取值。
	"""
	try :
		info = core.vsaji.Version()
	except Exception :
		return "", ""
	def _text(key : str) -> str :
		value = info.get(key) if hasattr(info, "get") else ""
		if isinstance(value, bytes) :
			value = value.decode()
		return value or ""
	return _text("path"), _text("aji")

def _aji_runtime_dir() -> str :
	"""aji 运行时目录（aji.dll / conf / rife 所在的那一层），找不到返回空串。

	运行时不是 VS 插件，不进 `vapoursynth\\plugins\\`，按规范放 `<包根>\\vs-plugins\\aji\\`；
	插件在运行期按自身路径 dlopen `aji.dll`，所以这里按优先级列出几种合法布局：

	1. `AJI_DIR` 环境变量（换版本调试）；
	2. 插件自报的 `aji` 目录（同级 aji.dll，或下级 aji\\）；
	3. 插件同级的 `aji\\`、插件上级的 `aji\\`（插件自报为空时兜底）；
	4. `<包根>\\vs-plugins\\aji`（本包拓展包的标准布局）。
	"""
	plugin_dir, reported = _aji_version_paths()
	cands = []
	if os.environ.get("AJI_DIR") :
		cands.append(os.environ["AJI_DIR"])
	if reported :
		cands.append(reported)
	if plugin_dir :
		cands.append(os.path.join(plugin_dir, "aji"))
		cands.append(os.path.join(os.path.dirname(plugin_dir), "aji"))
	cands.append(os.path.join(_aji_pack_root(), "vs-plugins", "aji"))
	for path in cands :
		if os.path.isdir(path) :
			return path
	return ""

def RIFE_AJI(
	input : vs.VideoNode,
	model : str = "4.14",
	ensemble : bool = False,
	fps_in : float = 23.976,
	fps_num : int = 2,
	fps_den : int = 1,
	sc_mode : typing.Literal[0, 1] = 1,
	gpu : int = 0,
	backend : str = "auto",
) -> vs.VideoNode :

	func_name = "RIFE_AJI"
	_validate_input_clip(func_name, input)
	if not isinstance(model, str) or "." not in model :
		raise vs.Error(f"模块 {func_name} 的子参数 model 的值无效")
	_validate_bool(func_name, "ensemble", ensemble)
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	_validate_numeric(func_name, "fps_num", fps_num, min_val=2, int_only=True)
	if not isinstance(fps_den, int) or fps_den >= fps_num or fps_num/fps_den <= 1 :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_den 的值无效")
	_validate_literal(func_name, "sc_mode", sc_mode, [0, 1])
	_validate_numeric(func_name, "gpu", gpu, min_val=0, int_only=True)
	_validate_literal(func_name, "backend", backend, _AJI_BACKENDS)

	## aji 的 factor 只有整数，非整数倍率直接拒绝（不回退到 FRAMEGEN_HUB）
	if fps_num % fps_den != 0 :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_den 无法整除 fps_num：aji 路径只支持整数倍率")

	_aji_load_plugin()
	_check_plugin(func_name, "vsaji")

	aji_dir = _aji_runtime_dir()
	if not aji_dir :
		raise vs.Error(f"模块 {func_name} 找不到 aji 运行时：请把 vsaji 的扁平包放到 <包根>\\vs-plugins\\aji\\，"
						f"vsaji.dll 放到 Lib\\site-packages\\vapoursynth\\plugins\\")

	mdl_dir = os.path.join(aji_dir, "rife")
	conf_dir = os.path.join(aji_dir, "conf")
	## conf 是运行期产物，缺失时补建，不依赖发行包预先放好
	if not os.path.isdir(conf_dir) :
		os.makedirs(conf_dir, exist_ok=True)

	## 模型名 → aji 的模型号：aji 取 s.size()==2 ? s[1:2] : s[1:3]，即 "4.7"→"47"、"4.14"→"414"
	mdl_base = model[:-5] if model.endswith("_lite") else model
	mini = mdl_base.split(".")[1]
	if not mini.isdigit() :
		raise vs.Error(f"模块 {func_name} 的子参数 model 的值无效")
	code = "4" + mini
	if model.endswith("_lite") :
		code = code + "1"

	onnx_name = "rife_v" + model + ("_ensemble" if ensemble else "")
	onnx_path = os.path.join(mdl_dir, onnx_name + ".onnx")
	if not os.path.exists(onnx_path) :
		raise vs.Error(f"模块 {func_name} 所请求的模型缺失: {onnx_path}")

	## conf 里的 factor 只为启用 RIFE 链，真实倍率与插帧相位由 VS 侧的 factor 参数给
	conf_body = (
		"[global]\n"
		"config_version=2\n"
		"backend=TensorRT\n"
		"logging=no\n"
		"\n"
		"[slot_1]\n"
		"profile_name=k7 RIFE\n"
		"chain_1_max_resolution=4096x2304\n"
		"chain_1_max_fps=241\n"
		"chain_1_rife=yes\n"
		"chain_1_rife_factor_numerator=2\n"
		"chain_1_rife_factor_denominator=1\n"
		f"chain_1_rife_model={code}\n"
		f"chain_1_rife_ensemble={'yes' if ensemble else 'no'}\n"
	)
	## sc_mode 0 交给 aji 自己的阈值检测；sc_mode 1 用 VS 侧属性，这里不写阈值
	if sc_mode == 0 :
		conf_body = conf_body + "chain_1_rife_scene_detect_threshold=0.150\n"

	conf_path = os.path.join(conf_dir, f"rife_only_{code}.conf")
	old_conf = None
	if os.path.isfile(conf_path) :
		with open(conf_path, "r", encoding="utf-8", newline="") as f :
			old_conf = f.read()
	if old_conf != conf_body :
		with open(conf_path, "w", encoding="utf-8", newline="\n") as f :
			f.write(conf_body)

	## 场景切换：aji 的 scene_prop 只认 _SceneChangeNext，源头没打过就先补一次
	if sc_mode == 1 and input.get_frame(0).props.get("_SceneChangeNext") is None :
		cut0 = SCENE_DETECT(input=input, sc_mode=1)
	else :
		cut0 = input

	factor = fps_num // fps_den
	fin = core.vsaji.Rife(cut0,
		conf=conf_path,
		rife_model_dir=mdl_dir,
		factor=factor,
		device=gpu,
		scene_prop=1 if sc_mode == 1 else 0,
		lib=os.path.join(aji_dir, "aji.dll"),
		backend=backend)

	## 插件保持输入 fps、帧数乘 factor，必须改回真实输出帧率，否则播放成慢动作
	output = core.std.AssumeFPS(clip=fin, fpsnum=int(round(fps_in * factor * 1e6)), fpsden=1000000)

	return output

##################################################
## 超分/时序 AnimeJaNai (libaji)
##################################################

## 发行包 models\ 里的 ONNX（不带扩展名）。短名只是调用方便，也可以直接写全名。
_AJI_UPSCALE_MODELS = {
	"hd_perf": "2x_AnimeJaNai_HD_V3.1_Performance_SPANF3_b5f48_unshuffle_fp16",
	"hd_bal": "2x_AnimeJaNai_HD_V3.1_Balanced_SPANF3_b8f64_unshuffle_fp16",
	"hd_perf_sharp": "2x_AnimeJaNai_HD_V3.1Sharp1_Performance_SPANF3_b5f48_unshuffle_fp16",
	"hd_bal_sharp": "2x_AnimeJaNai_HD_V3.1Sharp1_Balanced_SPANF3_b8f64_unshuffle_fp16",
	"sd": "2x_AnimeJaNai_SD_V1beta34_Compact_1x3xHxW_dyn-HW_strong_fp16_op21_dynamo",
}

_AJI_TEMPORAL_MODELS = {
	"s1": "temporalfix_s1_v1.1_op18_fp16",
	"s2": "temporalfix_s2_v1_op18_fp16",
	"s3": "temporalfix_s3_v1_op18_fp16",
}

_AJI_BACKENDS = ["auto", "conf", "native", "cuda", "TensorRT", "TensorRT-RTX"]

def _aji_model_name(func_name : str, param : str, model : str,
					table : dict[str, str]) -> str :
	"""短名 → ONNX 文件名；不在表里就原样当文件名用（方便换第三方模型）。"""

	if not isinstance(model, str) or not model :
		raise vs.Error(f"模块 {func_name} 的子参数 {param} 的值无效")
	return table.get(model, model)

def _aji_chain_dirs(func_name : str) -> tuple[str, str, str] :
	"""(aji 运行时目录, conf 目录, models 目录)，缺运行时直接报错。"""

	aji_dir = _aji_runtime_dir()
	if not aji_dir :
		raise vs.Error(f"模块 {func_name} 找不到 aji 运行时：请把 vsaji 的扁平包放到 <包根>\\vs-plugins\\aji\\，"
						f"vsaji.dll 放到 Lib\\site-packages\\vapoursynth\\plugins\\")

	conf_dir = os.path.join(aji_dir, "conf")
	if not os.path.isdir(conf_dir) :
		os.makedirs(conf_dir, exist_ok=True)

	model_dir = os.path.join(aji_dir, "models")
	if not os.path.isdir(model_dir) :
		raise vs.Error(f"模块 {func_name} 找不到模型目录: {model_dir}（用 pack.ps1 -ModelsFrom 打包）")

	return aji_dir, conf_dir, model_dir

def _aji_write_conf(path : str, body : str) -> str :
	"""conf 是运行期产物：内容没变就不重写，避免播放器多实例互相触发重载。"""

	old_conf = None
	if os.path.isfile(path) :
		with open(path, "r", encoding="utf-8", newline="") as f :
			old_conf = f.read()
	if old_conf != body :
		with open(path, "w", encoding="utf-8", newline="\n") as f :
			f.write(body)
	return path

def _aji_chain_header(name : str, max_resolution : str) -> str :
	"""三处 conf 共用的 [global] + [slot_1] 头；max_fps 给 241，60p 也够用。"""

	return (
		"[global]\n"
		"config_version=3\n"
		"backend=TensorRT\n"
		"logging=no\n"
		"\n"
		"[slot_1]\n"
		f"profile_name={name}\n"
		f"chain_1_max_resolution={max_resolution}\n"
		"chain_1_max_fps=241\n"
	)

def UPSCALE_AJI(
	input : vs.VideoNode,
	model : str = "hd_perf",
	gpu : int = 0,
	backend : str = "auto",
) -> vs.VideoNode :
	"""AnimeJaNai 超分（libaji 链，1 帧进 1 帧出，尺寸变大）。

	model 用短名（hd_perf/hd_bal/hd_perf_sharp/hd_bal_sharp/sd）或完整 ONNX 名。
	"""

	func_name = "UPSCALE_AJI"
	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "gpu", gpu, min_val=0, int_only=True)
	_validate_literal(func_name, "backend", backend, _AJI_BACKENDS)

	onnx_name = _aji_model_name(func_name, "model", model, _AJI_UPSCALE_MODELS)

	_aji_load_plugin()
	_check_plugin(func_name, "vsaji")
	aji_dir, conf_dir, model_dir = _aji_chain_dirs(func_name)

	onnx_path = os.path.join(model_dir, onnx_name + ".onnx")
	if not os.path.isfile(onnx_path) :
		raise vs.Error(f"模块 {func_name} 所请求的模型缺失: {onnx_path}")

	conf_body = _aji_chain_header("k7 Upscale", "4096x2304") + (
		f"chain_1_model_1_name={onnx_name}\n"
		"chain_1_model_1_resize_height_before_upscale=0\n"
		"chain_1_model_1_resize_factor_before_upscale=100\n"
	)
	conf_path = _aji_write_conf(os.path.join(conf_dir, f"upscale_{onnx_name}.conf"), conf_body)

	return core.vsaji.Upscale(
		clip=input,
		conf=conf_path,
		model_dir=model_dir,
		device=gpu,
		fps=float(input.fps),
		backend=backend,
		lib=os.path.join(aji_dir, "aji.dll"))

def TEMPORAL_FIX_AJI(
	input : vs.VideoNode,
	model : str = "s1",
	scene_threshold : float = 0.150,
	before_upscale : bool = False,
	gpu : int = 0,
	backend : str = "auto",
) -> vs.VideoNode :
	"""AnimeJaNai 时序模型（temporalfix）：1 帧进 1 帧出、尺寸不变。

	可以单独用，也可以叠加在任意插帧/超分结果之后（输入输出帧率、尺寸都一致）。
	model 用 s1/s2/s3 或完整 ONNX 名；s1 是 7 帧窗（r=3），代价见 vsaji 的 README。
	"""

	func_name = "TEMPORAL_FIX_AJI"
	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "scene_threshold", scene_threshold, min_val=0.0, max_val=1.0)
	_validate_bool(func_name, "before_upscale", before_upscale)
	_validate_numeric(func_name, "gpu", gpu, min_val=0, int_only=True)
	_validate_literal(func_name, "backend", backend, _AJI_BACKENDS)

	onnx_name = _aji_model_name(func_name, "model", model, _AJI_TEMPORAL_MODELS)

	_aji_load_plugin()
	_check_plugin(func_name, "vsaji")
	aji_dir, conf_dir, model_dir = _aji_chain_dirs(func_name)

	onnx_path = os.path.join(model_dir, onnx_name + ".onnx")
	if not os.path.isfile(onnx_path) :
		raise vs.Error(f"模块 {func_name} 所请求的模型缺失: {onnx_path}")

	conf_body = _aji_chain_header("k7 TemporalFix", "4096x2304") + (
		f"chain_1_temporal_fix_model={onnx_name}\n"
		f"chain_1_temporal_fix_before_upscale={'yes' if before_upscale else 'no'}\n"
		f"chain_1_temporal_fix_scene_threshold={scene_threshold:.3f}\n"
	)
	conf_path = _aji_write_conf(os.path.join(conf_dir, f"tfix_{onnx_name}.conf"), conf_body)

	return core.vsaji.TemporalFix(
		clip=input,
		conf=conf_path,
		model_dir=model_dir,
		device=gpu,
		fps=float(input.fps),
		backend=backend,
		lib=os.path.join(aji_dir, "aji.dll"))
