--[[
k7f_vpypar.lua —— 播放中用 OSD 菜单改 VapourSynth 滤镜的参数（含 aji 三层叠加）

原理：vpy 脚本本身读不到 mpv 的属性，但 `vf=vapoursynth` 有 user-data 子参数，
mpv 会把它作为全局变量 user_data 注入脚本（默认空串；mpv 每次 seek 都会重跑脚本，
所以跳转/重建后参数一定生效）。本脚本维护一份参数状态，改动后重建滤镜：

    vf set vapoursynth="~~/vs/MEMC_RIFE_AJI.vpy":user-data="model=4.14;multi=3"

上行：vpy 把实际生效的参数写到脚本同目录的 `<脚本>.k7f.json`，这里用
`k7f_bridge.watch_status()` 轮询（默认 0.5s），OSD 可显示“设置”和“实际”两行。
下行字符串与上行文件格式由 k7f_bridge.lua / k7sfunc.mod_vpy 统一定义。

三层结构（本脚本的核心假设，与 docs\k7-aji-rife.md 一致）：

    framegen（插帧） → upscale（超分） → tfix（时序）

每一层同时最多一条，三条可以共存（叠加）。链上的 vapoursynth 条目按脚本文件名
归层；不认识的 vapoursynth 脚本原样保留、不碰它的 user-data。

面板结构：一个面板自动跟随当前滤镜——**全局项固定在上**（倍数/阈值/显卡/日志/
耗时显示/RIFE 模型/ensemble/推理后端/超分模型/时序模型），**各层专属项在下**
（DLSSG 的预设、VFG 的档位、FSR4 的相机运动、aj i 的预缩高度等）。
全局项存一份，应用到所有层；每层按其引擎的键名映射（见 ENGINE_GLOBAL）。

    vdlssg  multi scene duplicate preset pre_h device log stats
    vxessfg multi scene duplicate pre_h device log stats
    vvfg    multi mode scene duplicate pre_h device log stats
    vfsr4   camera scene duplicate pre_h device log stats        （固定 2×，没有 multi）
    vaji    multi scene rife_model ensemble backend pre_h device log stats
    vaji_upscale  upscale_model backend device log stats
    vaji_tfix     tfix_model tfix_scene before_upscale backend device log stats

（上面是 user-data 里对外的键名；插件参数内部仍叫 gpu/model，mod_vpy 负责折回。）

“耗时显示”打开时 vpy 还会每 0.5s 写 `<脚本>.k7f-stats.json`（构图各阶段 + 逐帧延迟），
本脚本读它在 OSC 条上方画两行；关掉后文件不再更新，行也就隐藏了。

注意：改参 = 整条滤镜重建（引擎要重新初始化），不是逐帧实时；
multi 会改变输出帧数，本来也只能重建。multi 仍以 k7f_multi 为准：链上已有值时
本脚本只跟随，不反写，避免两边来回重建。

部署：
  1) k7f_bridge.lua 与本文件一起放到 portable_config\scripts\
  2) input.conf（或 input_uosc.conf）加一行，键自选：
        V  script-binding k7f_vpypar/menu
  3) 用 OSD 菜单改参；只有已知脚本的 vapoursynth 滤镜在跑时才真正重建，否则只存状态。

可选 script-opts（script-opts.conf）：
  script-opts-append = k7f_vpypar-script=~~/vs/MEMC_DLSSG_NV.vpy   （链上没有已知滤镜时的兜底）

脚本消息（给别的脚本/按键用）：
  script-message-to k7f_vpypar set global.rife_model 4.26
  script-message-to k7f_vpypar set rife_model 4.26
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
    -- 兜底脚本：滤镜链上没有已知滤镜时，面板改的是它的层
    script = '~~/vs/MEMC_DLSSG_NV.vpy',
    -- 参数状态落盘位置，换片/重启 mpv 后沿用上次的选择
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

-- 层的处理顺序就是这张表的顺序：插帧 → 超分 → 时序
local LAYER_ORDER = { framegen = 1, upscale = 2, tfix = 3 }
local LAYER_TITLES = { framegen = '插帧', upscale = '超分', tfix = '时序' }

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

local function scene_choices()
    return { { '关闭', '0' }, { '0.08', '0.08' }, { '0.12', '0.12' }, { '0.15', '0.15' },
             { '0.2', '0.2' }, { '0.25', '0.25' } }
end

local function bool_choices()
    return { { '关', '0' }, { '开', '1' } }
end

