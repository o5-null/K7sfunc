--[[
k7f_multi.lua —— 通用插帧倍数：一处设置，对所有补帧滤镜生效，自动落盘

菜单在 menu.conf 的 `VF 滤镜 (&F) > 倍数 (&M)`（由 manifests/config.yml 生成），
命令是 `script-message-to k7f_multi set N` / `clear`。

做的事：
1. 值存到 `~~/_cache/k7f_multi.json`（{"multi": N}），换片、重启 mpv 都沿用；
2. 选好之后立刻改写当前 vf 链：
   - `vapoursynth` 滤镜写进 user-data（`multi=N`，其它键原样保留），vpy 侧从
     `user_data` 里读得到；
   - `playkit` 原生滤镜改写 `multi` 参数；
3. 之后任何预设套用（vf 变化）都会被观察者补上同一个值，所以“先选倍数再换预设”
   和“先换预设再选倍数”结果一致；
4. 各引擎有自己的上限（XeSS-FG 3× 及以上会触发引擎致命错误 0xe0d55001，FSR4 只
   内插一对的中点），超上限时按上限钳制并在 OSD 里说明。

注意：改倍数 = 整条滤镜重建（引擎重新初始化），不是逐帧实时生效。

原生滤镜（vf_playkit）也用这里设定的倍数：菜单里只有一个条目，命令把 vf 规格发过来
（`script-message-to k7f_multi native playkit:dll=…:proxy_dir=…`），由本脚本在创建滤镜时
就把 multi 写进去——一次初始化，不会先建 6× 再改成目标值。没有设定过倍数时原生滤镜按
2×（vf_playkit 自己的默认是 6×，太激进）。

脚本消息：
  script-message-to k7f_multi set 4
  script-message-to k7f_multi clear
  script-message-to k7f_multi status
  script-message-to k7f_multi native playkit:dll=vs-plugins/vdlssg.dll:proxy_dir=vs-plugins/dlss45
]]

local mp = require 'mp'
local msg = require 'mp.msg'

-- k7f_bridge.lua 与本文件同目录；require 失败时按脚本目录、再按 ~~/scripts 回退
-- （与 k7f_vpypar.lua 的加载方式保持一致）
local function load_bridge()
	local ok, mod = pcall(require, 'k7f_bridge')
	if ok and type(mod) == 'table' then
		return mod
	end

	local source = debug.getinfo(1, 'S').source or ''
	local here = source:match('^@(.*[/\\])') or ''
	local candidates = {
		here .. 'k7f_bridge.lua',
		mp.command_native({ 'expand-path', '~~/scripts/k7f_bridge.lua' }),
	}
	for _, path in ipairs(candidates) do
		local chunk = path and loadfile(path)
		if chunk then
			local loaded = chunk()
			if type(loaded) == 'table' then
				return loaded
			end
		end
	end

	error('k7f_multi: 找不到 k7f_bridge.lua（与 k7f_multi.lua 放同一目录）')
end

local B = load_bridge()

-- 状态文件：与 k7f_vpypar 的 ~~/_cache/ 一致，不进仓库、不随包分发
local STATE_FILE = '~~/_cache/k7f_multi.json'

local MIN_MULTI, MAX_MULTI = 2, 6

-- 每个 vpy 能吃多少倍。表里没有的按 MAX_MULTI 处理：vpy 不认 multi 时只是多一个
-- 被忽略的 user-data 键，无害；以后哪个脚本开始认 multi，这里补一行上限即可。
local CAPS = {
	['MEMC_DLSSG_NV.vpy'] = 6,
	['MEMC_VFG_NV.vpy'] = 6,
	-- 引擎 sm86 的多帧生成（≥3×）有索引配对 bug，会直接结束进程
	['MEMC_XESSFG_NV.vpy'] = 2,
	-- FSR4 引擎只内插一对相邻帧的中点
	['MEMC_FSR4.vpy'] = 2,
	-- aji 的 rife_factor 只吃整数倍（vpy 侧 FRAMEGEN_VPY 会按 multi 取整倍数）
	['MEMC_RIFE_AJI.vpy'] = 6,
}

