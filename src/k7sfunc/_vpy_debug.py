"""_vpy_debug —— vpy 宿主层调试埋点（本 fork 专属）

由 `mod_vpy.FRAMEGEN_VPY()` 在 `log`（stderr 打点）或 `stats`（OSD 耗时行）
打开时调用；两者都关时本模块一行都不会执行，补帧图也不会多套节点。

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

	def _total(self) -> float :
		"""构图总耗时 = 各阶段之和。

		不能用 `perf_counter() - _start`：构图完成后 `Stages` 还会被
		`_make_stats_writer` 反复取快照（OSD 每 0.5s 一次），墙钟会一直涨，
		「合计」就会随播放时间虚增。
		"""

		return sum(value for _, value in self._parts)

	def report(self) -> str :
		"""一行拼好，例如 `resolve=0.4 resize=0.0 framegen=812.5 total=816.3ms`。"""

		parts = " ".join(f"{name}={value:.1f}" for name, value in self._parts)
		return f"{parts} total={self._total():.1f}ms"

	def snapshot(self) -> dict :
		"""结构化版本（给 OSD/JSON 用）：各阶段毫秒数 + total。"""

		return {
			"parts" : {name : round(value, 1) for name, value in self._parts},
			"total" : round(self._total(), 1),
		}


def _prop_value(props, key : str) :
	"""读帧属性；没有这个键、或读失败都返回 None（vs 的 FrameProps 支持 in/get）。"""

	if props is None :
		return None
	try :
		return props[key]
	except Exception :
		return None


def _cut_flag(props) -> bool :
	"""这一帧是不是补帧插件标的切镜帧（`_DLSSGSceneCut != 0`）。"""

	value = _prop_value(props, "_DLSSGSceneCut")
	try :
		return bool(int(value))
	except Exception :
		return False


class _Timeline :
	"""逐帧时间线：把"请求进入"和"帧就绪"按帧号配对，统计延迟与在飞帧数。"""

	def __init__(self, tag : str, on_ready = None) :
		self.tag = tag
		self._on_ready = on_ready
		self._lock = threading.Lock()
		self._start = time.perf_counter()
		self._pending = {}          # 帧号 -> (进入时刻, 进入线程号)
		self._frames = 0
		self._max_inflight = 0
		self._latency = deque(maxlen = _WINDOW)
		self._gaps = deque(maxlen = 60)      # 最近的就绪间隔，用来算实际吞吐
		self._threads = {}          # 线程号 -> 完成的帧数
		self._last_ready = None
		# 切镜：插件在输出帧上写 `_DLSSGSceneCut`（见 vs-dlssg/frame_gen.cpp 的
		# markFrame）。同一次切镜会连续标记同一组的几个中间帧（slot 1..multi-1），
		# 所以按组首帧计数，一次切镜只算一次。
		self._cuts = 0
		self._cut_at = None         # 最近一次切镜的就绪时刻（perf_counter）
		self._cut_frame = None      # 最近一次计数的帧号（没有 slot 属性时用来合并连续帧）

	def enter(self, n : int) -> None :
		now = time.perf_counter()
		with self._lock :
			self._pending[n] = (now, threading.get_ident())
			inflight = len(self._pending)
			if inflight > self._max_inflight :
				self._max_inflight = inflight

	def leave(self, n : int, props = None) -> None :
		"""帧就绪：配对进入时刻，算出这一帧的延迟；输出放在锁外做。

		`props` 是这一帧的帧属性（`frame.props`），带补帧插件的
		`_DLSSGSceneCut`/`_DLSSGMultiFrameIndex` 时顺便数切镜。
		"""

		cut = _cut_flag(props)
		slot = _prop_value(props, "_DLSSGMultiFrameIndex")
		now = time.perf_counter()
		tid = threading.get_ident()
		with self._lock :
			entry = self._pending.pop(n, None)
			if entry is None :
				return              # 埋点接上之前就在图里的帧，配不上，跳过
			entered, _ = entry
			latency = _ms(now - entered)
			has_prev = self._last_ready is not None
			gap = 0.0 if not has_prev else _ms(now - self._last_ready)
			self._last_ready = now
			self._frames += 1
			self._latency.append(latency)
			self._threads[tid] = self._threads.get(tid, 0) + 1
			if cut :
				self._note_cut(n, slot, now)
			frames = self._frames
			# 这一帧结束时仍在飞的帧数（加它自己）；≥2 说明这一帧确实和别的帧并行，
			# 是"VS 侧靠并发掩盖等待"的直接证据
			inflight = len(self._pending) + 1
			elapsed = _ms(now - self._start)
			if has_prev :
				self._gaps.append(gap)
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

		if self._on_ready is not None :
			try :
				self._on_ready(self.snapshot())
			except Exception :
				pass              # 统计输出同样不能影响播放

	def _note_cut(self, n : int, slot, now : float) -> None :
		"""记一次切镜。调用时已持有锁。

		插件对一次切镜会标记整组的中间帧（slot 1..multi-1），只数组首帧
		（slot 1）；没有 `_DLSSGMultiFrameIndex` 的引擎退化成"帧号不连续才算新一次"。
		"""

		if slot is not None and slot > 1 :
			return                  # 同一组的后续帧，算过组首了
		if slot is None :
			if self._cut_frame is not None and n - self._cut_frame <= 1 :
				# 没有 slot 说明不是 frame_gen 系插件，只能按帧号合并连续标记
				self._cut_frame = n
				return
		self._cuts += 1
		self._cut_at = now
		self._cut_frame = n

	def snapshot(self) -> dict :
		"""当前窗口的延迟统计（给 OSD 用）：帧数、平均/分位、在飞帧数、切镜数。"""

		with self._lock :
			samples = list(self._latency)
			gaps = list(self._gaps)
			frames = self._frames
			inflight = len(self._pending)
			max_inflight = self._max_inflight
			threads = len(self._threads)
			elapsed = _ms(time.perf_counter() - self._start)
			last_ready = self._last_ready
			cuts = self._cuts
			cut_at = self._cut_at

		average = sum(samples) / len(samples) if samples else 0.0
		# 吞吐用最近的帧间隔：总耗时/帧数会被暂停、缓冲的空档拖偏
		pace = (sum(gaps) / len(gaps)) if gaps else 0.0
		return {
			"frames" : frames,
			"elapsed" : round(elapsed / 1000.0, 2),
			"avg" : round(average, 2),
			"p50" : round(_pct(samples, 50), 2),
			"p90" : round(_pct(samples, 90), 2),
			"pace" : round(pace, 2),
			"fps" : round(1000.0 / pace, 1) if pace > 0 else 0.0,
			"inflight" : inflight,
			"max_inflight" : max_inflight,
			"threads" : threads,
			"last_ready" : None if last_ready is None else round(_ms(last_ready - self._start), 1),
			"cuts" : cuts,
			"last_cut" : None if cut_at is None else round(_ms(cut_at - self._start), 1),
		}

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


def attach_frame_probe(core, node, tag : str = "", on_ready = None) :
	"""给 `node` 套一层逐帧埋点，返回内容相同的节点；只在 log/stats 打开时调用。

	内层 `FrameEval` 记请求时刻，外层以它为 `prop_src` 记就绪时刻；两层回调都
	直接把输入节点交回去，不经 Python 碰像素、也不拷贝帧，所以只多两次空回调。

	`on_ready(snapshot)` 在每帧就绪后调用（频率等于输出帧率），调用方自己节流。
	"""

	timeline = _Timeline(tag, on_ready = on_ready)

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
			# 带 prop_src 时 VS 的 Python 绑定把补帧节点的输出帧直接传进来；
			# 老绑定给的是列表，两种都接住。它的 `_DLSSGSceneCut` 由插件写好
			if f is None :
				props = None
			elif hasattr(f, "props") :
				props = f.props
			else :
				props = f[0].props if len(f) > 0 else None
		except Exception :
			props = None
		try :
			timeline.leave(n, props)
		except Exception :
			pass
		return node

	return core.std.FrameEval(entered, _leave, prop_src = [entered])