local ENGINES = {
    ['memc_dlssg_nv.vpy'] = {
        title = 'DLSSG', engine = 'vdlssg', layer = 'framegen',
        script = '~~/vs/MEMC_DLSSG_NV.vpy',
        multi = { 2, 3, 4, 5, 6 }, default_multi = '3',
        preset = true,
    },
    ['memc_xessfg_nv.vpy'] = {
        title = 'XeSS-FG', engine = 'vxessfg', layer = 'framegen',
        script = '~~/vs/MEMC_XESSFG_NV.vpy',
        -- 引擎 sm86 的多帧生成（≥3×）会直接结束进程，候选里只留 2×
        multi = { 2 }, default_multi = '2',
    },
    ['memc_vfg_nv.vpy'] = {
        title = 'VFG', engine = 'vvfg', layer = 'framegen',
        script = '~~/vs/MEMC_VFG_NV.vpy',
        multi = { 2, 3, 4, 5, 6 }, default_multi = '2',
        mode = true,
    },
    ['memc_fsr4.vpy'] = {
        title = 'FSR4', engine = 'vfsr4', layer = 'framegen',
        script = '~~/vs/MEMC_FSR4.vpy',
        -- 固定 2×，vpy 侧不认 multi
        camera = true,
    },
    ['memc_rife_aji.vpy'] = {
        title = 'RIFE-AJI', engine = 'vaji', layer = 'framegen',
        script = '~~/vs/MEMC_RIFE_AJI.vpy',
        multi = { 2, 3, 4, 5, 6 }, default_multi = '2',
    },
    ['mix_upscale_aji.vpy'] = {
        title = '超分-AJI', engine = 'vaji_upscale', layer = 'upscale',
        script = '~~/vs/MIX_UPSCALE_AJI.vpy',
    },
    ['mix_tfix_aji.vpy'] = {
        title = '时序-AJI', engine = 'vaji_tfix', layer = 'tfix',
        script = '~~/vs/MIX_TFIX_AJI.vpy',
    },
    -- 叠加脚本本身（链上一个条目承载三层）
    ['mix_aji.vpy'] = {
        title = '叠层', engine = 'stack', layer = 'stack',
        script = STACK_SCRIPT,
    },
}

-- 叠加脚本：一个 MIX_AJI.vpy 承载三层，靠 user-data 的 stages/framegen 选层。
-- mpv 一条 vf 链只允许一个 vapoursynth 实例，所以三层只能在这一个脚本里串联。
local STACK_SCRIPT = '~~/vs/MIX_AJI.vpy'
local STACK_BASENAME = 'mix_aji.vpy'
local STACK_KEY = '__stack'

-- 补帧层可选引擎 → 它们的单层脚本名（参数表、标题都复用 ENGINES）
local FRAMEGEN_SCRIPTS = {
    vaji    = 'memc_rife_aji.vpy',
    vdlssg  = 'memc_dlssg_nv.vpy',
    vxessfg = 'memc_xessfg_nv.vpy',
    vvfg    = 'memc_vfg_nv.vpy',
    vfsr4   = 'memc_fsr4.vpy',
}
local FRAMEGEN_CHOICES = {
    { '关', 'off' }, { 'RIFE-AJI', 'vaji' }, { 'DLSSG', 'vdlssg' },
    { 'XeSS-FG', 'vxessfg' }, { 'VFG', 'vvfg' }, { 'FSR4', 'vfsr4' },
}

-- 参数表：默认值与各 MEMC_*/MIX_*.vpy 头部的用户选项一致（改默认值要三处一起改：
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
        choices = scene_choices(),
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
    -- aji 三层的专属参数
    if def.engine == 'vaji' then
        add({ key = 'model', name = 'RIFE 模型', default = '4.14',
              choices = { { '4.14（轻快）', '4.14' }, { '4.26（更稳）', '4.26' } } })
        add({ key = 'ensemble', name = 'ensemble', default = '0', choices = bool_choices() })
        add({ key = 'backend', name = '推理后端', default = 'auto',
              choices = { { '自动梯队', 'auto' }, { 'conf 文件', 'conf' }, { 'CUDA 原生内核', 'native' },
                          { 'TensorRT', 'TensorRT' }, { 'TensorRT-RTX', 'TensorRT-RTX' } } })
    elseif def.engine == 'vaji_upscale' then
        add({ key = 'model', name = '超分模型', default = 'hd_perf',
              choices = { { 'HD 性能', 'hd_perf' }, { 'HD 均衡', 'hd_bal' },
                          { 'HD 性能 Sharp', 'hd_perf_sharp' }, { 'HD 均衡 Sharp', 'hd_bal_sharp' },
                          { 'SD Compact', 'sd' } } })
        add({ key = 'backend', name = '推理后端', default = 'auto',
              choices = { { '自动梯队', 'auto' }, { 'CUDA 原生内核', 'native' },
                          { 'TensorRT', 'TensorRT' }, { 'TensorRT-RTX', 'TensorRT-RTX' } } })
    elseif def.engine == 'vaji_tfix' then
        add({ key = 'model', name = '时序模型', default = 's1',
              choices = { { 's1（7 帧窗）', 's1' }, { 's2', 's2' }, { 's3', 's3' } } })
        add({ key = 'before_upscale', name = '缩前修复', default = '0', choices = bool_choices() })
        add({ key = 'backend', name = '推理后端', default = 'auto',
              choices = { { '自动梯队', 'auto' }, { 'CUDA 原生内核', 'native' },
                          { 'TensorRT', 'TensorRT' }, { 'TensorRT-RTX', 'TensorRT-RTX' } } })
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
        choices = bool_choices(),
    })
    add({
        -- vpy 侧每 0.5s 写一份 <脚本>.k7f-stats.json，这里画在 OSC 上方
        key = 'stats', name = '耗时显示', default = '0',
        choices = bool_choices(),
    })

    return list
end

-- 每个引擎一份参数表（只算一次）
for _, def in pairs(ENGINES) do
    def.params = build_params(def)
end

-- #####################################
-- 全局项：一份状态，应用到所有层 ------
-- #####################################