-- 原生 playkit 滤镜的 multi 上限（plugin 侧 engineMaxMulti(Dlss) = 6）
local PLAYKIT_CAP = 6
-- 原生滤镜没有"不指定"这个状态（multi 是 vf_playkit 的必填参数，默认 6×）：
-- 没设过全局倍数时按 2×，避免"什么都没选却跑 6 倍"。
local NATIVE_DEFAULT = 2

-- 当前设定；nil = 不干预，各脚本用自己的默认值
local multi = nil
-- 我们自己正在写 vf：观察者要忽略这次变化，避免反复重建
local applying = false

local function state_path()
	return B.expand(STATE_FILE)
end

local function clamp(value)
	return math.max(MIN_MULTI, math.min(MAX_MULTI, math.floor(value)))
end

local function load_state()
	local path = state_path()
	local data, err = B.read_json(path)
	local value = data and tonumber(data.multi)
	if value then
		multi = clamp(value)
	elseif err == '解析失败' or err == '文件为空' then
		-- 文件在、但读不出来：别静默退回默认倍速，不然“设了 5 倍却是 3 倍”查不到原因
		msg.warn(('倍数设置文件无法读取，已按默认处理：%s（%s）'):format(tostring(path), tostring(err)))
	end
end

local function save_state()
	local path = state_path()
	if not path then
		msg.warn('倍数设置存不下来：拿不到状态文件路径（本次仅内存生效）')
		return
	end

	-- 没有设定 = 删掉记录，下次启动回到各脚本默认
	if not multi then
		local fh = io.open(path, 'r')
		if not fh then
			return
		end
		fh:close()
		local ok, err = os.remove(path)
		if not ok then
			msg.warn(('删不掉旧倍数设置 %s: %s'):format(path, tostring(err)))
		end
		return
	end

	local ok, err = B.write_json(path, { multi = multi })
	if not ok then
		msg.warn('写不进状态文件 ' .. path .. ': ' .. tostring(err))
	end
end

-- vpy 的能力上限：按 vf 参数里 file 的文件名查表。
-- 叠层脚本（MIX_AJI.vpy）按它 user-data 里的补帧引擎取上限。
local function vpy_cap(script, ud_text)
	local name = tostring(script or ''):gsub('\\', '/'):match('([^/]+)$') or ''
	if name == 'MIX_AJI.vpy' then
		local fg = B.parse_user_data(ud_text or '').framegen
		if fg == 'vxessfg' or fg == 'vfsr4' then return 2 end
		return CAPS['MEMC_RIFE_AJI.vpy']
	end
	return CAPS[name] or MAX_MULTI
end

