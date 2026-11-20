"""与视频处理无关的辅助函数

"""

import os
import vapoursynth as vs
import typing
import threading

##################################################
## https://github.com/python/cpython/blob/v3.11.8/Lib/distutils/version.py
##################################################

import re

class Version:
	def __init__ (self, vstring=None):
		if vstring:
			self.parse(vstring)
	def __repr__ (self):
		return "%s ('%s')" % (self.__class__.__name__, str(self))
	def __eq__(self, other):
		c = self._cmp(other)
		if c is NotImplemented:
			return c
		return c == 0
	def __lt__(self, other):
		c = self._cmp(other)
		if c is NotImplemented:
			return c
		return c < 0
	def __le__(self, other):
		c = self._cmp(other)
		if c is NotImplemented:
			return c
		return c <= 0
	def __gt__(self, other):
		c = self._cmp(other)
		if c is NotImplemented:
			return c
		return c > 0
	def __ge__(self, other):
		c = self._cmp(other)
		if c is NotImplemented:
			return c
		return c >= 0

class StrictVersion (Version):
	version_re = re.compile(r'^(\d+) \. (\d+) (\. (\d+))? ([ab](\d+))?$',
							re.VERBOSE | re.ASCII)
	def parse (self, vstring):
		match = self.version_re.match(vstring)
		if not match:
			raise ValueError("invalid version number '%s'" % vstring)
		(major, minor, patch, prerelease, prerelease_num) = \
			match.group(1, 2, 4, 5, 6)
		if patch:
			self.version = tuple(map(int, [major, minor, patch]))
		else:
			self.version = tuple(map(int, [major, minor])) + (0,)
		if prerelease:
			self.prerelease = (prerelease[0], int(prerelease_num))
		else:
			self.prerelease = None
	def __str__ (self):
		if self.version[2] == 0:
			vstring = '.'.join(map(str, self.version[0:2]))
		else:
			vstring = '.'.join(map(str, self.version))
		if self.prerelease:
			vstring = vstring + self.prerelease[0] + str(self.prerelease[1])
		return vstring
	def _cmp (self, other):
		if isinstance(other, str):
			other = StrictVersion(other)
		elif not isinstance(other, StrictVersion):
			return NotImplemented
		if self.version != other.version:
			if self.version < other.version:
				return -1
			else:
				return 1
		if (not self.prerelease and not other.prerelease):
			return 0
		elif (self.prerelease and not other.prerelease):
			return -1
		elif (not self.prerelease and other.prerelease):
			return 1
		elif (self.prerelease and other.prerelease):
			if self.prerelease == other.prerelease:
				return 0
			elif self.prerelease < other.prerelease:
				return -1
			else:
				return 1
		else:
			assert False, "never get here"

class LooseVersion (Version):
	component_re = re.compile(r'(\d+ | [a-z]+ | \.)', re.VERBOSE)
	def __init__ (self, vstring=None):
		if vstring:
			self.parse(vstring)
	def parse (self, vstring):
		self.vstring = vstring
		components = [x for x in self.component_re.split(vstring)
								if x and x != '.']
		for i, obj in enumerate(components):
			try:
				components[i] = int(obj)
			except ValueError:
				pass
		self.version = components
	def __str__ (self):
		return self.vstring
	def __repr__ (self):
		return "LooseVersion ('%s')" % str(self)
	def _cmp (self, other):
		if isinstance(other, str):
			other = LooseVersion(other)
		elif not isinstance(other, LooseVersion):
			return NotImplemented
		if self.version == other.version:
			return 0
		if self.version < other.version:
			return -1
		if self.version > other.version:
			return 1

##################################################
## 初始设置
##################################################

vs_thd_init = os.cpu_count()
vs_t_dft = 1
if vs_thd_init > 8 and vs_thd_init <= 16 :
	vs_t_dft = 8
elif vs_thd_init > 16 :
	if vs_thd_init <= 32 :
		vs_t_dft = vs_thd_init // 2
		if vs_t_dft % 2 != 0 :
			vs_t_dft = vs_t_dft - 1
	else :
		vs_t_dft = 16
else :
	vs_t_dft = vs_thd_init

vs_api = vs.__api_version__.api_major
if vs_api < 4 :
	raise ImportError("帧服务器 VapourSynth 的版本号过低，至少 R57")

core = vs.core

##################################################
## 参数验证
##################################################

def _validate_input_clip(
	func_name : str,
	input,
) -> None :
	if not isinstance(input, vs.VideoNode) :
		raise vs.Error(f"模块 {func_name} 的子参数 input 的值无效")

def _validate_bool(
	func_name : str,
	param_name : str,
	value,
) -> None :
	if not isinstance(value, bool) :
		raise vs.Error(f"模块 {func_name} 的子参数 {param_name} 的值无效")