-- 每层的引擎参数键 → 全局键。没列进映射的键就是“层专属项”，只在那一层出现。
local ENGINE_GLOBAL = {
    vdlssg = { multi = 'multi', scene = 'scene', gpu = 'device', log = 'log', stats = 'stats' },
    vxessfg = { multi = 'multi', scene = 'scene', gpu = 'device', log = 'log', stats = 'stats' },
    vvfg = { multi = 'multi', scene = 'scene', gpu = 'device', log = 'log', stats = 'stats' },
    vfsr4 = { scene = 'scene', gpu = 'device', log = 'log', stats = 'stats' },
    vaji = {
        multi = 'multi', scene = 'scene', gpu = 'device', log = 'log', stats = 'stats',
        model = 'rife_model', ensemble = 'ensemble', backend = 'backend',
    },
    vaji_upscale = { gpu = 'device', log = 'log', stats = 'stats', model = 'upscale_model', backend = 'backend' },
    vaji_tfix = { scene = 'tfix_scene', gpu = 'device', log = 'log', stats = 'stats', model = 'tfix_model', backend = 'backend' },
}

-- 全局项列表：名字/默认值/作用层/候选值。choices 与 mod_vpy 的取值集合保持一致。
local GLOBAL = {
    { key = 'multi', name = '倍数', default = '2', layers = { framegen = true },
      choices = multi_choices({ 2, 3, 4, 5, 6 }) },
    { key = 'scene', name = '切镜阈值', default = '0.15', layers = { framegen = true }, choices = scene_choices() },
    { key = 'tfix_scene', name = '时序阈值', default = '0.15', layers = { tfix = true }, choices = scene_choices() },
    { key = 'rife_model', name = 'RIFE 模型', default = '4.14', layers = { framegen = true },
      choices = { { '4.14（轻快）', '4.14' }, { '4.26（更稳）', '4.26' } } },
    { key = 'ensemble', name = 'ensemble', default = '0', layers = { framegen = true }, choices = bool_choices() },
    { key = 'upscale_model', name = '超分模型', default = 'hd_perf', layers = { upscale = true },
      choices = { { 'HD 性能', 'hd_perf' }, { 'HD 均衡', 'hd_bal' },
                  { 'HD 性能 Sharp', 'hd_perf_sharp' }, { 'HD 均衡 Sharp', 'hd_bal_sharp' },
                  { 'SD Compact', 'sd' } } },
    { key = 'tfix_model', name = '时序模型', default = 's1', layers = { tfix = true },
      choices = { { 's1（7 帧窗）', 's1' }, { 's2', 's2' }, { 's3', 's3' } } },
    { key = 'backend', name = '推理后端', default = 'auto', layers = { framegen = true, upscale = true, tfix = true },
      choices = { { '自动梯队', 'auto' }, { 'conf 文件', 'conf' }, { 'CUDA 原生内核', 'native' },
                  { 'TensorRT', 'TensorRT' }, { 'TensorRT-RTX', 'TensorRT-RTX' } } },
    { key = 'device', name = '显卡', default = 'auto', layers = { framegen = true, upscale = true, tfix = true },
      choices = { { '自动', 'auto' }, { '0', '0' }, { '1', '1' }, { '2', '2' } } },
    { key = 'log', name = '耗时日志', default = '0', layers = { framegen = true, upscale = true, tfix = true },
      choices = bool_choices() },
    { key = 'stats', name = '耗时显示', default = '0', layers = { framegen = true, upscale = true, tfix = true },
      choices = bool_choices() },
    -- 下面两项只调 OSD 那一行本身，不进 user-data（vpy 不认识，改了不必重建引擎）
    { key = 'stats_pos', name = '耗时行位置', default = 'bottom', frontend = true,
      layers = { framegen = true, upscale = true, tfix = true },
      choices = { { '下方', 'bottom' }, { '上方', 'top' } } },
    { key = 'stats_alpha', name = '耗时行不透明度', default = '100', frontend = true,
      layers = { framegen = true, upscale = true, tfix = true },
      choices = { { '100%', '100' }, { '80%', '80' }, { '60%', '60' }, { '40%', '40' } } },
}

-- ##########
-- 当前目标 --
-- ##########

-- 链上现存的已知条目：[层] = { name=脚本名, script=链上路径, def=引擎定义, filter=条目 }
local present = {}

local state = {}         -- [脚本名或 __global] = { key = 取值字符串 }

local function engine_name(path)
    local name = script_basename(path)
    return ENGINES[name] and name or nil
end

-- 状态桶：全局一份，按脚本分开存，换预设/换引擎不会互相覆盖
local function ensure_bucket(name)
    if state[name] then return state[name] end

    local bucket = {}
    local list
    if name == '__global' then
        list = GLOBAL
    elseif name == STACK_KEY then
        -- 叠加状态：补帧引擎 + 超分/时序开关（字符串，和别处一致）
        bucket.framegen = 'off'
        bucket.upscale = '0'
        bucket.tfix = '0'
        state[name] = bucket
        return bucket
    elseif ENGINES[name] then
        list = ENGINES[name].params
    else
        list = {}
    end
    for _, p in ipairs(list) do
        bucket[p.key] = p.default
    end
    state[name] = bucket
    return bucket
end

local function gst()
    return ensure_bucket('__global')
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