-- 在 user-data 里写入/去掉 multi，其它键保留；multi 固定放最前，结果顺序稳定，
-- 同一份设置重复套用不会因为顺序变化而反复重建滤镜
local function with_multi(text, value)
	local opts = B.parse_user_data(text)
	opts.multi = value and tostring(value) or nil

	local order = {}
	if opts.multi then
		order[#order + 1] = 'multi'
	end
	local rest = {}
	for key in pairs(opts) do
		if key ~= 'multi' then
			rest[#rest + 1] = key
		end
	end
	table.sort(rest)
	for _, key in ipairs(rest) do
		order[#order + 1] = key
	end

	return B.build_user_data(opts, order)
end

-- 就地改写一条 vf 链；返回是否真的改了什么（没改动就不用重设，避免白重建）
local function rewrite(chain)
	local changed = false

	for _, filter in ipairs(chain) do
		local params = filter.params
		if type(params) == 'table' then
			if filter.name == 'vapoursynth' then
				local value = multi
				local old = params['user-data'] or ''
				if value then
					value = math.min(value, vpy_cap(params.file, old))
				end
				local new = with_multi(old, value)
				if new ~= old then
					params['user-data'] = (new ~= '' and new or nil)
					changed = true
				end
			elseif filter.name == 'playkit' then
				-- 原生滤镜不认"不指定"：multi 是必填参数，缺省时 vf_playkit 自己会给 6×。
				-- 所以这里始终写一个值，没设过全局倍数就按 NATIVE_DEFAULT（2×）。
				local value = math.min(multi or NATIVE_DEFAULT, PLAYKIT_CAP)
				if tonumber(params.multi) ~= value then
					params.multi = value
					changed = true
				end
			end
		end
	end

	return changed
end

local function push(chain)
	applying = true
	local ok, err = pcall(mp.set_property_native, 'vf', chain)
	applying = false
	if not ok then
		msg.warn('改写 vf 失败: ' .. tostring(err))
		return false
	end

	-- 复核要等一拍：mpv 的属性写入是排到播放循环里生效的，立刻读回来可能还是旧链。
	-- 只判断“链上根本没有 multi 这个键”，不比对具体值：另一个脚本紧接着重建链表是常事，
	-- 值不同不代表这次没写成（值域/写入顺序由各脚本自己的观者兜）
	mp.add_timeout(0.5, function()
		if not multi then return end
		local after = mp.get_property_native('vf')
		if type(after) ~= 'table' then return end
		local missing = false
		for _, f in ipairs(after) do
			if f.name == 'vapoursynth' and type(f.params) == 'table'
				and B.parse_user_data(f.params['user-data'] or '').multi == nil then
				missing = true
			end
		end
		if missing then
			msg.warn('倍数没能在滤镜链里生效（脚本冲突或链被反复重建），当前可能仍是脚本默认值')
		end
	end)
	return true
end

-- 把当前设定套到正在跑的滤镜上（clear 时即去掉注入的键）
local function apply_now()
	local chain = mp.get_property_native('vf')
	if type(chain) ~= 'table' then
		return false
	end
	if not rewrite(chain) then
		return false
	end
	return push(chain)
end

-- vpy 报回来的实际倍数（上行 sidecar）用来在 OSD 里显示“实际”值
local function describe()
	if not multi then
		return ('倍数：不指定（vpy 用各自默认，原生滤镜 %d×）'):format(NATIVE_DEFAULT)
	end
	return ('倍数：%d×（已保存）'):format(multi)
end

mp.register_script_message('set', function(text)
	local value = tonumber(text)
	if not value then
		mp.osd_message('倍数：无效参数 ' .. tostring(text), 2)
		return
	end

	multi = clamp(value)
	save_state()
	apply_now()
	mp.osd_message(describe(), 2)
end)

mp.register_script_message('clear', function()
	multi = nil
	save_state()
	apply_now()
	mp.osd_message(describe(), 2)
end)

mp.register_script_message('status', function()
	mp.osd_message(describe(), 3)
end)

-- 原生滤镜的套用：菜单命令把 vf 规格（playkit:param=value:…）发过来，这里连同当前
-- 倍数一次写进 `vf set`，所以引擎只初始化一次。规格里不带 multi（带了也以这里为准）。
mp.register_script_message('native', function(spec)
	if type(spec) ~= 'string' or spec == '' then
		mp.osd_message('原生滤镜：缺少 vf 规格', 2)
		return
	end

	local value = math.min(multi or NATIVE_DEFAULT, PLAYKIT_CAP)
	local name, rest = spec:match('^([^:]+):(.+)$')
	local with_multi
	if name then
		with_multi = ('%s:multi=%d:%s'):format(name, value, rest)
	else
		-- 只有一个滤镜名，没有参数
		with_multi = ('%s:multi=%d'):format(spec, value)
	end

	-- 与 push() 同样的守卫：观察者不要把我们这次的写入再套一遍
	applying = true
	local ok, err = pcall(mp.command, 'vf set ' .. with_multi)
	applying = false
	if not ok then
		msg.warn('原生滤镜套用失败: ' .. tostring(err))
		mp.osd_message('原生滤镜：套用失败（见日志）', 2)
		return
	end
	mp.osd_message(('原生滤镜：%d×'):format(value), 2)
end)

-- 预设套用时补上同一个值：menu.conf / 键位都是 `vf set vapoursynth=…`，
-- 命令本身不带倍数，由这里统一注入
mp.observe_property('vf', 'native', function(_, chain)
	if applying or not multi or type(chain) ~= 'table' then
		return
	end
	if rewrite(chain) then
		push(chain)
	end
end)

load_state()
if multi then
	msg.info(('启动时恢复倍数设定 %d×'):format(multi))
end