def _validate_numeric(
	func_name : str,
	param_name : str,
	value,
	min_val = None,
	max_val = None,
	exclusive_min = False,
	int_only = False,
) -> None :
	if int_only :
		if not isinstance(value, int) :
			raise vs.Error(f"模块 {func_name} 的子参数 {param_name} 的值无效")
	else :
		if not isinstance(value, (int, float)) :
			raise vs.Error(f"模块 {func_name} 的子参数 {param_name} 的值无效")
	if min_val is not None :
		if exclusive_min and value <= min_val :
			raise vs.Error(f"模块 {func_name} 的子参数 {param_name} 的值无效")
		elif not exclusive_min and value < min_val :
			raise vs.Error(f"模块 {func_name} 的子参数 {param_name} 的值无效")
	if max_val is not None and value > max_val :
		raise vs.Error(f"模块 {func_name} 的子参数 {param_name} 的值无效")

def _validate_literal(
	func_name : str,
	param_name : str,
	value,
	valid_values : list,
) -> None :
	if value not in valid_values :
		raise vs.Error(f"模块 {func_name} 的子参数 {param_name} 的值无效")

def _validate_string_length(
	func_name : str,
	param_name : str,
	value : str,
	min_length : int,
) -> None :
	if len(value) <= min_length :
		raise vs.Error(f"模块 {func_name} 的子参数 {param_name} 的值无效")

##################################################
## 依赖检查
##################################################

_plugin_cache = {}
_plugin_cache_lock = threading.Lock()

def _check_plugin(
	func_name : str,
	plugin_name: str,
) -> None :
	with _plugin_cache_lock :
		if plugin_name not in _plugin_cache :
			_plugin_cache[plugin_name] = hasattr(core, plugin_name)
		if not _plugin_cache[plugin_name] :
			raise ModuleNotFoundError(f"模块 {func_name} 依赖错误：缺失插件，检查项目 {plugin_name}")

def _check_trt(
	func_name : str,
) -> str :
	"""返回可用的 TensorRT 命名空间。

	官方 vs-mlrt 发的是 core.trt（nvinfer），playkit 的 tensorrt-rtx 补丁包
	只带 core.trt_rtx（TensorRT-RTX），两者接口相近但后端对象不同。
	"""
	for plugin_name in ("trt", "trt_rtx") :
		with _plugin_cache_lock :
			if plugin_name not in _plugin_cache :
				_plugin_cache[plugin_name] = hasattr(core, plugin_name)
			available = _plugin_cache[plugin_name]
		if available :
			return plugin_name
	raise ModuleNotFoundError(f"模块 {func_name} 依赖错误：缺失插件，检查项目 trt 或 trt_rtx")

def _trt_plugin_dir(
	trt_ns : str,
) -> str :
	"""插件自身所在目录，模型统一放在其下的 models/ 里。

	API4 插件返回 str，旧版返回 bytes，这里统一成 str。
	"""
	plugin_path = getattr(core, trt_ns).Version()["path"]
	if isinstance(plugin_path, bytes) :
		plugin_path = plugin_path.decode()
	return os.path.dirname(plugin_path)

def _trt_backend(
	vsmlrt : typing.Any,
	trt_ns : str,
	**kwargs,
) -> typing.Any :
	"""按可用命名空间构造 TensorRT 后端。

	TRT_RTX 是另一套 builder：只认自己 dataclass 里的字段，
	fp16 用 fp16 开关表达，输出精度由引擎自身决定。
	"""
	if trt_ns == "trt" :
		return vsmlrt.BackendV2.TRT(**kwargs)
	fields = getattr(vsmlrt.Backend.TRT_RTX, "__dataclass_fields__", {})
	mapped = {key: value for key, value in kwargs.items() if key in fields}
	if "force_fp16" in kwargs :
		mapped["fp16"] = bool(kwargs["force_fp16"])
	# TensorRT-RTX 的动态形状引擎会在 cuDNN graph 编译处失败（cugan 等网络实测），
	# 统一退化成静态引擎：由 vsmlrt 按当前 clip 尺寸建引擎，代价是换分辨率要重建。
	for key in ("min_shapes", "opt_shapes", "max_shapes") :
		mapped.pop(key, None)
	mapped["static_shape"] = True
	return vsmlrt.BackendV2.TRT_RTX(**mapped)

def _check_script(
	func_name : str,
	script_name : str,
	min_version : typing.Optional[str] = None,
) -> typing.Any :
	"""检查并导入外部脚本"""
	script_var = globals().get(script_name)
	if script_var is None :
		try :
			script_var = __import__(script_name)
			globals()[script_name] = script_var
		except ImportError :
			raise ImportError(f"模块 {func_name} 依赖错误：缺失库 {script_name}")
	if min_version is not None :
		if LooseVersion(script_var.__version__) < LooseVersion(min_version) :
			raise ImportError(f"模块 {func_name} 依赖错误：缺失脚本 {script_name} 的版本号过低，至少 {min_version}")
	return script_var
