--[[
k7f_vpypar.lua —— 播放中用 OSD 菜单改 VapourSynth 补帧脚本的参数

原理：vpy 脚本本身读不到 mpv 的属性，但 `vf=vapoursynth` 有 user-data 子参数，
mpv 会把它作为全局变量 user_data 注入脚本（默认空串；mpv 每次 seek 都会重跑脚本，
所以跳转/重建后参数一定生效）。本脚本维护一份参数状态，改动后重建滤镜：

    vf append @<label>:vapoursynth="<脚本>":user-data="multi=4;scene=0.12;duplicate=0.005"

上行：vpy 把实际生效的参数写到脚本同目录的 `<脚本>.k7f.json`，这里用
`k7f_bridge.watch_status()` 轮询（默认 0.5s），OSD 可显示“设置”和“实际”两行。
下行字符串与上行文件格式由 k7f_bridge.lua / k7sfunc.mod_vpy 统一定义。

引擎识别：按**滤镜链里 vapoursynth 的脚本文件名**认引擎（MEMC_DLSSG_NV.vpy 等），
所以换预设后菜单会自动切到新引擎的参数表，不需要重配。每个引擎的键与范围必须和
k7sfunc.mod_vpy 的 FRAMEGEN_VPY_OPTIONS / _FRAMEGEN_VALUE_SPECS 一致：

  vdlssg  multi(2-6) preset scene duplicate pre_h gpu log stats
  vxessfg multi(2)   scene duplicate pre_h gpu log stats
  vvfg    multi(2-6) mode scene duplicate pre_h gpu log stats
  vfsr4   camera scene duplicate pre_h gpu log stats        （固定 2×，没有 multi）

“耗时显示”打开时 vpy 还会每 0.5s 写 `<脚本>.k7f-stats.json`（构图各阶段 + 逐帧延迟），
本脚本读它在 OSC 条上方画两行；关掉后文件不再更新，行也就隐藏了。

注意：改参 = 整条滤镜重建（引擎要重新初始化），不是逐帧实时；
multi 会改变输出帧数，本来也只能重建。multi 仍以 k7f_multi 为准：链上已有值时
本脚本只跟随，不反写，避免两边来回重建。

部署：
  1) k7f_bridge.lua 与本文件一起放到 portable_config\scripts\
  2) input.conf（或 input_uosc.conf）加一行，键自选：
       V  script-binding k7f_vpypar/menu
  3) 用 OSD 菜单改参；只有同名脚本的 vapoursynth 滤镜在跑时才真正重建，否则只存状态。

可选 script-opts（script-opts.conf）：
  script-opts-append = k7f_vpypar-script=~~/vs/MEMC_DLSSG_NV.vpy   （链上无补帧滤镜时的兜底）

脚本消息（给别的脚本/按键用）：
  script-message-to k7f_vpypar set multi 4
  script-message-to k7f_vpypar apply
  script-message-to k7f_vpypar status
]]

local mp = require 'mp'
local msg = require 'mp.msg'
local input = require 'mp.input'

-- mpv 在生产环境会把 ~~/scripts 加进 package.path；开发时脚本可能在任意目录，
-- 所以 require 失败后先找脚本同目录，再退回 ~~/scripts/
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
            if type(loaded) == 'table' then return loaded end
        end
    end

    error('k7f_vpypar: 找不到 k7f_bridge.lua（与 k7f_vpypar.lua 放同一目录）')
end

local B = load_bridge()

local o = {
    -- 兜底脚本：滤镜链上没有已知补帧滤镜时，OSD 改的是它的参数
    script = '~~/vs/MEMC_DLSSG_NV.vpy',
    -- 参数状态落盘位置，换片/重启 mpv 后沿用上次的选择（按脚本分桶存）
    state_file = '~~/_cache/k7f_vpypar.json',
    -- 上行报告轮询间隔（秒）；0.5s 够快，又不会明显增加 IO
    status_interval = 0.5,
}
require 'mp.options'.read_options(o, 'k7f_vpypar')

local function script_basename(path)
    local name = tostring(path or ''):gsub('\\', '/')
    return (name:match('[^/]+$') or ''):lower()
end

-- ##################################
-- 引擎表（键名/范围以 mod_vpy 为准）--
-- ##################################

