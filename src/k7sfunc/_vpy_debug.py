"""_vpy_debug —— vpy 宿主层调试埋点（本 fork 专属）

只在 `log` 打开时由 `mod_vpy.FRAMEGEN_VPY()` 调用；正常播放（log 关）时本模块
一行都不会执行，补帧图也不会多套节点。

两块：

- `Stages`：构图阶段计时（参数解析 / 预缩 / 插件加载 / 建帧生成节点 / 上报），
  `mark()` 记录距上一个标记的毫秒数，结束时拼成一行。
- `attach_frame_probe()`：逐帧时间线。在补帧节点外面套两层 `std.FrameEval`：
  内层不带 `prop_src`，回调在帧请求刚进来时触发；外层把内层当 `prop_src`，
  回调在帧就绪之后触发。两者之差就是这一帧在 VS 内部的实际延迟，同时还能
  读到在飞帧数（inflight）和参与的线程号。

  这是判断"VS 路径是否靠并发掩盖 GPU 等待"的直接证据：inflight 长期 >1、
  而单帧延迟接近每对帧的引擎耗时，说明一次同步在等下一对的时候已经有别的帧
  在跑。原生 `vf_playkit` 的串行结构做不到这一点。

输出统一走 stderr，带 `[vpy-debug]` 前缀，与插件的 `[vdlssg] …` 探针同一条路，
`2> 文件` 就能单独收走，不会和 mpv 自己的日志（stdout）搅在一起。

代价（只影响 log 打开的排查场景）：`FrameEval` 会按帧号缓存返回的节点，
长片一直开着会让内存慢慢涨；另外每个输出帧多两次空回调。所以这是开关，
不是常驻功能。
"""

import sys
import threading
import time
from collections import deque

## 输出前缀：与 [vpy] / [playkit] 区分开，便于 grep 与 2> 收集
_PREFIX = "[vpy-debug]"

## 逐帧埋点的输出预算：前 _BURST 帧每帧一行，之后每 _STRIDE 帧一行，
## 每 _SUMMARY 帧再打一行汇总（延迟分位数、最大在飞帧数、线程构成）
_BURST = 90
_STRIDE = 30
_SUMMARY = 240

## 延迟样本窗口：汇总里的分位数只统计最近这么多个样本，长片也不占内存
_WINDOW = 4096

__all__ = [
	"dbg",
	"Stages",
	"attach_frame_probe",
]


def dbg(message : str) -> None :
	"""一行调试输出：stderr + flush（和插件的探针同一条流）。"""

	print(f"{_PREFIX} {message}", file = sys.stderr, flush = True)


def _ms(seconds : float) -> float :
	return seconds * 1000.0


def _pct(samples, p : float) -> float :
	"""分位数；样本为空时返回 0。"""

	if not samples :
		return 0.0
	data = sorted(samples)
	index = int(round(p / 100.0 * (len(data) - 1)))
	return data[max(0, min(index, len(data) - 1))]


class Stages :
	"""构图阶段计时：每次 `mark()` 记下与上一个标记之间的耗时（毫秒）。"""

	def __init__(self) :
		self._start = time.perf_counter()
		self._last = self._start
		self._parts = []

	def mark(self, name : str) -> None :
		now = time.perf_counter()
		self._parts.append((name, _ms(now - self._last)))
		self._last = now

	def report(self) -> str :
		"""一行拼好，例如 `resolve=0.4 resize=0.0 framegen=812.5 total=816.3ms`。"""

		total = _ms(time.perf_counter() - self._start)
		parts = " ".join(f"{name}={value:.1f}" for name, value in self._parts)
		return f"{parts} total={total:.1f}ms"


class _Timeline :
	"""逐帧时间线：把"请求进入"和"帧就绪"按帧号配对，统计延迟与在飞帧数。"""

	def __init__(self, tag : str) :
		self.tag = tag
		self._lock = threading.Lock()
		self._start = time.perf_counter()
		self._pending = {}          # 帧号 -> (进入时刻, 进入线程号)
		self._frames = 0
		self._max_inflight = 0
		self._latency = deque(maxlen = _WINDOW)
		self._threads = {}          # 线程号 -> 完成的帧数
		self._last_ready = None

	def enter(self, n : int) -> None :
		now = time.perf_counter()
		with self._lock :
			self._pending[n] = (now, threading.get_ident())
			inflight = len(self._pending)
			if inflight > self._max_inflight :
				self._max_inflight = inflight

	def leave(self, n : int) -> None :
		"""帧就绪：配对进入时刻，算出这一帧的延迟；输出放在锁外做。"""

		now = time.perf_counter()
		tid = threading.get_ident()
		with self._lock :
			entry = self._pending.pop(n, None)
			if entry is None :
				return              # 埋点接上之前就在图里的帧，配不上，跳过
			entered, _ = entry
			latency = _ms(now - entered)
			gap = 0.0 if self._last_ready is None else _ms(now - self._last_ready)
			self._last_ready = now
			self._frames += 1
			self._latency.append(latency)
			self._threads[tid] = self._threads.get(tid, 0) + 1
			frames = self._frames
			# 这一帧结束时仍在飞的帧数（加它自己）；≥2 说明这一帧确实和别的帧并行，
			# 是"VS 侧靠并发掩盖等待"的直接证据
			inflight = len(self._pending) + 1
			elapsed = _ms(now - self._start)
			samples = list(self._latency)
			threads = dict(self._threads)
			max_inflight = self._max_inflight
			print_line = frames <= _BURST or frames % _STRIDE == 0
			print_summary = frames % _SUMMARY == 0

		if print_line :
			dbg(f"probe[{self.tag}] 帧={n} t={elapsed:.1f}ms 延迟={latency:.2f}ms "
				f"间隔={gap:.2f}ms 同飞={inflight} 线程={tid}")
		if print_summary :
			dbg(_Timeline._summary(self.tag, frames, samples, threads, max_inflight, elapsed))

	@staticmethod
	def _summary(tag, frames, samples, threads, max_inflight, elapsed) -> str :
		top = sorted(threads.items(), key = lambda kv : kv[1], reverse = True)[:3]
		composition = ",".join(f"{tid}:{count}" for tid, count in top)
		if len(threads) > len(top) :
			composition += ",…"
		average = sum(samples) / len(samples) if samples else 0.0
		return (f"probe[{tag}] 汇总 {elapsed / 1000.0:.2f}s "
			f"帧={frames} 平均={average:.2f}ms "
			f"p50={_pct(samples, 50):.2f} p90={_pct(samples, 90):.2f} "
			f"最大在飞={max_inflight} 线程={len(threads)} ({composition})")


def attach_frame_probe(core, node, tag : str = "") :
	"""给 `node` 套一层逐帧埋点，返回内容相同的节点；只在 log 打开时调用。

	内层 `FrameEval` 记请求时刻，外层以它为 `prop_src` 记就绪时刻；两层回调都
	直接把输入节点交回去，不经 Python 碰像素、也不拷贝帧，所以只多两次空回调。
	"""

	timeline = _Timeline(tag)

	# 回调签名：不带 prop_src 时 VS 只传帧号，带上 prop_src 时再传帧本身；
	# 这里统一写成带默认值的形式，两种调用方式都能接住
	def _enter(n, f = None) :
		try :
			timeline.enter(n)
		except Exception :
			pass              # 埋点绝不能把播放搞崩
		return node

	entered = core.std.FrameEval(node, _enter)

	def _leave(n, f = None) :
		try :
			timeline.leave(n)
		except Exception :
			pass
		return node

	return core.std.FrameEval(entered, _leave, prop_src = [entered])