-- 把落盘值归一成字符串再校验；旧文件里可能有数字/布尔
local function normalize(p, val)
    if type(val) == 'number' then
        val = tostring(val)
    elseif type(val) == 'boolean' then
        val = val and '1' or '0'
    end
    return valid_value(p, val) and val or p.default
end

local function param_list(name)
    if name == '__global' then return GLOBAL end
    local def = ENGINES[name]
    return def and def.params or {}
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

    -- 旧格式是平的 {multi=…, scene=…}：归到兜底脚本名下（现在还会顺带补进全局桶）。
    -- 只要出现任一已知脚本名，就按新格式（按桶存）处理。
    local legacy = saved.multi ~= nil
    for name in pairs(ENGINES) do
        if type(saved[name]) == 'table' then legacy = false end
    end

    local function fill(name, values)
        local bucket = ensure_bucket(name)
        for _, p in ipairs(param_list(name)) do
            if values[p.key] ~= nil then
                bucket[p.key] = normalize(p, values[p.key])
            end
        end
    end

    if legacy then
        fill(engine_name(o.script) or 'memc_dlssg_nv.vpy', saved)
        fill('__global', saved)
    else
        for name, values in pairs(saved) do
            if (name == '__global' or name == STACK_KEY or ENGINES[name]) and type(values) == 'table' then
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

-- ##########
-- 参数取值 --
-- ##########

-- 全局项的当前显示值：倍数这类“以链为准”的键由 observe 同步进全局桶
local function global_value(key)
    return gst()[key]
end

local function label_of(p, value)
    for _, c in ipairs(p.choices) do
        if c[2] == value then return c[1] end
    end
    return value or '?'
end

local function global_label(p)
    return label_of(p, global_value(p.key))
end