-- 倍数的候选档位：只有一档的引擎（XeSS-FG / FSR4）标注“固定”，
-- 原先前端一律给 2-6，选大了要靠后端钳制，OSD 上会显示成“设置 5× / 实际 2×”
local function multi_choices(list)
    local out = {}
    for _, n in ipairs(list) do
        out[#out + 1] = {
            (#list == 1) and (n .. '×（固定）') or (n .. '×'),
            tostring(n),
        }
    end
    return out
end

local ENGINES = {
    ['memc_dlssg_nv.vpy'] = {
        title = 'DLSSG', engine = 'vdlssg',
        script = '~~/vs/MEMC_DLSSG_NV.vpy',
        multi = { 2, 3, 4, 5, 6 }, default_multi = '3',
        preset = true,
    },
    ['memc_xessfg_nv.vpy'] = {
        title = 'XeSS-FG', engine = 'vxessfg',
        script = '~~/vs/MEMC_XESSFG_NV.vpy',
        -- 引擎 sm86 的多帧生成（≥3×）会直接结束进程，候选里只留 2×
        multi = { 2 }, default_multi = '2',
    },
    ['memc_vfg_nv.vpy'] = {
        title = 'VFG', engine = 'vvfg',
        script = '~~/vs/MEMC_VFG_NV.vpy',
        multi = { 2, 3, 4, 5, 6 }, default_multi = '2',
        mode = true,
    },
    ['memc_fsr4.vpy'] = {
        title = 'FSR4', engine = 'vfsr4',
        script = '~~/vs/MEMC_FSR4.vpy',
        -- 固定 2×，vpy 侧不认 multi
        camera = true,
    },
}

-- 参数表：默认值与各 MEMC_*.vpy 头部的用户选项一致（改默认值要三处一起改：
-- 这里、mod_vpy 的 FRAMEGEN_ENGINES/字面量、vpy 用户选项）
local function build_params(def)
    local list = {}
    local function add(p) list[#list + 1] = p end

    if def.multi then
        add({
            key = 'multi', name = '倍数', default = def.default_multi,
            choices = multi_choices(def.multi),
        })
    end
    if def.mode then
        add({
            key = 'mode', name = '档位', default = '1',
            choices = { { '低', '0' }, { '中', '1' }, { '高', '2' } },
        })
    end
    if def.preset then
        add({
            key = 'preset', name = '模型预设', default = 'a',
            choices = { { '默认 a', 'a' }, { 'FG Lite c', 'c' } },
        })
    end
    add({
        key = 'scene', name = '切镜阈值', default = '0.12',
        choices = { { '关闭', '0' }, { '0.08', '0.08' }, { '0.12', '0.12' }, { '0.15', '0.15' }, { '0.2', '0.2' } },
    })
    add({
        key = 'duplicate', name = '重复帧阈值', default = '0.005',
        choices = { { '关闭', '0' }, { '0.003', '0.003' }, { '0.005', '0.005' }, { '0.01', '0.01' } },
    })
    if def.camera then
        -- mod_vpy 里 camera 是“相机运动速度（像素/帧）”，0 关闭
        add({
            key = 'camera', name = '相机运动', default = '0',
            choices = { { '关闭', '0' }, { '1', '1' }, { '2', '2' }, { '4', '4' } },
        })
    end
    add({
        key = 'pre_h', name = '预缩高度', default = '0',
        choices = { { '关', '0' }, { '720', '720' }, { '1080', '1080' }, { '1440', '1440' } },
    })
    add({
        key = 'gpu', name = '显卡', default = 'auto',
        choices = { { '自动', 'auto' }, { '0', '0' }, { '1', '1' }, { '2', '2' } },
    })
    add({
        key = 'log', name = '耗时日志', default = '0',
        choices = { { '关', '0' }, { '开', '1' } },
    })
    add({
        -- vpy 侧每 0.5s 写一份 <脚本>.k7f-stats.json，这里画在 OSC 上方
        key = 'stats', name = '耗时显示', default = '0',
        choices = { { '关', '0' }, { '开', '1' } },
    })

    return list
end

-- 每个引擎一份参数表（只算一次）
for _, def in pairs(ENGINES) do
    def.params = build_params(def)
end

-- ##########
-- 当前目标 --
-- ##########

-- 当前在改哪个引擎：链上检测到的优先，没有滤镜时用 script-opts 的兜底脚本
local def = nil          -- 当前引擎定义
local params = {}        -- 当前参数表
local order = {}         -- 写 user-data 的键序
local target_name = nil  -- 状态桶的键（脚本文件名，小写）
local target_script = nil -- 写进 vf / 找 sidecar 的脚本路径

local state = {}         -- [脚本名] = { key = 取值字符串 }

local function engine_name(path)
    local name = script_basename(path)
    return ENGINES[name] and name or nil
end

local function use_engine(name, script)
    def = ENGINES[name]
    params = def.params
    order = {}
    for i, p in ipairs(params) do
        order[i] = p.key
    end
    target_name = name
    target_script = script or def.script
end

-- 滤镜链上第一条已知补帧滤镜（多条时可执行串联只在极端情况下出现，取第一条）
local function detect_engine()
    local list = mp.get_property_native('vf') or {}
    for _, f in ipairs(list) do
        if f.name == 'vapoursynth' and f.params then
            local name = engine_name(f.params.file)
            if name then
                return name, f.params.file
            end
        end
    end
    return nil
end

-- 状态桶：按脚本分开存，换预设/换引擎不会互相覆盖
local function ensure_bucket(name)
    if state[name] then return state[name] end

    local bucket = {}
    for _, p in ipairs(ENGINES[name].params) do
        bucket[p.key] = p.default
    end
    state[name] = bucket
    return bucket
end

local function st()
    return ensure_bucket(target_name)
end

-- 最近一次 vpy 报告（上行）；nil 表示还没收到
local remote = nil
-- 最近一次重建的时间戳：只有重建后短时间内的报告才主动弹 OSD
local applied_at = 0
local APPLY_REPORT_WINDOW = 5

-- ##########
-- 状态读写 --
-- ##########

local function state_path()
    return B.expand(o.state_file)
end

local function valid_value(p, val)
    for _, c in ipairs(p.choices) do
        if c[2] == val then return true end
    end
    return false
end

local function find_param(key)
    for _, p in ipairs(params) do
        if p.key == key then return p end
    end
    return nil
end

-- 把落盘值归一成字符串再校验；旧文件里可能有数字/布尔
local function normalize(p, val)
    if type(val) == 'number' then
        val = tostring(val)
    elseif type(val) == 'boolean' then
        val = val and '1' or '0'
    end
    return valid_value(p, val) and val or p.default
end

local function load_state()
    local saved = B.read_json(state_path())

    -- 兼容旧版逐行 k=v 的状态文件（那时没有 utils.format_json）
    local text
    if not saved then
        text = B.read_text(state_path())
        if text and text ~= '' then
            saved = {}
            for k, v in text:gmatch('([%w_]+)%s*=%s*([^%s]+)') do
                saved[k] = v
            end
        end
    end
    -- 文件在、两种格式都读不出来：别静默退回默认值，不然“改过的参数又变回去了”查不到原因
    if not saved and text and text ~= '' then
        msg.warn('参数状态文件读不出来，已按默认值处理：' .. tostring(state_path()))
    end
    saved = saved or {}

    -- 旧格式是平的 {multi=…, scene=…}：归到 script-opts 的兜底脚本名下。
    -- 只要出现任一已知脚本名，就按新格式（按脚本分桶）处理。
    local legacy = saved.multi ~= nil
    for name in pairs(ENGINES) do
        if type(saved[name]) == 'table' then legacy = false end
    end

    local function fill(name, values)
        local bucket = ensure_bucket(name)
        for _, p in ipairs(ENGINES[name].params) do
            if values[p.key] ~= nil then
                bucket[p.key] = normalize(p, values[p.key])
            end
        end
    end

    if legacy then
        fill(engine_name(o.script) or 'memc_dlssg_nv.vpy', saved)
    else
        for name, values in pairs(saved) do
            if ENGINES[name] and type(values) == 'table' then
                fill(name, values)
            end
        end
    end
end

local function save_state()
    local ok, err = B.write_json(state_path(), state)
    if not ok then
        msg.warn('状态写入失败：' .. tostring(state_path()) .. '（' .. tostring(err) .. '，本次仅内存生效）')
    end
end

local function value_label(p)
    for _, c in ipairs(p.choices) do
        local v = st()[p.key]
        if c[2] == v then return c[1] end
    end
    return st()[p.key] or '?'
end

local function describe()
    local out = {}
    for _, p in ipairs(params) do
        out[#out + 1] = p.name .. ' ' .. value_label(p)
    end
    return table.concat(out, ' · ')
end

-- ############################
-- 组装 user-data 并重建滤镜 --
-- ############################

local function build_user_data()
    return B.build_user_data(st(), order)
end

-- 两个 vpy 路径是不是同一个脚本：只比文件名（链里存的是绝对路径，配置里可能是 ~~/）
local function file_matches(a, b)
    if not a or not b then return false end
    return script_basename(a) == script_basename(b)
end

-- 收集**所有**指向本引擎脚本的条目：标签相同的，或文件名对得上的无标签 vapoursynth。
-- 正常情况下只有一条；多于一条说明之前有次移除没匹配上（见 apply 里的说明），
-- 那会让补帧滤镜串联（两条 ×5 = 25 倍，OSD 上就是 600fps），必须合并掉。
-- 旧版本按 o.label（默认 VDLSSG）认滤镜；现在按脚本名，标签只是次要线索
local function label_matches(label)
    return tostring(label):upper() == (def and def.title or ''):upper()
end

local function is_ours(f)
    -- 只认 vapoursynth：别的滤镜恰好用了同名标签时不能误伤
    if f.name ~= 'vapoursynth' then
        return false
    end
    -- 链上的条目一定带 params.file，优先按脚本文件名认
    if f.params and f.params.file then
        return file_matches(f.params.file, target_script)
    end
    -- 参数缺失（理论上不该发生）时才退回标签
    return f.label ~= nil and label_matches(f.label)
end

local function find_entries()
    local list = mp.get_property_native('vf') or {}
    local out = {}
    for i, f in ipairs(list) do
        if is_ours(f) then
            out[#out + 1] = { index = i, filter = f }
        end
    end
    return list, out
end

-- user-data 是 "k=v;k=v" 串：键序不同、内容相同的两份设置视为相同。
-- k7f_multi 会按自己的键序改写这条串，若这里只比字符串就会把“同样设置”当成变化，
-- 无谓地重建滤镜（每次都重初始化引擎）；两边都在写时还会来回打架。
local function same_user_data(a, b)
    if a == b then return true end
    local pa = B.parse_user_data(a or '')
    local pb = B.parse_user_data(b or '')
    local na, nb = 0, 0
    for _ in pairs(pa) do na = na + 1 end
    for _ in pairs(pb) do nb = nb + 1 end
    if na ~= nb then return false end
    for k, v in pairs(pa) do
        if tostring(pb[k]) ~= tostring(v) then return false end
    end
    return true
end

-- 正在重建时忽略 vf 属性回调，避免自己触发自己
local applying = false

-- ~~/ 在 --no-config 或未设配置目录时会展开成相对路径，这时保留原串交给 mpv
local function resolve_path(p)
    local ex = mp.command_native({ 'expand-path', p })
    if ex and (ex:match('^%a:[/\\]') or ex:sub(1, 1) == '/' or ex:match('^\\\\')) then
        return ex
    end
    return p
end

local function apply(silent)
    save_state()

    -- 设置变了，旧报告不再对应当前选择；等 vpy 重建后写新报告
    remote = nil

    local list, entries = find_entries()
    local f = entries[1] and entries[1].filter
    if not f or f.name ~= 'vapoursynth' then
        if not silent then
            mp.osd_message(('参数已保存（%s 滤镜未启用）：%s'):format(def.title, describe()), 3)
        end
        return false
    end

    -- script 可能写成 ~~/vs/…：能解析成绝对路径就用绝对路径
    local script = resolve_path(target_script)
    local ud = build_user_data()

    -- 整链重建：剔掉所有指向本脚本的旧条目，末尾放一条带标签的新条目。
    -- 不用 `vf remove <spec>`：它按 name+全部参数做内容匹配，条目只要被别的脚本
    -- 加过 user-data 就匹配不上，而 mpv 对“没找到”是静默成功——旧条目留在链上，
    -- 新条目 append 上去就变成两条补帧串联（倍数相乘，×5 变 25 倍 / 600fps）。
    local chain = {}
    for _, filter in ipairs(list) do
        if not is_ours(filter) then
            chain[#chain + 1] = filter
        end
    end
    local dropped = #list - #chain
    chain[#chain + 1] = {
        name = 'vapoursynth',
        label = def.title,
        enabled = true,
        params = { file = script, ['user-data'] = ud },
    }

    applying = true
    local ok, err = pcall(mp.set_property_native, 'vf', chain)
    applying = false
    if not ok then
        local text = '重建滤镜失败：' .. tostring(err)
        msg.error(text)
        mp.osd_message('重建滤镜失败，见控制台日志', 3)
        return false
    end

    -- 兜底复核：mpv 里“命令没生效”常常也是成功返回，改完链后自己数一遍
    local _, after = find_entries()
    if #after ~= 1 then
        local text = ('滤镜链里仍有 %d 条 %s（应为 1 条）'):format(#after, def.title)
        msg.error(text)
        mp.osd_message(text, 3)
        return false
    end
    if dropped > 1 then
        local text = ('清理了 %d 条重复的 %s 滤镜（多条补帧会叠乘倍数）'):format(dropped - 1, def.title)
        msg.warn(text)
        mp.osd_message(text, 3)
    end

    applied_at = os.time()
    if not silent then
        mp.osd_message(('已应用：%s'):format(describe()), 2)
    end
    return true
end

-- ###############
-- 上行报告监看 --
-- ###############

local watcher = nil

local function start_watch()
    if watcher then
        watcher:kill()
        watcher = nil
    end

    watcher = B.watch_status(target_script, o.status_interval, function(data)
        remote = data

        -- 只在刚重建后主动回显一次“实际参数”；平时不打扰，避免 seek 后反复弹
        if data and os.time() - applied_at <= APPLY_REPORT_WINDOW then
            local line = B.format_status(data, nil, true)
            if line then
                mp.osd_message('实际：' .. line, 2)
            end
        end
    end)
end

local function status_text()
    local lines = { ('%s 设置：%s'):format(def.title, describe()) }
    if remote then
        local actual = B.format_status(remote, nil, true)
        if actual then
            lines[#lines + 1] = '实际：' .. actual
        end
    end
    return table.concat(lines, '\n')
end

-- ##################
-- 耗时行（OSD 常驻）--
-- ##################

-- vpy 在“耗时显示”打开时按 STATS_INTERVAL（0.5s）写这个文件：
--   <脚本>.k7f-stats.json = {ts, engine, title, multi, input, output, stages, live}
-- stages 是构图各阶段耗时（一次性），live 是逐帧延迟统计（滚动窗口）。
-- 这里只负责读文件、排版、钉在 OSC 条上方；没有独立开关时完全不动文件。
local STATS_SUFFIX = '.k7f-stats.json'
-- 报告超过这么久没更新就认为补帧停了（暂停时保留最后一次，不闪）
local STATS_STALE = 3
local STATS_STAGE_LABELS = {
    resolve = '解析', resize = '预缩', load = '加载', prep = '准备',
    framegen = '建引擎', fps = '帧率', publish = '上报', probe = '探针',
}
local STATS_STAGE_ORDER = { 'resolve', 'resize', 'load', 'prep', 'framegen', 'fps', 'publish', 'probe' }

local stats_timer = nil
local stats_overlay = nil
local stats_shown = false
local stats_geo = { w = 0, h = 0, bottom = 0 }

-- OSD 坐标系与 uosc 的底栏高度：uosc 把底栏占比写到 user-data/osc/margins
local function stats_geometry()
    local dims = mp.get_property_native('osd-dimensions') or {}
    local margins = mp.get_property_native('user-data/osc/margins') or {}
    stats_geo.w = tonumber(dims.w) or 0
    stats_geo.h = tonumber(dims.h) or 0
    stats_geo.bottom = tonumber(margins.b) or 0
    return stats_geo.w > 0 and stats_geo.h > 0
end

local function stats_enabled()
    return st().stats == '1'
end

local function stats_text(data)
    local lines = {}
    local live = data.live
    if live and (tonumber(live.frames) or 0) > 0 then
        lines[#lines + 1] = ('%s %s× · 单帧 %.2fms p90 %.2f · 同飞 %d · %dfps'):format(
            data.title or def.title,
            tostring(data.multi or '?'),
            tonumber(live.avg) or 0,
            tonumber(live.p90) or 0,
            tonumber(live.inflight) or 0,
            math.floor((tonumber(live.fps) or 0) + 0.5))
    end
    local stages = data.stages
    if stages and stages.parts then
        local parts = {}
        for _, key in ipairs(STATS_STAGE_ORDER) do
            local value = tonumber(stages.parts[key])
            if value then
                parts[#parts + 1] = ('%s %.1f'):format(STATS_STAGE_LABELS[key] or key, value)
            end
        end
        if #parts > 0 then
            lines[#lines + 1] = ('构图 %s ms（合计 %.0f）'):format(
                table.concat(parts, ' · '), tonumber(stages.total) or 0)
        end
    end
    if #lines == 0 then return nil end
    return table.concat(lines, '\\N')
end

local function stats_hide()
    if stats_overlay and stats_shown then
        stats_overlay:remove()
        stats_shown = false
    end
end

local function stats_show(text)
    if not stats_geometry() then return end
    if not stats_overlay then
        stats_overlay = mp.create_osd_overlay('ass-events')
    end
    local w, h = stats_geo.w, stats_geo.h
    -- 底栏占比是相对整屏的：文字底边放在底栏上沿再抬一点
    local base = h - math.floor(h * stats_geo.bottom) - math.floor(h * 0.010)
    local size = math.max(16, math.floor(h * 0.020))
    stats_overlay.res_x = w
    stats_overlay.res_y = h
    stats_overlay.data = ('{\\an2\\pos(%d,%d)\\fs%d\\bord2\\shad0\\1c&HFFFFFF&\\3c&H000000&}%s')
        :format(math.floor(w / 2), base, size, text)
    stats_overlay:update()
    stats_shown = true
end

local function refresh_stats()
    if not stats_enabled() then
        stats_hide()
        return
    end

    local data = B.read_json(resolve_path(target_script) .. STATS_SUFFIX)
    if not data or tostring(data.engine or '') ~= (def and def.engine or '') then
        stats_hide()
        return
    end

    -- 暂停时保留最后一次统计（没有新帧不等于没有数据）；播放中过期说明补帧停了
    local stale = os.time() - (tonumber(data.ts) or 0) > STATS_STALE
    if stale and not mp.get_property_native('pause') then
        stats_hide()
        return
    end

    local text = stats_text(data)
    if not text then
        stats_hide()
        return
    end
    msg.trace(('耗时行：%s'):format((text:gsub('\\N', ' | '))))
    stats_show(text)
end

-- ###############
-- OSD 菜单本体 --
-- ###############

local open_param_menu  -- 前向声明：选择完数值后要回到这里
-- 外面主动关面板（script-message close）时不要又被 closed 回调拉起来
local reopen_suppressed = false

local function open_value_menu(p)
    local items, values, default_item = {}, {}, nil
    local submitted = false
    local current = st()[p.key]
    for i, c in ipairs(p.choices) do
        items[i] = c[1] .. (c[2] == current and '  ●' or '')
        values[i] = c[2]
        if c[2] == current then default_item = i end
    end

    input.select {
        prompt = ('%s   （←/→ 或回车确认 · Esc 返回）'):format(p.name),
        items = items,
        default_item = default_item,
        submit = function(index)
            submitted = true
            st()[p.key] = values[index]
            apply(false)
            mp.add_timeout(0.05, open_param_menu)
        end,
        -- Esc 关掉取值这一层时回到参数列表，而不是把整个面板收掉；
        -- 提交也会走到 closed，所以先看 submitted 标记。
        closed = function()
            if submitted or reopen_suppressed then
                return
            end
            mp.add_timeout(0.05, open_param_menu)
        end,
    }
end

open_param_menu = function()
    reopen_suppressed = false
    local items = {}
    for i, p in ipairs(params) do
        items[i] = ('%s：%s'):format(p.name, value_label(p))
    end

    local active = detect_engine()
    local prompt = ('%s 补帧参数   （回车修改 · Esc 关闭）'):format(def.title)
    if not active then
        -- 没有滤镜在跑也允许改：下次套用预设时状态会被补上
        prompt = ('%s 补帧参数（当前未启用，改的是下次套用的值）'):format(def.title)
    end

    input.select {
        prompt = prompt,
        items = items,
        submit = function(index)
            open_value_menu(params[index])
        end,
    }
end

-- ##########
-- 对外接口 --
-- ##########

mp.add_key_binding(nil, 'menu', open_param_menu)

mp.register_script_message('set', function(key, value)
    for _, p in ipairs(params) do
        if p.key == key then
            if not valid_value(p, value) then
                msg.warn(('不认识的取值：%s=%s'):format(key, tostring(value)))
                return
            end
            st()[key] = value
            apply(false)
            return
        end
    end
    msg.warn('不认识的参数：' .. tostring(key))
end)

mp.register_script_message('apply', function() apply(false) end)

mp.register_script_message('menu', open_param_menu)
mp.register_script_message('close', function()
    reopen_suppressed = true
    input.terminate()
end)

mp.register_script_message('status', function()
    local text = status_text()
    -- 同时写日志：OSD 不落盘，排错和端到端测试都靠这一行
    mp.msg.info(text)
    mp.osd_message(text, 4)
end)

-- 用户用别的方式（menu.conf 预设等）挂上滤镜时：
-- 1) 换引擎了就切参数表并重新挂上行监看；
-- 2) 把落盘的参数补回去，免得点了预设又回到脚本里的硬编码默认值。
mp.observe_property('vf', 'none', function()
    if applying then return end

    local name, script = detect_engine()
    if name and (name ~= target_name or not file_matches(script, target_script)) then
        use_engine(name, script)
        ensure_bucket(name)
        start_watch()
        msg.verbose(('检测到 %s，切换到它参数表'):format(def.title))
    end

    local _, entries = find_entries()
    if #entries == 0 then
        remote = nil
        return
    end
    -- 多于一条：有旧条目没被清掉（见 apply 的说明），合并成一条
    if #entries > 1 then
        msg.warn(('%s 在滤镜链里有 %d 条，合并为一条'):format(def.title, #entries))
        mp.add_timeout(0.05, function() apply(true) end)
        return
    end
    local f = entries[1].filter
    if f.name ~= 'vapoursynth' then
        remote = nil
        return
    end
    local ud = f.params and f.params['user-data'] or ''

    -- 倍数归 k7f_multi 统一管（它的菜单会往 user-data 里写 multi）。链上已经有值
    -- 时以链为准，否则两边各写各的会陷入“写 3 → 写 5 → 写 3”的无限重建。
    local p_multi = find_param('multi')
    local on_chain = B.parse_user_data(ud).multi
    if p_multi and on_chain ~= nil then
        on_chain = tostring(on_chain)
        if valid_value(p_multi, on_chain) and st().multi ~= on_chain then
            st().multi = on_chain
            save_state()
            msg.verbose('倍数以滤镜链为准：' .. on_chain)
        end
    end

    if same_user_data(ud, build_user_data()) then return end
    mp.add_timeout(0.05, function() apply(true) end)
end)

-- 初始目标：链上已有的引擎，否则用 script-opts 的兜底脚本
do
    local name, script = detect_engine()
    if name then
        use_engine(name, script)
    else
        local fallback = engine_name(o.script)
        if not fallback then
            msg.warn('k7f_vpypar-script 不是已知补帧脚本，按 DLSSG 处理：' .. tostring(o.script))
            fallback = 'memc_dlssg_nv.vpy'
        end
        use_engine(fallback, ENGINES[fallback].script)
    end
end

load_state()
start_watch()

-- 耗时行：1s 读一次统计文件；窗口尺寸 / uosc 底栏高度变化时重新定位
stats_timer = mp.add_periodic_timer(1.0, refresh_stats)
mp.observe_property('osd-dimensions', 'none', function()
    if stats_shown then refresh_stats() end
end)
mp.observe_property('user-data/osc/margins', 'none', function()
    if stats_shown then refresh_stats() end
end)

msg.verbose(('已加载 %s，当前：%s（报告：%s）')
    :format(def.title, describe(), tostring(watcher and '监看中' or '未启用')))