-- 层专属项：该引擎参数键不在 ENGINE_GLOBAL 里的那些
local function local_params(def)
    local map = ENGINE_GLOBAL[def.engine] or {}
    local out = {}
    for _, p in ipairs(def.params) do
        if not map[p.key] then out[#out + 1] = p end
    end
    return out
end

-- 面板上该不该出现这一项：它服务的层里至少有一层在跑，或者当前一层都没有
local function layer_relevant(p)
    local any = false
    for layer in pairs(p.layers) do
        any = true
        for _, e in pairs(present) do
            if e.def.layer == layer then return true end
        end
    end
    return not any or next(present) == nil
end

local function describe()
    local out = {}
    for _, p in ipairs(GLOBAL) do
        if layer_relevant(p) then
            out[#out + 1] = p.name .. ' ' .. global_label(p)
        end
    end
    local parts = {}
    for _, layer in ipairs({ 'framegen', 'upscale', 'tfix' }) do
        for _, e in pairs(present) do
            if e.def.layer == layer then
                parts[#parts + 1] = e.def.title
            end
        end
    end
    if #parts > 0 then out[#out + 1] = table.concat(parts, ' → ') end
    return table.concat(out, ' · ')
end

-- ############################
-- 组装 user-data 并重建滤镜 --
-- ############################

-- 层内参数键 → user-data 的对外键名（面板/日志里统一用 device、rife_model…；gpu 对所有引擎都改名）
local ENGINE_UD_KEY = {
    vaji = { model = 'rife_model' },
    vaji_upscale = { model = 'upscale_model' },
    vaji_tfix = { model = 'tfix_model' },
}

local function ud_key(engine, key)
    local m = ENGINE_UD_KEY[engine]
    if m and m[key] then return m[key] end
    if key == 'gpu' then return 'device' end
    return key
end

-- 两个 vpy 路径是不是同一个脚本：只比文件名（链里存的是绝对路径，配置里可能是 ~~/）
local function file_matches(a, b)
    if not a or not b then return false end
    return script_basename(a) == script_basename(b)
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

-- 叠加状态 → 生效的层（present 由面板状态决定，不再取决于链上有几个脚本）
local function stages_of(st)
    local s = ''
    if st.framegen ~= 'off' then s = s .. 'f' end
    if st.upscale == '1' then s = s .. 'u' end
    if st.tfix == '1' then s = s .. 't' end
    return s
end

local function refresh_present()
    local st = ensure_bucket(STACK_KEY)
    present = {}
    local fg_script = FRAMEGEN_SCRIPTS[st.framegen]
    if fg_script then
        present.framegen = { name = fg_script, def = ENGINES[fg_script], script = STACK_SCRIPT }
    end
    if st.upscale == '1' then
        present.upscale = { name = 'mix_upscale_aji.vpy', def = ENGINES['mix_upscale_aji.vpy'], script = STACK_SCRIPT }
    end
    if st.tfix == '1' then
        present.tfix = { name = 'mix_tfix_aji.vpy', def = ENGINES['mix_tfix_aji.vpy'], script = STACK_SCRIPT }
    end
    return present
end

-- 整条链的 user-data：stages/framegen + 各生效层的参数（层专属键取该引擎的桶）
local function stack_ud()
    local st = ensure_bucket(STACK_KEY)
    local ud = { stages = stages_of(st) }
    if st.framegen ~= 'off' then ud.framegen = st.framegen end
    for _, layer in ipairs({ 'framegen', 'upscale', 'tfix' }) do
        local e = present[layer]
        if e then
            local map = ENGINE_GLOBAL[e.def.engine] or {}
            local bucket = ensure_bucket(e.name)
            for _, p in ipairs(e.def.params) do
                local g = map[p.key]
                local v = g and gst()[g] or bucket[p.key]
                if v ~= nil and v ~= '' then
                    local k = ud_key(e.def.engine, p.key)
                    if ud[k] == nil then ud[k] = v end
                end
            end
        end
    end
    return B.build_user_data(ud)
end

-- 前向声明：apply 里只改了前端项（耗时行位置/透明度）时要直接刷新 OSD
local refresh_stats

-- 扫描滤镜链：只关心「我们的条目」（叠加脚本或旧的单层脚本），其它 vapoursynth 条目原样保留
local function scan_chain()
    local list = mp.get_property_native('vf') or {}
    local found = {}
    for i, f in ipairs(list) do
        if f.name == 'vapoursynth' and f.params and f.params.file then
            local name = engine_name(f.params.file)
            if name then
                local def = ENGINES[name]
                found[#found + 1] = { name = name, script = f.params.file, def = def,
                                      filter = f, index = i }
            end
        end
    end
    return list, found
end

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

    refresh_present()
    local st = ensure_bucket(STACK_KEY)
    local stages = stages_of(st)

    local list, found = scan_chain()
    local by_index = {}
    local ours = {}
    for _, e in ipairs(found) do
        by_index[e.index] = e
        ours[#ours + 1] = e.index
    end
    table.sort(ours)

    if stages == '' and #ours == 0 then
        if not silent then
            mp.osd_message(('参数已保存（三层都关着，链上没有我们的滤镜）：%s'):format(describe()), 3)
        end
        return false
    end

    local ud = stages ~= '' and stack_ud() or nil
    local new_entry = nil
    if stages ~= '' then
        new_entry = {
            name = 'vapoursynth',
            label = '叠层-AJI',
            params = { file = resolve_path(STACK_SCRIPT), ['user-data'] = ud },
        }
    end

    -- 链上只要不是「恰好一条、内容一致」，就整链重建
    local target = ours[1]
    local dirty = false
    if stages == '' then
        dirty = #ours > 0
    elseif #ours ~= 1 then
        dirty = true
    elseif not (file_matches(by_index[target].filter.params.file, STACK_SCRIPT)
            and same_user_data(by_index[target].filter.params['user-data'], ud)) then
        dirty = true
    end

    if not dirty then
        -- 参数没变：只改了前端显示项（或原样重存），刷新一行 OSD 就够了
        if refresh_stats then refresh_stats() end
        if not silent then
            mp.osd_message(('已应用：%s'):format(describe()), 2)
        end
        return true
    end

    local final = {}
    for i, f in ipairs(list) do
        if i == target then
            if new_entry then final[#final + 1] = new_entry end
        elseif by_index[i] then
            -- 我们的旧条目（单层脚本或重复的叠加脚本）：删掉
        else
            final[#final + 1] = f
        end
    end
    if target == nil and new_entry then final[#final + 1] = new_entry end

    applying = true
    local ok, err = pcall(mp.set_property_native, 'vf', final)
    applying = false
    if not ok then
        local text = '重建滤镜失败：' .. tostring(err)
        msg.error(text)
        mp.osd_message('重建滤镜失败，见控制台日志', 3)
        return false
    end

    -- 兜底复核：mpv 里“命令没生效”常常也是成功返回，改完链后自己再扫一遍
    local count = 0
    for _, f in ipairs(mp.get_property_native('vf') or {}) do
        if f.name == 'vapoursynth' and f.params and f.params.file then
            if file_matches(f.params.file, STACK_SCRIPT) then count = count + 1 end
        end
    end
    if count ~= (new_entry and 1 or 0) then
        msg.error(('叠加脚本条目数不对（应为 %d 条，实际 %d 条）'):format(new_entry and 1 or 0, count))
        mp.osd_message('重建后叠加滤镜不在链上，见控制台日志', 3)
        return false
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
local watch_script = nil

-- 统计行/报告看的是“主层”：有插帧看插帧，否则超分，最后时序
local function primary()
    for _, layer in ipairs({ 'framegen', 'upscale', 'tfix' }) do
        if present[layer] then return present[layer] end
    end
    return nil
end

local function target_script()
    local e = primary()
    return e and e.script or o.script
end

-- 叠加脚本在 sidecar 里上报的是 vaji_stack（它把三层合成一条）
local function engine_tag(def)
    if def and def.engine == 'stack' then return 'vaji_stack' end
    return def and def.engine
end

local function target_def()
    local e = primary()
    if e then return e.def end
    return ENGINES[engine_name(o.script) or 'memc_dlssg_nv.vpy']
end

local function start_watch()
    if watcher then
        watcher:kill()
        watcher = nil
    end
    watch_script = target_script()

    watcher = B.watch_status(watch_script, o.status_interval, function(data)
        -- 看的是主层的报告；层切换后旧文件的变化不算数
        if watch_script ~= target_script() then return end
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
    local lines = { ('设置：%s'):format(describe()) }
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
local stats_geo = { w = 0, h = 0, top = 0, bottom = 0 }
-- 切镜行：累计数（vpy 报告里 live.cuts 是本次播放的累计值），刚切过时染成暖色
local stats_cuts_seen = 0
local stats_cut_at = 0
local STATS_CUT_FLASH = 3

-- OSD 坐标系与 uosc 的底栏高度：uosc 把底栏占比写到 user-data/osc/margins
local function stats_geometry()
    local dims = mp.get_property_native('osd-dimensions') or {}
    local margins = mp.get_property_native('user-data/osc/margins') or {}
    stats_geo.w = tonumber(dims.w) or 0
    stats_geo.h = tonumber(dims.h) or 0
    stats_geo.top = tonumber(margins.t) or 0
    stats_geo.bottom = tonumber(margins.b) or 0
    return stats_geo.w > 0 and stats_geo.h > 0
end

local function stats_enabled()
    return global_value('stats') == '1'
end

local function stats_text(data)
    local lines = {}
    local live = data.live
    if live and (tonumber(live.frames) or 0) > 0 then
        local line = ('%s %s× · 单帧 %.2fms p90 %.2f · 同飞 %d · %dfps'):format(
            data.title or (target_def() and target_def().title) or '?',
            tostring(data.multi or '?'),
            tonumber(live.avg) or 0,
            tonumber(live.p90) or 0,
            tonumber(live.inflight) or 0,
            math.floor((tonumber(live.fps) or 0) + 0.5))
        -- 插件自己在输出帧上标了切镜（_DLSSGSceneCut），vpy 侧累计后写进 live.cuts
        local cuts = tonumber(live.cuts) or 0
        if cuts > 0 then
            line = line .. (' · 切镜 %d'):format(cuts)
        end
        lines[#lines + 1] = line
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

local function stats_show(text, accent)
    if not stats_geometry() then return end
    if not stats_overlay then
        stats_overlay = mp.create_osd_overlay('ass-events')
    end
    local w, h = stats_geo.w, stats_geo.h
    local pad = math.floor(h * 0.010)
    -- 位置：下方贴在 uosc 底栏上沿；上方贴在标题栏下沿（没开标题栏就贴画面顶）
    local top = global_value('stats_pos') == 'top'
    local base
    if top then
        base = math.floor(h * stats_geo.top) + pad
    else
        base = h - math.floor(h * stats_geo.bottom) - pad
    end
    -- 不透明度：设置是百分比，ASS 的 \1a/\3a 反过来（00 不透明、FF 全透明）
    local alpha = 255 - math.floor((tonumber(global_value('stats_alpha')) or 100) / 100 * 255 + 0.5)
    -- 刚切镜的几秒里把字染成暖黄，其余时候纯白
    local color = accent and '&H00D7FF&' or '&HFFFFFF&'
    local size = math.max(16, math.floor(h * 0.020))
    stats_overlay.res_x = w
    stats_overlay.res_y = h
    stats_overlay.data = ('{\\an%d\\pos(%d,%d)\\fs%d\\bord2\\shad0\\1c%s\\1a&H%02X&\\3c&H000000&\\3a&H%02X&}%s')
        :format(top and 8 or 2, math.floor(w / 2), base, size, color, alpha, alpha, text)
    stats_overlay:update()
    stats_shown = true
end

refresh_stats = function()
    if not stats_enabled() then
        stats_hide()
        return
    end

    local def = target_def()
    local data = B.read_json(resolve_path(target_script()) .. STATS_SUFFIX)
    if not data or not def or tostring(data.engine or '') ~= tostring(engine_tag(def) or '') then
        stats_hide()
        return
    end

    -- 暂停时保留最后一次统计（没有新帧不等于没有数据）；播放中过期说明补帧停了
    local stale = os.time() - (tonumber(data.ts) or 0) > STATS_STALE
    if stale and not mp.get_property_native('pause') then
        stats_hide()
        return
    end

    -- 切镜计数是 vpy 侧的累计值：变大说明刚切过，变小说明换了片/重建过
    local cuts = tonumber((data.live or {}).cuts) or 0
    if cuts < stats_cuts_seen then
        stats_cuts_seen = 0
        stats_cut_at = 0
    end
    if cuts > stats_cuts_seen then
        stats_cuts_seen = cuts
        stats_cut_at = os.time()
        msg.verbose(('切镜 #%d'):format(cuts))
    end
    local accent = stats_cut_at > 0 and (os.time() - stats_cut_at <= STATS_CUT_FLASH)

    local text = stats_text(data)
    if not text then
        stats_hide()
        return
    end
    msg.trace(('耗时行：%s'):format((text:gsub('\\N', ' | '))))
    stats_show(text, accent)
end

-- ###############
-- OSD 菜单本体 --
-- ###############

local open_param_menu  -- 前向声明：选择完数值后要回到这里
-- 外面主动关面板（script-message close）时不要又被 closed 回调拉起来
local reopen_suppressed = false

-- 面板条目：叠层开关（选层）+ 全局段 + 每层专属段。
-- kind='stack' 用 STACK_KEY 桶（切层/切引擎只重建同一条滤镜），kind='global' 用全局桶，
-- kind='local' 用该层引擎自己的桶。
local ON_OFF = { { '关', '0' }, { '开', '1' } }

local function stack_choices(key)
    if key == 'framegen' then return FRAMEGEN_CHOICES end
    return ON_OFF
end

local function stack_label(key)
    local cur = ensure_bucket(STACK_KEY)[key]
    for _, c in ipairs(stack_choices(key)) do
        if c[2] == cur then return c[1] end
    end
    return tostring(cur)
end

local function menu_entries()
    local items = {}

    items[#items + 1] = { kind = 'stack', key = 'framegen', name = '补帧',
                          p = { name = '补帧', choices = FRAMEGEN_CHOICES },
                          text = ('补帧：%s'):format(stack_label('framegen')) }
    items[#items + 1] = { kind = 'stack', key = 'upscale', name = '超分',
                          p = { name = '超分', choices = ON_OFF },
                          text = ('超分：%s'):format(stack_label('upscale')) }
    items[#items + 1] = { kind = 'stack', key = 'tfix', name = '时序',
                          p = { name = '时序', choices = ON_OFF },
                          text = ('时序：%s'):format(stack_label('tfix')) }

    for _, p in ipairs(GLOBAL) do
        if layer_relevant(p) then
            items[#items + 1] = {
                kind = 'global', p = p,
                text = ('%s：%s'):format(p.name, global_label(p)),
            }
        end
    end

    for _, layer in ipairs({ 'framegen', 'upscale', 'tfix' }) do
        local entry = present[layer]
        if entry then
            for _, p in ipairs(local_params(entry.def)) do
                items[#items + 1] = {
                    kind = 'local', p = p, entry = entry,
                    text = ('[%s] %s：%s'):format(LAYER_TITLES[layer], p.name,
                        label_of(p, ensure_bucket(entry.name or '')[p.key])) ,
                }
            end
        end
    end

    return items
end

local function value_of(item)
    if item.kind == 'stack' then
        return ensure_bucket(STACK_KEY)[item.key]
    elseif item.kind == 'global' then
        return gst()[item.p.key]
    end
    return ensure_bucket(item.entry.name or '')[item.p.key]
end

local function set_value(item, value)
    if item.kind == 'stack' then
        ensure_bucket(STACK_KEY)[item.key] = value
    elseif item.kind == 'global' then
        gst()[item.p.key] = value
    else
        ensure_bucket(item.entry.name or '')[item.p.key] = value
    end
end

-- 前置声明：菜单提交在 queue_apply 定义之前就要用到
local queue_apply

local function open_value_menu(item)
    local p = item.p
    local items, values, default_item = {}, {}, nil
    local submitted = false
    local current = value_of(item)
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
            set_value(item, values[index])
            queue_apply()
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
    scan_chain()

    local entries = menu_entries()
    local items = {}
    for i, item in ipairs(entries) do
        items[i] = item.text
    end

    local prompt
    if next(present) == nil then
        -- 没有滤镜在跑也允许改：下次套用预设时状态会被补上
        prompt = ('参数（当前未启用滤镜，改的是下次套用的值）  回车修改 · Esc 关闭')
    else
        local names = {}
        for _, layer in ipairs({ 'framegen', 'upscale', 'tfix' }) do
            if present[layer] then names[#names + 1] = present[layer].def.title end
        end
        prompt = ('参数：%s   （全局在上，回车修改 · Esc 关闭）'):format(table.concat(names, ' → '))
    end

    input.select {
        prompt = prompt,
        items = items,
        submit = function(index)
            open_value_menu(entries[index])
        end,
    }
end

-- ##########
-- 对外接口 --
-- ##########

mp.add_key_binding(nil, 'menu', open_param_menu)

local function find_global(key)
    for _, p in ipairs(GLOBAL) do
        if p.key == key then return p end
    end
    return nil
end

-- 改全局项；也可直接写层专属键（写到主层），或写叠层开关（framegen/upscale/tfix/stages）
local function set_by_key(key, value)
    local g = find_global(key)
    if g then
        if not valid_value(g, value) then
            msg.warn(('不认识的取值：%s=%s'):format(key, tostring(value)))
            return false
        end
        gst()[key] = value
        return true
    end

    if key == 'stages' then
        local stages = tostring(value):lower()
        if not stages:match('^[fut]*$') then
            msg.warn('stages 只能是 f/u/t 的组合：' .. tostring(value))
            return false
        end
        local st = ensure_bucket(STACK_KEY)
        st.upscale = stages:find('u', 1, true) and '1' or '0'
        st.tfix = stages:find('t', 1, true) and '1' or '0'
        if not stages:find('f', 1, true) then st.framegen = 'off' end
        return true
    end

    if key == 'framegen' or key == 'upscale' or key == 'tfix' then
        local ok = false
        for _, c in ipairs(stack_choices(key)) do
            if c[2] == tostring(value) then ok = true end
        end
        if not ok then
            msg.warn(('不认识的取值：%s=%s'):format(key, tostring(value)))
            return false
        end
        ensure_bucket(STACK_KEY)[key] = tostring(value)
        return true
    end

    local e = primary()
    if e then
        for _, p in ipairs(local_params(e.def)) do
            if p.key == key then
                if not valid_value(p, value) then
                    msg.warn(('不认识的取值：%s=%s'):format(key, tostring(value)))
                    return false
                end
                ensure_bucket(e.name)[key] = value
                return true
            end
        end
    end
    msg.warn('不认识的参数：' .. tostring(key))
    return false
end

-- 改一项就重建一次会对播放中的 vf 连做多次 destroy/init（VS 的 freeNode 回调是
-- 它自己的线程异步跑的，紧邻的销毁容易撞 vf_vapoursynth 的 !in_node_active 断言）。
-- 因此 set 只做去抖排队，连续 set（菜单预设一次发好几条）合并成一次重建；
-- 显式 apply 立即冲刷掉排队中的重建。
local apply_timer = nil

local function cancel_pending_apply()
    if apply_timer then
        apply_timer:kill()
        apply_timer = nil
    end
end

function queue_apply(delay)
    if apply_timer then return end -- 首次排队时间点不变，避免连续 set 无限延后
    apply_timer = mp.add_timeout(delay or 0.12, function()
        apply_timer = nil
        apply(false)
    end)
end

mp.register_script_message('set', function(key, value)
    if set_by_key(tostring(key):gsub('^global%.', ''), tostring(value)) then
        queue_apply()
    end
end)

mp.register_script_message('apply', function()
    cancel_pending_apply()
    apply(false)
end)

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
-- 1) 链上的 user-data 反写回面板状态（切层、切引擎、参数）；
-- 2) 拉不到的状态用落盘值补，再按状态重建，保证链与面板一致；
-- 3) 倍数归 k7f_multi 统一管。
-- 解析链上 user-data，把能对上的键写回状态桶
local function adopt_ud(ud)
    local st = ensure_bucket(STACK_KEY)
    local stages = tostring(ud.stages or '')
    local fg = ud.framegen and tostring(ud.framegen)
    if fg and FRAMEGEN_SCRIPTS[fg] and stages:find('f', 1, true) then
        st.framegen = fg
    else
        st.framegen = 'off'
    end
    st.upscale = stages:find('u', 1, true) and '1' or '0'
    st.tfix = stages:find('t', 1, true) and '1' or '0'

    refresh_present()
    for _, layer in ipairs({ 'framegen', 'upscale', 'tfix' }) do
        local e = present[layer]
        if e then
            local map = ENGINE_GLOBAL[e.def.engine] or {}
            local bucket = ensure_bucket(e.name)
            for _, p in ipairs(e.def.params) do
                local v = ud[ud_key(e.def.engine, p.key)]
                if v ~= nil then
                    local s = tostring(v)
                    if map[p.key] then gst()[map[p.key]] = s else bucket[p.key] = s end
                end
            end
        end
    end
end

mp.observe_property('vf', 'none', function()
    if applying then return end

    local before = {}
    for layer, e in pairs(present) do before[layer] = e.name end

    local _, found = scan_chain()
    local stack_entry, legacy = nil, 0
    for _, e in ipairs(found) do
        if file_matches(e.script, STACK_SCRIPT) then
            stack_entry = e
        else
            legacy = legacy + 1
        end
    end

    -- 链上有叠加脚本：以它为准（预设刚挂上时面板要跟着变）
    if stack_entry then
        adopt_ud(B.parse_user_data(stack_entry.filter.params['user-data'] or '') or {})
        save_state()
    else
        refresh_present()
    end

    start_watch()

    local changed = legacy > 0
    for layer, e in pairs(present) do
        if before[layer] ~= e.name then changed = true end
    end
    for layer in pairs(before) do
        if not present[layer] then changed = true end
    end
    if changed then
        local names = {}
        for _, layer in ipairs({ 'framegen', 'upscale', 'tfix' }) do
            if present[layer] then names[#names + 1] = present[layer].def.title end
        end
        msg.verbose('当前滤镜：' .. (#names > 0 and table.concat(names, ' → ') or '无'))
    end

    -- 倍数：链上已经有值就以它为准（面板只跟随，不反写，避免和 k7f_multi 打架）
    if stack_entry and stack_entry.filter.params then
        local on_chain = tostring(B.parse_user_data(stack_entry.filter.params['user-data'] or '').multi or '')
        local p = find_global('multi')
        if on_chain ~= '' and p and valid_value(p, on_chain) and gst().multi ~= on_chain then
            gst().multi = on_chain
            save_state()
            msg.verbose('倍数以滤镜链为准：' .. on_chain)
        end
    end

    -- 链的形态与状态不一致（刚点完预设 / 旧的单层脚本）：按状态重建一次。
    -- 只比 stages 与补帧引擎，参数差异不触发重建，免得点个预设就重启一次引擎。
    local st = ensure_bucket(STACK_KEY)
    local stages = stages_of(st)
    local chain_ud = stack_entry and B.parse_user_data(stack_entry.filter.params['user-data'] or '') or {}
    local chain_stages = tostring(chain_ud.stages or '')
    local dirty = legacy > 0 or chain_stages ~= stages
    if stages:find('f', 1, true) and tostring(chain_ud.framegen or '') ~= st.framegen then
        dirty = true
    end
    if dirty then
        mp.add_timeout(0.05, function() apply(true) end)
    end
end)

-- 初始目标：链上已有的引擎，否则用 script-opts 的兜底脚本
do
    local _, found = scan_chain()
    if #found == 0 then
        local fallback = engine_name(o.script)
        if not fallback then
            msg.warn('k7f_vpypar-script 不是已知脚本，按 DLSSG 处理：' .. tostring(o.script))
            fallback = 'memc_dlssg_nv.vpy'
        end
        if ENGINES[fallback] then
            msg.verbose(('链上没有滤镜，面板兜底为 %s'):format(ENGINES[fallback].title))
        end
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

msg.verbose(('已加载 k7f_vpypar，当前：%s（报告：%s）')
    :format(describe(), tostring(watcher and '监看中' or '未启用')))
